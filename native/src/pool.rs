//! The async pool: hyper's HTTP/1.1 client on the asyncio loop's thread, no tokio, no I/O thread.
//!
//! Python owns the sockets: it connects them (`loop.sock_connect`, asked for through
//! `spawn_connect`) and registers each with `loop.add_reader`, whose callback is `on_readable`.
//! That marks the socket readable and runs a single-thread executor (futures' `LocalPool`) polling
//! hyper under the GIL. A send that would block asks for `loop.add_writer` (one-shot: `on_writable`
//! removes it again). Finished requests are resolved in one pass, building exactly what lothc's
//! verbs return (see `response.rs`), through asyncio futures created on the loop.
//!
//! Timeouts share ONE `loop.call_later` timer, armed for the earliest deadline in a heap (a
//! per-request `call_later` + `cancel()` measured ~1.3us of ~11us). A request that expires in
//! flight has its connection's socket shut down, which fails hyper's I/O; that failure is reported
//! as the timeout. In-flight state lives in a generation-tagged slab (O(1), no hashing), and heap
//! entries for finished requests are dropped lazily.
//!
//! Connections are pooled per origin (scheme, host, port), opened on demand when requests queue
//! up, and replaced when one dies. A request hyper reports as never sent (a stale keep-alive
//! connection) is re-queued, as reqwest's pool does; anything else fails. Idle connections expire
//! as reqwest's do (`idle.rs`); the reaper that closes them with no request arriving rides the
//! same shared timer as the request deadlines (`reap_at`), so pooling a connection arms nothing.
//!
//! A connection through a proxy connects its socket to the proxy, then runs the proxy's handshake
//! (`proxy.rs`) on the executor over the same loop readiness, before TLS and hyper. It counts as
//! connecting until then, so the requests waiting for it (and their deadlines) cover the proxy
//! hop. For a `socks5://` proxy, Python resolves the target first (`loop.getaddrinfo`).
//!
//! A redirect to follow (`redirect.rs`) keeps the request's slot, its Future and its deadline, so
//! one total timeout covers every hop, as reqwest's does: the redirect's connection goes back to
//! the pool (hyper drains the unread response or closes it), then the hop's request is queued on
//! its own origin like any other, so a hop to the same origin finds that connection first.
//!
//! A streamed request (`stream`, a `Stream`) is a request whose slot stays until Python
//! closes it: its head resolves the open Future, then a pump task reads the body a few chunks
//! ahead of the reads (`stream.rs`), each read a Future of its own. Its connection is busy until
//! the body ends, so it is never idle (or reaped) mid-stream, and its deadline, if any, covers the
//! whole body. A body read to its end returns the connection to the pool; closing a stream early
//! shuts its socket down and retires it, so the server sees the hang-up.
//!
//! `close()` fails every request still pending with the `RuntimeError` a request after it raises,
//! as pyreqwest's does, except a stream whose head has arrived: its body outlives the client, as
//! on pyreqwest, under its deadline, and its connection is closed (never pooled) once the body
//! ends or the stream is closed.
use std::cell::Cell;
use std::cmp::Reverse;
use std::collections::{BinaryHeap, VecDeque};
use std::mem::ManuallyDrop;
use std::os::fd::RawFd;
use std::rc::Rc;
use std::sync::Arc;
use std::thread::{self, ThreadId};
use std::time::{Duration, Instant};

use bytes::Bytes;
use futures_executor::{LocalPool, LocalSpawner};
use futures_util::task::LocalSpawnExt;
use http::Request;
use http::{HeaderMap, Version};
use hyper::client::conn::http1;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};
use rustls::pki_types::ServerName;
use rustls::ClientConnection;

use crate::body::RequestBody;
use crate::cookie::Cookies;
use crate::error::{error_chain, Failure};
use crate::idle::IdlePolicy;
use crate::io::{shutdown, LoopReady, Stream};
use crate::proxy::{Proxies, Route};
use crate::redirect::{Hop, Redirect};
use crate::request::{connect_address, ClientConfig, DeadlineClock, OriginKey, VerbHeaders};
use crate::response::{fetch, send, Exchange, Fetched, Glue, Head, Sender};
use crate::stream::{BodyStream, Channel};
use crate::tls::{TlsSettings, TlsSocket};

/// A value usable only on the thread that created it, which is what lets `Pool` hold the
/// executor's `Rc`s and still be an ordinary (`Send + Sync`) pyclass: another thread's access is
/// a `RuntimeError`, not pyo3's panic for an `unsendable` class.
struct ThreadBound<T> {
    owner: ThreadId,
    value: ManuallyDrop<T>,
}

// SAFETY: `value` is reachable only through `get_mut`, which refuses every thread but `owner`, and
// it is dropped only on `owner` (leaked otherwise, see `Drop`), so it is never touched, moved or
// dropped by any other thread. pyo3's borrow checking of the `Pool` pyclass keeps `&mut` unique.
unsafe impl<T> Send for ThreadBound<T> {}
// SAFETY: as for `Send`: no `&T` is ever handed to a thread other than `owner`.
unsafe impl<T> Sync for ThreadBound<T> {}

impl<T> ThreadBound<T> {
    fn new(value: T) -> Self {
        Self { owner: thread::current().id(), value: ManuallyDrop::new(value) }
    }

    fn get_mut(&mut self) -> PyResult<&mut T> {
        if thread::current().id() == self.owner {
            Ok(&mut self.value)
        } else {
            Err(PyRuntimeError::new_err(
                "an async lothc client's native transport can only be used from the thread (and event \
                 loop) it was opened on",
            ))
        }
    }
}

impl<T> Drop for ThreadBound<T> {
    fn drop(&mut self) {
        if thread::current().id() == self.owner {
            // SAFETY: dropped exactly once, here, on the owning thread; `value` is never used again.
            unsafe { ManuallyDrop::drop(&mut self.value) };
        }
        // On any other thread the value is leaked: its `Rc`s must not be released elsewhere.
    }
}

/// One request's state, in the slab while it waits or runs.
struct Meta {
    /// Taken by `start`; put back if hyper hands it back unsent.
    request: Option<Request<RequestBody>>,
    /// What a redirect is followed from (`None` if they aren't): taken by `start` with the
    /// request, and put back with it, or with the hop's request.
    redirect: Option<Box<Redirect>>,
    /// Where the current request's response's cookies go, with `cookie_store=True` (the hop's,
    /// once it has one).
    cookies: Option<Cookies>,
    exchange: Exchange,
    /// What the request resolves: its `Response`, or a stream's head.
    fut: Py<PyAny>,
    origin: usize,
    timed_out: bool,
    /// The caller gave up on it: cancelled, or its stream closed.
    cancelled: bool,
    /// `Some` for a streamed request; boxed, so a buffered one stays small.
    stream: Option<Box<StreamMeta>>,
}

impl Meta {
    /// A stream whose head has arrived, not given up on: as on pyreqwest, its body outlives the
    /// client.
    fn reads_on(&self) -> bool {
        !self.cancelled && self.stream.as_ref().is_some_and(|stream| stream.head)
    }
}

/// A streamed request's state, from `stream` until Python closes it.
struct StreamMeta {
    /// The body's chunks read ahead, shared with its pump task.
    channel: Rc<Channel>,
    /// The read waiting for the next chunk, if any.
    read: Option<Py<PyAny>>,
    /// How the body ended, until a read reports it.
    end: Option<Result<(), Failure>>,
    /// The body has ended (its end reported or not).
    done: bool,
    opened: bool,
    /// Its head has arrived, resolving the open Future.
    head: bool,
    /// The total timeout, from `open`.
    timeout: Option<Duration>,
}

