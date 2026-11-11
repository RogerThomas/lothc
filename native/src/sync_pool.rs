//! The sync pool: the same hyper futures and request building as the async pool, driven on the
//! calling thread. Instead of the asyncio loop, the thread waits for its own socket with poll(2),
//! GIL released, bounded by the request's deadline. Sockets stay non-blocking: hyper polls them
//! between requests to notice a server close, which a blocking read would turn into a hang.
//! Connections are pooled per origin behind a mutex, so threads share one client.
//!
//! Idle connections expire as reqwest's do (`idle.rs`), but only when the pool is used: checkout
//! closes the expired ones it passes, and pooling a connection closes every origin's expired
//! ones. There is no reaper, since nothing runs between a sync client's calls without a thread of
//! its own, so an unused client's idle sockets stay open until its next request or `close()`.
//!
//! A connection through a proxy does the proxy's handshake (`proxy.rs`, TLS to an `https://`
//! proxy included when it carries a tunnel) on its socket first, on the same thread and under the
//! same deadline.
//!
//! Sockets are set up as `connect.rs` says, and every wait is bounded by the request's `Limits`:
//! its total timeout, `connect_timeout` while connecting (a TLS handshake included, which the
//! first request drives) and `read_timeout`. A sync body is read only when the caller asks for a
//! chunk, so `read_timeout`'s gap starts again with each read: unlike pyreqwest, whose reader
//! reads ahead into a two-chunk buffer, a caller slower than `read_timeout` doesn't time out.
//!
//! A request that finds no idle connection waits in its origin's queue while it opens one of its
//! own, as hyper-util's checkout races its connect: a connection another thread frees meanwhile
//! is handed to the oldest request waiting, rather than pooled. One mid proxy handshake is woken
//! for it (its poll(2) also watches a `WakePipe`); one still in its blocking TCP connect takes it
//! once the connect returns. The connection it was opening is finished anyway and pooled, as
//! hyper-util's is in the background: right there if nothing is left but setup without I/O, else
//! on a thread of its own for the rest of the proxy handshake (under its `connect_timeout` only,
//! as hyper-util's has no deadline of its own; closing the pool cuts it off, as it is in
//! `InFlight`).
//!
//! With `max_connections` (`limit.rs`), a request takes one of the client's slots before it
//! looks for a connection, waiting for one with the GIL released, and gives it back once its
//! body has been read (a stream's once it is read to its end, fails or is closed). The wait
//! counts towards its total timeout but not towards `read_timeout`, which starts once it has
//! its slot.
//!
//! A redirect to follow (`redirect.rs`) puts its connection back (hyper drains the unread response
//! or closes it) and sends the hop's request the same way, under the same deadline, so a hop to the
//! same origin takes that connection again.
//!
//! A streamed request (`stream`, a `SyncStream`) takes a connection the same way, then holds it
//! while its body is read, one chunk per call and only when asked (`stream.rs`), each wait under
//! the request's total timeout, if any. A body read to its end returns the connection to the
//! pool; a stream closed or dropped early closes it, so the server sees the hang-up.
//!
//! `close()` fails every request still running with the `RuntimeError` a request after it raises,
//! as pyreqwest's does: it shuts down the sockets in `InFlight`, which wakes their threads' poll(2),
//! and wakes every thread waiting for a `max_connections` slot.
//! A stream's body, once its head has arrived, outlives the client, as on pyreqwest.
use std::cell::Cell;
use std::collections::VecDeque;
use std::future::{poll_fn, Future};
use std::io;
use std::net::{IpAddr, TcpStream};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll, Wake, Waker};
use std::thread;
use std::time::{Duration, Instant};

use bytes::Bytes;
use http::{Request, Response};
use hyper::body::Incoming;
use hyper::client::conn::http1;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString, PyTuple};
use rustls::pki_types::ServerName;
use rustls::ClientConnection;
use url::Url;

use crate::body::RequestBody;
use crate::connect::{ConnectOptions, ConnectSettings};
use crate::cookie::Cookies;
use crate::error::{error_chain, Failure};
use crate::idle::IdlePolicy;
use crate::io::{has_input, shutdown, Carrier, PlainSocket, PollReady};
use crate::limit::{checked_max_connections, SyncLimiter, SyncPermit};
use crate::proxy::{Proxies, Route};
use crate::redirect::Redirect;
use crate::request::{connect_address, ClientConfig, DeadlineClock, OriginKey, Prepared, VerbHeaders};
use crate::response::{fetch, send, Exchange, Fetched, Glue, Head, Sender};
use crate::stream::BodyStream;
use crate::tls::{tunnel_config, TlsSettings};

/// A waker that only records that something was woken, so the loop re-polls before waiting.
struct FlagWaker(AtomicBool);

impl Wake for FlagWaker {
    fn wake(self: Arc<Self>) {
        self.0.store(true, Ordering::Relaxed);
    }

    fn wake_by_ref(self: &Arc<Self>) {
        self.0.store(true, Ordering::Relaxed);
    }
}

/// The connections requests are running on, so `close()` can shut their sockets down. Idle
/// connections aren't in it (`close()` drops those), nor a stream's once its head has arrived.
#[derive(Default)]
struct InFlight {
    /// Set by `close()`, under this lock, before it shuts the sockets down: a connection entered
    /// later is refused, and a request whose socket it shut down sees it when it leaves.
    closed: bool,
    next: u64,
    fds: Vec<(u64, RawFd)>,
}

/// A connection's place in `InFlight` while a request runs on it, left on drop.
struct Busy {
    in_flight: Arc<Mutex<InFlight>>,
    id: u64,
    left: bool,
}

impl Busy {
    /// Leave `InFlight`: whether the pool closed first.
    fn leave(&mut self) -> bool {
        let mut in_flight = lock(&self.in_flight);
        if let Some(at) = in_flight.fds.iter().position(|(id, _)| *id == self.id) {
            in_flight.fds.swap_remove(at);
        }
        self.left = true;
        in_flight.closed
    }
}

impl Drop for Busy {
    fn drop(&mut self) {
        if !self.left {
            self.leave();
        }
    }
}

struct SyncConn {
    /// Set while a request runs on it. First, so a dropped connection leaves `InFlight` before its
    /// descriptor closes: `close()` must never shut down a number a new socket has reused.
    busy: Option<Busy>,
    /// Owns the descriptor `fd`, closing it on drop.
    _stream: TcpStream,
    fd: RawFd,
    ready: PollReady,
    sender: Sender,
    driver: Driver,
    closed: bool,
    /// Its `connect_timeout` deadline, which still applies while its TLS handshake is under way.
    connect_by: Option<Instant>,
}

impl SyncConn {
    /// Done with the request it was busy with: whether the pool closed first.
    fn leave(&mut self) -> bool {
        self.busy.take().is_some_and(|mut busy| busy.leave())
    }
}

