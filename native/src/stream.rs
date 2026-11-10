//! A response body read as it arrives, for the streaming verbs (`stream_get`/`stream_post`,
//! `download`, `sse`) on both pools: hyper's frames in, chunks out, as pyreqwest's body reader
//! hands them over with `streamed_read_buffer_limit(1)` (every non-empty chunk as soon as it
//! arrives, never withheld to fill a buffer). A compressed body is decoded as it arrives, in
//! tower-http's pieces (`decompress.rs`).
//!
//! Backpressure is hyper's own: its body channel holds one chunk, and its connection reads the
//! socket for a body only while that chunk has been taken. The sync pool reads only when asked
//! for a chunk; the async pool's pump task (`Channel`) reads at most `READ_AHEAD` chunks ahead of
//! the consumer, then waits, so a slow consumer never makes it buffer the body.
use std::cell::Cell;
use std::collections::VecDeque;
use std::future::poll_fn;
use std::pin::Pin;
use std::task::{ready, Context, Poll, Waker};

use bytes::Bytes;
use http::header::{CONTENT_ENCODING, CONTENT_LENGTH};
use http::HeaderMap;
use hyper::body::{Body, Incoming};

use crate::decompress::{self, Decoder};
use crate::error::{error_chain, Failure};

/// How many chunks the async pump reads ahead of its consumer, as pyreqwest's body reader's
/// channel holds two.
const READ_AHEAD: usize = 2;

/// A streamed response body, decoded as it arrives when its `Content-Encoding` is one reqwest
/// decodes.
pub(crate) struct BodyStream {
    body: Incoming,
    decoder: Option<Decoder>,
    /// The frame the decoder is part way through.
    frame: Option<Bytes>,
    ended: bool,
}

impl BodyStream {
    /// `body`, decoded if `headers` says so, which then loses `Content-Encoding` and
    /// `Content-Length` as tower-http takes them out once it decides to decode.
    pub(crate) fn new(headers: &mut HeaderMap, body: Incoming) -> Self {
        let decoder = decompress::coding(headers).map(|coding| {
            headers.remove(CONTENT_ENCODING);
            headers.remove(CONTENT_LENGTH);
            Decoder::new(coding)
        });
        Self { body, decoder, frame: None, ended: false }
    }

    /// The next non-empty chunk, or `None` at the end of the body.
    pub(crate) fn poll_chunk(&mut self, cx: &mut Context<'_>) -> Poll<Result<Option<Bytes>, Failure>> {
        loop {
            if let (Some(decoder), Some(frame)) = (self.decoder.as_mut(), self.frame.as_mut()) {
                match decoder.decode_next(frame) {
                    Ok(Some(chunk)) => return Poll::Ready(Ok(Some(chunk))),
                    Ok(None) => self.frame = None,
                    Err(failure) => return Poll::Ready(Err(failure)),
                }
                continue;
            }
            if self.ended {
                return Poll::Ready(Ok(None));
            }
            let frame = match ready!(Pin::new(&mut self.body).poll_frame(cx)) {
                None => return Poll::Ready(self.finish()),
                Some(Err(error)) => return Poll::Ready(Err(Failure::Transport(error_chain(&error)))),
                Some(Ok(frame)) => frame,
            };
            // A trailers frame carries no data.
            let Ok(data) = frame.into_data() else { continue };
            match self.decoder.as_mut() {
                None if data.is_empty() => {}
                None => return Poll::Ready(Ok(Some(data))),
                Some(decoder) => match decoder.accepts(&data) {
                    Ok(true) => self.frame = Some(data),
                    Ok(false) => {}
                    Err(failure) => return Poll::Ready(Err(failure)),
                },
            }
        }
    }

    /// The body has ended: whatever the decoder still holds, else its end.
    fn finish(&mut self) -> Result<Option<Bytes>, Failure> {
        self.ended = true;
        match self.decoder.take() {
            None => Ok(None),
            Some(decoder) => Ok(Some(decoder.finish()?).filter(|tail| !tail.is_empty())),
        }
    }
}

/// What the async pool's pump task for one streamed body shares with the pool: the chunks it has
/// read ahead of the consumer, and its waker while it waits for room. Only the loop's thread
/// touches it; `Cell`s, so there is no borrow to fail.
#[derive(Default)]
pub(crate) struct Channel {
    chunks: Cell<VecDeque<Bytes>>,
    room: Cell<Option<Waker>>,
    closed: Cell<bool>,
}

impl Channel {
    /// The oldest chunk read ahead, which makes room for the pump.
    pub(crate) fn pop(&self) -> Option<Bytes> {
        let mut chunks = self.chunks.take();
        let chunk = chunks.pop_front();
        self.chunks.set(chunks);
        if chunk.is_some() {
            self.wake();
        }
        chunk
    }

    /// The consumer is gone: the pump stops at its next turn.
    pub(crate) fn close(&self) {
        self.closed.set(true);
        self.wake();
    }

    fn wake(&self) {
        if let Some(waker) = self.room.take() {
            waker.wake();
        }
    }

    fn push(&self, chunk: Bytes) {
        let mut chunks = self.chunks.take();
        chunks.push_back(chunk);
        self.chunks.set(chunks);
    }

    /// Ready once there is room for another chunk (`false`: the consumer has gone).
    fn poll_room(&self, cx: &mut Context<'_>) -> Poll<bool> {
        if self.closed.get() {
            return Poll::Ready(false);
        }
        let chunks = self.chunks.take();
        let full = chunks.len() >= READ_AHEAD;
        self.chunks.set(chunks);
        if full {
            self.room.set(Some(cx.waker().clone()));
            return Poll::Pending;
        }
        Poll::Ready(true)
    }

    /// Read `body` into the channel until it ends, calling `arrived` after each chunk.
    pub(crate) async fn pump(&self, body: &mut BodyStream, arrived: impl Fn()) -> Result<(), Failure> {
        loop {
            if !poll_fn(|cx| self.poll_room(cx)).await {
                return Err(Failure::Transport("the stream was closed".to_owned()));
            }
            match poll_fn(|cx| body.poll_chunk(cx)).await? {
                Some(chunk) => {
                    self.push(chunk);
                    arrived();
                }
                None => return Ok(()),
            }
        }
    }
}
