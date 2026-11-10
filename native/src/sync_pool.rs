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
//! A connection through a proxy does the proxy's handshake (`proxy.rs`) on its socket first, on
//! the same thread and under the same deadline.
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
//! as pyreqwest's does: it shuts down the sockets in `InFlight`, which wakes their threads' poll(2).
//! A stream's body, once its head has arrived, outlives the client, as on pyreqwest.
use std::future::{poll_fn, Future};
use std::io;
use std::net::{IpAddr, SocketAddr, TcpStream, ToSocketAddrs};
use std::os::fd::{AsRawFd, RawFd};
use std::pin::Pin;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll, Wake, Waker};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http::{Request, Response};
use http_body_util::Full;
use hyper::body::Incoming;
use hyper::client::conn::http1;
use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString, PyTuple};
use rustls::pki_types::ServerName;
use rustls::ClientConnection;
use url::Url;

use crate::error::{error_chain, Failure};
use crate::idle::IdlePolicy;
use crate::io::{shutdown, PollReady, Stream};
use crate::proxy::{Handshake, Proxies, Route};
use crate::redirect::Redirect;
use crate::request::{connect_address, ClientConfig, DeadlineClock, OriginKey, Prepared, VerbHeaders};
use crate::response::{fetch, send, Exchange, Fetched, Glue, Head, Sender};
use crate::stream::BodyStream;
use crate::tls::{TlsSettings, TlsSocket};

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
}

impl SyncConn {
    /// Done with the request it was busy with: whether the pool closed first.
    fn leave(&mut self) -> bool {
        self.busy.take().is_some_and(|mut busy| busy.leave())
    }
}

/// Where one request goes: its origin, where its connection connects (the origin, or a proxy),
/// and the name an https one is verified against.
struct Target {
    key: OriginKey,
    host: String,
    port: u16,
    server_name: Option<ServerName<'static>>,
    route: Route,
    /// The target host a `socks5://` proxy wants resolved here first.
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

/// Wait for `fd` to be readable (or writable, if `writable`) until `deadline`; `Ok(false)` if it
/// passed first.
fn wait(fd: RawFd, ready: &PollReady, deadline: Option<Instant>) -> Result<bool, Failure> {
    loop {
        let Some(timeout) = poll_timeout(deadline) else { return Ok(false) };
        let events = if ready.wants_writable() { libc::POLLIN | libc::POLLOUT } else { libc::POLLIN };
        let mut pfd = libc::pollfd { fd, events, revents: 0 };
        // SAFETY: `pfd` is one valid, writable `pollfd` for the whole call, matching `nfds` = 1.
        match unsafe { libc::poll(&mut pfd, 1, timeout) } {
            0 => return Ok(false),
            n if n < 0 => {
                let error = io::Error::last_os_error();
                if error.kind() != io::ErrorKind::Interrupted {
                    return Err(Failure::Transport(error_chain(&error)));
                }
            }
            _ => {
                let failed = pfd.revents & (libc::POLLHUP | libc::POLLERR | libc::POLLNVAL) != 0;
                if failed || pfd.revents & libc::POLLIN != 0 {
                    ready.set_readable();
                }
                if failed || pfd.revents & libc::POLLOUT != 0 {
                    ready.set_writable();
                }
                return Ok(true);
            }
        }
    }
}

/// A connection's hyper driver, `()` once the connection has closed.
type Driver = Pin<Box<dyn Future<Output = ()> + Send>>;

/// Drive a connection's hyper driver and `fut` (which runs on that connection) until `fut`
/// finishes, waiting for socket `fd` in poll(2) until `deadline`.
fn drive<F: Future>(
    fd: RawFd,
    ready: &PollReady,
    driver: &mut Driver,
    closed: &mut bool,
    mut fut: Pin<&mut F>,
    deadline: Option<Instant>,
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
        if !wait(fd, ready, deadline)? {
            return Err(Failure::Timeout);
        }
    }
}

/// Drive `conn`'s hyper driver and one request until the request finishes (or gets a redirect
/// to follow, see `fetch`).
fn run_on(conn: &mut SyncConn, request: Request<Full<Bytes>>, deadline: Option<Instant>, redirect: Option<&mut Redirect>) -> Result<Fetched, Failure> {
    let SyncConn { fd, ready, sender, driver, closed, .. } = conn;
    drive(*fd, ready, driver, closed, std::pin::pin!(fetch(sender, request, redirect)), deadline)?
}

