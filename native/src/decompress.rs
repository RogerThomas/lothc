//! Response decompression exactly as reqwest 0.13 does it: tower-http 0.6.11's `Decompression`
//! layer driving async-compression 0.4.50's `bufread` decoders over compression-codecs 0.4.45's
//! codecs. The codecs are the crate's own; this module is only the glue around them.
//!
//! - The request gets `Accept-Encoding: zstd,gzip,deflate,br` when it has none (`service.rs`).
//! - The response is decoded only when its first `Content-Encoding` is byte for byte `gzip`,
//!   `deflate` (zlib-wrapped), `br` or `zstd`, case-sensitive, never a list (`future.rs`); then
//!   both `Content-Encoding` and `Content-Length` are removed from its headers.
//! - `Driver` is async-compression's `generic/bufread/decoder.rs` loop: one gzip/deflate/br
//!   stream, several zstd frames (`multiple_members(true)`, tower-http's `body.rs`).
//! - `Decoder` is tower-http's `WrapBody::poll_frame` (`compression_utils.rs`) over it: an error
//!   before any data was read is dropped, and a non-empty frame after the stream ended is one.
//!
//! The body is decoded frame by frame as hyper hands it over. A buffered body is decoded whole
//! (`read`); a streamed one (`stream.rs`) takes each `poll_read`'s output as its own chunk
//! (`decode_next`), as tower-http yields them, so its memory stays bounded however well the body
//! compresses.
use std::io;
use std::ops::ControlFlow;

use bytes::{Buf, Bytes};
use compression_codecs::{BrotliDecoder, DecodeV2, GzipDecoder, ZlibDecoder, ZstdDecoder};
use compression_core::util::{PartialBuffer, WriteBuffer};
use http::header::{CONTENT_ENCODING, CONTENT_LENGTH};
use http::{HeaderMap, HeaderValue};
use http_body_util::BodyExt;
use hyper::body::Incoming;

use crate::error::{error_chain, Failure};

/// tower-http's `WrapBody::INTERNAL_BUF_CAPACITY`: the output each read gets.
const READ_CAPACITY: usize = 4096;

/// tower-http's `AcceptEncoding::to_header_value` with all four codings on, as pyreqwest
/// enables them.
pub(crate) fn accept_encoding() -> HeaderValue {
    HeaderValue::from_static("zstd,gzip,deflate,br")
}

/// A `Content-Encoding` reqwest decodes.
#[derive(Clone, Copy)]
pub(crate) enum Coding {
    Gzip,
    Deflate,
    Brotli,
    Zstd,
}

/// The coding to decode `headers`' response with: its first `Content-Encoding`, byte for byte.
/// One lookup, so an uncompressed response pays nothing more.
pub(crate) fn coding(headers: &HeaderMap) -> Option<Coding> {
    match headers.get(CONTENT_ENCODING)?.as_bytes() {
        b"gzip" => Some(Coding::Gzip),
        b"deflate" => Some(Coding::Deflate),
        b"br" => Some(Coding::Brotli),
        b"zstd" => Some(Coding::Zstd),
        _ => None,
    }
}

/// A decoding failure as `Failure::Decode`, worded as reqwest's.
fn corrupt(cause: &dyn std::fmt::Display) -> Failure {
    Failure::Decode(format!("error decoding response body: {cause}"))
}

/// async-compression's `generic::bufread::decoder::State`.
enum State {
    Decoding,
    Flushing,
    Done,
    Next,
    Error(io::Error),
}

/// async-compression's `generic::bufread::Decoder`, ported line for line.
struct Driver {
    state: State,
    multiple_members: bool,
}