/// What a streamed request's task reports, in order: its head, each chunk read ahead, its end.
enum StreamEvent {
    Head { slot: usize, status: u16, version: Version, headers: HeaderMap },
    /// A redirect to follow before any head: its connection is free again.
    Hop { conn: usize, slot: usize, sender: Sender, redirect: Box<Redirect>, hop: Hop },
    Arrived(usize),
    End { conn: usize, slot: usize, sender: Sender, result: Result<(), Failure> },
}

/// What a read is resolved with.
enum ReadOutcome {
    Chunk(Bytes),
    End,
    Failed(Failure),
}

struct Slot {
    generation: u32,
    meta: Option<Meta>,
}

struct Completion {
    conn: usize,
    slot: usize,
    sender: Sender,
    result: Result<Fetched, Failure>,
    /// The request's redirect state, as `fetch` left it (the hop's, for a `Fetched::Hop`).
    redirect: Option<Box<Redirect>>,
}

#[derive(PartialEq)]
enum State {
    Connecting,
    /// Connected to a proxy, running its handshake.
    Handshaking,
    Ready,
    Dead,
}

struct Conn {
    origin: usize,
    fd: RawFd,
    /// Bumped whenever the slot is retired, so a stale "driver finished" event is ignored.
    generation: u32,
    state: State,
    sender: Option<Sender>,
    ready: Option<LoopReady>,
    /// The slab slot of the request it is running.
    current: Option<usize>,
}

struct Origin {
    key: OriginKey,
    /// Where its connections connect: the origin, or its proxy.
    host: String,
    port: u16,
    /// Direct, or through which proxy; fixed for the client's life, as the matcher is.
    route: Route,
    /// The target host a `socks5://` proxy wants resolved first.
    resolve: Option<String>,
    /// `Some` for https: what the certificate is checked against, or why it can't be.
    server_name: Option<Result<ServerName<'static>, String>>,
    /// (conn, when it went idle), oldest first; checkout takes from the end.
    idle: Vec<(usize, Instant)>,
    /// Slab slots waiting for a connection.
    waiting: VecDeque<usize>,
    connecting: usize,
}

/// The asyncio loop's methods and Python's connection callbacks.
struct LoopGlue {
    create_future: Py<PyAny>,
    call_later: Py<PyAny>,
    add_reader: Py<PyAny>,
    remove_reader: Py<PyAny>,
    add_writer: Py<PyAny>,
    remove_writer: Py<PyAny>,
    spawn_connect: Py<PyAny>,
    close_conn: Py<PyAny>,
    /// The pool's own bound `on_deadline`/`on_writable`/`on_readable`, handed back once by
    /// `set_callbacks`.
    on_deadline: Option<Py<PyAny>>,
    on_writable: Option<Py<PyAny>>,
    on_readable: Option<Py<PyAny>>,
}

struct PoolState {
    exec: LocalPool,
    spawner: LocalSpawner,
    config: ClientConfig,
    tls: TlsSettings,
    tls_config: Option<Arc<rustls::ClientConfig>>,
    idle_policy: IdlePolicy,
    /// When the idle reaper next runs; `None` while it isn't running (no idle connections).
    reap_at: Option<Instant>,
    origins: Vec<Origin>,
    conns: Vec<Conn>,
    slots: Vec<Slot>,
    free: Vec<usize>,
    /// (deadline, slot, generation); stale entries are skipped when they surface.
    deadlines: BinaryHeap<Reverse<(Instant, usize, u32)>>,
    /// The one armed timer: when it fires, and its handle.
    armed: Option<(Instant, Py<PyAny>)>,
    done: Rc<Cell<Vec<Completion>>>,
    /// Connections whose hyper driver finished: (conn, generation).
    closed: Rc<Cell<Vec<(usize, u32)>>>,
    /// Connections whose proxy handshake finished.
    handshaken: Rc<Cell<Vec<Handshaken>>>,
    /// Connections whose send is blocked and want `loop.add_writer`.
    writers: Rc<Cell<Vec<usize>>>,
    /// Paused connections a read is waiting on again, which want `loop.add_reader` back.
    readers: Rc<Cell<Vec<usize>>>,
    /// What streamed requests' tasks reported.
    stream_events: Rc<Cell<Vec<StreamEvent>>>,
    event_loop: LoopGlue,
    glue: Glue,
    clock: DeadlineClock,
    /// Set by `close`: nothing new is sent, and only streams that read on (`Meta::reads_on`)
    /// still have a connection, closed once their body ends.
    is_closed: bool,
}

/// A finished proxy handshake: (conn, generation, `Err` the connection error's message).
type Handshaken = (usize, u32, Result<(), String>);

fn push<T>(cell: &Cell<Vec<T>>, item: T) {
    let mut items = cell.take();
    items.push(item);
    cell.set(items);
}

fn stream_misused(what: &str) -> PyErr {
    PyRuntimeError::new_err(format!("internal error: {what}"))
}

impl PoolState {
    fn alloc(&mut self, meta: Meta) -> (usize, u32) {
        let slot = match self.free.pop() {
            Some(slot) => slot,
            None => {
                self.slots.push(Slot { generation: 0, meta: None });
                self.slots.len() - 1
            }
        };
        let entry = &mut self.slots[slot];
        entry.meta = Some(meta);
        (slot, entry.generation)
    }

    fn release(&mut self, slot: usize) -> Option<Meta> {
        let entry = self.slots.get_mut(slot)?;
        let meta = entry.meta.take()?;
        entry.generation = entry.generation.wrapping_add(1);
        self.free.push(slot);
        Some(meta)
    }

    fn is_live(&self, slot: usize, generation: u32) -> bool {
        self.slots.get(slot).is_some_and(|entry| entry.generation == generation && entry.meta.is_some())
    }

    fn meta(&mut self, slot: usize) -> Option<&mut Meta> {
        self.slots.get_mut(slot)?.meta.as_mut()
    }

    /// `url`'s origin, created with `route` the first time. An origin's route never changes (the
    /// matcher is fixed), so its pool only ever holds connections made the same way.
    fn origin_for(&mut self, url: &url::Url, route: Route) -> usize {
        if let Some(index) = self.origins.iter().position(|origin| origin.key.matches(url)) {
            return index;
        }
        let key = OriginKey::of(url);
        let (host, port) = route.connect_address(connect_address(url));
        let server_name = key.https.then(|| route.server_name(url));
        let resolve = route.resolve_locally(&key.host);
        self.origins.push(Origin {
            key,
            host,
            port,
            route,
            resolve,
            server_name,
            idle: Vec::new(),
            waiting: VecDeque::new(),
            connecting: 0,
        });
        self.origins.len() - 1
    }