/// Drive `conn`'s hyper driver and one request until its response's head arrives.
fn send_on(conn: &mut SyncConn, request: Request<Full<Bytes>>, deadline: Option<Instant>) -> Result<Response<Incoming>, Failure> {
    let SyncConn { fd, ready, sender, driver, closed, .. } = conn;
    drive(*fd, ready, driver, closed, std::pin::pin!(send(sender, request)), deadline)?
}

/// Drive a new connection's proxy handshake on the calling thread until it finishes, or `deadline`
/// passes first.
fn drive_handshake(handshake: Handshake, fd: RawFd, ready: &PollReady, deadline: Option<Instant>) -> Result<(), Failure> {
    let flag = Arc::new(FlagWaker(AtomicBool::new(false)));
    let waker = Waker::from(flag.clone());
    let mut cx = Context::from_waker(&waker);
    let mut fut = std::pin::pin!(handshake.run(fd, ready.clone()));
    loop {
        flag.0.store(false, Ordering::Relaxed);
        if let Poll::Ready(result) = fut.as_mut().poll(&mut cx) {
            return result.map_err(Failure::Transport);
        }
        if flag.0.load(Ordering::Relaxed) {
            continue;
        }
        if !wait(fd, ready, deadline)? {
            return Err(Failure::Timeout);
        }
    }
}

/// reqwest's local resolution for a `socks5://` proxy: the first address, if there is one.
fn resolve_for_socks(host: &str) -> Result<Option<IpAddr>, Failure> {
    let mut addresses = (host, 0).to_socket_addrs().map_err(|e| Failure::Transport(format!("error resolving for socks proxy: {e}")))?;
    Ok(addresses.next().map(|address| address.ip()))
}