impl Driver {
    fn do_poll_read(
        &mut self,
        output: &mut WriteBuffer<'_>,
        decoder: &mut dyn DecodeV2,
        input: &mut PartialBuffer<&[u8]>,
        mut first: bool,
    ) -> ControlFlow<io::Result<()>> {
        loop {
            self.state = match std::mem::replace(&mut self.state, State::Done) {
                State::Decoding => {
                    if input.unwritten().is_empty() && !first {
                        // Avoid attempting to reinitialise the decoder if the reader has
                        // returned EOF.
                        self.multiple_members = false;
                        State::Flushing
                    } else {
                        match decoder.decode(input, output) {
                            Ok(true) => State::Flushing,
                            // Ignore the first error, which occurs when input is empty but decode
                            // must run to flush.
                            Err(error) if !first => {
                                self.state = State::Error(error);
                                if output.written_len() > 0 {
                                    return ControlFlow::Break(Ok(()));
                                }
                                continue;
                            }
                            // Poll for more data for the next decode.
                            _ => {
                                self.state = State::Decoding;
                                break;
                            }
                        }
                    }
                }
                State::Flushing => {
                    let before = output.written_len();
                    let result = decoder.finish(output).and_then(|finished| {
                        if !finished && output.written_len() == before && !output.has_no_spare_space() {
                            Err(io::ErrorKind::UnexpectedEof.into())
                        } else {
                            Ok(finished)
                        }
                    });
                    match result {
                        Ok(true) if self.multiple_members => {
                            if let Err(error) = decoder.reinit() {
                                self.state = State::Error(error);
                                if output.written_len() > 0 {
                                    return ControlFlow::Break(Ok(()));
                                }
                                continue;
                            }
                            // Poll again only if the decode stage consumed all the input.
                            first = input.unwritten().is_empty();
                            State::Next
                        }
                        Ok(true) => State::Done,
                        Ok(false) => State::Flushing,
                        Err(error) => {
                            self.state = State::Error(error);
                            if output.written_len() > 0 {
                                return ControlFlow::Break(Ok(()));
                            }
                            continue;
                        }
                    }
                }
                State::Done => {
                    self.state = State::Done;
                    return ControlFlow::Break(Ok(()));
                }
                State::Next => {
                    if input.unwritten().is_empty() {
                        if first {
                            // Poll for more data to check if there's another stream.
                            self.state = State::Next;
                            break;
                        }
                        State::Done
                    } else {
                        State::Decoding
                    }
                }
                // `self.state` is already `Done`, as async-compression leaves it.
                State::Error(error) => return ControlFlow::Break(Err(error)),
            };
            if output.has_no_spare_space() {
                return ControlFlow::Break(Ok(()));
            }
        }
        if output.has_no_spare_space() {
            ControlFlow::Break(Ok(()))
        } else {
            ControlFlow::Continue(())
        }
    }
}

/// Where a read's input comes from: the frame hyper handed over (`Pending` once it's used up),
/// or the end of the body.
enum Source<'a, 'b> {
    Frame(&'a mut PartialBuffer<&'b [u8]>),
    End,
}

/// What one read gave: `Pending` means it needs the next frame.
enum Read {
    Pending,
    Ready(io::Result<usize>),
}

/// A response body's decoder, fed hyper's data frames in order.
pub(crate) struct Decoder {
    /// `Send`, so a sync stream can be read on whichever thread asks for its next chunk.
    codec: Box<dyn DecodeV2 + Send>,
    driver: Driver,
    /// `StreamErrorIntoIoError::read_some_data`: whether the body has yielded any data.
    read_some: bool,
    /// `WrapBody::read_all_data`: the decoder reached its end.
    read_all: bool,
    /// The body's first frame after that was empty, which ends tower-http's body.
    closed: bool,
    out: Vec<u8>,
}

impl Decoder {
    pub(crate) fn new(coding: Coding) -> Self {
        let (codec, multiple_members): (Box<dyn DecodeV2 + Send>, bool) = match coding {
            Coding::Gzip => (Box::new(GzipDecoder::new()), false),
            Coding::Deflate => (Box::new(ZlibDecoder::new()), false),
            Coding::Brotli => (Box::new(BrotliDecoder::new()), false),
            Coding::Zstd => (Box::new(ZstdDecoder::new()), true),
        };
        Self {
            codec,
            driver: Driver { state: State::Decoding, multiple_members },
            read_some: false,
            read_all: false,
            closed: false,
            out: Vec::new(),
        }
    }

