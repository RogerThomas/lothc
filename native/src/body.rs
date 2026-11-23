//! A request's body as hyper sends it: the bytes of any body (hyper's `Full`, as before), or a
//! multipart body with a file or a streamed part in it, whose files are read, and streamed parts
//! pulled (`chunks.rs`), as the body is written rather than held in memory, as reqwest streams a
//! `Part::file` or a `Part::stream` (`multipart.rs`).
use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Read};
use std::pin::Pin;
use std::task::{Context, Poll};

use bytes::Bytes;
use http_body_util::Full;
use hyper::body::{Body, Frame, SizeHint};

use crate::chunks::Chunks;

/// The most read from a file part at once in a body of known length: a few of these fill hyper's
/// write buffer, so an upload holds O(chunk) of its files in memory however large they are.
pub(crate) const FILE_CHUNK: u64 = 64 * 1024;

/// The most read from a file part at once in a chunked body: the capacity of tokio-util's
/// `ReaderStream`, which reqwest reads a `Part::file` with. Each read goes out as a chunk of its
/// own there, so reads of the same size put the same chunks on the wire.
pub(crate) const CHUNKED_FILE_CHUNK: u64 = 4 * 1024;

/// One piece of a streamed body, in order.
pub(crate) enum Segment {
    Bytes(Bytes),
    /// Read to its end, not to the length its metadata gave (`expected`, which only sizes each
    /// read), as reqwest's `ReaderStream` reads, at most `chunk` at a time.
    File { file: File, expected: u64, chunk: u64 },
    /// A streamed part, a chunk of the body per chunk pulled.
    Stream(Chunks),
}

/// A body of segments, never empty when made (a body without a file or streamed part is
/// `Full`).
pub(crate) struct Segments {
    queue: VecDeque<Segment>,
    /// Whether the read due next from the file at the front has yielded first.
    yielded: bool,
}

impl Segments {
    pub(crate) fn new(queue: VecDeque<Segment>) -> Self {
        Self { queue, yielded: false }
    }

    /// The next chunk: the next in-memory segment whole, the next read from the file at the
    /// front, or the next chunk pulled from the streamed part at the front. Each read first
    /// yields once (`Pending`, waking itself), so hyper flushes what it has buffered, as it does
    /// before each of reqwest's reads, which tokio's `File` makes on another thread: a read that
    /// fails leaves the server with the same bytes as there.
    fn poll_chunk(&mut self, cx: &mut Context<'_>) -> Poll<Option<io::Result<Bytes>>> {
        loop {
            let Some(segment) = self.queue.front_mut() else { return Poll::Ready(None) };
            let (file, expected, chunk) = match segment {
                Segment::Bytes(bytes) => {
                    let bytes = std::mem::take(bytes);
                    self.queue.pop_front();
                    return Poll::Ready(Some(Ok(bytes)));
                }
                Segment::Stream(chunks) => {
                    let polled = chunks.poll_chunk(cx);
                    if let Poll::Ready(None) = polled {
                        self.queue.pop_front();
                        continue;
                    }
                    return polled;
                }
                Segment::File { file, expected, chunk } => (file, expected, *chunk),
            };
            if !self.yielded {
                self.yielded = true;
                cx.waker().wake_by_ref();
                return Poll::Pending;
            }
            self.yielded = false;
            // Sized to what's left, so a small file's chunk isn't a mostly empty buffer.
            let capacity = usize::try_from((*expected).clamp(1, chunk)).unwrap_or(1);
            let mut read_chunk = Vec::with_capacity(capacity);
            match file.by_ref().take(chunk).read_to_end(&mut read_chunk) {
                Ok(0) => {
                    self.queue.pop_front();
                }
                Ok(read) => {
                    *expected = expected.saturating_sub(read as u64);
                    return Poll::Ready(Some(Ok(Bytes::from(read_chunk))));
                }
                Err(error) => return Poll::Ready(Some(Err(error))),
            }
        }
    }
}

pub(crate) enum RequestBody {
    Full(Full<Bytes>),
    Segments(Segments),
}

impl RequestBody {
    pub(crate) fn full(bytes: Bytes) -> Self {
        Self::Full(Full::new(bytes))
    }
}

impl Body for RequestBody {
    type Data = Bytes;
    type Error = io::Error;

    fn poll_frame(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Option<Result<Frame<Bytes>, io::Error>>> {
        match self.get_mut() {
            Self::Full(full) => Pin::new(full).poll_frame(cx).map_err(|never| match never {}),
            Self::Segments(segments) => segments.poll_chunk(cx).map(|chunk| chunk.map(|chunk| chunk.map(Frame::data))),
        }
    }

    fn is_end_stream(&self) -> bool {
        match self {
            Self::Full(full) => full.is_end_stream(),
            Self::Segments(segments) => segments.queue.is_empty(),
        }
    }

    /// Unknown for a segmented body, as for reqwest's streamed multipart body: its
    /// `Content-Length` header, when the length is known, is what hyper writes it by.
    fn size_hint(&self) -> SizeHint {
        match self {
            Self::Full(full) => full.size_hint(),
            Self::Segments(_) => SizeHint::default(),
        }
    }
}