/// Where one request goes: its origin, where its connection connects (the origin, or a proxy),
/// and the name its TLS, if it goes over TLS, is verified against (`Route::tls_name`).
struct Target {
    key: OriginKey,
    host: String,
    port: u16,
    server_name: Option<ServerName<'static>>,
    route: Route,
    /// The target host a `socks4://` or `socks5://` proxy wants resolved here first.
    resolve: Option<String>,
}

/// How long `poll(2)` may wait for `deadline`: `None` once it has passed, `-1` for no deadline.
fn poll_timeout(deadline: Option<Instant>) -> Option<i32> {
    match deadline {
        None => Some(-1),
        Some(deadline) => {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return None;
            }
            Some(i32::try_from(left.as_millis().max(1)).unwrap_or(i32::MAX))
        }
    }
}

/// The earlier of two deadlines, if either.
fn earliest(a: Option<Instant>, b: Option<Instant>) -> Option<Instant> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (a, b) => a.or(b),
    }
}

/// What bounds one request's waits: its total timeout's deadline, and reqwest's `read_timeout`,
/// which until the response's head arrives is a fixed deadline from the request's start
/// (`head_by`), then a limit on each gap while its body is read.
#[derive(Clone, Copy)]
struct Limits {
    total: Option<Instant>,
    read_timeout: Option<Duration>,
    head_by: Option<Instant>,
    clock: DeadlineClock,
}

impl Limits {
    fn new(clock: DeadlineClock, started: Instant, timeout: Option<Duration>, read_timeout: Option<Duration>) -> Self {
        let deadline = |timeout| clock.deadline(started, timeout);
        Self { total: timeout.and_then(deadline), read_timeout, head_by: read_timeout.and_then(deadline), clock }
    }

    /// These limits with `read_timeout`'s deadline for the head from `now`, once a wait for a
    /// `max_connections` slot is over: that wait counts towards the total timeout, but isn't
    /// reading.
    fn reading_from(self, now: Instant) -> Self {
        Self { head_by: self.read_timeout.and_then(|timeout| self.clock.deadline(now, timeout)), ..self }
    }

    /// The deadline until the response's head arrives.
    fn before_head(&self) -> Option<Instant> {
        earliest(self.total, self.head_by)
    }
}

/// One `drive`'s view of its request's `Limits`, as its connection makes progress.
struct Waits<'a> {
    limits: Limits,
    /// When the final response's head arrived, set by the future being driven (or already, for a
    /// body read): from then on `read_timeout` limits each gap, from it or the last read.
    head: &'a Cell<Option<Instant>>,
    last_read: Option<Instant>,
    /// A new connection's `connect_timeout` deadline, while its TLS handshake is under way.
    connect_by: Option<Instant>,
}

impl Waits<'_> {
    fn deadline(&self, ready: &PollReady) -> Option<Instant> {
        let read = match (self.limits.read_timeout, self.head.get()) {
            (None, _) => None,
            (Some(_), None) => self.limits.head_by,
            (Some(timeout), Some(head)) => self.limits.clock.deadline(self.last_read.map_or(head, |read| read.max(head)), timeout),
        };
        earliest(earliest(self.limits.total, read), self.connect_by.filter(|_| ready.handshaking()))
    }
}