    /// async-compression's `bufread` `poll_read` into the next `READ_CAPACITY` bytes of `out`:
    /// how many bytes it decoded.
    fn poll_read(&mut self, source: Source<'_, '_>) -> Read {
        let start = self.out.len();
        self.out.resize(start + READ_CAPACITY, 0);
        let mut output = WriteBuffer::new_initialized(self.out.get_mut(start..).unwrap_or_default());
        let mut empty = PartialBuffer::new(&[][..]);
        // `None` is `Poll::Pending`.
        let result = match self.driver.do_poll_read(&mut output, &mut *self.codec, &mut empty, true) {
            ControlFlow::Break(result) => Some(result),
            ControlFlow::Continue(()) => match source {
                // The reader keeps returning EOF until the driver flushes or fails.
                Source::End => loop {
                    let mut empty = PartialBuffer::new(&[][..]);
                    if let ControlFlow::Break(result) = self.driver.do_poll_read(&mut output, &mut *self.codec, &mut empty, false) {
                        break Some(result);
                    }
                },
                Source::Frame(frame) => loop {
                    if frame.unwritten().is_empty() {
                        break (output.written_len() > 0).then_some(Ok(()));
                    }
                    let mut input = PartialBuffer::new(frame.unwritten());
                    let flow = self.driver.do_poll_read(&mut output, &mut *self.codec, &mut input, false);
                    frame.advance(input.written_len());
                    if let ControlFlow::Break(result) = flow {
                        break Some(result);
                    }
                },
            },
        };
        let written = output.written_len();
        self.out.truncate(start + written);
        match result {
            None => Read::Pending,
            Some(result) => Read::Ready(result.map(|()| written)),
        }
    }

    /// Whether a data frame from the body goes to the decoder at all: `WrapBody::poll_frame`'s
    /// look at the body once the decoder is done, where an empty frame ends the body and any
    /// other is an error.
    pub(crate) fn accepts(&mut self, frame: &[u8]) -> Result<bool, Failure> {
        if self.read_all {
            if self.closed || frame.is_empty() {
                self.closed = true;
                return Ok(false);
            }
            return Err(corrupt(&"there are extra bytes after body has been decompressed"));
        }
        self.read_some = true;
        Ok(true)
    }

    /// One `poll_read` of `frame` into `out`: whether it decoded anything. `false` once the frame
    /// is used up, or once the stream has ended, the rest of the frame then staying unread, as in
    /// the `StreamReader`'s buffer.
    fn step(&mut self, frame: &mut PartialBuffer<&[u8]>) -> Result<bool, Failure> {
        match self.poll_read(Source::Frame(frame)) {
            Read::Pending => Ok(false),
            Read::Ready(Ok(0)) => {
                self.read_all = true;
                Ok(false)
            }
            Read::Ready(Ok(_)) => Ok(true),
            Read::Ready(Err(error)) => Err(corrupt(&error)),
        }
    }

    /// One data frame from the body.
    pub(crate) fn push(&mut self, frame: &[u8]) -> Result<(), Failure> {
        if !self.accepts(frame)? {
            return Ok(());
        }
        let mut frame = PartialBuffer::new(frame);
        while self.step(&mut frame)? {}
        Ok(())
    }

    /// A streamed body's next decoded chunk out of `frame` (one `poll_read`, at most
    /// `READ_CAPACITY` bytes, as tower-http yields a frame), which is advanced past the input it
    /// used; `None` once the frame is used up or the stream has ended (the rest is then dropped).
    /// Call it again until `None`: the decoder may still hold output once its input is used up.
    pub(crate) fn decode_next(&mut self, frame: &mut Bytes) -> Result<Option<Bytes>, Failure> {
        let mut input = PartialBuffer::new(&frame[..]);
        let decoded = self.step(&mut input);
        let used = input.written_len();
        frame.advance(used);
        if !decoded? {
            frame.clear();
            return Ok(None);
        }
        Ok(Some(Bytes::from(std::mem::take(&mut self.out))))
    }

    /// The whole decoded body, once the response body has ended.
    pub(crate) fn finish(mut self) -> Result<Bytes, Failure> {
        while !self.read_all {
            match self.poll_read(Source::End) {
                Read::Ready(Ok(0)) => self.read_all = true,
                Read::Ready(Ok(_)) | Read::Pending => {}
                // Nothing was read, so tower-http drops the error and the body just ends.
                Read::Ready(Err(_)) if !self.read_some => break,
                Read::Ready(Err(error)) => return Err(corrupt(&error)),
            }
        }
        Ok(Bytes::from(self.out))
    }
}

/// Read and decode `body`, taking `Content-Encoding` and `Content-Length` out of `headers`
/// first, as tower-http does once it decides to decode.
pub(crate) async fn read(coding: Coding, headers: &mut HeaderMap, mut body: Incoming) -> Result<Bytes, Failure> {
    headers.remove(CONTENT_ENCODING);
    headers.remove(CONTENT_LENGTH);
    let mut decoder = Decoder::new(coding);
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|e| Failure::Transport(error_chain(&e)))?;
        if let Ok(data) = frame.into_data() {
            decoder.push(&data)?;
        }
    }
    decoder.finish()
}