    /// Run `slot`'s request on idle connection `conn`.
    fn start(&mut self, conn: usize, slot: usize) -> PyResult<()> {
        let Some(mut sender) = self.conns[conn].sender.take() else {
            return Err(PyRuntimeError::new_err("internal error: an idle connection had no sender"));
        };
        let Some(meta) = self.meta(slot) else {
            return Err(PyRuntimeError::new_err("internal error: a queued request was already started"));
        };
        let Some(request) = meta.request.take() else {
            return Err(PyRuntimeError::new_err("internal error: a queued request was already started"));
        };
        let mut redirect = meta.redirect.take();
        let cookies = meta.cookies.clone();
        let channel = meta.stream.as_ref().map(|stream| stream.channel.clone());
        self.conns[conn].current = Some(slot);
        let done = self.done.clone();
        let Some(channel) = channel else {
            return self
                .spawner
                .spawn_local(async move {
                    let result = fetch(&mut sender, request, redirect.as_deref_mut(), cookies.as_ref()).await;
                    push(&done, Completion { conn, slot, sender, result, redirect });
                })
                .map_err(|e| PyRuntimeError::new_err(e.to_string()));
        };
        let events = self.stream_events.clone();
        self.spawner
            .spawn_local(async move {
                let response = match send(&mut sender, request).await {
                    Ok(response) => response,
                    // Before the head: a failure like any request's (re-queued if unsent).
                    Err(failure) => return push(&done, Completion { conn, slot, sender, result: Err(failure), redirect }),
                };
                if let Some(cookies) = &cookies {
                    cookies.store(response.headers());
                }
                if let Some(mut state) = redirect {
                    match state.next(response.status().as_u16(), response.headers()) {
                        Ok(None) => {}
                        Ok(Some(hop)) => return push(&events, StreamEvent::Hop { conn, slot, sender, redirect: state, hop }),
                        Err(failure) => return push(&done, Completion { conn, slot, sender, result: Err(failure), redirect: None }),
                    }
                }
                let (mut parts, incoming) = response.into_parts();
                let mut body = BodyStream::new(&mut parts.headers, incoming);
                let (status, version, headers) = (parts.status.as_u16(), parts.version, parts.headers);
                push(&events, StreamEvent::Head { slot, status, version, headers });
                let result = channel.pump(&mut body, || push(&events, StreamEvent::Arrived(slot))).await;
                drop(body);
                push(&events, StreamEvent::End { conn, slot, sender, result });
            })
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
    }

    /// Start `slot`'s request on an idle connection to `origin` (`true`), or queue it for one.
    fn place(&mut self, py: Python<'_>, slot: usize, origin: usize, now: Instant) -> PyResult<bool> {
        match self.checkout(py, origin, now)? {
            Some(conn) => {
                self.start(conn, slot)?;
                Ok(true)
            }
            None => {
                self.origins[origin].waiting.push_back(slot);
                self.ensure_connections(py, origin)?;
                Ok(false)
            }
        }
    }

    /// `place` a new request; with a `deadline`, the shared timer covers it.
    fn enqueue(&mut self, py: Python<'_>, slot: usize, generation: u32, origin: usize, now: Instant, deadline: Option<Instant>) -> PyResult<()> {
        if let Some(deadline) = deadline {
            self.deadlines.push(Reverse((deadline, slot, generation)));
            self.arm(py, deadline)?;
        }
        if self.place(py, slot, origin, now)? {
            self.run(py)
        } else {
            Ok(())
        }
    }

    /// Open connections until every request waiting on `origin` has one coming.
    fn ensure_connections(&mut self, py: Python<'_>, origin: usize) -> PyResult<()> {
        while self.origins[origin].waiting.len() > self.origins[origin].connecting {
            let conn = match self.conns.iter().position(|c| c.state == State::Dead) {
                Some(conn) => conn,
                None => {
                    self.conns.push(Conn {
                        origin,
                        fd: -1,
                        generation: 0,
                        state: State::Dead,
                        sender: None,
                        ready: None,
                        current: None,
                    });
                    self.conns.len() - 1
                }
            };
            let c = &mut self.conns[conn];
            c.origin = origin;
            c.state = State::Connecting;
            let o = &mut self.origins[origin];
            o.connecting += 1;
            self.event_loop.spawn_connect.call1(py, (conn, o.host.as_str(), o.port, o.resolve.as_deref()))?;
        }
        Ok(())
    }

    /// Take a connection out of service; Python unregisters and closes its socket.
    fn drop_connection(&mut self, py: Python<'_>, conn: usize) -> PyResult<usize> {
        let c = &mut self.conns[conn];
        let origin = c.origin;
        c.state = State::Dead;
        c.sender = None;
        c.ready = None;
        c.current = None;
        c.fd = -1;
        c.generation = c.generation.wrapping_add(1);
        self.origins[origin].idle.retain(|(i, _)| *i != conn);
        self.event_loop.close_conn.call1(py, (conn,))?;
        Ok(origin)
    }

    /// `drop_connection`, then replace it if requests are waiting for one.
    fn retire(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        let origin = self.drop_connection(py, conn)?;
        self.ensure_connections(py, origin)
    }

    /// Close a healthy connection on purpose (expired, or one more than the pool keeps). Its
    /// sender goes first and hyper's driver runs to its end while the socket is still open, so it
    /// shuts the connection down (TLS's close_notify included) as reqwest's would; only then is
    /// the descriptor closed, so the driver can't touch a number a new socket has reused.
    fn expire(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        self.conns[conn].sender = None;
        self.exec.run_until_stalled();
        self.retire(py, conn)
    }

    /// Pool a free connection, or close it if its origin already holds as many idle ones as
    /// allowed (the ones already idle stay, as in hyper-util). Pooling starts the reaper if it
    /// isn't running.
    fn park(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        let origin = self.conns[conn].origin;
        if !self.idle_policy.has_room(self.origins[origin].idle.len()) {
            return self.expire(py, conn);
        }
        let now = Instant::now();
        self.origins[origin].idle.push((conn, now));
        if self.reap_at.is_none() {
            if let Some(at) = self.idle_policy.next_reap(now) {
                self.reap_at = Some(at);
                self.arm(py, at)?;
            }
        }
        Ok(())
    }

    /// A free connection: give it the next request waiting on its origin, else pool it.
    fn dispatch(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        let origin = self.conns[conn].origin;
        match self.origins[origin].waiting.pop_front() {
            Some(next) => self.start(conn, next),
            None => self.park(py, conn),
        }
    }

    /// The most recently idled connection to `origin` still fresh at `now`, closing the expired
    /// ones it passes (hyper-util's checkout).
    fn checkout(&mut self, py: Python<'_>, origin: usize, now: Instant) -> PyResult<Option<usize>> {
        while let Some((conn, idle_at)) = self.origins[origin].idle.pop() {
            if !self.idle_policy.expired(idle_at, now) {
                return Ok(Some(conn));
            }
            self.expire(py, conn)?;
        }
        Ok(None)
    }

    /// The reaper's turn: close every expired idle connection, then run again an interval from
    /// now while any are left idle.
    fn reap(&mut self, py: Python<'_>, now: Instant) -> PyResult<()> {
        let policy = &self.idle_policy;
        let expired: Vec<usize> = self
            .origins
            .iter()
            .flat_map(|o| o.idle.iter().filter(|(_, idle_at)| policy.expired(*idle_at, now)).map(|(conn, _)| *conn))
            .collect();
        for conn in expired {
            self.expire(py, conn)?;
        }
        let any_idle = self.origins.iter().any(|o| !o.idle.is_empty());
        self.reap_at = if any_idle { self.idle_policy.next_reap(now) } else { None };
        Ok(())
    }

    /// Make sure the one timer fires no later than `deadline`.
    fn arm(&mut self, py: Python<'_>, deadline: Instant) -> PyResult<()> {
        if let Some((at, handle)) = &self.armed {
            if *at <= deadline {
                return Ok(());
            }
            handle.bind(py).call_method0(intern!(py, "cancel"))?;
        }
        let Some(callback) = &self.event_loop.on_deadline else { return Ok(()) };
        let delay = deadline.saturating_duration_since(Instant::now()).as_secs_f64();
        let handle = self.event_loop.call_later.call1(py, (delay, callback.clone_ref(py)))?;
        self.armed = Some((deadline, handle));
        Ok(())
    }

    /// Drop heap entries for requests that already finished (keeps the heap near live size).
    fn prune_deadlines(&mut self) {
        while let Some(Reverse((_, slot, generation))) = self.deadlines.peek() {
            if self.is_live(*slot, *generation) {
                break;
            }
            self.deadlines.pop();
        }
        let live = self.slots.len() - self.free.len();
        if self.deadlines.len() > 4 * live + 64 {
            let slots = &self.slots;
            self.deadlines.retain(|Reverse((_, s, g))| slots[*s].generation == *g && slots[*s].meta.is_some());
        }
    }