/// Wait for `fd` to be readable (or writable, if a send is blocked) until `deadline`: `None` if
/// it passed first, else whether it is readable.
fn wait(fd: RawFd, ready: &PollReady, deadline: Option<Instant>) -> Result<Option<bool>, Failure> {
    loop {
        let Some(timeout) = poll_timeout(deadline) else { return Ok(None) };
        let events = if ready.wants_writable() { libc::POLLIN | libc::POLLOUT } else { libc::POLLIN };
        let mut pfd = libc::pollfd { fd, events, revents: 0 };
        // SAFETY: `pfd` is one valid, writable `pollfd` for the whole call, matching `nfds` = 1.
        match unsafe { libc::poll(&mut pfd, 1, timeout) } {
            0 => return Ok(None),
            n if n < 0 => {
                let error = io::Error::last_os_error();
                if error.kind() != io::ErrorKind::Interrupted {
                    return Err(Failure::Transport(error_chain(&error)));
                }
            }
            _ => {
                let failed = pfd.revents & (libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0;
                let readable = failed || pfd.revents & libc::POLLIN != 0;
                if readable {
                    ready.set_readable();
                }
                if failed || pfd.revents & libc::POLLOUT != 0 {
                    ready.set_writable();
                }
                return Ok(Some(readable));
            }
        }
    }
}

/// What `wait_or_woken` saw first.
enum Woke {
    Socket,
    /// The `WakePipe`: another thread handed the request a connection.
    Pipe,
    TimedOut,
}

/// `wait`, also watching `wake` (a `WakePipe`'s read end, drained once it fires), which wins
/// over the socket when both are ready, as hyper-util's checkout is polled before its connect.
fn wait_or_woken(fd: RawFd, ready: &PollReady, wake: Option<&WakePipe>, deadline: Option<Instant>) -> Result<Woke, Failure> {
    let Some(wake) = wake else {
        return wait(fd, ready, deadline).map(|ready| if ready.is_some() { Woke::Socket } else { Woke::TimedOut });
    };
    loop {
        let Some(timeout) = poll_timeout(deadline) else { return Ok(Woke::TimedOut) };
        let events = if ready.wants_writable() { libc::POLLIN | libc::POLLOUT } else { libc::POLLIN };
        let mut pfds = [libc::pollfd { fd, events, revents: 0 }, libc::pollfd { fd: wake.read.as_raw_fd(), events: libc::POLLIN, revents: 0 }];
        // SAFETY: `pfds` is two valid, writable `pollfd`s for the whole call, matching `nfds` = 2.
        match unsafe { libc::poll(pfds.as_mut_ptr(), 2, timeout) } {
            0 => return Ok(Woke::TimedOut),
            n if n < 0 => {
                let error = io::Error::last_os_error();
                if error.kind() != io::ErrorKind::Interrupted {
                    return Err(Failure::Transport(error_chain(&error)));
                }
            }
            _ => {
                let revents = pfds[0].revents;
                let failed = revents & (libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0;
                if failed || revents & libc::POLLIN != 0 {
                    ready.set_readable();
                }
                if failed || revents & libc::POLLOUT != 0 {
                    ready.set_writable();
                }
                if pfds[1].revents != 0 {
                    wake.drain();
                    return Ok(Woke::Pipe);
                }
                return Ok(Woke::Socket);
            }
        }
    }
}

/// A pipe a thread waiting in poll(2) watches too, so another thread can wake it: a request mid
/// proxy handshake that is handed a connection meanwhile.
struct WakePipe {
    read: OwnedFd,
    write: OwnedFd,
}

impl WakePipe {
    fn new() -> io::Result<Self> {
        let mut fds = [0; 2];
        // SAFETY: `fds` is a writable array of the two descriptors pipe(2) fills in.
        if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: pipe(2) just opened both descriptors, and nothing else owns them.
        let (read, write) = unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) };
        for fd in [&read, &write] {
            // SAFETY: fcntl(2) on a descriptor this owns, with integer arguments only.
            let set = unsafe {
                libc::fcntl(fd.as_raw_fd(), libc::F_SETFL, libc::O_NONBLOCK) == 0
                    && libc::fcntl(fd.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) == 0
            };
            if !set {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(Self { read, write })
    }

    fn wake(&self) {
        let byte = 1u8;
        // SAFETY: writes one byte from a live local; a full pipe (EAGAIN) is already awake.
        unsafe { libc::write(self.write.as_raw_fd(), (&raw const byte).cast(), 1) };
    }

    fn drain(&self) {
        let mut buf = [0u8; 16];
        // SAFETY: reads into a live, writable local buffer of exactly `buf.len()` bytes.
        while unsafe { libc::read(self.read.as_raw_fd(), buf.as_mut_ptr().cast(), buf.len()) } > 0 {}
    }
}

/// A connection's hyper driver, `()` once the connection has closed.
type Driver = Pin<Box<dyn Future<Output = ()> + Send>>;

/// A proxy's handshake on a new connection, boxed so that a request handed another connection
/// mid handshake can leave it to finish on a thread of its own: what hyper then talks through, or
/// the connection error.
type HandshakeFuture = Pin<Box<dyn Future<Output = Result<Carrier<PollReady>, String>> + Send>>;

/// Drive a connection's hyper driver and `fut` (which runs on that connection) until `fut`
/// finishes, waiting for socket `fd` in poll(2) until `waits` says to stop.
fn drive<F: Future>(
    fd: RawFd,
    ready: &PollReady,
    driver: &mut Driver,
    closed: &mut bool,
    mut fut: Pin<&mut F>,
    waits: &mut Waits<'_>,
) -> Result<F::Output, Failure> {
    let flag = Arc::new(FlagWaker(AtomicBool::new(false)));
    let waker = Waker::from(flag.clone());
    let mut cx = Context::from_waker(&waker);
    loop {
        flag.0.store(false, Ordering::Relaxed);
        if !*closed && driver.as_mut().poll(&mut cx).is_ready() {
            *closed = true;
        }
        if let Poll::Ready(result) = fut.as_mut().poll(&mut cx) {
            return Ok(result);
        }
        if flag.0.load(Ordering::Relaxed) {
            continue;
        }
        match wait(fd, ready, waits.deadline(ready))? {
            None => return Err(Failure::Timeout),
            Some(true) if waits.limits.read_timeout.is_some() => waits.last_read = Some(Instant::now()),
            Some(_) => {}
        }
    }
}

/// Drive `conn`'s hyper driver and one request until the request finishes (or gets a redirect
/// to follow, see `fetch`).
fn run_on(conn: &mut SyncConn, request: Request<RequestBody>, limits: Limits, redirect: Option<&mut Redirect>, cookies: Option<&Cookies>) -> Result<Fetched, Failure> {
    let SyncConn { fd, ready, sender, driver, closed, connect_by, .. } = conn;
    let head = Cell::new(None);
    let mut waits = Waits { limits, head: &head, last_read: None, connect_by: *connect_by };
    let mark = limits.read_timeout.is_some().then_some(&head);
    let fetched = drive(*fd, ready, driver, closed, std::pin::pin!(fetch(sender, request, redirect, cookies, mark)), &mut waits);
    fetched?
}

/// Drive `conn`'s hyper driver and one request until its response's head arrives.
fn send_on(conn: &mut SyncConn, request: Request<RequestBody>, limits: Limits) -> Result<Response<Incoming>, Failure> {
    let SyncConn { fd, ready, sender, driver, closed, connect_by, .. } = conn;
    let head = Cell::new(None);
    let mut waits = Waits { limits, head: &head, last_read: None, connect_by: *connect_by };
    let sent = drive(*fd, ready, driver, closed, std::pin::pin!(send(sender, request)), &mut waits);
    sent?
}

/// Drive a new connection's proxy handshake on the calling thread until it finishes (`Ok(Some)`,
/// what hyper then talks through), `deadline` passes first, or `wake` fires (`Ok(None)`): another
/// thread handed the request a connection, and the handshake, unfinished, can go on elsewhere.
fn drive_handshake(
    handshake: &mut HandshakeFuture,
    fd: RawFd,
    ready: &PollReady,
    deadline: Option<Instant>,
    wake: Option<&WakePipe>,
) -> Result<Option<Carrier<PollReady>>, Failure> {
    let flag = Arc::new(FlagWaker(AtomicBool::new(false)));
    let waker = Waker::from(flag.clone());
    let mut cx = Context::from_waker(&waker);
    loop {
        flag.0.store(false, Ordering::Relaxed);
        if let Poll::Ready(result) = handshake.as_mut().poll(&mut cx) {
            return result.map(Some).map_err(Failure::Transport);
        }
        if flag.0.load(Ordering::Relaxed) {
            continue;
        }
        match wait_or_woken(fd, ready, wake, deadline)? {
            Woke::Socket => {}
            Woke::Pipe => return Ok(None),
            Woke::TimedOut => return Err(Failure::Timeout),
        }
    }
}

/// Whether a pooled connection is still usable: nothing reads an idle socket here (no event
/// loop), so a server's silent close is only noticed by looking, as urllib3 does before reuse.
fn still_open(conn: &mut SyncConn) -> bool {
    if has_input(conn.fd) {
        // EOF (or stray bytes): let hyper's driver read it and decide.
        conn.ready.set_readable();
        let mut cx = Context::from_waker(Waker::noop());
        if conn.driver.as_mut().poll(&mut cx).is_ready() {
            conn.closed = true;
        }
    }
    !conn.closed && !conn.sender.is_closed()
}

/// Close a healthy idle connection on purpose: the sender goes first, so hyper's driver shuts the
/// connection down (TLS's close_notify included) before the socket is closed, as reqwest's does.
fn close_idle(conn: SyncConn) {
    let SyncConn { busy, _stream: stream, sender, mut driver, closed, .. } = conn;
    drop(busy);
    drop(sender);
    if !closed {
        let _ = driver.as_mut().poll(&mut Context::from_waker(Waker::noop()));
    }
    drop(driver);
    drop(stream);
}

/// One origin's idle connections, (connection, when it went idle), oldest first; checkout takes
/// from the end.
type IdleConns = Vec<(SyncConn, Instant)>;

/// A request in its origin's queue, opening a connection of its own.
struct Waiting {
    id: u64,
    /// Wakes it, once it waits in poll(2) for a proxy handshake.
    wake: Option<Arc<WakePipe>>,
    /// The connection another thread freed for it, until it takes it.
    handed: Option<SyncConn>,
}

/// One origin's idle connections and the requests waiting for one.
struct SyncOrigin {
    key: OriginKey,
    idle: IdleConns,
    /// Oldest first, each handed the first connection freed after it arrived.
    waiting: VecDeque<Waiting>,
}

#[derive(Default)]
struct Origins {
    list: Vec<SyncOrigin>,
    /// The next `Waiting::id`.
    next: u64,
}

impl Origins {
    fn find(&mut self, key: &OriginKey) -> Option<&mut SyncOrigin> {
        self.list.iter_mut().find(|origin| &origin.key == key)
    }

    /// Queue a request for a connection to `key`: its id.
    fn wait(&mut self, key: &OriginKey) -> u64 {
        let id = self.next;
        self.next += 1;
        let waiting = Waiting { id, wake: None, handed: None };
        match self.find(key) {
            Some(origin) => origin.waiting.push_back(waiting),
            None => self.list.push(SyncOrigin { key: key.clone(), idle: Vec::new(), waiting: VecDeque::from([waiting]) }),
        }
        id
    }

    /// Take request `id` out of `key`'s queue, if it is still there (and `only_handed` doesn't
    /// keep it there for want of a connection handed to it): what it was handed, if anything.
    fn leave(&mut self, key: &OriginKey, id: u64, only_handed: bool) -> Option<SyncConn> {
        let origin = self.find(key)?;
        let at = origin.waiting.iter().position(|waiting| waiting.id == id && (!only_handed || waiting.handed.is_some()))?;
        origin.waiting.remove(at)?.handed
    }
}

/// What a `SyncPool` shares with the threads that finish a connection for it in the background.
struct Shared {
    idle_policy: IdlePolicy,
    origins: Mutex<Origins>,
    closed: AtomicBool,
}

impl Shared {
    /// The most recently idled connection to `key` that is still fresh and open, closing the
    /// expired ones it passes (hyper-util's checkout). Without one, the request queues for one
    /// while it opens its own (`Ticket`), if connections are pooled at all.
    fn take_idle<'a>(&'a self, key: &'a OriginKey) -> Result<SyncConn, Option<Ticket<'a>>> {
        let now = Instant::now();
        loop {
            let (mut conn, idle_at) = {
                let mut origins = lock(&self.origins);
                match origins.find(key).and_then(|origin| origin.idle.pop()) {
                    Some(idle) => idle,
                    None => {
                        let ticket = self.idle_policy.pools().then(|| Ticket { shared: self, key, id: origins.wait(key) });
                        return Err(ticket);
                    }
                }
            };
            if self.idle_policy.expired(idle_at, now) {
                close_idle(conn);
            } else if still_open(&mut conn) {
                return Ok(conn);
            }
        }
    }

    /// Hand a finished connection to the oldest request waiting for one to its origin, else pool
    /// it, unless its origin already holds as many idle ones as allowed (the ones already idle
    /// stay, as in hyper-util, which offers a waiting request one however many are idle); first
    /// take every origin's expired ones out. Each origin's list is oldest first, so its expired
    /// ones are a prefix.
    fn put_idle(&self, key: &OriginKey, conn: SyncConn) {
        if self.closed.load(Ordering::Relaxed) {
            return;
        }
        let mut expired = Vec::new();
        let mut woken = None;
        let rejected = {
            let mut origins = lock(&self.origins);
            let now = Instant::now();
            for origin in &mut origins.list {
                let stale = origin.idle.iter().take_while(|(_, idle_at)| self.idle_policy.expired(*idle_at, now)).count();
                expired.extend(origin.idle.drain(..stale).map(|(conn, _)| conn));
            }
            origins.list.retain(|origin| !origin.idle.is_empty() || !origin.waiting.is_empty() || &origin.key == key);
            match origins.find(key) {
                Some(origin) => match origin.waiting.iter_mut().find(|waiting| waiting.handed.is_none()) {
                    Some(waiting) => {
                        woken = waiting.wake.clone();
                        waiting.handed = Some(conn);
                        None
                    }
                    None if !self.idle_policy.has_room(origin.idle.len()) => Some(conn),
                    None => {
                        origin.idle.push((conn, now));
                        None
                    }
                },
                None if !self.idle_policy.has_room(0) => Some(conn),
                None => {
                    origins.list.push(SyncOrigin { key: key.clone(), idle: vec![(conn, now)], waiting: VecDeque::new() });
                    None
                }
            }
        };
        // Woken and closed outside the lock: shutting a connection down may write to its socket.
        if let Some(wake) = woken {
            wake.wake();
        }
        expired.into_iter().chain(rejected).for_each(close_idle);
    }

    /// A connection finished for nobody's request in particular: pooled (or handed on), unless
    /// the pool closed first.
    fn put_back(&self, key: &OriginKey, mut conn: SyncConn) {
        if !conn.leave() && !conn.closed {
            self.put_idle(key, conn);
        }
    }
}

/// A request's place in its origin's queue while it opens a connection of its own. Dropped, it
/// leaves the queue, and a connection handed to it too late goes on to the next request waiting,
/// or the pool.
struct Ticket<'a> {
    shared: &'a Shared,
    key: &'a OriginKey,
    id: u64,
}

impl Ticket<'_> {
    /// The connection another thread freed for this request, if one has been (it leaves the
    /// queue then) and the server hasn't closed it since: then the request goes on with its own.
    fn handed(&self) -> Option<SyncConn> {
        let mut conn = lock(&self.shared.origins).leave(self.key, self.id, true)?;
        if still_open(&mut conn) {
            return Some(conn);
        }
        close_idle(conn);
        None
    }

    /// A pipe that wakes this request's poll(2) when a connection is handed to it; `None` if no
    /// pipe can be opened (it then only notices once its own connection is made).
    fn wake(&self) -> Option<Arc<WakePipe>> {
        let wake = Arc::new(WakePipe::new().ok()?);
        let mut origins = lock(&self.shared.origins);
        let waiting = origins.find(self.key)?.waiting.iter_mut().find(|waiting| waiting.id == self.id)?;
        waiting.wake = Some(wake.clone());
        Some(wake)
    }
}

