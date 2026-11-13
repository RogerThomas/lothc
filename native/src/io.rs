//! Sockets for hyper: raw non-blocking `recv`/`send` on a file descriptor Python (async) or Rust
//! (sync) owns, with readiness coming from wherever the pool waits: the asyncio loop's reader and
//! writer callbacks (`LoopReady`) or `poll(2)` on the calling thread (`PollReady`).
use std::cell::Cell;
use std::io;
use std::os::fd::RawFd;
use std::pin::Pin;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::task::{Context, Poll, Waker};

use rustls::ClientConnection;

use crate::tls::TlsSocket;

/// `send(2)`, retrying on `EINTR`. `MSG_NOSIGNAL` so a peer that has gone away is an `EPIPE`
/// error rather than a `SIGPIPE` (Python ignores it anyway, embedders might not).
pub(crate) fn send(fd: RawFd, buf: &[u8]) -> io::Result<usize> {
    loop {
        // SAFETY: `buf` is a live, readable slice of exactly `buf.len()` bytes for the whole call,
        // and send(2) only reads from it.
        let n = unsafe { libc::send(fd, buf.as_ptr().cast(), buf.len(), libc::MSG_NOSIGNAL) };
        if let Ok(n) = usize::try_from(n) {
            return Ok(n);
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

/// Whether a socket has anything to read right now (bytes, the peer's close, or an error), from a
/// `recv(2)` that peeks and doesn't wait. Not a zero-timeout poll(2): on macOS, polling a socket
/// the asyncio loop's kqueue also watches cost the async pool a third of its throughput.
pub(crate) fn has_input(fd: RawFd) -> bool {
    let mut byte = 0u8;
    // SAFETY: `byte` is one live, writable byte for the whole call, and recv(2) with `MSG_PEEK`
    // writes at most `len` = 1 byte into it.
    let n = unsafe { libc::recv(fd, (&raw mut byte).cast(), 1, libc::MSG_PEEK | libc::MSG_DONTWAIT) };
    n >= 0 || io::Error::last_os_error().kind() != io::ErrorKind::WouldBlock
}

/// Shut both directions of a socket down, failing whatever hyper is waiting on (EOF/EPIPE) while
/// leaving the descriptor itself for its owner to close.
pub(crate) fn shutdown(fd: RawFd) {
    if fd >= 0 {
        // SAFETY: shutdown(2) takes no pointers; on a descriptor that is already shut down or
        // closed it only returns an error, which there is nothing to do about here.
        unsafe { libc::shutdown(fd, libc::SHUT_RDWR) };
    }
}

/// Where a socket's readiness comes from. Both pools' sockets go through this, so the plain and
/// TLS streams are written once.
pub(crate) trait Readiness: Unpin {
    /// Whether a `recv` may find data. When it can't, `cx`'s waker is kept for when it may.
    fn poll_readable(&self, cx: &mut Context<'_>) -> bool;
    /// A short read emptied the socket, so the next read waits instead of hitting `EAGAIN`.
    fn mark_drained(&self);
    /// `recv` said `EAGAIN`: wait (with `cx`'s waker) until the socket is readable again.
    fn wait_readable(&self, cx: &mut Context<'_>);
    /// `send` said `EAGAIN`: wait (with `cx`'s waker) until the socket is writable again.
    fn wait_writable(&self, cx: &mut Context<'_>);
    /// A TLS handshake over the socket has started (`true`) or finished (`false`): until it
    /// finishes, the connection is still connecting, as far as `connect_timeout` goes.
    fn set_handshaking(&self, handshaking: bool);
}

/// One loop-driven socket's readiness. `Cell`s, not `RefCell`s: nothing here can be borrowed
/// twice, so there is no borrow to fail.
#[derive(Default)]
struct LoopFlags {
    readable: Cell<bool>,
    read_waker: Cell<Option<Waker>>,
    write_waker: Cell<Option<Waker>>,
    /// Whether the pool has been asked to `loop.add_writer` this socket and hasn't heard back.
    writer_requested: Cell<bool>,
    /// Whether the pool stopped watching the socket (`loop.remove_reader`) because nothing was
    /// reading it: a streamed body whose consumer is behind, with data waiting unread.
    paused: Cell<bool>,
    /// A TLS handshake is under way over the socket.
    handshaking: Cell<bool>,
}

/// Readiness from the asyncio loop: `loop.add_reader` is registered (level-triggered) except
/// while paused, and `loop.add_writer` only while a send is blocked, both requested through
/// `readers`/`writers` because only the pool can call into Python.
///
/// A paused socket stays marked readable, so the next read goes straight to `recv`; only once that
/// would block is watching it again requested (`resume`). Without the pause a level-triggered
/// reader fires on every loop iteration for data hyper won't read yet (its body channel is full),
/// a busy loop for as long as a streamed body's consumer is slow.
#[derive(Clone)]
pub(crate) struct LoopReady {
    conn: usize,
    flags: Rc<LoopFlags>,
    writers: Rc<Cell<Vec<usize>>>,
    readers: Rc<Cell<Vec<usize>>>,
}

impl LoopReady {
    pub(crate) fn new(conn: usize, writers: Rc<Cell<Vec<usize>>>, readers: Rc<Cell<Vec<usize>>>) -> Self {
        Self { conn, flags: Rc::new(LoopFlags::default()), writers, readers }
    }

    /// The pool stopped watching the socket (nothing was waiting to read it).
    pub(crate) fn pause(&self) {
        self.flags.paused.set(true);
    }

    /// A read is about to wait: if the socket isn't watched, ask the pool to watch it again.
    fn resume(&self) {
        if self.flags.paused.replace(false) {
            let mut readers = self.readers.take();
            readers.push(self.conn);
            self.readers.set(readers);
        }
    }

    /// The loop says the socket is readable: the waiting read, if any, should run.
    pub(crate) fn set_readable(&self) -> Option<Waker> {
        self.flags.readable.set(true);
        self.flags.read_waker.take()
    }

    /// The loop says the socket is writable (its one-shot writer was removed by the pool).
    pub(crate) fn set_writable(&self) -> Option<Waker> {
        self.flags.writer_requested.set(false);
        self.flags.write_waker.take()
    }

    /// Whether a TLS handshake is still under way over the socket.
    pub(crate) fn handshaking(&self) -> bool {
        self.flags.handshaking.get()
    }
}

impl Readiness for LoopReady {
    fn poll_readable(&self, cx: &mut Context<'_>) -> bool {
        if self.flags.readable.get() {
            return true;
        }
        self.flags.read_waker.set(Some(cx.waker().clone()));
        self.resume();
        false
    }

    fn mark_drained(&self) {
        self.flags.readable.set(false);
    }

    fn wait_readable(&self, cx: &mut Context<'_>) {
        self.flags.readable.set(false);
        self.flags.read_waker.set(Some(cx.waker().clone()));
        self.resume();
    }

    fn wait_writable(&self, cx: &mut Context<'_>) {
        self.flags.write_waker.set(Some(cx.waker().clone()));
        if !self.flags.writer_requested.replace(true) {
            let mut writers = self.writers.take();
            writers.push(self.conn);
            self.writers.set(writers);
        }
    }

    fn set_handshaking(&self, handshaking: bool) {
        self.flags.handshaking.set(handshaking);
    }
}

/// Readiness from `poll(2)` on the calling thread: the sync pool's request loop polls the socket
/// whenever every future is stalled, then re-polls them all, so no waker is needed here.
#[derive(Clone)]
pub(crate) struct PollReady(Arc<PollFlags>);

struct PollFlags {
    readable: AtomicBool,
    writable: AtomicBool,
    handshaking: AtomicBool,
}

impl PollReady {
    pub(crate) fn new() -> Self {
        Self(Arc::new(PollFlags {
            readable: AtomicBool::new(false),
            writable: AtomicBool::new(true),
            handshaking: AtomicBool::new(false),
        }))
    }

    pub(crate) fn set_readable(&self) {
        self.0.readable.store(true, Ordering::Relaxed);
    }

    pub(crate) fn set_writable(&self) {
        self.0.writable.store(true, Ordering::Relaxed);
    }

    /// Whether a send is blocked, so the next `poll(2)` should also wait for `POLLOUT`.
    pub(crate) fn wants_writable(&self) -> bool {
        !self.0.writable.load(Ordering::Relaxed)
    }

    /// Whether a TLS handshake is still under way over the socket.
    pub(crate) fn handshaking(&self) -> bool {
        self.0.handshaking.load(Ordering::Relaxed)
    }
}

impl Readiness for PollReady {
    fn poll_readable(&self, _cx: &mut Context<'_>) -> bool {
        self.0.readable.load(Ordering::Relaxed)
    }

    fn mark_drained(&self) {
        self.0.readable.store(false, Ordering::Relaxed);
    }

    fn wait_readable(&self, _cx: &mut Context<'_>) {
        self.0.readable.store(false, Ordering::Relaxed);
    }

    fn wait_writable(&self, _cx: &mut Context<'_>) {
        self.0.writable.store(false, Ordering::Relaxed);
    }

    fn set_handshaking(&self, handshaking: bool) {
        self.0.handshaking.store(handshaking, Ordering::Relaxed);
    }
}

/// What a TLS session runs over: a socket, or another TLS session (an `https://` proxy's, which a
/// tunnel's TLS to the origin runs inside).
pub(crate) trait Transport: hyper::rt::Read + hyper::rt::Write + Unpin {
    /// A TLS handshake over it has started (`true`) or finished (`false`): `Readiness`'s, for the
    /// socket underneath.
    fn set_handshaking(&self, handshaking: bool);
}

/// A plain TCP socket for hyper.
pub(crate) struct PlainSocket<R> {
    fd: RawFd,
    ready: R,
}

impl<R: Readiness> PlainSocket<R> {
    pub(crate) fn new(fd: RawFd, ready: R) -> Self {
        Self { fd, ready }
    }
}

impl<R: Readiness> Transport for PlainSocket<R> {
    fn set_handshaking(&self, handshaking: bool) {
        self.ready.set_handshaking(handshaking);
    }
}

impl<R: Readiness> hyper::rt::Read for PlainSocket<R> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        mut buf: hyper::rt::ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        if !self.ready.poll_readable(cx) {
            return Poll::Pending;
        }
        let (n, capacity) = loop {
            // SAFETY: only the uninitialised tail is handed to recv(2), which writes into it and
            // never reads it; `advance(n)` below then marks exactly the `n` bytes it wrote.
            let dst = unsafe { buf.as_mut() };
            let capacity = dst.len();
            // SAFETY: `dst` is a live, writable region of `capacity` bytes for the whole call.
            let n = unsafe { libc::recv(self.fd, dst.as_mut_ptr().cast(), capacity, 0) };
            if let Ok(n) = usize::try_from(n) {
                break (n, capacity);
            }
            let error = io::Error::last_os_error();
            match error.kind() {
                io::ErrorKind::Interrupted => {}
                io::ErrorKind::WouldBlock => {
                    self.ready.wait_readable(cx);
                    return Poll::Pending;
                }
                _ => return Poll::Ready(Err(error)),
            }
        };
        // A short read drained the socket: skip the recv that would only say EAGAIN. Readiness
        // is level-triggered, so data that arrives later re-arms it.
        if n > 0 && n < capacity {
            self.ready.mark_drained();
        }
        // SAFETY: recv(2) initialised exactly `n` bytes at the start of the region above.
        unsafe { buf.advance(n) };
        Poll::Ready(Ok(()))
    }
}

impl<R: Readiness> hyper::rt::Write for PlainSocket<R> {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        match send(self.fd, buf) {
            Ok(n) => Poll::Ready(Ok(n)),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                self.ready.wait_writable(cx);
                Poll::Pending
            }
            Err(error) => Poll::Ready(Err(error)),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

/// What hyper's connection runs over: one type for plain and TLS, so both pools hold a single
/// `SendRequest`/`Connection` type.
pub(crate) enum Stream<R> {
    Plain(PlainSocket<R>),
    Tls(Box<TlsSocket<PlainSocket<R>>>),
    /// TLS to the origin inside TLS to an `https://` proxy: a tunnel through it.
    TlsInTls(Box<TlsSocket<TlsSocket<PlainSocket<R>>>>),
}

/// A new connection's socket once its proxy's handshake is done (`proxy.rs`), before hyper: the
/// socket itself, or the TLS session to an `https://` proxy a tunnel was opened in.
pub(crate) enum Carrier<R> {
    Plain(PlainSocket<R>),
    Tls(Box<TlsSocket<PlainSocket<R>>>),
}

impl<R: Readiness> Carrier<R> {
    /// The stream hyper runs over: the carrier, or a TLS session inside it (`tls`, for an https
    /// origin, or an http one forwarded through an `https://` proxy).
    pub(crate) fn into_stream(self, tls: Option<ClientConnection>) -> Stream<R> {
        match (self, tls) {
            (Self::Plain(socket), None) => Stream::Plain(socket),
            (Self::Plain(socket), Some(tls)) => Stream::Tls(Box::new(TlsSocket::new(socket, tls))),
            (Self::Tls(proxy), None) => Stream::Tls(proxy),
            (Self::Tls(proxy), Some(tls)) => Stream::TlsInTls(Box::new(TlsSocket::new(*proxy, tls))),
        }
    }
}

impl<R: Readiness> hyper::rt::Read for Stream<R> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: hyper::rt::ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        match self.get_mut() {
            Self::Plain(socket) => Pin::new(socket).poll_read(cx, buf),
            Self::Tls(socket) => Pin::new(socket.as_mut()).poll_read(cx, buf),
            Self::TlsInTls(socket) => Pin::new(socket.as_mut()).poll_read(cx, buf),
        }
    }
}

impl<R: Readiness> hyper::rt::Write for Stream<R> {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        match self.get_mut() {
            Self::Plain(socket) => Pin::new(socket).poll_write(cx, buf),
            Self::Tls(socket) => Pin::new(socket.as_mut()).poll_write(cx, buf),
            Self::TlsInTls(socket) => Pin::new(socket.as_mut()).poll_write(cx, buf),
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            Self::Plain(socket) => Pin::new(socket).poll_flush(cx),
            Self::Tls(socket) => Pin::new(socket.as_mut()).poll_flush(cx),
            Self::TlsInTls(socket) => Pin::new(socket.as_mut()).poll_flush(cx),
        }
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        match self.get_mut() {
            Self::Plain(socket) => Pin::new(socket).poll_shutdown(cx),
            Self::Tls(socket) => Pin::new(socket.as_mut()).poll_shutdown(cx),
            Self::TlsInTls(socket) => Pin::new(socket.as_mut()).poll_shutdown(cx),
        }
    }
}