    fn deliver(&self, py: Python<'_>, meta: &Meta, result: Result<Head, Failure>) -> PyResult<()> {
        let outcome = if meta.cancelled {
            Ok(py.None().into_bound(py))
        } else {
            self.glue.outcome(py, &meta.exchange, result)
        };
        Glue::settle(py, &meta.fut, outcome)
    }

    /// Fail the oldest request waiting on `origin`, if a connection it was counting on failed
    /// (reqwest fails the request whose connect failed).
    fn fail_waiting(&mut self, py: Python<'_>, origin: usize, message: String) -> PyResult<()> {
        let o = &mut self.origins[origin];
        let slot = if o.waiting.len() > o.connecting { o.waiting.pop_front() } else { None };
        if let Some(meta) = slot.and_then(|slot| self.release(slot)) {
            Glue::settle(py, &meta.fut, Err(self.glue.connection_error(py, message)))?;
        }
        Ok(())
    }

    /// Ask the loop to watch every socket whose send is blocked.
    fn register_writers(&mut self, py: Python<'_>) -> PyResult<()> {
        let writers = self.writers.take();
        if writers.is_empty() {
            return Ok(());
        }
        let Some(callback) = &self.event_loop.on_writable else { return Ok(()) };
        for conn in writers {
            if let Some(c) = self.conns.get(conn).filter(|c| matches!(c.state, State::Ready | State::Handshaking)) {
                self.event_loop.add_writer.call1(py, (c.fd, callback.clone_ref(py), conn))?;
            }
        }
        Ok(())
    }

    /// Watch again every paused socket a read is now waiting on (see `LoopReady`).
    fn register_readers(&mut self, py: Python<'_>) -> PyResult<()> {
        let readers = self.readers.take();
        if readers.is_empty() {
            return Ok(());
        }
        let Some(callback) = &self.event_loop.on_readable else { return Ok(()) };
        for conn in readers {
            if let Some(c) = self.conns.get(conn).filter(|c| matches!(c.state, State::Ready | State::Handshaking)) {
                self.event_loop.add_reader.call1(py, (c.fd, callback.clone_ref(py), conn))?;
            }
        }
        Ok(())
    }

    /// Settle `slot`'s waiting read, if there is one and something to give it: the next chunk,
    /// else the end of the body.
    fn stream_progress(&mut self, py: Python<'_>, slot: usize) -> PyResult<()> {
        let Some(meta) = self.meta(slot) else { return Ok(()) };
        let timed_out = meta.timed_out;
        let Some(stream) = meta.stream.as_mut() else { return Ok(()) };
        if stream.read.is_none() {
            return Ok(());
        }
        let outcome = match stream.channel.pop() {
            Some(chunk) => ReadOutcome::Chunk(chunk),
            None => match stream.end.take() {
                // A deadline ends the body by shutting its socket down, which reads as a clean
                // end for a body delimited by the connection closing.
                Some(_) if timed_out => ReadOutcome::Failed(Failure::Timeout),
                Some(Ok(())) => ReadOutcome::End,
                Some(Err(failure)) => ReadOutcome::Failed(failure),
                None if stream.done => ReadOutcome::End,
                None => return Ok(()),
            },
        };
        let Some(read) = stream.read.take() else { return Ok(()) };
        let outcome = match outcome {
            ReadOutcome::Chunk(chunk) => Ok(PyBytes::new(py, &chunk).into_any()),
            ReadOutcome::End => Ok(py.None().into_bound(py)),
            ReadOutcome::Failed(failure) => Err(self.glue.failure(py, failure)),
        };
        Glue::settle(py, &read, outcome)
    }

    /// A streamed request's head arrived: resolve its open Future.
    fn stream_head(&mut self, py: Python<'_>, slot: usize, status: u16, version: Version, headers: HeaderMap) -> PyResult<()> {
        let Some(meta) = self.meta(slot).filter(|meta| !meta.cancelled) else { return Ok(()) };
        if let Some(stream) = meta.stream.as_mut() {
            stream.head = true;
        }
        let fut = meta.fut.clone_ref(py);
        let outcome = self.glue.stream_head(py, status, version, headers).map(Bound::into_any);
        Glue::settle(py, &fut, outcome)
    }

    /// Hand a free connection on: to the next request waiting on its origin, else the pool (or
    /// close it, when the pool keeps none or has closed).
    fn release_connection(&mut self, py: Python<'_>, conn: usize, sender: Sender) -> PyResult<()> {
        self.conns[conn].sender = Some(sender);
        if self.idle_policy.pools() && !self.is_closed {
            self.dispatch(py, conn)
        } else {
            self.expire(py, conn)
        }
    }

    /// hyper handed `slot`'s request back unsent (`conn` was already closing): it goes to the
    /// front of its origin's queue again, unless it timed out or was given up on meanwhile.
    fn requeue(&mut self, py: Python<'_>, conn: usize, slot: usize, sender: Sender, request: Request<RequestBody>, redirect: Option<Box<Redirect>>) -> PyResult<()> {
        let origin = self.conns[conn].origin;
        match self.meta(slot) {
            Some(meta) if !meta.timed_out && !meta.cancelled => {
                meta.request = Some(request);
                meta.redirect = redirect;
                self.origins[origin].waiting.push_front(slot);
            }
            _ => {
                if let Some(meta) = self.release(slot) {
                    let result = if meta.timed_out { Err(Failure::Timeout) } else { Err(Failure::Unsent(Box::new(request))) };
                    self.deliver(py, &meta, result)?;
                }
            }
        }
        drop(sender);
        self.retire(py, conn)
    }

    /// `slot`'s request got a redirect to follow on `conn`: the connection is free again, and the
    /// hop's request queues on its own origin, keeping the slot, its Future and its deadline. A
    /// request that timed out or was given up on meanwhile ends there instead.
    fn hop(&mut self, py: Python<'_>, conn: usize, slot: usize, sender: Sender, redirect: Box<Redirect>, hop: Hop) -> PyResult<()> {
        let abandoned = self.meta(slot).is_none_or(|meta| meta.timed_out || meta.cancelled);
        if abandoned {
            drop(sender);
            if let Some(meta) = self.release(slot) {
                self.deliver(py, &meta, Err(Failure::Timeout))?;
            }
            return self.retire(py, conn);
        }
        self.release_connection(py, conn, sender)?;
        let prepared = match self.config.hop(py, &self.glue, &redirect, hop) {
            Ok(prepared) => prepared,
            Err(error) => {
                if let Some(meta) = self.release(slot) {
                    Glue::settle(py, &meta.fut, Err(error))?;
                }
                return Ok(());
            }
        };
        let origin = self.origin_for(&prepared.url, prepared.route);
        let Some(meta) = self.meta(slot) else { return Ok(()) };
        meta.request = Some(prepared.request);
        meta.redirect = Some(redirect);
        meta.cookies = prepared.cookies;
        meta.origin = origin;
        self.place(py, slot, origin, Instant::now()).map(drop)
    }

    /// A streamed request's body ended: its connection goes back to the pool if it's still good,
    /// and the slot goes once Python has closed the stream.
    fn stream_end(&mut self, py: Python<'_>, conn: usize, slot: usize, sender: Sender, result: Result<(), Failure>) -> PyResult<()> {
        let Some(c) = self.conns.get_mut(conn) else { return Ok(()) };
        c.current = None;
        let (healthy, closed) = match self.meta(slot) {
            Some(meta) => {
                let healthy = result.is_ok() && !meta.timed_out && !meta.cancelled;
                let closed = meta.cancelled;
                if let Some(stream) = meta.stream.as_mut() {
                    stream.end = Some(result);
                    stream.done = true;
                }
                (healthy, closed)
            }
            None => (false, false),
        };
        if closed {
            self.release(slot);
        } else {
            self.stream_progress(py, slot)?;
        }
        if !healthy {
            drop(sender);
            return self.retire(py, conn);
        }
        self.release_connection(py, conn, sender)
    }