/// Whether a pooled connection is still usable: nothing reads an idle socket here (no event
/// loop), so a server's silent close is only noticed by looking, as urllib3 does before reuse.
fn still_open(conn: &mut SyncConn) -> bool {
    let mut pfd = libc::pollfd { fd: conn.fd, events: libc::POLLIN, revents: 0 };
    // SAFETY: `pfd` is one valid, writable `pollfd` for the whole call, matching `nfds` = 1.
    if unsafe { libc::poll(&mut pfd, 1, 0) } > 0 {
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

fn connect_error(error: &io::Error) -> Failure {
    if error.kind() == io::ErrorKind::TimedOut {
        Failure::Timeout
    } else {
        Failure::Transport(error_chain(error))
    }
}

fn open_socket(host: &str, port: u16, deadline: Option<Instant>) -> Result<TcpStream, Failure> {
    let addresses: Vec<SocketAddr> = (host, port).to_socket_addrs().map_err(|e| connect_error(&e))?.collect();
    let mut last_error = None;
    for address in addresses {
        let attempt = match deadline {
            Some(deadline) => {
                let left = deadline.saturating_duration_since(Instant::now());
                if left.is_zero() {
                    return Err(Failure::Timeout);
                }
                TcpStream::connect_timeout(&address, left.max(Duration::from_millis(1)))
            }
            None => TcpStream::connect(address),
        };
        match attempt {
            Ok(stream) => return Ok(stream),
            Err(error) => last_error = Some(error),
        }
    }
    Err(match last_error {
        Some(error) => connect_error(&error),
        None => Failure::Transport(format!("no addresses found for {host}")),
    })
}

/// One origin's idle connections, (connection, when it went idle), oldest first; checkout takes
/// from the end.
type IdleConns = Vec<(SyncConn, Instant)>;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct SyncPool {
    config: ClientConfig,
    tls: TlsSettings,
    tls_config: Mutex<Option<Arc<rustls::ClientConfig>>>,
    idle_policy: IdlePolicy,
    idle: Mutex<Vec<(OriginKey, IdleConns)>>,
    in_flight: Arc<Mutex<InFlight>>,
    closed: AtomicBool,
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

    fn connect(&self, target: &Target, deadline: Option<Instant>) -> Result<SyncConn, Failure> {
        let transport = |e: io::Error| Failure::Transport(error_chain(&e));
        // reqwest resolves a `socks5://` proxy's target before connecting to the proxy.
        let resolved = match &target.resolve {
            Some(host) => resolve_for_socks(host)?,
            None => None,
        };
        let stream = open_socket(&target.host, target.port, deadline)?;
        // Before anything waits on it, so `close()` can cut a proxy's or TLS's handshake short too.
        let busy = self.enter(stream.as_raw_fd())?;
        stream.set_nodelay(true).map_err(transport)?;
        stream.set_nonblocking(true).map_err(transport)?;
        let fd = stream.as_raw_fd();
        let ready = PollReady::new();
        if let Some(handshake) = target.route.handshake(&target.key.host, target.key.port, self.config.user_agent(), resolved).map_err(Failure::Transport)? {
            drive_handshake(handshake, fd, &ready, deadline)?;
        }
        let io = match &target.server_name {
            None => Stream::plain(fd, ready.clone()),
            Some(name) => {
                let tls = ClientConnection::new(self.tls_config()?, name.clone()).map_err(|e| Failure::Transport(error_chain(&e)))?;
                Stream::Tls(Box::new(TlsSocket::new(fd, ready.clone(), tls)))
            }
        };
        let (sender, driver) = futures_executor::block_on(http1::Builder::new().title_case_headers(true).handshake(io))
            .map_err(|e| Failure::Transport(error_chain(&e)))?;
        let driver: Driver = Box::pin(async move {
            let _ = driver.await;
        });
        Ok(SyncConn { busy: Some(busy), _stream: stream, fd, ready, sender, driver, closed: false })
    }

    /// A connection for a request to `target`, in `InFlight`: an idle one, or a new one. A failure
    /// after `close()` began is that close's doing.
    fn checkout(&self, target: &Target, deadline: Option<Instant>) -> Result<SyncConn, Failure> {
        let conn = match self.take_idle(&target.key) {
            Some(mut conn) => self.enter(conn.fd).map(|busy| {
                conn.busy = Some(busy);
                conn
            }),
            None => self.connect(target, deadline),
        };
        match conn {
            Err(_) if self.closing() => Err(Failure::Closed),
            conn => conn,
        }
    }

    /// The most recently idled connection to `key` that is still fresh and open, closing the
    /// expired ones it passes (hyper-util's checkout).
    fn take_idle(&self, key: &OriginKey) -> Option<SyncConn> {
        let now = Instant::now();
        loop {
            let (mut conn, idle_at) = {
                let mut idle = lock(&self.idle);
                let (_, conns) = idle.iter_mut().find(|(k, _)| k == key)?;
                conns.pop()?
            };
            if self.idle_policy.expired(idle_at, now) {
                close_idle(conn);
            } else if still_open(&mut conn) {
                return Some(conn);
            }
        }
    }

    /// Pool a finished connection, unless its origin already holds as many idle ones as allowed
    /// (the ones already idle stay, as in hyper-util); first take every origin's expired ones out.
    /// Each origin's list is oldest first, so its expired ones are a prefix.
    fn put_idle(&self, key: &OriginKey, conn: SyncConn) {
        if self.closed.load(Ordering::Relaxed) {
            return;
        }
        let mut expired = Vec::new();
        let rejected = {
            let mut idle = lock(&self.idle);
            let now = Instant::now();
            for (_, conns) in idle.iter_mut() {
                let stale = conns.iter().take_while(|(_, idle_at)| self.idle_policy.expired(*idle_at, now)).count();
                expired.extend(conns.drain(..stale).map(|(conn, _)| conn));
            }
            idle.retain(|(k, conns)| !conns.is_empty() || k == key);
            match idle.iter_mut().find(|(k, _)| k == key) {
                Some((_, conns)) if !self.idle_policy.has_room(conns.len()) => Some(conn),
                Some((_, conns)) => {
                    conns.push((conn, now));
                    None
                }
                None if !self.idle_policy.has_room(0) => Some(conn),
                None => {
                    idle.push((key.clone(), vec![(conn, now)]));
                    None
                }
            }
        };
        // Closed outside the lock: shutting a connection down may write to its socket.
        expired.into_iter().chain(rejected).for_each(close_idle);
    }

    /// Take an idle connection (or open one), run the request, put the connection back if it
    /// is still good. A request hyper hands back unsent goes out again on a fresh connection.
    fn send_blocking(
        &self,
        target: &Target,
        mut request: Request<Full<Bytes>>,
        deadline: Option<Instant>,
        mut redirect: Option<&mut Redirect>,
    ) -> Result<Fetched, Failure> {
        for _ in 0..3 {
            let mut conn = self.checkout(target, deadline)?;
            let outcome = run_on(&mut conn, request, deadline, redirect.as_deref_mut());
            if conn.leave() {
                // `close()` shut its socket down: whatever came back doesn't count, not even a body
                // that looks whole (one that ends with the connection reads as complete).
                return Err(Failure::Closed);
            }
            match outcome {
                Ok(fetched) => {
                    if !conn.closed {
                        self.put_idle(&target.key, conn);
                    }
                    return Ok(fetched);
                }
                Err(Failure::Unsent(unsent)) => request = *unsent,
                Err(other) => {
                    // A redirect that can't be followed only dropped its response.
                    if other.keeps_connection() && !conn.closed {
                        self.put_idle(&target.key, conn);
                    }
                    return Err(other);
                }
            }
        }
        Err(Failure::Transport("connection closed before the request could be sent".to_owned()))
    }

    /// The hop a redirect leads to: its request and where it goes (`Err` the exception it fails
    /// with before any I/O).
    fn hop(&self, py: Python<'_>, redirect: &Redirect, hop: crate::redirect::Hop) -> PyResult<(Target, Request<Full<Bytes>>)> {
        let Prepared { request, url, route, .. } = self.config.hop(py, &self.glue, redirect, hop)?;
        match Self::target(&url, route) {
            Ok(target) => Ok((target, request)),
            Err(message) => Err(self.glue.connection_error(py, message)),
        }
    }

    /// `send_blocking`, following redirects (`redirect`, if the client does) under the same
    /// `deadline`: the final response, or how it failed. `Err` for a hop that can't be made.
    fn send_following(
        &self,
        py: Python<'_>,
        mut target: Target,
        mut request: Request<Full<Bytes>>,
        deadline: Option<Instant>,
        mut redirect: Option<Box<Redirect>>,
    ) -> PyResult<Result<Head, Failure>> {
        loop {
            let state = redirect.as_deref_mut();
            let fetched = py.detach(|| self.send_blocking(&target, request, deadline, state));
            match (fetched, redirect.as_deref()) {
                (Ok(Fetched::Hop(hop)), Some(state)) => (target, request) = self.hop(py, state, hop)?,
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
        mut request: Request<Full<Bytes>>,
        deadline: Option<Instant>,
        mut redirect: Option<Box<Redirect>>,
    ) -> PyResult<(Target, SyncConn, Response<Incoming>)> {
        loop {
            let (conn, response) = py.detach(|| self.open_stream(&target, request, deadline)).map_err(|f| self.glue.failure(py, f))?;
            let Some(state) = redirect.as_deref_mut() else { return Ok((target, conn, response)) };
            let next = state.next(response.status().as_u16(), response.headers());
            if matches!(next, Ok(None)) {
                return Ok((target, conn, response));
            }
            // A redirect followed or refused goes unread, and its connection back to the pool.
            drop(response);
            if !conn.closed {
                self.put_idle(&target.key, conn);
            }
            match next {
                Ok(Some(hop)) => (target, request) = self.hop(py, state, hop)?,
                Ok(None) => unreachable!("handled above"),
                Err(failure) => return Err(self.glue.failure(py, failure)),
            }
        }
    }

    /// `send_blocking` for a streamed request: the connection it runs on (now busy until its body
    /// is read) and the response's head.
    fn open_stream(&self, target: &Target, mut request: Request<Full<Bytes>>, deadline: Option<Instant>) -> Result<(SyncConn, Response<Incoming>), Failure> {
        for _ in 0..3 {
            let mut conn = self.checkout(target, deadline)?;
            let outcome = send_on(&mut conn, request, deadline);
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
        let server_name = key.https.then(|| route.server_name(url)).transpose()?;
        let resolve = route.resolve_locally(&key.host);
        Ok(Target { key, host, port, server_name, route, resolve })
    }
}

#[pymethods]
impl SyncPool {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (base_url, default_headers, user_agent, https_only, follow_redirects, max_redirects, root_certificates, identity_pem, min_tls_version, max_tls_version, danger_accept_invalid_certs, timeout, pool_idle_timeout, pool_max_idle_per_host, proxy, no_proxy, glue))]
    fn new(
        py: Python<'_>,
        base_url: Option<&str>,
        default_headers: Option<&Bound<'_, PyAny>>,
        user_agent: &str,
        https_only: bool,
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
        proxy: Option<&str>,
        no_proxy: Option<&str>,
        glue: &Bound<'_, PyAny>,
    ) -> PyResult<Self> {
        // The client's `timeout` is only converted, so that a bad one fails on entry with pyo3's
        // own message, as on pyreqwest's builder: each request carries its own timeout.
        let _ = timeout;
        Ok(Self {
            config: ClientConfig::new(base_url, default_headers, user_agent, https_only)?
                .with_proxies(Proxies::new(proxy, no_proxy)?)
                .with_redirects(follow_redirects, max_redirects),
            tls: TlsSettings::new(
                &root_certificates,
                identity_pem.as_deref(),
                min_tls_version,
                max_tls_version,
                danger_accept_invalid_certs,
            )?,
            tls_config: Mutex::new(None),
            idle_policy: IdlePolicy::new(pool_idle_timeout, pool_max_idle_per_host),
            idle: Mutex::new(Vec::new()),
            in_flight: Arc::default(),
            closed: AtomicBool::new(false),
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
        if self.closed.load(Ordering::Relaxed) {
            return Err(Glue::closed_error());
        }
        let Prepared { request, url, route, redirect } = self.config.prepare(
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
        let deadline = timeout.and_then(|timeout| self.clock.deadline(started, timeout));
        let result = self.send_following(py, target, request, deadline, redirect)?;
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
        if pool.closed.load(Ordering::Relaxed) {
            return Err(Glue::closed_error());
        }
        let verb_headers = VerbHeaders::sse(accept, last_event_id)?;
        let Prepared { request, url, route, redirect } = pool.config.prepare_with(
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
        let prepared = Box::new(PreparedStream { target: Self::target(&url, route), request, redirect });
        Ok(SyncStream { pool: slf.clone().unbind(), request_info, timeout, state: Mutex::new(StreamState::Prepared(prepared)) })
    }

    /// Fail every request still running (see the module's doc) and drop every idle connection.
    fn close(&self) {
        self.closed.store(true, Ordering::Relaxed);
        {
            let mut in_flight = lock(&self.in_flight);
            in_flight.closed = true;
            // Under the lock, so no descriptor here can close (and its number be reused) first.
            for (_, fd) in &in_flight.fds {
                shutdown(*fd);
            }
        }
        lock(&self.idle).clear();
    }

    fn __repr__(&self) -> &'static str {
        "<lothc._native.SyncPool>"
    }
}

/// A prepared stream's request and where it goes (`Err` the connection error it fails with).
struct PreparedStream {
    target: Result<Target, String>,
    request: Request<Full<Bytes>>,
    redirect: Option<Box<Redirect>>,
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
    deadline: Option<Instant>,
}

impl OpenStream {
    fn next_chunk(&mut self) -> Result<Option<Bytes>, Failure> {
        let Self { conn, body, deadline, .. } = self;
        let SyncConn { fd, ready, driver, closed, .. } = conn;
        drive(*fd, ready, driver, closed, std::pin::pin!(poll_fn(|cx| body.poll_chunk(cx))), *deadline)?
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
        let PreparedStream { target, request, redirect } = match self.take() {
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
        let deadline = self.timeout.and_then(|timeout| pool.clock.deadline(Instant::now(), timeout));
        let (target, conn, response) = match pool.open_following(py, target, request, deadline, redirect) {
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
                self.put(StreamState::Open(Box::new(OpenStream { key: target.key, conn, body, deadline })));
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
                    pool.put_idle(&key, conn);
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