impl Drop for Ticket<'_> {
    fn drop(&mut self) {
        let late = lock(&self.shared.origins).leave(self.key, self.id, false);
        if let Some(conn) = late {
            self.shared.put_idle(self.key, conn);
        }
    }
}

/// A new connection's socket, connected (to the origin or its proxy) and in `InFlight`, with the
/// proxy's handshake, if any, still to run before TLS and hyper.
struct Opening {
    /// First, so a connection given up leaves `InFlight` before its descriptor closes.
    busy: Busy,
    stream: TcpStream,
    fd: RawFd,
    ready: PollReady,
    handshake: Option<HandshakeFuture>,
    /// Its `connect_timeout` deadline, which bounds its proxy's handshake and, later, its TLS one.
    connect_by: Option<Instant>,
}

/// What the TLS a connection's requests go over is set up with, or `None` for plain http.
type TlsSetup = Option<(Arc<rustls::ClientConfig>, ServerName<'static>)>;

impl Opening {
    /// The socket itself, for a connection without a proxy handshake.
    fn plain(&self) -> Carrier<PollReady> {
        Carrier::Plain(PlainSocket::new(self.fd, self.ready.clone()))
    }

    /// Set up TLS (when its requests go over TLS) and hyper's connection over `carrier` (the
    /// socket, or what its proxy's handshake left), once that handshake is done: neither does any
    /// I/O yet (TLS's handshake runs with the first request).
    fn establish(self, carrier: Carrier<PollReady>, tls: TlsSetup) -> Result<SyncConn, Failure> {
        let Self { busy, stream, fd, ready, connect_by, .. } = self;
        let tls = tls.map(|(config, name)| ClientConnection::new(config, name)).transpose();
        let io = carrier.into_stream(tls.map_err(|e| Failure::Transport(error_chain(&e)))?);
        let (sender, driver) = futures_executor::block_on(http1::Builder::new().title_case_headers(true).handshake(io))
            .map_err(|e| Failure::Transport(error_chain(&e)))?;
        let driver: Driver = Box::pin(async move {
            let _ = driver.await;
        });
        Ok(SyncConn { busy: Some(busy), _stream: stream, fd, ready, sender, driver, closed: false, connect_by })
    }

    /// Finish the connection for the pool, now that the request it was for took another: what
    /// is left of its proxy's handshake runs on a thread of its own, under its `connect_timeout`
    /// only (closing the pool shuts its socket down); without one, it is finished right here.
    fn finish_elsewhere(mut self, shared: &Arc<Shared>, key: &OriginKey, tls: TlsSetup) {
        let Some(mut handshake) = self.handshake.take() else {
            let carrier = self.plain();
            if let Ok(conn) = self.establish(carrier, tls) {
                shared.put_back(key, conn);
            }
            return;
        };
        let (shared, key) = (shared.clone(), key.clone());
        // If no thread can be started, the connection is closed with the closure.
        let _ = thread::Builder::new().name("lothc-connect".to_owned()).spawn(move || {
            if let Ok(Some(carrier)) = drive_handshake(&mut handshake, self.fd, &self.ready, self.connect_by, None) {
                if let Ok(conn) = self.establish(carrier, tls) {
                    shared.put_back(&key, conn);
                }
            }
        });
    }
}

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct SyncPool {
    config: ClientConfig,
    connect: Arc<ConnectSettings>,
    tls: TlsSettings,
    tls_config: Mutex<Option<Arc<rustls::ClientConfig>>>,
    /// `tls_config` for the TLS to an `https://` proxy that carries a tunnel (`tunnel_config`).
    tunnel_tls_config: Mutex<Option<Arc<rustls::ClientConfig>>>,
    shared: Arc<Shared>,
    /// `max_connections`' slots; `None` without it.
    limiter: Option<Arc<SyncLimiter>>,
    in_flight: Arc<Mutex<InFlight>>,
    glue: Glue,
    clock: DeadlineClock,
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

impl SyncPool {
    fn tls_config(&self) -> Result<Arc<rustls::ClientConfig>, Failure> {
        let mut config = lock(&self.tls_config);
        if let Some(config) = config.as_ref() {
            return Ok(config.clone());
        }
        let built = self.tls.client_config().map_err(Failure::Transport)?;
        *config = Some(built.clone());
        Ok(built)
    }

    fn tunnel_tls_config(&self) -> Result<Arc<rustls::ClientConfig>, Failure> {
        let mut config = lock(&self.tunnel_tls_config);
        if let Some(config) = config.as_ref() {
            return Ok(config.clone());
        }
        let built = tunnel_config(&*self.tls_config()?);
        *config = Some(built.clone());
        Ok(built)
    }

    /// Enter a connection's socket `fd` into `InFlight`, unless the pool has closed.
    fn enter(&self, fd: RawFd) -> Result<Busy, Failure> {
        let mut in_flight = lock(&self.in_flight);
        if in_flight.closed {
            return Err(Failure::Closed);
        }
        let id = in_flight.next;
        in_flight.next += 1;
        in_flight.fds.push((id, fd));
        Ok(Busy { in_flight: self.in_flight.clone(), id, left: false })
    }

    /// Whether `close()` has shut `InFlight`'s sockets down (or is doing so).
    fn closing(&self) -> bool {
        lock(&self.in_flight).closed
    }

    /// The TLS `target`'s requests go over, if any (`Route::tls_name`).
    fn tls_setup(&self, target: &Target) -> Result<TlsSetup, Failure> {
        target.server_name.as_ref().map(|name| Ok((self.tls_config()?, name.clone()))).transpose()
    }

    /// A newly connected socket, entered into `InFlight` before anything waits on it, so
    /// `close()` can cut a proxy's or TLS's handshake short too, with its proxy's handshake ready
    /// and its `connect_timeout` deadline (`connect_by`).
    fn opening(&self, target: &Target, stream: TcpStream, resolved: Option<IpAddr>, connect_by: Option<Instant>) -> Result<Opening, Failure> {
        let transport = |e: io::Error| Failure::Transport(error_chain(&e));
        let busy = self.enter(stream.as_raw_fd())?;
        stream.set_nodelay(true).map_err(transport)?;
        stream.set_nonblocking(true).map_err(transport)?;
        let fd = stream.as_raw_fd();
        let ready = PollReady::new();
        let tunnel_config = target.route.tunnels_in_tls().then(|| self.tunnel_tls_config()).transpose()?;
        let handshake = target.route.handshake(&target.key.host, target.key.port, self.config.user_agent(), resolved, tunnel_config);
        let handshake = handshake.map_err(Failure::Transport)?.map(|handshake| -> HandshakeFuture {
            Box::pin(handshake.run(PlainSocket::new(fd, ready.clone())))
        });
        Ok(Opening { busy, stream, fd, ready, handshake, connect_by })
    }

    /// A request takes `conn`, handed over while it was opening a connection of its own
    /// (`opening`, if it got as far as a socket), which is finished for the pool elsewhere.
    fn take_handed(&self, target: &Target, conn: SyncConn, opening: Option<Opening>) -> Result<SyncConn, Failure> {
        if let Some(opening) = opening {
            if let Ok(tls) = self.tls_setup(target) {
                opening.finish_elsewhere(&self.shared, &target.key, tls);
            }
        }
        self.enter_conn(conn)
    }

    /// A new connection for a request to `target`, in `InFlight`, under `limits` and
    /// `connect_timeout` (which still applies to its TLS handshake, which the first request
    /// drives: `connect_by`). Until it is made, a connection another thread frees for the
    /// request (`ticket`) wins, as hyper-util's checkout wins over its connect, unless the
    /// request's own deadline passed first.
    fn connect(&self, target: &Target, limits: Limits, ticket: Option<&Ticket<'_>>) -> Result<SyncConn, Failure> {
        let connect_by = self.connect.connect_timeout.and_then(|timeout| self.clock.deadline(Instant::now(), timeout));
        let deadline = earliest(limits.before_head(), connect_by);
        // reqwest resolves a `socks4://` or `socks5://` proxy's target before connecting to the
        // proxy.
        let connected = match &target.resolve {
            Some(host) => self.connect.resolve_first(host),
            None => Ok(None),
        }
        .and_then(|resolved| Ok((self.connect.connect_blocking(&target.host, target.port, deadline)?, resolved)));
        // A blocking connect can't be woken: a connection handed over meanwhile is taken now,
        // even if `connect_timeout` ran out, as long as the request's own deadline hasn't.
        let expired = matches!(connected, Err(Failure::Timeout)) && limits.before_head().is_some_and(|until| until <= Instant::now());
        let handed = ticket.filter(|_| !expired).and_then(Ticket::handed);
        if let Some(handed) = handed {
            let opening = connected.ok().and_then(|(stream, resolved)| self.opening(target, stream, resolved, connect_by).ok());
            return self.take_handed(target, handed, opening);
        }
        let (stream, resolved) = connected?;
        let mut opening = self.opening(target, stream, resolved, connect_by)?;
        let carrier = match opening.handshake.take() {
            None => opening.plain(),
            Some(mut handshake) => {
                let wake = ticket.and_then(Ticket::wake);
                loop {
                    // Checked before each wait, as a connection handed over before the pipe was
                    // attached didn't write to it.
                    if let Some(handed) = ticket.and_then(Ticket::handed) {
                        opening.handshake = Some(handshake);
                        return self.take_handed(target, handed, Some(opening));
                    }
                    if let Some(carrier) = drive_handshake(&mut handshake, opening.fd, &opening.ready, deadline, wake.as_deref())? {
                        break carrier;
                    }
                }
            }
        };
        opening.establish(carrier, self.tls_setup(target)?)
    }

    /// `conn`, entered into `InFlight` for a request.
    fn enter_conn(&self, mut conn: SyncConn) -> Result<SyncConn, Failure> {
        conn.busy = Some(self.enter(conn.fd)?);
        Ok(conn)
    }

    /// A connection for a request to `target`, in `InFlight`: an idle one, or a new one (or one
    /// freed for it while it connected). A failure after `close()` began is that close's doing.
    fn checkout(&self, target: &Target, limits: Limits) -> Result<SyncConn, Failure> {
        let conn = match self.shared.take_idle(&target.key) {
            Ok(conn) => self.enter_conn(conn),
            // Its place in the queue is given up as soon as it has a connection.
            Err(ticket) => self.connect(target, limits, ticket.as_ref()),
        };
        match conn {
            Err(_) if self.closing() => Err(Failure::Closed),
            conn => conn,
        }
    }

    /// Take an idle connection (or open one), run the request, put the connection back if it
    /// is still good. A request hyper hands back unsent goes out again on a fresh connection.
    fn send_blocking(
        &self,
        target: &Target,
        mut request: Request<RequestBody>,
        limits: Limits,
        mut redirect: Option<&mut Redirect>,
        cookies: Option<&Cookies>,
    ) -> Result<Fetched, Failure> {
        for _ in 0..3 {
            let mut conn = self.checkout(target, limits)?;
            let outcome = run_on(&mut conn, request, limits, redirect.as_deref_mut(), cookies);
            if conn.leave() {
                // `close()` shut its socket down: whatever came back doesn't count, not even a body
                // that looks whole (one that ends with the connection reads as complete).
                return Err(Failure::Closed);
            }
            match outcome {
                Ok(fetched) => {
                    if !conn.closed {
                        self.shared.put_idle(&target.key, conn);
                    }
                    return Ok(fetched);
                }
                Err(Failure::Unsent(unsent)) => request = *unsent,
                Err(other) => {
                    // A redirect that can't be followed only dropped its response.
                    if other.keeps_connection() && !conn.closed {
                        self.shared.put_idle(&target.key, conn);
                    }
                    return Err(other);
                }
            }
        }
        Err(Failure::Transport("connection closed before the request could be sent".to_owned()))
    }

    /// The hop a redirect leads to: its request, where it goes, and where its response's cookies
    /// go (`Err` the exception it fails with before any I/O).
    fn hop(&self, py: Python<'_>, redirect: &Redirect, hop: crate::redirect::Hop) -> PyResult<(Target, Request<RequestBody>, Option<Cookies>)> {
        let Prepared { request, url, route, cookies, .. } = self.config.hop(py, &self.glue, redirect, hop)?;
        match Self::target(&url, route) {
            Ok(target) => Ok((target, request, cookies)),
            Err(message) => Err(self.glue.connection_error(py, message)),
        }
    }

    /// `send_blocking`, following redirects (`redirect`, if the client does) under the same
    /// `limits`: the final response, or how it failed. `Err` for a hop that can't be made.
    fn send_following(
        &self,
        py: Python<'_>,
        mut target: Target,
        mut request: Request<RequestBody>,
        limits: Limits,
        mut redirect: Option<Box<Redirect>>,
        mut cookies: Option<Cookies>,
    ) -> PyResult<Result<Head, Failure>> {
        loop {
            let state = redirect.as_deref_mut();
            let fetched = py.detach(|| self.send_blocking(&target, request, limits, state, cookies.as_ref()));
            match (fetched, redirect.as_deref()) {
                (Ok(Fetched::Hop(hop)), Some(state)) => (target, request, cookies) = self.hop(py, state, hop)?,
                (Ok(Fetched::Hop(_)), None) => return Ok(Err(Failure::Transport("internal error: a redirect lost its state".to_owned()))),
                (Ok(Fetched::Head(head)), _) => return Ok(Ok(head)),
                (Err(failure), _) => return Ok(Err(failure)),
            }
        }
    }

    /// `open_stream`, following redirects as `send_following` does: the final response's head,
    /// with the connection its body will be read from, and where it went.
    fn open_following(
        &self,
        py: Python<'_>,
        mut target: Target,
        mut request: Request<RequestBody>,
        limits: Limits,
        mut redirect: Option<Box<Redirect>>,
        mut cookies: Option<Cookies>,
    ) -> PyResult<(Target, SyncConn, Response<Incoming>)> {
        loop {
            let (conn, response) = py.detach(|| self.open_stream(&target, request, limits)).map_err(|f| self.glue.failure(py, f))?;
            if let Some(cookies) = &cookies {
                cookies.store(response.headers());
            }
            let Some(state) = redirect.as_deref_mut() else { return Ok((target, conn, response)) };
            let next = state.next(response.status().as_u16(), response.headers());
            if matches!(next, Ok(None)) {
                return Ok((target, conn, response));
            }
            // A redirect followed or refused goes unread, and its connection back to the pool.
            drop(response);
            if !conn.closed {
                self.shared.put_idle(&target.key, conn);
            }
            match next {
                Ok(Some(hop)) => (target, request, cookies) = self.hop(py, state, hop)?,
                Ok(None) => unreachable!("handled above"),
                Err(failure) => return Err(self.glue.failure(py, failure)),
            }
        }
    }

    /// `send_blocking` for a streamed request: the connection it runs on (now busy until its body
    /// is read) and the response's head.
    fn open_stream(&self, target: &Target, mut request: Request<RequestBody>, limits: Limits) -> Result<(SyncConn, Response<Incoming>), Failure> {
        for _ in 0..3 {
            let mut conn = self.checkout(target, limits)?;
            let outcome = send_on(&mut conn, request, limits);
            // Left even with the head in: its body outlives the client, as on pyreqwest.
            if conn.leave() {
                return Err(Failure::Closed);
            }
            match outcome {
                Ok(response) => return Ok((conn, response)),
                Err(Failure::Unsent(unsent)) => request = *unsent,
                Err(other) => return Err(other),
            }
        }
        Err(Failure::Transport("connection closed before the request could be sent".to_owned()))
    }

    /// Where a request to `url` connects, and how; `Err` the connection error an https URL whose
    /// host can't be a certificate name raises.
    fn target(url: &Url, route: Route) -> Result<Target, String> {
        let (host, port) = route.connect_address(connect_address(url));
        let key = OriginKey::of(url);
        let server_name = route.tls_name(url).transpose()?;
        let resolve = route.resolve_locally(&key.host);
        Ok(Target { key, host, port, server_name, route, resolve })
    }
}

#[pymethods]
impl SyncPool {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (base_url, default_headers, user_agent, https_only, cookie_store, follow_redirects, max_redirects, root_certificates, identity_pem, min_tls_version, max_tls_version, danger_accept_invalid_certs, timeout, pool_idle_timeout, pool_max_idle_per_host, max_connections, pool_timeout, proxy, no_proxy, glue, connect))]
    fn new(
        py: Python<'_>,
        base_url: Option<&str>,
        default_headers: Option<&Bound<'_, PyAny>>,
        user_agent: &str,
        https_only: bool,
        cookie_store: bool,
        follow_redirects: bool,
        max_redirects: Option<usize>,
        root_certificates: Vec<Vec<u8>>,
        identity_pem: Option<Vec<u8>>,
        min_tls_version: Option<&str>,
        max_tls_version: Option<&str>,
        danger_accept_invalid_certs: bool,
        timeout: Option<Duration>,
        pool_idle_timeout: Option<Duration>,
        pool_max_idle_per_host: Option<usize>,
        max_connections: Option<usize>,
        pool_timeout: Option<Duration>,
        proxy: Option<&str>,
        no_proxy: Option<&str>,
        glue: &Bound<'_, PyAny>,
        connect: &ConnectOptions,
    ) -> PyResult<Self> {
        // The client's `timeout` is only converted, so that a bad one fails on entry with pyo3's
        // own message, as on pyreqwest's builder: each request carries its own timeout.
        let _ = timeout;
        let max_connections = checked_max_connections(max_connections)?;
        Ok(Self {
            config: ClientConfig::new(base_url, default_headers, user_agent, https_only)?
                .with_proxies(Proxies::new(proxy, no_proxy)?)
                .with_redirects(follow_redirects, max_redirects)
                .with_cookies(cookie_store),
            connect: connect.settings.clone(),
            tls: TlsSettings::new(
                &root_certificates,
                identity_pem.as_deref(),
                min_tls_version,
                max_tls_version,
                danger_accept_invalid_certs,
            )?,
            tls_config: Mutex::new(None),
            tunnel_tls_config: Mutex::new(None),
            shared: Arc::new(Shared {
                idle_policy: IdlePolicy::new(pool_idle_timeout, pool_max_idle_per_host),
                origins: Mutex::default(),
                closed: AtomicBool::new(false),
            }),
            limiter: SyncLimiter::new(max_connections, pool_timeout),
            in_flight: Arc::default(),
            glue: Glue::new(py, glue)?,
            clock: DeadlineClock::new(),
        })
    }

    /// One blocking request: same arguments as `Pool.request`, returns lothc's `Response`.
    #[pyo3(signature = (method, path, query, headers, auth, body, body_kind, decode, timeout, raises, typed_headers, headers_only))]
    #[allow(clippy::too_many_arguments)]
    fn request<'py>(
        &self,
        py: Python<'py>,
        method: Bound<'py, PyString>,
        path: &str,
        query: Option<Bound<'py, PyAny>>,
        headers: Option<Bound<'py, PyAny>>,
        auth: Option<&str>,
        body: Option<Bound<'py, PyAny>>,
        body_kind: u8,
        decode: Option<Py<PyAny>>,
        timeout: Option<Duration>,
        raises: Option<Py<PyAny>>,
        typed_headers: Option<Py<PyAny>>,
        headers_only: bool,
    ) -> PyResult<Bound<'py, PyAny>> {
        if self.shared.closed.load(Ordering::Relaxed) {
            return Err(Glue::closed_error());
        }
        let Prepared { request, url, route, redirect, cookies } = self.config.prepare(
            py,
            &self.glue,
            method.to_str()?,
            path,
            query.as_ref(),
            headers.as_ref(),
            auth,
            body.as_ref(),
            body_kind,
        )?;
        let target = match Self::target(&url, route) {
            Ok(target) => target,
            Err(message) => return Err(self.glue.connection_error(py, message)),
        };
        let started = Instant::now();
        let limits = Limits::new(self.clock, started, timeout, self.connect.read_timeout);
        let result = match &self.limiter {
            None => self.send_following(py, target, request, limits, redirect, cookies)?,
            Some(limiter) => match py.detach(|| limiter.acquire(started, self.clock, limits.total)) {
                // Given back once the body has been read, before it is decoded.
                Ok(_permit) => self.send_following(py, target, request, limits.reading_from(Instant::now()), redirect, cookies)?,
                Err(failure) => Err(failure),
            },
        };
        let exchange = Exchange {
            method: method.unbind(),
            url,
            decode,
            raises,
            typed_headers,
            headers_only,
            started,
        };
        self.glue.outcome(py, &exchange, result)
    }

    /// Prepare a streamed request (nothing is sent until its `open`): `Pool.stream`'s arguments.
    #[pyo3(signature = (method, path, query, headers, auth, body, body_kind, timeout, accept, last_event_id))]
    #[allow(clippy::too_many_arguments)]
    fn stream(
        slf: &Bound<'_, Self>,
        py: Python<'_>,
        method: Bound<'_, PyString>,
        path: &str,
        query: Option<Bound<'_, PyAny>>,
        headers: Option<Bound<'_, PyAny>>,
        auth: Option<&str>,
        body: Option<Bound<'_, PyAny>>,
        body_kind: u8,
        timeout: Option<Duration>,
        accept: Option<&str>,
        last_event_id: Option<&str>,
    ) -> PyResult<SyncStream> {
        let pool = slf.get();
        if pool.shared.closed.load(Ordering::Relaxed) {
            return Err(Glue::closed_error());
        }
        let verb_headers = VerbHeaders::sse(accept, last_event_id)?;
        let Prepared { request, url, route, redirect, cookies } = pool.config.prepare_with(
            py,
            &pool.glue,
            method.to_str()?,
            path,
            query.as_ref(),
            headers.as_ref(),
            auth,
            body.as_ref(),
            body_kind,
            verb_headers,
        )?;
        let request_info = pool.glue.request_info_of(py, &method, &url)?.unbind();
        let prepared = Box::new(PreparedStream { target: Self::target(&url, route), request, redirect, cookies });
        Ok(SyncStream { pool: slf.clone().unbind(), request_info, timeout, state: Mutex::new(StreamState::Prepared(prepared)) })
    }

    /// Fail every request still running or waiting (see the module's doc) and drop every idle
    /// connection.
    fn close(&self) {
        self.shared.closed.store(true, Ordering::Relaxed);
        if let Some(limiter) = &self.limiter {
            limiter.close();
        }
        {
            let mut in_flight = lock(&self.in_flight);
            in_flight.closed = true;
            // Under the lock, so no descriptor here can close (and its number be reused) first.
            for (_, fd) in &in_flight.fds {
                shutdown(*fd);
            }
        }
        lock(&self.shared.origins).list.clear();
    }

    fn __repr__(&self) -> &'static str {
        "<lothc._native.SyncPool>"
    }
}