    /// Poll until stalled, resolve what finished, hand freed connections to waiters; repeat.
    fn run(&mut self, py: Python<'_>) -> PyResult<()> {
        loop {
            self.exec.run_until_stalled();
            self.register_writers(py)?;
            self.register_readers(py)?;
            let closed = self.closed.take();
            let finished = self.done.take();
            let handshaken = self.handshaken.take();
            let stream_events = self.stream_events.take();
            if closed.is_empty() && finished.is_empty() && handshaken.is_empty() && stream_events.is_empty() {
                return Ok(());
            }
            for (conn, generation, result) in handshaken {
                self.handshake_finished(py, conn, generation, result)?;
            }
            for (conn, generation) in closed {
                // An idle keep-alive connection the server closed. A busy one fails its own
                // request, which retires it below.
                let idle = |c: &Conn| c.generation == generation && c.state == State::Ready && c.current.is_none();
                if self.conns.get(conn).is_some_and(idle) {
                    self.retire(py, conn)?;
                }
            }
            for event in stream_events {
                match event {
                    StreamEvent::Head { slot, status, version, headers } => self.stream_head(py, slot, status, version, headers)?,
                    StreamEvent::Hop { conn, slot, sender, redirect, hop } => {
                        if let Some(c) = self.conns.get_mut(conn) {
                            c.current = None;
                            self.hop(py, conn, slot, sender, redirect, hop)?;
                        }
                    }
                    StreamEvent::Arrived(slot) => self.stream_progress(py, slot)?,
                    StreamEvent::End { conn, slot, sender, result } => self.stream_end(py, conn, slot, sender, result)?,
                }
            }
            for Completion { conn, slot, sender, result, redirect } in finished {
                let Some(c) = self.conns.get_mut(conn) else { continue };
                c.current = None;
                let result = match (result, redirect) {
                    (Ok(Fetched::Hop(hop)), Some(redirect)) => {
                        self.hop(py, conn, slot, sender, redirect, hop)?;
                        continue;
                    }
                    (Err(Failure::Unsent(request)), redirect) => {
                        self.requeue(py, conn, slot, sender, *request, redirect)?;
                        continue;
                    }
                    (Ok(Fetched::Head(head)), _) => Ok(head),
                    (Ok(Fetched::Hop(_)), None) => Err(Failure::Transport("internal error: a redirect lost its state".to_owned())),
                    (Err(failure), _) => Err(failure),
                };
                let Some(meta) = self.release(slot) else { continue };
                // A failed, timed-out or cancelled exchange leaves the connection unusable; a
                // redirect that wasn't followed only dropped its response.
                let healthy = result.as_ref().map_or_else(Failure::keeps_connection, |_| true) && !meta.timed_out && !meta.cancelled;
                let result = if meta.timed_out { Err(Failure::Timeout) } else { result };
                self.deliver(py, &meta, result)?;
                if healthy {
                    self.release_connection(py, conn, sender)?;
                } else {
                    drop(sender);
                    self.retire(py, conn)?;
                }
            }
            self.prune_deadlines();
        }
    }

    fn tls_config(&mut self) -> Result<Arc<rustls::ClientConfig>, String> {
        if let Some(config) = &self.tls_config {
            return Ok(config.clone());
        }
        let config = self.tls.client_config()?;
        self.tls_config = Some(config.clone());
        Ok(config)
    }

    /// The stream a new connection's socket becomes: plain, or TLS for an https origin.
    fn stream(&mut self, conn: usize, fd: RawFd, ready: LoopReady) -> Result<Stream<LoopReady>, String> {
        let origin = self.conns[conn].origin;
        let name = match &self.origins[origin].server_name {
            None => return Ok(Stream::plain(fd, ready)),
            Some(name) => name.clone()?,
        };
        let config = self.tls_config()?;
        let tls = ClientConnection::new(config, name).map_err(|e| error_chain(&e))?;
        Ok(Stream::Tls(Box::new(TlsSocket::new(fd, ready, tls))))
    }

    /// A connection that won't be made after all (its connect or proxy handshake failed): it
    /// stops counting as connecting, and the oldest request counting on it fails.
    fn abandon(&mut self, py: Python<'_>, conn: usize, message: String) -> PyResult<()> {
        let origin = self.conns[conn].origin;
        self.origins[origin].connecting = self.origins[origin].connecting.saturating_sub(1);
        // Not `retire`: replacing it would only fail the same way, again and again.
        self.drop_connection(py, conn)?;
        self.fail_waiting(py, origin, message)
    }

    /// Socket `fd` is connected to the origin, directly or through a finished proxy handshake:
    /// set up TLS (for https) and hyper's connection over it, and give it a request.
    fn establish(&mut self, py: Python<'_>, conn: usize, fd: RawFd, ready: LoopReady) -> PyResult<()> {
        let origin = self.conns[conn].origin;
        let generation = self.conns[conn].generation;
        self.origins[origin].connecting = self.origins[origin].connecting.saturating_sub(1);
        // http1's handshake does no I/O: it is ready on the first poll. The TLS handshake
        // happens lazily, driven by the first request's write and reads.
        let handshake = self.stream(conn, fd, ready.clone()).and_then(|stream| {
            futures_executor::block_on(http1::Builder::new().title_case_headers(true).handshake(stream))
                .map_err(|e| error_chain(&e))
        });
        let (sender, driver) = match handshake {
            Ok(pair) => pair,
            Err(message) => {
                // Not `retire`: replacing it would only fail the same way, again and again.
                self.drop_connection(py, conn)?;
                return self.fail_waiting(py, origin, message);
            }
        };
        let closed = self.closed.clone();
        self.spawner
            .spawn_local(async move {
                let _ = driver.await;
                push(&closed, (conn, generation));
            })
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        let c = &mut self.conns[conn];
        c.fd = fd;
        c.ready = Some(ready);
        c.sender = Some(sender);
        c.state = State::Ready;
        self.dispatch(py, conn)
    }

    /// Python connected `conn`'s socket `fd` (to the origin, or its proxy); `resolved` is the
    /// first address of the host `spawn_connect` asked it to resolve, if any.
    fn connected(&mut self, py: Python<'_>, conn: usize, fd: RawFd, resolved: Option<&str>) -> PyResult<()> {
        let Some(c) = self.conns.get(conn).filter(|c| c.state == State::Connecting) else {
            return self.event_loop.close_conn.call1(py, (conn,)).map(drop);
        };
        let origin = &self.origins[c.origin];
        let generation = c.generation;
        let ready = LoopReady::new(conn, self.writers.clone(), self.readers.clone());
        // A scope id (`fe80::1%en0`) isn't part of a SOCKS address.
        let resolved = resolved.and_then(|address| address.split('%').next()?.parse().ok());
        let handshake = match origin.route.handshake(&origin.key.host, origin.key.port, self.config.user_agent(), resolved) {
            Ok(Some(handshake)) => handshake,
            Ok(None) => {
                self.establish(py, conn, fd, ready)?;
                return self.run(py);
            }
            Err(message) => return self.abandon(py, conn, message),
        };
        let c = &mut self.conns[conn];
        c.fd = fd;
        c.ready = Some(ready.clone());
        c.state = State::Handshaking;
        let handshaken = self.handshaken.clone();
        self.spawner
            .spawn_local(async move {
                let result = handshake.run(fd, ready).await;
                push(&handshaken, (conn, generation, result));
            })
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))?;
        self.run(py)
    }

    /// `conn`'s proxy handshake finished: on to TLS and hyper, or the connection is given up.
    fn handshake_finished(&mut self, py: Python<'_>, conn: usize, generation: u32, result: Result<(), String>) -> PyResult<()> {
        let Some(c) = self.conns.get(conn).filter(|c| c.generation == generation && c.state == State::Handshaking) else {
            return Ok(());
        };
        let (fd, ready) = (c.fd, c.ready.clone());
        match (result, ready) {
            (Ok(()), Some(ready)) => self.establish(py, conn, fd, ready),
            (Err(message), _) => self.abandon(py, conn, message),
            (Ok(()), None) => self.abandon(py, conn, "internal error: a proxied connection lost its socket".to_owned()),
        }
    }

    fn connect_failed(&mut self, py: Python<'_>, conn: usize, message: String) -> PyResult<()> {
        let Some(c) = self.conns.get_mut(conn).filter(|c| c.state == State::Connecting) else { return Ok(()) };
        let origin = c.origin;
        c.state = State::Dead;
        c.generation = c.generation.wrapping_add(1);
        self.origins[origin].connecting = self.origins[origin].connecting.saturating_sub(1);
        self.fail_waiting(py, origin, message)
    }

    fn on_readable(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        if let Some(c) = self.conns.get(conn) {
            if let Some(ready) = c.ready.as_ref() {
                match ready.set_readable() {
                    Some(waker) => waker.wake(),
                    // Nothing waits to read it (a streamed body's consumer is behind): stop
                    // watching until a read would wait, rather than be called again at once.
                    None => {
                        ready.pause();
                        self.event_loop.remove_reader.call1(py, (c.fd,))?;
                    }
                }
            }
        }
        self.run(py)
    }

    fn on_writable(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        let Some(c) = self.conns.get(conn).filter(|c| matches!(c.state, State::Ready | State::Handshaking)) else { return Ok(()) };
        self.event_loop.remove_writer.call1(py, (c.fd,))?;
        if let Some(waker) = c.ready.as_ref().and_then(LoopReady::set_writable) {
            waker.wake();
        }
        self.run(py)
    }

    fn running(&self, slot: usize) -> Option<usize> {
        self.conns.iter().position(|c| c.current == Some(slot))
    }

    fn cancel(&mut self, py: Python<'_>, fut: &Bound<'_, PyAny>) -> PyResult<()> {
        let is_fut = |slots: &[Slot], slot: usize| slots[slot].meta.as_ref().is_some_and(|m| m.fut.is(fut));
        for origin in 0..self.origins.len() {
            if let Some(pos) = self.origins[origin].waiting.iter().position(|s| is_fut(&self.slots, *s)) {
                if let Some(slot) = self.origins[origin].waiting.remove(pos) {
                    self.release(slot);
                }
                return Ok(());
            }
        }
        let found = self.conns.iter().find_map(|c| c.current.filter(|s| is_fut(&self.slots, *s)));
        if let Some(slot) = found {
            if let Some(meta) = self.meta(slot) {
                meta.cancelled = true;
            }
            if let Some(conn) = self.running(slot) {
                shutdown(self.conns[conn].fd);
            }
        }
        self.run(py)
    }

    fn on_deadline(&mut self, py: Python<'_>) -> PyResult<()> {
        self.armed = None;
        let now = Instant::now();
        let mut next = None;
        while let Some(Reverse((deadline, slot, generation))) = self.deadlines.peek().copied() {
            if deadline > now {
                next = Some(deadline);
                break;
            }
            self.deadlines.pop();
            if !self.is_live(slot, generation) {
                continue;
            }
            if let Some(conn) = self.running(slot) {
                // In flight: shutting the socket down fails hyper's I/O, and the completion
                // reports it as the timeout.
                if let Some(meta) = self.meta(slot) {
                    meta.timed_out = true;
                }
                shutdown(self.conns[conn].fd);
            } else if let Some(origin) = self.meta(slot).map(|meta| meta.origin) {
                if let Some(pos) = self.origins[origin].waiting.iter().position(|s| *s == slot) {
                    self.origins[origin].waiting.remove(pos);
                    if let Some(meta) = self.release(slot) {
                        self.deliver(py, &meta, Err(Failure::Timeout))?;
                    }
                }
            }
        }
        if self.reap_at.is_some_and(|at| at <= now) {
            self.reap(py, now)?;
        }
        // One timer for whichever comes first, the next request deadline or the reaper's turn.
        if let Some(at) = next.into_iter().chain(self.reap_at).min() {
            self.arm(py, at)?;
        }
        self.run(py)
    }

    #[allow(clippy::too_many_arguments)]
    fn request(
        &mut self,
        py: Python<'_>,
        method: Bound<'_, PyString>,
        path: &str,
        query: Option<&Bound<'_, PyAny>>,
        headers: Option<&Bound<'_, PyAny>>,
        auth: Option<&str>,
        body: Option<&Bound<'_, PyAny>>,
        body_kind: u8,
        decode: Option<Py<PyAny>>,
        timeout: Option<Duration>,
        raises: Option<Py<PyAny>>,
        typed_headers: Option<Py<PyAny>>,
        headers_only: bool,
    ) -> PyResult<Py<PyAny>> {
        if self.is_closed {
            return Err(Glue::closed_error());
        }
        let prepared =
            self.config.prepare(py, &self.glue, method.to_str()?, path, query, headers, auth, body, body_kind)?;
        let origin = self.origin_for(&prepared.url, prepared.route);
        let fut = self.event_loop.create_future.call0(py)?;
        let started = Instant::now();
        let deadline = timeout.and_then(|timeout| self.clock.deadline(started, timeout));
        let meta = Meta {
            request: Some(prepared.request),
            redirect: prepared.redirect,
            cookies: prepared.cookies,
            exchange: Exchange {
                method: method.unbind(),
                url: prepared.url,
                decode,
                raises,
                typed_headers,
                headers_only,
                started,
            },
            fut: fut.clone_ref(py),
            origin,
            timed_out: false,
            cancelled: false,
            stream: None,
        };
        let (slot, generation) = self.alloc(meta);
        self.enqueue(py, slot, generation, origin, started, deadline)?;
        Ok(fut)
    }

    /// Prepare a streamed request, sending nothing yet: its slot, generation and `RequestInfo`.
    #[allow(clippy::too_many_arguments)]
    fn prepare_stream(
        &mut self,
        py: Python<'_>,
        method: Bound<'_, PyString>,
        path: &str,
        query: Option<&Bound<'_, PyAny>>,
        headers: Option<&Bound<'_, PyAny>>,
        auth: Option<&str>,
        body: Option<&Bound<'_, PyAny>>,
        body_kind: u8,
        timeout: Option<Duration>,
        verb_headers: VerbHeaders,
    ) -> PyResult<(usize, u32, Py<PyAny>)> {
        if self.is_closed {
            return Err(Glue::closed_error());
        }
        let prepared =
            self.config.prepare_with(py, &self.glue, method.to_str()?, path, query, headers, auth, body, body_kind, verb_headers)?;
        let request_info = self.glue.request_info_of(py, &method, &prepared.url)?.unbind();
        let origin = self.origin_for(&prepared.url, prepared.route);
        let fut = self.event_loop.create_future.call0(py)?;
        let meta = Meta {
            request: Some(prepared.request),
            redirect: prepared.redirect,
            cookies: prepared.cookies,
            exchange: Exchange {
                method: method.unbind(),
                url: prepared.url,
                decode: None,
                raises: None,
                typed_headers: None,
                headers_only: false,
                started: Instant::now(),
            },
            fut,
            origin,
            timed_out: false,
            cancelled: false,
            stream: Some(Box::new(StreamMeta {
                channel: Rc::default(),
                read: None,
                end: None,
                done: false,
                opened: false,
                head: false,
                timeout,
            })),
        };
        let (slot, generation) = self.alloc(meta);
        Ok((slot, generation, request_info))
    }

    /// Send a prepared stream: the Future its head resolves, as `(status, RawHeaders, version)`.
    fn stream_open(&mut self, py: Python<'_>, slot: usize, generation: u32) -> PyResult<Py<PyAny>> {
        if self.is_closed {
            return Err(Glue::closed_error());
        }
        if !self.is_live(slot, generation) {
            return Err(stream_misused("a stream was opened after it closed"));
        }
        let (started, clock) = (Instant::now(), self.clock);
        let Some(meta) = self.meta(slot) else { return Err(stream_misused("a stream lost its state")) };
        let Some(stream) = meta.stream.as_mut().filter(|stream| !stream.opened) else {
            return Err(stream_misused("a stream was opened twice"));
        };
        stream.opened = true;
        let deadline = stream.timeout.and_then(|timeout| clock.deadline(started, timeout));
        meta.exchange.started = started;
        let (origin, fut) = (meta.origin, meta.fut.clone_ref(py));
        self.enqueue(py, slot, generation, origin, started, deadline)?;
        Ok(fut)
    }

    /// The Future of an open stream's next chunk (`bytes`), or `None` at the end of its body. A
    /// stream that reads on (`Meta::reads_on`) still reads once the pool has closed.
    fn stream_read(&mut self, py: Python<'_>, slot: usize, generation: u32) -> PyResult<Py<PyAny>> {
        if !self.is_live(slot, generation) {
            return Err(if self.is_closed { Glue::closed_error() } else { stream_misused("a stream was read after it closed") });
        }
        let fut = self.event_loop.create_future.call0(py)?;
        let Some(stream) = self.meta(slot).and_then(|meta| meta.stream.as_mut()) else {
            return Err(stream_misused("a stream lost its state"));
        };
        if stream.read.is_some() {
            return Err(stream_misused("a stream was read while a read was already waiting"));
        }
        stream.read = Some(fut.clone_ref(py));
        self.stream_progress(py, slot)?;
        // A chunk taken makes room: the pump reads on.
        self.run(py)?;
        Ok(fut)
    }

    /// Python is done with a stream. One still being read is cut off: its socket is shut down
    /// and the connection retired once its task has seen that, so the server sees the hang-up.
    fn stream_close(&mut self, py: Python<'_>, slot: usize, generation: u32) -> PyResult<()> {
        if !self.is_live(slot, generation) {
            return Ok(());
        }
        let Some(origin) = self.meta(slot).map(|meta| meta.origin) else { return Ok(()) };
        if let Some(pos) = self.origins[origin].waiting.iter().position(|s| *s == slot) {
            self.origins[origin].waiting.remove(pos);
            self.release(slot);
            return Ok(());
        }
        let Some(conn) = self.running(slot) else {
            // Never opened, or its body is done.
            self.release(slot);
            return Ok(());
        };
        if let Some(meta) = self.meta(slot) {
            meta.cancelled = true;
            if let Some(stream) = &meta.stream {
                stream.channel.close();
            }
        }
        if self.is_closed {
            // Its connection is retired right here: after the pool's close, the loop may not run
            // again to report the socket.
            self.cut_off(conn);
        } else {
            shutdown(self.conns[conn].fd);
        }
        self.run(py)
    }

    /// Shut `conn`'s socket down and wake whatever waits on it, so its tasks see the hang-up on
    /// their next poll, not once the loop reports the socket.
    fn cut_off(&self, conn: usize) {
        let c = &self.conns[conn];
        shutdown(c.fd);
        if let Some(ready) = &c.ready {
            for waker in [ready.set_readable(), ready.set_writable()].into_iter().flatten() {
                waker.wake();
            }
        }
    }

    /// Fail everything still pending and drop every connection and the timer, except what the
    /// streams that read on (`Meta::reads_on`) still need: as on pyreqwest, each one's body
    /// outlives the client, its deadline included, and its connection is closed, never pooled,
    /// once the body ends or the stream is closed.
    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        self.is_closed = true;
        self.reap_at = None;
        if let Some((_, handle)) = self.armed.take() {
            handle.bind(py).call_method0(intern!(py, "cancel"))?;
        }
        for slot in 0..self.slots.len() {
            if self.slots[slot].meta.as_ref().is_some_and(Meta::reads_on) {
                continue;
            }
            if let Some(meta) = self.release(slot) {
                Glue::settle(py, &meta.fut, Err(Glue::closed_error()))?;
                if let Some(read) = meta.stream.and_then(|stream| stream.read) {
                    Glue::settle(py, &read, Err(Glue::closed_error()))?;
                }
            }
        }
        // Every other connection is cut off and its tasks run to their end before its socket is
        // closed, so none can touch the descriptor's number once a new socket reuses it.
        let slots = &self.slots;
        let reading_on = |c: &Conn| c.current.is_some_and(|slot| slots[slot].meta.is_some());
        let cut: Vec<usize> = (0..self.conns.len()).filter(|&conn| self.conns[conn].state != State::Dead && !reading_on(&self.conns[conn])).collect();
        for &conn in &cut {
            self.cut_off(conn);
        }
        self.exec.run_until_stalled();
        for &conn in &cut {
            self.drop_connection(py, conn)?;
        }
        for origin in &mut self.origins {
            origin.waiting.clear();
            origin.connecting = 0;
        }
        // What their tasks reported goes with them; the streams that read on keep theirs.
        drop((self.done.take(), self.handshaken.take()));
        let events = self.stream_events.take();
        let slots = &self.slots;
        let live = |slot: usize| slots[slot].meta.is_some();
        let kept: Vec<StreamEvent> = events
            .into_iter()
            .filter(|event| match event {
                StreamEvent::Arrived(slot) | StreamEvent::End { slot, .. } => live(*slot),
                StreamEvent::Head { .. } | StreamEvent::Hop { .. } => false,
            })
            .collect();
        self.stream_events.set(kept);
        if self.slots.iter().all(|slot| slot.meta.is_none()) {
            // Nothing reads on: dropping the executor drops anything still in it.
            self.exec = LocalPool::new();
            self.spawner = self.exec.spawner();
            drop((self.closed.take(), self.writers.take(), self.readers.take(), self.stream_events.take()));
            self.conns.clear();
            self.origins.clear();
            self.deadlines.clear();
            return Ok(());
        }
        let slots = &self.slots;
        self.deadlines.retain(|Reverse((_, s, g))| slots[*s].generation == *g && slots[*s].meta.is_some());
        if let Some(Reverse((deadline, _, _))) = self.deadlines.peek().copied() {
            self.arm(py, deadline)?;
        }
        self.run(py)
    }
}