/// A prepared stream's request and where it goes (`Err` the connection error it fails with).
struct PreparedStream {
    target: Result<Target, String>,
    request: Request<RequestBody>,
    redirect: Option<Box<Redirect>>,
    cookies: Option<Cookies>,
}

/// Where a `SyncStream` is.
enum StreamState {
    /// Prepared, not sent yet.
    Prepared(Box<PreparedStream>),
    Open(Box<OpenStream>),
    /// A call has it: each call takes the state out while it waits, so none holds the lock.
    Busy,
    /// Read to its end, failed, or closed.
    Done,
}

/// A stream whose head has arrived, holding its connection while its body is read.
struct OpenStream {
    key: OriginKey,
    conn: SyncConn,
    body: BodyStream,
    limits: Limits,
    /// Its `max_connections` slot, given back once the body is read to its end, fails or the
    /// stream is closed.
    _permit: Option<SyncPermit>,
}

impl OpenStream {
    /// The body's next chunk, under its total deadline and, from now, `read_timeout`'s gap.
    fn next_chunk(&mut self) -> Result<Option<Bytes>, Failure> {
        let Self { conn, body, limits, .. } = self;
        let SyncConn { fd, ready, driver, closed, .. } = conn;
        let head = Cell::new(limits.read_timeout.map(|_| Instant::now()));
        let mut waits = Waits { limits: *limits, head: &head, last_read: None, connect_by: None };
        let chunk = drive(*fd, ready, driver, closed, std::pin::pin!(poll_fn(|cx| body.poll_chunk(cx))), &mut waits);
        chunk?
    }
}