#[pyclass(module = "lothc._native")]
pub(crate) struct Pool {
    state: ThreadBound<PoolState>,
}

#[pymethods]
impl Pool {
    #[new]
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (base_url, default_headers, user_agent, https_only, cookie_store, follow_redirects, max_redirects, root_certificates, identity_pem, min_tls_version, max_tls_version, danger_accept_invalid_certs, timeout, pool_idle_timeout, pool_max_idle_per_host, proxy, no_proxy, glue, event_loop, spawn_connect, close_conn))]
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
        proxy: Option<&str>,
        no_proxy: Option<&str>,
        glue: &Bound<'_, PyAny>,
        event_loop: &Bound<'_, PyAny>,
        spawn_connect: Py<PyAny>,
        close_conn: Py<PyAny>,
    ) -> PyResult<Self> {
        // The client's `timeout` is only converted, so that a bad one fails on entry with pyo3's
        // own message, as on pyreqwest's builder: each request carries its own timeout.
        let _ = timeout;
        let exec = LocalPool::new();
        let spawner = exec.spawner();
        let state = PoolState {
            exec,
            spawner,
            config: ClientConfig::new(base_url, default_headers, user_agent, https_only)?
                .with_proxies(Proxies::new(proxy, no_proxy)?)
                .with_redirects(follow_redirects, max_redirects)
                .with_cookies(cookie_store),
            tls: TlsSettings::new(
                &root_certificates,
                identity_pem.as_deref(),
                min_tls_version,
                max_tls_version,
                danger_accept_invalid_certs,
            )?,
            tls_config: None,
            idle_policy: IdlePolicy::new(pool_idle_timeout, pool_max_idle_per_host),
            reap_at: None,
            origins: Vec::new(),
            conns: Vec::new(),
            slots: Vec::new(),
            free: Vec::new(),
            deadlines: BinaryHeap::new(),
            armed: None,
            done: Rc::new(Cell::new(Vec::new())),
            closed: Rc::new(Cell::new(Vec::new())),
            handshaken: Rc::new(Cell::new(Vec::new())),
            writers: Rc::new(Cell::new(Vec::new())),
            readers: Rc::new(Cell::new(Vec::new())),
            stream_events: Rc::new(Cell::new(Vec::new())),
            event_loop: LoopGlue {
                create_future: event_loop.getattr("create_future")?.unbind(),
                call_later: event_loop.getattr("call_later")?.unbind(),
                add_reader: event_loop.getattr("add_reader")?.unbind(),
                remove_reader: event_loop.getattr("remove_reader")?.unbind(),
                add_writer: event_loop.getattr("add_writer")?.unbind(),
                remove_writer: event_loop.getattr("remove_writer")?.unbind(),
                spawn_connect,
                close_conn,
                on_deadline: None,
                on_writable: None,
                on_readable: None,
            },
            glue: Glue::new(py, glue)?,
            clock: DeadlineClock::new(),
            is_closed: false,
        };
        Ok(Self { state: ThreadBound::new(state) })
    }

    /// The pool's own bound `on_deadline`, `on_writable` and `on_readable`, handed back once so
    /// arming the timer, a writer or a paused reader doesn't look them up.
    fn set_callbacks(&mut self, on_deadline: Py<PyAny>, on_writable: Py<PyAny>, on_readable: Py<PyAny>) -> PyResult<()> {
        let state = self.state.get_mut()?;
        state.event_loop.on_deadline = Some(on_deadline);
        state.event_loop.on_writable = Some(on_writable);
        state.event_loop.on_readable = Some(on_readable);
        Ok(())
    }

    /// Start a request and return the asyncio Future it resolves (see `response.rs`).
    #[pyo3(signature = (method, path, query, headers, auth, body, body_kind, decode, timeout, raises, typed_headers, headers_only))]
    #[allow(clippy::too_many_arguments)]
    fn request(
        &mut self,
        py: Python<'_>,
        method: Bound<'_, PyString>,
        path: &str,
        query: Option<Bound<'_, PyAny>>,
        headers: Option<Bound<'_, PyAny>>,
        auth: Option<&str>,
        body: Option<Bound<'_, PyAny>>,
        body_kind: u8,
        decode: Option<Py<PyAny>>,
        timeout: Option<Duration>,
        raises: Option<Py<PyAny>>,
        typed_headers: Option<Py<PyAny>>,
        headers_only: bool,
    ) -> PyResult<Py<PyAny>> {
        self.state.get_mut()?.request(
            py,
            method,
            path,
            query.as_ref(),
            headers.as_ref(),
            auth,
            body.as_ref(),
            body_kind,
            decode,
            timeout,
            raises,
            typed_headers,
            headers_only,
        )
    }

    /// Prepare a streamed request (nothing is sent until its `open`): `request`'s arguments, less
    /// the decoding and status check (Python reads an error's body itself), plus `accept` and
    /// `last_event_id`, the headers `sse()` sets.
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
    ) -> PyResult<AsyncStream> {
        let verb_headers = VerbHeaders::sse(accept, last_event_id)?;
        let (slot, generation, request_info) = slf.try_borrow_mut()?.state.get_mut()?.prepare_stream(
            py,
            method,
            path,
            query.as_ref(),
            headers.as_ref(),
            auth,
            body.as_ref(),
            body_kind,
            timeout,
            verb_headers,
        )?;
        Ok(AsyncStream { pool: slf.clone().unbind(), slot, generation, request_info })
    }

    /// Python finished `sock_connect` for connection `conn`; `fd` is its socket, and `resolved`
    /// the first address of the host `spawn_connect` asked it to resolve, if any.
    #[pyo3(signature = (conn, fd, resolved=None))]
    fn connected(&mut self, py: Python<'_>, conn: usize, fd: RawFd, resolved: Option<&str>) -> PyResult<()> {
        self.state.get_mut()?.connected(py, conn, fd, resolved)
    }

    /// Connecting `conn` failed with `message`.
    fn connect_failed(&mut self, py: Python<'_>, conn: usize, message: String) -> PyResult<()> {
        self.state.get_mut()?.connect_failed(py, conn, message)
    }

    fn on_readable(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        self.state.get_mut()?.on_readable(py, conn)
    }

    fn on_writable(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        self.state.get_mut()?.on_writable(py, conn)
    }

    /// The caller's task was cancelled while awaiting `fut`.
    fn cancel(&mut self, py: Python<'_>, fut: &Bound<'_, PyAny>) -> PyResult<()> {
        self.state.get_mut()?.cancel(py, fut)
    }

    /// The shared timer fired: expire every request due and, on the reaper's turn, every idle
    /// connection due, then re-arm for whichever comes next.
    fn on_deadline(&mut self, py: Python<'_>) -> PyResult<()> {
        self.state.get_mut()?.on_deadline(py)
    }

    /// The client is closing: pending requests fail with `RuntimeError`, connections are dropped,
    /// all but those of streams whose head has arrived, which read on.
    fn close(&mut self, py: Python<'_>) -> PyResult<()> {
        self.state.get_mut()?.close(py)
    }

    fn __repr__(&self) -> &'static str {
        "<lothc._native.Pool>"
    }
}

/// One streamed request on an async `Pool`, from `Pool.stream`: `open` it, `read` its body chunk
/// by chunk, `close` it (always, however reading ended). Each call returns at once; `open` and
/// `read` give Futures the pool resolves.
#[pyclass(frozen, name = "Stream", module = "lothc._native")]
pub(crate) struct AsyncStream {
    pool: Py<Pool>,
    slot: usize,
    generation: u32,
    request_info: Py<PyAny>,
}

impl AsyncStream {
    fn with<T>(&self, py: Python<'_>, call: impl FnOnce(&mut PoolState) -> PyResult<T>) -> PyResult<T> {
        let mut pool = self.pool.bind(py).try_borrow_mut()?;
        call(pool.state.get_mut()?)
    }
}

#[pymethods]
impl AsyncStream {
    #[getter]
    fn request_info(&self, py: Python<'_>) -> Py<PyAny> {
        self.request_info.clone_ref(py)
    }

    /// Send the request: a Future of its head, `(status, RawHeaders, http_version)`.
    fn open(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.with(py, |state| state.stream_open(py, self.slot, self.generation))
    }

    /// A Future of the body's next chunk, `None` at its end.
    fn read(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        self.with(py, |state| state.stream_read(py, self.slot, self.generation))
    }

    fn close(&self, py: Python<'_>) -> PyResult<()> {
        self.with(py, |state| state.stream_close(py, self.slot, self.generation))
    }

    fn __repr__(&self) -> &'static str {
        "<lothc._native.Stream>"
    }
}