fn stream_misused(what: &str) -> PyErr {
    PyRuntimeError::new_err(format!("internal error: {what}"))
}

/// One streamed request on a `SyncPool`, from `SyncPool.stream`: `open` it, `read` its body chunk
/// by chunk, `close` it (dropping it closes it too). Each call blocks with the GIL released,
/// waiting in poll(2) under the request's total timeout; a Ctrl-C can't interrupt the wait (a
/// signal only retries it), which is what lothc's worker thread is for. Any thread may make the
/// calls, one at a time.
#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct SyncStream {
    pool: Py<SyncPool>,
    request_info: Py<PyAny>,
    timeout: Option<Duration>,
    state: Mutex<StreamState>,
}

impl SyncStream {
    fn take(&self) -> StreamState {
        std::mem::replace(&mut *lock(&self.state), StreamState::Busy)
    }

    /// Put back what a call took, unless the stream was closed meanwhile (it is dropped then).
    fn put(&self, state: StreamState) {
        let leftover = {
            let mut current = lock(&self.state);
            if matches!(*current, StreamState::Busy) {
                std::mem::replace(&mut *current, state)
            } else {
                state
            }
        };
        drop(leftover);
    }
}

#[pymethods]
impl SyncStream {
    #[getter]
    fn request_info(&self, py: Python<'_>) -> Py<PyAny> {
        self.request_info.clone_ref(py)
    }

    /// Send the request and wait for its head: `(status, RawHeaders, http_version)`.
    fn open<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let PreparedStream { target, request, redirect, cookies } = match self.take() {
            StreamState::Prepared(prepared) => *prepared,
            other => {
                self.put(other);
                return Err(stream_misused("a stream was opened twice"));
            }
        };
        let pool = self.pool.get();
        let target = match target {
            Ok(target) => target,
            Err(message) => {
                self.put(StreamState::Done);
                return Err(pool.glue.connection_error(py, message));
            }
        };
        let started = Instant::now();
        let mut limits = Limits::new(pool.clock, started, self.timeout, pool.connect.read_timeout);
        let permit = match &pool.limiter {
            None => None,
            Some(limiter) => match py.detach(|| limiter.acquire(started, pool.clock, limits.total)) {
                Ok(permit) => {
                    limits = limits.reading_from(Instant::now());
                    Some(permit)
                }
                Err(failure) => {
                    self.put(StreamState::Done);
                    return Err(pool.glue.failure(py, failure));
                }
            },
        };
        let (target, conn, response) = match pool.open_following(py, target, request, limits, redirect, cookies) {
            Ok(opened) => opened,
            Err(error) => {
                self.put(StreamState::Done);
                return Err(error);
            }
        };
        let (mut parts, incoming) = response.into_parts();
        let body = BodyStream::new(&mut parts.headers, incoming);
        match pool.glue.stream_head(py, parts.status.as_u16(), parts.version, parts.headers) {
            Ok(head) => {
                self.put(StreamState::Open(Box::new(OpenStream { key: target.key, conn, body, limits, _permit: permit })));
                Ok(head)
            }
            Err(error) => {
                self.put(StreamState::Done);
                Err(error)
            }
        }
    }

    /// The body's next chunk, `None` at its end (and after, or once closed).
    fn read<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let mut open = match self.take() {
            StreamState::Open(open) => open,
            StreamState::Done => {
                self.put(StreamState::Done);
                return Ok(None);
            }
            other => {
                self.put(other);
                return Err(stream_misused("a stream was read before it was opened"));
            }
        };
        let pool = self.pool.get();
        match py.detach(|| open.next_chunk()) {
            Ok(Some(chunk)) => {
                self.put(StreamState::Open(open));
                Ok(Some(PyBytes::new(py, &chunk)))
            }
            Ok(None) => {
                let OpenStream { key, conn, .. } = *open;
                if !conn.closed {
                    pool.shared.put_idle(&key, conn);
                }
                self.put(StreamState::Done);
                Ok(None)
            }
            Err(failure) => {
                drop(open);
                self.put(StreamState::Done);
                Err(pool.glue.failure(py, failure))
            }
        }
    }

    /// Done with the stream: one still being read is cut off, its connection closed.
    fn close(&self) {
        let state = std::mem::replace(&mut *lock(&self.state), StreamState::Done);
        drop(state);
    }

    fn __repr__(&self) -> &'static str {
        "<lothc._native.SyncStream>"
    }
}
