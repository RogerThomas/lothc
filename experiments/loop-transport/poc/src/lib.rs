//! Loop-native HTTP/1.1 GET POC (after fastpg's design): no tokio, no I/O thread.
//!
//! Python owns the sockets and registers each with `loop.add_reader`; the callback calls
//! `Pool.on_readable(i)`, which marks the socket readable and runs a single-thread executor
//! (futures' `LocalPool`) polling hyper's low-level http1 client under the GIL. Finished
//! requests are resolved in one pass, building exactly what lothc's `get()` returns today:
//! a `Response` (decoded body, status, `_LazyHeaders`, `RequestInfo`, http version, elapsed),
//! or lothc's own `HTTPResponseError` / `HTTPConnectionError` / `HTTPTimeoutError`.
//!
//! Timeouts share ONE `loop.call_later` timer, armed for the earliest deadline in a heap:
//! a per-request `call_later` + `cancel()` measured ~1.3us of ~11us. A request that expires
//! in flight has its connection's socket shut down, which fails hyper's read; that failure is
//! reported as the timeout. In-flight state lives in a generation-tagged slab (O(1), no
//! hashing), and heap entries for finished requests are dropped lazily.
//!
//! Connections are opened on demand (Python's `loop.sock_connect`, via `spawn_connect`) when
//! requests queue up, and replaced when one dies. A request hyper reports as never sent (a
//! stale keep-alive connection) is re-queued, as reqwest's pool does; anything else fails.
use std::cell::RefCell;
use std::cmp::Reverse;
use std::collections::{BinaryHeap, VecDeque};
use std::io::{self, Read as _, Write as _};
use std::os::fd::RawFd;
use std::pin::Pin;
use std::rc::Rc;
use std::future::Future;
use std::net::{TcpStream, ToSocketAddrs};
use std::os::fd::AsRawFd;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::task::Wake;
use std::task::{Context, Poll, Waker};
use std::time::{Duration, Instant};

use bytes::Bytes;
use futures_executor::{LocalPool, LocalSpawner};
use futures_util::task::LocalSpawnExt;
use http::header::{HeaderMap, HeaderName, HeaderValue, ACCEPT, CONTENT_TYPE, HOST, USER_AGENT};
use http::{Method, Request};
use http_body_util::{BodyExt, Full};
use hyper::client::conn::http1::{self, SendRequest};
use pyo3::intern;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::CryptoProvider;
use rustls::pki_types::pem::PemObject;
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, ClientConnection, DigitallySignedStruct, SignatureScheme};
use pyo3::prelude::*;
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

#[derive(Default)]
struct IoFlags {
    readable: bool,
    read_waker: Option<Waker>,
}

/// A non-blocking socket owned by Python; readiness comes from the loop's reader callback.
struct LoopSocket {
    fd: RawFd,
    flags: Rc<RefCell<IoFlags>>,
}

impl hyper::rt::Read for LoopSocket {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        mut buf: hyper::rt::ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        let mut flags = self.flags.borrow_mut();
        if !flags.readable {
            flags.read_waker = Some(cx.waker().clone());
            return Poll::Pending;
        }
        // SAFETY: recv only writes into the slice; we advance by exactly what it wrote.
        let dst = unsafe { buf.as_mut() };
        let n = unsafe { libc::recv(self.fd, dst.as_mut_ptr().cast(), dst.len(), 0) };
        if n < 0 {
            let err = io::Error::last_os_error();
            if err.kind() == io::ErrorKind::WouldBlock {
                flags.readable = false;
                flags.read_waker = Some(cx.waker().clone());
                return Poll::Pending;
            }
            return Poll::Ready(Err(err));
        }
        let n = n as usize;
        // A short read drained the socket: skip the recv that would only return EAGAIN.
        // The reader is level-triggered, so later data re-arms `readable`.
        if n > 0 && n < dst.len() {
            flags.readable = false;
        }
        unsafe { buf.advance(n) };
        Poll::Ready(Ok(()))
    }
}

impl hyper::rt::Write for LoopSocket {
    fn poll_write(self: Pin<&mut Self>, _cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        let n = unsafe { libc::send(self.fd, buf.as_ptr().cast(), buf.len(), libc::MSG_NOSIGNAL) };
        if n < 0 {
            // POC: requests are tiny, a full send buffer is not handled (no add_writer).
            return Poll::Ready(Err(io::Error::last_os_error()));
        }
        Poll::Ready(Ok(n as usize))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

/// `send()` on the raw fd, for rustls's `write_tls`.
struct FdWriter(RawFd);

impl io::Write for FdWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let n = unsafe { libc::send(self.0, buf.as_ptr().cast(), buf.len(), libc::MSG_NOSIGNAL) };
        // POC: a full send buffer surfaces as an error (no add_writer), as for plain sockets.
        if n < 0 { Err(io::Error::last_os_error()) } else { Ok(n as usize) }
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// TLS over the loop-driven socket. rustls is sans-I/O: records read from the socket go into
/// `read_tls` + `process_new_packets`, plaintext comes out of `reader()`, and whatever rustls
/// wants to send (handshake messages, encrypted writes) goes out through `write_tls`.
struct TlsSocket {
    fd: RawFd,
    flags: Rc<RefCell<IoFlags>>,
    tls: ClientConnection,
    rx: Box<[u8]>,
}

fn tls_error(e: rustls::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, e)
}

impl TlsSocket {
    fn flush_tls(&mut self) -> io::Result<()> {
        while self.tls.wants_write() {
            self.tls.write_tls(&mut FdWriter(self.fd))?;
        }
        Ok(())
    }

    /// Feed one `recv` worth of TLS records to rustls; `Pending` registers the read waker.
    fn poll_fill(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let n = {
            let mut flags = self.flags.borrow_mut();
            if !flags.readable {
                flags.read_waker = Some(cx.waker().clone());
                return Poll::Pending;
            }
            let n = unsafe { libc::recv(self.fd, self.rx.as_mut_ptr().cast(), self.rx.len(), 0) };
            if n < 0 {
                let err = io::Error::last_os_error();
                if err.kind() == io::ErrorKind::WouldBlock {
                    flags.readable = false;
                    flags.read_waker = Some(cx.waker().clone());
                    return Poll::Pending;
                }
                return Poll::Ready(Err(err));
            }
            let n = n as usize;
            if n > 0 && n < self.rx.len() {
                flags.readable = false;
            }
            n
        };
        let mut records = &self.rx[..n];
        loop {
            // An empty slice tells rustls the peer closed (EOF).
            if let Err(e) = self.tls.read_tls(&mut records) {
                return Poll::Ready(Err(e));
            }
            if let Err(e) = self.tls.process_new_packets() {
                let _ = self.flush_tls(); // send the alert rustls queued
                return Poll::Ready(Err(tls_error(e)));
            }
            if records.is_empty() {
                return Poll::Ready(Ok(()));
            }
        }
    }
}

impl hyper::rt::Read for TlsSocket {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        mut buf: hyper::rt::ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        loop {
            let mut plain = [0u8; 16 * 1024];
            let want = buf.remaining().min(plain.len());
            match this.tls.reader().read(&mut plain[..want]) {
                // Ok(0) is a clean close_notify: EOF for hyper.
                Ok(n) => {
                    buf.put_slice(&plain[..n]);
                    return Poll::Ready(Ok(()));
                }
                Err(e) if e.kind() == io::ErrorKind::WouldBlock => {}
                Err(e) => return Poll::Ready(Err(e)),
            }
            if let Err(e) = this.flush_tls() {
                return Poll::Ready(Err(e));
            }
            match this.poll_fill(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Err(e)) => return Poll::Ready(Err(e)),
                Poll::Ready(Ok(())) => {}
            }
        }
    }
}

impl hyper::rt::Write for TlsSocket {
    fn poll_write(self: Pin<&mut Self>, _cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        // Before the handshake finishes rustls buffers this and sends it once it can.
        let result = this.tls.writer().write(buf).and_then(|n| this.flush_tls().map(|()| n));
        Poll::Ready(result)
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(self.get_mut().flush_tls())
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        this.tls.send_close_notify();
        Poll::Ready(this.flush_tls())
    }
}

/// `danger_accept_invalid_certs=True`: any certificate, signatures still checked.
#[derive(Debug)]
struct AcceptAnyCert(Arc<CryptoProvider>);

impl ServerCertVerifier for AcceptAnyCert {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(&self, message: &[u8], cert: &CertificateDer<'_>, dss: &DigitallySignedStruct) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn verify_tls13_signature(&self, message: &[u8], cert: &CertificateDer<'_>, dss: &DigitallySignedStruct) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// reqwest 0.13's `rustls` setup: aws-lc-rs, the OS trust store via rustls-platform-verifier,
/// `root_certificates` as extra roots (reqwest's `add_root_certificate`).
fn tls_config(root_certificates: &[Vec<u8>], danger_accept_invalid_certs: bool) -> PyResult<Arc<ClientConfig>> {
    let value_error = |e: &dyn std::fmt::Display| PyValueError::new_err(e.to_string());
    let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
    let builder = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .map_err(|e| value_error(&e))?
        .dangerous();
    let mut config = if danger_accept_invalid_certs {
        builder.with_custom_certificate_verifier(Arc::new(AcceptAnyCert(provider))).with_no_client_auth()
    } else {
        let mut roots = Vec::new();
        for pem in root_certificates {
            for cert in CertificateDer::pem_slice_iter(pem) {
                roots.push(cert.map_err(|e| value_error(&e))?);
            }
        }
        let verifier = rustls_platform_verifier::Verifier::new_with_extra_roots(roots, provider).map_err(|e| value_error(&e))?;
        builder.with_custom_certificate_verifier(Arc::new(verifier)).with_no_client_auth()
    };
    config.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(Arc::new(config))
}

/// hyper's `Display` omits causes; lothc's messages (e.g. `match="UnknownIssuer"`) need them.
fn error_chain(error: &dyn std::error::Error) -> String {
    let mut message = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        message.push_str(": ");
        message.push_str(&cause.to_string());
        source = cause.source();
    }
    message
}

/// The response headers, kept in Rust until `_LazyHeaders` first reads them via `.items()`.
#[pyclass(frozen)]
struct RawHeaders {
    map: HeaderMap,
}

#[pymethods]
impl RawHeaders {
    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(py);
        for (name, value) in &self.map {
            let value = String::from_utf8_lossy(value.as_bytes());
            list.append(PyTuple::new(py, [PyString::new(py, name.as_str()), PyString::new(py, &value)])?)?;
        }
        Ok(list)
    }
}

type Sender = SendRequest<Full<Bytes>>;

/// One request's Python-facing state, in the slab while it waits or runs.
struct Meta {
    /// Taken by `start`; put back if hyper hands it back unsent.
    request: Option<Request<Full<Bytes>>>,
    method: Py<PyString>,
    /// Path plus query, for `RequestInfo` (`path_end` splits off the query).
    uri: String,
    path_end: usize,
    fut: Py<PyAny>,
    decode: Option<Py<PyAny>>,
    raises: Option<Py<PyAny>>,
    started: Instant,
    timed_out: bool,
    cancelled: bool,
}

struct Slot {
    generation: u32,
    meta: Option<Meta>,
}

struct Head {
    status: u16,
    headers: HeaderMap,
    body: Bytes,
}

enum Failure {
    /// hyper never sent it (the connection was already closing): safe to re-queue.
    Unsent(Request<Full<Bytes>>),
    Transport(String),
    Timeout,
}

struct Completion {
    conn: usize,
    slot: usize,
    sender: Sender,
    result: Result<Head, Failure>,
}

#[derive(PartialEq)]
enum State {
    Connecting,
    Ready,
    Dead,
}

struct Conn {
    fd: RawFd,
    /// Bumped whenever the slot is retired, so a stale "driver finished" event is ignored.
    generation: u32,
    state: State,
    sender: Option<Sender>,
    flags: Rc<RefCell<IoFlags>>,
    /// The slab slot of the request it is running.
    current: Option<usize>,
}

/// lothc's own classes and helpers, so what a caller gets back is exactly today's types.
struct Glue {
    create_future: Py<PyAny>,
    call_later: Py<PyAny>,
    spawn_connect: Py<PyAny>,
    close_conn: Py<PyAny>,
    on_deadline: Option<Py<PyAny>>,
    response: Py<PyAny>,
    request_info: Py<PyAny>,
    lazy_headers: Py<PyAny>,
    status_error: Py<PyAny>,
    connection_error: Py<PyAny>,
    timeout_error: Py<PyAny>,
    http_11: Py<PyString>,
}

#[pyclass(unsendable)]
struct Pool {
    exec: LocalPool,
    spawner: LocalSpawner,
    conns: Vec<Conn>,
    connecting: usize,
    idle: VecDeque<usize>,
    /// Slab slots waiting for a connection.
    waiting: VecDeque<usize>,
    slots: Vec<Slot>,
    free: Vec<usize>,
    /// (deadline, slot, generation); stale entries are skipped when they surface.
    deadlines: BinaryHeap<Reverse<(Instant, usize, u32)>>,
    /// The one armed timer: when it fires, and its handle.
    armed: Option<(Instant, Py<PyAny>)>,
    done: Rc<RefCell<Vec<Completion>>>,
    /// Connections whose hyper driver finished: (conn, generation).
    closed: Rc<RefCell<Vec<(usize, u32)>>>,
    host: HeaderValue,
    host_str: Py<PyString>,
    origin: String,
    /// Sent unless the request sets them itself (reqwest's client-default semantics).
    default_headers: HeaderMap,
    /// `Some` for an https base URL.
    tls: Option<(Arc<ClientConfig>, ServerName<'static>)>,
    bare: bool,
    glue: Glue,
}

async fn fetch(sender: &mut Sender, request: Request<Full<Bytes>>) -> Result<Head, Failure> {
    let transport = |e: hyper::Error| Failure::Transport(error_chain(&e));
    if sender.ready().await.is_err() {
        return Err(Failure::Unsent(request));
    }
    let resp = match sender.try_send_request(request).await {
        Ok(resp) => resp,
        Err(mut e) => {
            return Err(match e.take_message() {
                Some(request) => Failure::Unsent(request),
                None => Failure::Transport(error_chain(&e.into_error())),
            })
        }
    };
    let (parts, body) = resp.into_parts();
    let body = body.collect().await.map_err(transport)?.to_bytes();
    Ok(Head { status: parts.status.as_u16(), headers: parts.headers, body })
}

/// Append one query pair as reqwest's `.query()` would write it (serde_urlencoded: bools
/// lowercase, ints as decimal, floats via ryu), with no allocation for the value.
fn append_query_value(
    serializer: &mut form_urlencoded::Serializer<'_, String>,
    name: &str,
    value: &Bound<'_, PyAny>,
) -> PyResult<()> {
    if let Ok(s) = value.cast::<PyString>() {
        serializer.append_pair(name, s.to_str()?);
    } else if let Ok(b) = value.cast::<PyBool>() {
        serializer.append_pair(name, if b.is_true() { "true" } else { "false" });
    } else if value.is_instance_of::<PyInt>() {
        // As pyreqwest's conversion does: an i64, else an f64 (2**63 goes out as 9.2e18).
        if let Ok(n) = value.extract::<i64>() {
            serializer.append_pair(name, itoa::Buffer::new().format(n));
        } else {
            let f: f64 = value.call_method0(intern!(value.py(), "__float__"))?.extract()?;
            serializer.append_pair(name, ryu::Buffer::new().format(f));
        }
    } else if let Ok(f) = value.cast::<PyFloat>() {
        serializer.append_pair(name, ryu::Buffer::new().format(f.value()));
    } else {
        return Err(PyTypeError::new_err(format!("unsupported query value: {}", value.repr()?)));
    }
    Ok(())
}

/// `query` is either a plain `dict` (lothc's `Params` mapping: a `list`/`tuple` value repeats
/// the key, as `_query_pairs` does) or the `(name, value)` pairs lothc's helpers produced for
/// a model/Struct.
fn append_query(serializer: &mut form_urlencoded::Serializer<'_, String>, query: &Bound<'_, PyAny>) -> PyResult<()> {
    let mut add = |name: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>| -> PyResult<()> {
        let name = name.cast::<PyString>()?.to_str()?;
        if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
            for item in value.try_iter()? {
                append_query_value(serializer, name, &item?)?;
            }
            Ok(())
        } else {
            append_query_value(serializer, name, value)
        }
    };
    if let Ok(dict) = query.cast::<PyDict>() {
        for (name, value) in dict.iter() {
            add(&name, &value)?;
        }
    } else {
        for pair in query.try_iter()? {
            let pair = pair?;
            let pair = pair.cast::<PyTuple>()?;
            add(&pair.get_item(0)?, &pair.get_item(1)?)?;
        }
    }
    Ok(())
}

fn header_pair(name: &str, value: &str) -> PyResult<(HeaderName, HeaderValue)> {
    let name = HeaderName::from_bytes(name.as_bytes()).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let value = HeaderValue::from_str(value).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok((name, value))
}

/// The wire request, built identically for the async and sync pools: path plus the encoded
/// query, `Host`, JSON content type, auth, the caller's headers (replacing), then the client
/// defaults for anything not set. Returns it with its path+query and where the path ends.
#[allow(clippy::too_many_arguments)]
fn build_request(
    host: &HeaderValue,
    default_headers: &HeaderMap,
    method: &Bound<'_, PyString>,
    path: &str,
    query: Option<Bound<'_, PyAny>>,
    headers: Option<Bound<'_, PyDict>>,
    auth: Option<&str>,
    body: Option<Bound<'_, PyBytes>>,
    json_body: bool,
) -> PyResult<(Request<Full<Bytes>>, String, usize)> {
    let mut uri = path.to_owned();
    let path_end = uri.find('?').unwrap_or(uri.len());
    if let Some(query) = query.filter(|q| q.len().is_ok_and(|n| n > 0)) {
        let had_query = path_end < uri.len();
        if !had_query {
            uri.push('?'); // with a query already there, the serializer adds the '&'
        }
        let mut serializer = form_urlencoded::Serializer::for_suffix(uri, path_end + 1);
        append_query(&mut serializer, &query)?;
        uri = serializer.finish();
    }
    let method_value = match method.to_str()? {
        "GET" => Method::GET,
        "POST" => Method::POST,
        other => Method::from_bytes(other.as_bytes()).map_err(|e| PyValueError::new_err(e.to_string()))?,
    };
    let body = body.map_or_else(Bytes::new, |b| Bytes::copy_from_slice(b.as_bytes()));
    let mut request = Request::builder()
        .method(method_value)
        .uri(uri.as_str())
        .body(Full::new(body))
        .map_err(|e| PyValueError::new_err(e.to_string()))?;
    let map = request.headers_mut();
    map.insert(HOST, host.clone());
    if json_body {
        map.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
    }
    if let Some(auth) = auth {
        let mut value = HeaderValue::from_str(auth).map_err(|e| PyValueError::new_err(e.to_string()))?;
        value.set_sensitive(true);
        map.insert(http::header::AUTHORIZATION, value);
    }
    if let Some(headers) = headers {
        for (name, value) in headers.iter() {
            let (name, value) = header_pair(name.cast::<PyString>()?.to_str()?, value.cast::<PyString>()?.to_str()?)?;
            map.insert(name, value);
        }
    }
    for (name, value) in default_headers {
        map.entry(name).or_insert_with(|| value.clone());
    }
    Ok((request, uri, path_end))
}

impl Pool {
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

    fn release(&mut self, slot: usize) -> Meta {
        let entry = &mut self.slots[slot];
        entry.generation = entry.generation.wrapping_add(1);
        self.free.push(slot);
        entry.meta.take().expect("released slot is live")
    }

    fn is_live(&self, slot: usize, generation: u32) -> bool {
        let entry = &self.slots[slot];
        entry.generation == generation && entry.meta.is_some()
    }

    fn meta(&mut self, slot: usize) -> &mut Meta {
        self.slots[slot].meta.as_mut().expect("slot is live")
    }

    fn start(&mut self, conn: usize, slot: usize) {
        let mut sender = self.conns[conn].sender.take().expect("idle connection has its sender");
        self.conns[conn].current = Some(slot);
        let request = self.meta(slot).request.take().expect("request not yet started");
        let done = self.done.clone();
        self.spawner
            .spawn_local(async move {
                let result = fetch(&mut sender, request).await;
                done.borrow_mut().push(Completion { conn, slot, sender, result });
            })
            .expect("spawn");
    }

    /// Open connections until every waiting request has one coming.
    fn ensure_connections(&mut self, py: Python<'_>) -> PyResult<()> {
        while self.waiting.len() > self.connecting {
            let conn = match self.conns.iter().position(|c| c.state == State::Dead) {
                Some(conn) => conn,
                None => {
                    self.conns.push(Conn {
                        fd: -1,
                        generation: 0,
                        state: State::Dead,
                        sender: None,
                        flags: Rc::new(RefCell::new(IoFlags::default())),
                        current: None,
                    });
                    self.conns.len() - 1
                }
            };
            self.conns[conn].state = State::Connecting;
            self.connecting += 1;
            self.glue.spawn_connect.call1(py, (conn,))?;
        }
        Ok(())
    }

    /// Take a connection out of service; Python unregisters and closes its socket.
    fn retire(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        let c = &mut self.conns[conn];
        c.state = State::Dead;
        c.sender = None;
        c.current = None;
        c.fd = -1;
        c.generation = c.generation.wrapping_add(1);
        self.idle.retain(|i| *i != conn);
        self.glue.close_conn.call1(py, (conn,))?;
        self.ensure_connections(py)
    }

    /// A free connection: give it the next waiting request, else mark it idle.
    fn dispatch(&mut self, conn: usize) {
        match self.waiting.pop_front() {
            Some(next) => self.start(conn, next),
            None => self.idle.push_back(conn),
        }
    }

    /// Make sure the one timer fires no later than `deadline`.
    fn arm(&mut self, py: Python<'_>, deadline: Instant) -> PyResult<()> {
        if let Some((at, handle)) = &self.armed {
            if *at <= deadline {
                return Ok(());
            }
            handle.bind(py).call_method0(intern!(py, "cancel"))?;
        }
        let Some(callback) = &self.glue.on_deadline else { return Ok(()) };
        let delay = deadline.saturating_duration_since(Instant::now()).as_secs_f64();
        let handle = self.glue.call_later.call1(py, (delay, callback.clone_ref(py)))?;
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

    fn request_info<'py>(&self, py: Python<'py>, meta: &Meta) -> PyResult<Bound<'py, PyAny>> {
        let url = format!("{}{}", self.origin, meta.uri);
        let path = &meta.uri[..meta.path_end];
        self.glue.request_info.bind(py).call1((meta.method.bind(py), url, path, self.host_str.bind(py)))
    }

    fn transport_error(&self, py: Python<'_>, msg: String) -> PyResult<PyErr> {
        Ok(PyErr::from_value(self.glue.connection_error.bind(py).call1((msg,))?))
    }

    /// What lothc's `get()` would have returned or raised for this request.
    fn outcome<'py>(&self, py: Python<'py>, meta: &Meta, result: Result<Head, Failure>) -> PyResult<Bound<'py, PyAny>> {
        let head = match result {
            Ok(head) => head,
            Err(Failure::Timeout) => {
                return Err(PyErr::from_value(self.glue.timeout_error.bind(py).call1(("request timed out",))?))
            }
            Err(Failure::Transport(msg)) => return Err(self.transport_error(py, msg)?),
            Err(Failure::Unsent(_)) => unreachable!("unsent requests are re-queued, not delivered"),
        };
        if self.bare && head.status < 400 {
            let raw = PyBytes::new(py, &head.body);
            return match &meta.decode {
                Some(decode) => decode.bind(py).call1((raw,)),
                None => Ok(raw.into_any()),
            };
        }
        let elapsed = meta.started.elapsed().as_secs_f64();
        let request_info = self.request_info(py, meta)?;
        let raw = PyBytes::new(py, &head.body);
        let headers = self.glue.lazy_headers.bind(py).call1((RawHeaders { map: head.headers },))?;
        if head.status >= 400 {
            if let Some(raises) = &meta.raises {
                let error = self.glue.status_error.bind(py).call1((raises.bind(py), head.status, raw, request_info, headers))?;
                return Err(PyErr::from_value(error));
            }
        }
        let data = match &meta.decode {
            Some(decode) => decode.bind(py).call1((raw,))?,
            None => raw.into_any(),
        };
        self.glue.response.bind(py).call1((data, head.status, headers, py.None(), request_info, self.glue.http_11.bind(py), elapsed))
    }

    fn settle(&self, py: Python<'_>, meta: &Meta, outcome: PyResult<Bound<'_, PyAny>>) -> PyResult<()> {
        let fut = meta.fut.bind(py);
        if fut.call_method0(intern!(py, "done"))?.is_truthy()? {
            return Ok(()); // the caller cancelled
        }
        match outcome {
            Ok(value) => fut.call_method1(intern!(py, "set_result"), (value,))?,
            Err(err) => fut.call_method1(intern!(py, "set_exception"), (err.into_value(py),))?,
        };
        Ok(())
    }

    fn deliver(&self, py: Python<'_>, meta: Meta, result: Result<Head, Failure>) -> PyResult<()> {
        let outcome = if meta.cancelled { Ok(py.None().into_bound(py)) } else { self.outcome(py, &meta, result) };
        self.settle(py, &meta, outcome)
    }

    /// Poll until stalled, resolve what finished, hand freed connections to waiters; repeat.
    fn run(&mut self, py: Python<'_>) -> PyResult<()> {
        loop {
            self.exec.run_until_stalled();
            let closed = std::mem::take(&mut *self.closed.borrow_mut());
            let finished = std::mem::take(&mut *self.done.borrow_mut());
            if closed.is_empty() && finished.is_empty() {
                return Ok(());
            }
            for (conn, generation) in closed {
                // An idle keep-alive connection the server closed. A busy one fails its own
                // request, which retires it below.
                let c = &self.conns[conn];
                if c.generation == generation && c.state == State::Ready && c.current.is_none() {
                    self.retire(py, conn)?;
                }
            }
            for Completion { conn, slot, sender, result } in finished {
                self.conns[conn].current = None;
                if let Err(Failure::Unsent(request)) = result {
                    let meta = self.meta(slot);
                    if meta.timed_out || meta.cancelled {
                        let meta = self.release(slot);
                        let result = if meta.timed_out { Err(Failure::Timeout) } else { Err(Failure::Transport(String::new())) };
                        self.deliver(py, meta, result)?;
                    } else {
                        meta.request = Some(request);
                        self.waiting.push_front(slot);
                    }
                    drop(sender);
                    self.retire(py, conn)?;
                    continue;
                }
                let meta = self.release(slot);
                // A failed, timed-out or cancelled exchange leaves the connection unusable.
                let healthy = result.is_ok() && !meta.timed_out && !meta.cancelled;
                let result = if meta.timed_out { Err(Failure::Timeout) } else { result };
                self.deliver(py, meta, result)?;
                if healthy {
                    self.conns[conn].sender = Some(sender);
                    self.dispatch(conn);
                } else {
                    drop(sender);
                    self.retire(py, conn)?;
                }
            }
            self.prune_deadlines();
        }
    }

    /// Fail an in-flight request's exchange by shutting its socket (reqwest drops the
    /// connection too); its completion then reports why.
    fn abort_in_flight(&mut self, conn: usize) {
        unsafe { libc::shutdown(self.conns[conn].fd, libc::SHUT_RDWR) };
    }
}

#[pymethods]
impl Pool {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        host: &str,
        origin: String,
        user_agent: &str,
        event_loop: &Bound<'_, PyAny>,
        spawn_connect: Py<PyAny>,
        close_conn: Py<PyAny>,
        response: Py<PyAny>,
        request_info: Py<PyAny>,
        lazy_headers: Py<PyAny>,
        status_error: Py<PyAny>,
        connection_error: Py<PyAny>,
        timeout_error: Py<PyAny>,
        https: bool,
        root_certificates: Vec<Vec<u8>>,
        danger_accept_invalid_certs: bool,
    ) -> PyResult<Self> {
        let exec = LocalPool::new();
        let spawner = exec.spawner();
        let hostname = host.split(':').next().unwrap_or(host);
        let tls = if https {
            let name = ServerName::try_from(hostname.to_owned()).map_err(|e| PyValueError::new_err(e.to_string()))?;
            Some((tls_config(&root_certificates, danger_accept_invalid_certs)?, name))
        } else {
            None
        };
        let mut default_headers = HeaderMap::new();
        default_headers.insert(USER_AGENT, HeaderValue::from_str(user_agent).map_err(|e| PyValueError::new_err(e.to_string()))?);
        default_headers.insert(ACCEPT, HeaderValue::from_static("*/*"));
        Ok(Self {
            exec,
            spawner,
            conns: Vec::new(),
            connecting: 0,
            idle: VecDeque::new(),
            waiting: VecDeque::new(),
            slots: Vec::new(),
            free: Vec::new(),
            deadlines: BinaryHeap::new(),
            armed: None,
            done: Rc::new(RefCell::new(Vec::new())),
            closed: Rc::new(RefCell::new(Vec::new())),
            host: HeaderValue::from_str(host).map_err(|e| PyValueError::new_err(e.to_string()))?,
            host_str: PyString::new(py, hostname).unbind(),
            origin,
            default_headers,
            tls,
            bare: false,
            glue: Glue {
                create_future: event_loop.getattr("create_future")?.unbind(),
                call_later: event_loop.getattr("call_later")?.unbind(),
                spawn_connect,
                close_conn,
                on_deadline: None,
                response,
                request_info,
                lazy_headers,
                status_error,
                connection_error,
                timeout_error,
                http_11: PyString::intern(py, "HTTP/1.1").unbind(),
            },
        })
    }

    /// The bound `on_deadline`, handed back once so arming doesn't look it up.
    fn set_deadline_callback(&mut self, callback: Py<PyAny>) {
        self.glue.on_deadline = Some(callback);
    }

    fn set_bare(&mut self, bare: bool) {
        self.bare = bare;
    }

    /// Start a request and return its asyncio Future. `query` is lothc's `_query_pairs`
    /// output, `headers` its `_encode_headers` output, `body` already-encoded bytes.
    #[pyo3(signature = (method, path, query, headers, auth, body, json_body, decode, timeout, raises))]
    #[allow(clippy::too_many_arguments)]
    fn request(
        &mut self,
        py: Python<'_>,
        method: Bound<'_, PyString>,
        path: &str,
        query: Option<Bound<'_, PyAny>>,
        headers: Option<Bound<'_, PyDict>>,
        auth: Option<&str>,
        body: Option<Bound<'_, PyBytes>>,
        json_body: bool,
        decode: Option<Py<PyAny>>,
        timeout: Option<f64>,
        raises: Option<Py<PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let (request, uri, path_end) =
            build_request(&self.host, &self.default_headers, &method, path, query, headers, auth, body, json_body)?;

        let fut = self.glue.create_future.call0(py)?;
        let started = Instant::now();
        let meta = Meta {
            request: Some(request),
            method: method.unbind(),
            uri,
            path_end,
            fut: fut.clone_ref(py),
            decode,
            raises,
            started,
            timed_out: false,
            cancelled: false,
        };
        let (slot, generation) = self.alloc(meta);
        if let Some(t) = timeout {
            let deadline = started + Duration::from_secs_f64(t);
            self.deadlines.push(Reverse((deadline, slot, generation)));
            self.arm(py, deadline)?;
        }
        match self.idle.pop_front() {
            Some(conn) => {
                self.start(conn, slot);
                self.run(py)?;
            }
            None => {
                self.waiting.push_back(slot);
                self.ensure_connections(py)?;
            }
        }
        Ok(fut)
    }

    /// Python finished `sock_connect` for connection `conn`.
    fn connected(&mut self, py: Python<'_>, conn: usize, fd: RawFd) -> PyResult<()> {
        self.connecting -= 1;
        let flags = Rc::new(RefCell::new(IoFlags::default()));
        let runtime_error = |e: &dyn std::fmt::Display| pyo3::exceptions::PyRuntimeError::new_err(e.to_string());
        // http1's handshake does no I/O: it is ready on the first poll. The TLS handshake
        // happens lazily, driven by the first request's write and reads.
        let (closed, generation) = (self.closed.clone(), self.conns[conn].generation);
        let sender = match &self.tls {
            None => {
                let io = LoopSocket { fd, flags: flags.clone() };
                let (sender, driver) = futures_executor::block_on(http1::handshake(io)).map_err(|e| runtime_error(&e))?;
                self.spawner.spawn_local(async move {
                    let _ = driver.await;
                    closed.borrow_mut().push((conn, generation));
                }).expect("spawn");
                sender
            }
            Some((config, name)) => {
                let tls = ClientConnection::new(config.clone(), name.clone()).map_err(|e| runtime_error(&e))?;
                let io = TlsSocket { fd, flags: flags.clone(), tls, rx: vec![0; 16 * 1024 + 256].into_boxed_slice() };
                let (sender, driver) = futures_executor::block_on(http1::handshake(io)).map_err(|e| runtime_error(&e))?;
                self.spawner.spawn_local(async move {
                    let _ = driver.await;
                    closed.borrow_mut().push((conn, generation));
                }).expect("spawn");
                sender
            }
        };
        let c = &mut self.conns[conn];
        c.fd = fd;
        c.flags = flags;
        c.sender = Some(sender);
        c.state = State::Ready;
        self.dispatch(conn);
        self.run(py)
    }

    /// `sock_connect` failed: the oldest waiting request gets the error (reqwest fails the
    /// request whose connect failed).
    fn connect_failed(&mut self, py: Python<'_>, conn: usize, message: String) -> PyResult<()> {
        self.connecting -= 1;
        let c = &mut self.conns[conn];
        c.state = State::Dead;
        c.generation = c.generation.wrapping_add(1);
        if self.waiting.len() > self.connecting {
            if let Some(slot) = self.waiting.pop_front() {
                let meta = self.release(slot);
                let err = self.transport_error(py, message)?;
                self.settle(py, &meta, Err(err))?;
            }
        }
        Ok(())
    }

    fn on_readable(&mut self, py: Python<'_>, conn: usize) -> PyResult<()> {
        let waker = {
            let mut flags = self.conns[conn].flags.borrow_mut();
            flags.readable = true;
            flags.read_waker.take()
        };
        if let Some(w) = waker {
            w.wake();
        }
        self.run(py)
    }

    /// The caller's task was cancelled while awaiting `fut`.
    fn cancel(&mut self, py: Python<'_>, fut: &Bound<'_, PyAny>) -> PyResult<()> {
        if let Some(pos) = self.waiting.iter().position(|s| self.slots[*s].meta.as_ref().is_some_and(|m| m.fut.is(fut))) {
            let slot = self.waiting.remove(pos).expect("position is in range");
            self.release(slot);
            return Ok(());
        }
        let found = self.conns.iter().enumerate().find_map(|(i, c)| {
            c.current.filter(|s| self.slots[*s].meta.as_ref().is_some_and(|m| m.fut.is(fut))).map(|s| (i, s))
        });
        if let Some((conn, slot)) = found {
            self.meta(slot).cancelled = true;
            self.abort_in_flight(conn);
        }
        self.run(py)
    }

    /// The shared timer fired: expire everything due, then re-arm for the next deadline.
    fn on_deadline(&mut self, py: Python<'_>) -> PyResult<()> {
        self.armed = None;
        let now = Instant::now();
        while let Some(Reverse((deadline, slot, generation))) = self.deadlines.peek().copied() {
            if deadline > now {
                self.arm(py, deadline)?;
                break;
            }
            self.deadlines.pop();
            if !self.is_live(slot, generation) {
                continue;
            }
            if let Some(conn) = self.conns.iter().position(|c| c.current == Some(slot)) {
                // In flight: shutting the socket down fails hyper's read (EOF), and the
                // completion reports it as the timeout.
                self.meta(slot).timed_out = true;
                self.abort_in_flight(conn);
            } else if let Some(pos) = self.waiting.iter().position(|s| *s == slot) {
                self.waiting.remove(pos);
                let meta = self.release(slot);
                self.deliver(py, meta, Err(Failure::Timeout))?;
            }
        }
        self.run(py)
    }
}


// ---------------------------------------------------------------------------------------
// Sync client spike. Same hyper client and request building; instead of the asyncio loop,
// the calling thread waits for its own socket with poll(2), GIL released, bounded by the
// request's deadline. The socket stays non-blocking: hyper polls it between requests to
// notice a server close, which a blocking read would turn into a hang.
// ---------------------------------------------------------------------------------------

/// Readiness for the sync pool: `readable` is set after poll(2) reports the fd readable.
struct SyncSocket {
    fd: RawFd,
    readable: Arc<AtomicBool>,
}

impl hyper::rt::Read for SyncSocket {
    fn poll_read(self: Pin<&mut Self>, _cx: &mut Context<'_>, mut buf: hyper::rt::ReadBufCursor<'_>) -> Poll<io::Result<()>> {
        if !self.readable.load(Ordering::Relaxed) {
            return Poll::Pending; // the request loop polls the fd, then re-polls everything
        }
        let dst = unsafe { buf.as_mut() };
        let n = unsafe { libc::recv(self.fd, dst.as_mut_ptr().cast(), dst.len(), 0) };
        if n < 0 {
            let err = io::Error::last_os_error();
            if err.kind() == io::ErrorKind::WouldBlock {
                self.readable.store(false, Ordering::Relaxed);
                return Poll::Pending;
            }
            return Poll::Ready(Err(err));
        }
        let n = n as usize;
        if n > 0 && n < dst.len() {
            self.readable.store(false, Ordering::Relaxed);
        }
        unsafe { buf.advance(n) };
        Poll::Ready(Ok(()))
    }
}

impl hyper::rt::Write for SyncSocket {
    fn poll_write(self: Pin<&mut Self>, _cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        Poll::Ready(FdWriter(self.fd).write(buf))
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_shutdown(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }
}

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

struct SyncConn {
    _stream: TcpStream, // owns the fd
    fd: RawFd,
    readable: Arc<AtomicBool>,
    sender: Sender,
    driver: Pin<Box<dyn Future<Output = ()> + Send>>,
    closed: bool,
}

/// Drive `conn`'s hyper driver and one request until the request finishes.
fn run_on(conn: &mut SyncConn, request: Request<Full<Bytes>>, deadline: Option<Instant>) -> Result<Head, Failure> {
    let flag = Arc::new(FlagWaker(AtomicBool::new(false)));
    let waker = Waker::from(flag.clone());
    let mut cx = Context::from_waker(&waker);
    let SyncConn { fd, readable, sender, driver, closed, .. } = conn;
    let mut fut = std::pin::pin!(fetch(sender, request));
    loop {
        flag.0.store(false, Ordering::Relaxed);
        if !*closed && driver.as_mut().poll(&mut cx).is_ready() {
            *closed = true;
        }
        if let Poll::Ready(result) = fut.as_mut().poll(&mut cx) {
            return result;
        }
        if flag.0.load(Ordering::Relaxed) {
            continue;
        }
        let timeout_ms = match deadline {
            Some(d) => {
                let left = d.saturating_duration_since(Instant::now());
                if left.is_zero() {
                    return Err(Failure::Timeout);
                }
                left.as_millis().clamp(1, i32::MAX as u128) as i32
            }
            None => -1,
        };
        let mut pfd = libc::pollfd { fd: *fd, events: libc::POLLIN, revents: 0 };
        match unsafe { libc::poll(&mut pfd, 1, timeout_ms) } {
            0 => return Err(Failure::Timeout),
            n if n < 0 => {
                let err = io::Error::last_os_error();
                if err.kind() != io::ErrorKind::Interrupted {
                    return Err(Failure::Transport(err.to_string()));
                }
            }
            _ => readable.store(true, Ordering::Relaxed),
        }
    }
}

/// lothc's result types, shared by both pools' result building.
struct Types {
    response: Py<PyAny>,
    request_info: Py<PyAny>,
    lazy_headers: Py<PyAny>,
    status_error: Py<PyAny>,
    connection_error: Py<PyAny>,
    timeout_error: Py<PyAny>,
    http_11: Py<PyString>,
}

#[pyclass(frozen)]
struct SyncPool {
    addr: (String, u16),
    host: HeaderValue,
    host_str: Py<PyString>,
    origin: String,
    default_headers: HeaderMap,
    idle: Mutex<Vec<SyncConn>>,
    types: Types,
}

/// Whether a pooled connection is still usable: nothing reads an idle socket here (no event
/// loop), so a server's silent close is only noticed by looking, as urllib3 does before reuse.
fn still_open(conn: &mut SyncConn) -> bool {
    let mut pfd = libc::pollfd { fd: conn.fd, events: libc::POLLIN, revents: 0 };
    if unsafe { libc::poll(&mut pfd, 1, 0) } > 0 {
        // EOF (or stray bytes): let hyper's driver read it and decide.
        conn.readable.store(true, Ordering::Relaxed);
        let mut cx = Context::from_waker(Waker::noop());
        if conn.driver.as_mut().poll(&mut cx).is_ready() {
            conn.closed = true;
        }
    }
    !conn.closed && !conn.sender.is_closed()
}

impl SyncPool {
    fn connect(&self, deadline: Option<Instant>) -> Result<SyncConn, Failure> {
        let transport = |e: io::Error| Failure::Transport(error_chain(&e));
        let addr = self.addr.to_socket_addrs().map_err(transport)?.next()
            .ok_or_else(|| Failure::Transport("no address".into()))?;
        let stream = match deadline {
            Some(d) => TcpStream::connect_timeout(&addr, d.saturating_duration_since(Instant::now()).max(Duration::from_millis(1))),
            None => TcpStream::connect(addr),
        }
        .map_err(transport)?;
        stream.set_nodelay(true).map_err(transport)?;
        stream.set_nonblocking(true).map_err(transport)?;
        let fd = stream.as_raw_fd();
        let readable = Arc::new(AtomicBool::new(false));
        let io = SyncSocket { fd, readable: readable.clone() };
        let (sender, driver) = futures_executor::block_on(http1::handshake(io)).map_err(|e| Failure::Transport(error_chain(&e)))?;
        let driver: Pin<Box<dyn Future<Output = ()> + Send>> = Box::pin(async move {
            let _ = driver.await;
        });
        Ok(SyncConn { _stream: stream, fd, readable, sender, driver, closed: false })
    }

    /// Take an idle connection (or open one), run the request, put the connection back if it
    /// is still good. A request hyper hands back unsent goes out again on a fresh connection.
    fn send_blocking(&self, mut request: Request<Full<Bytes>>, deadline: Option<Instant>) -> Result<Head, Failure> {
        for _ in 0..3 {
            let mut pooled = None;
            while let Some(mut conn) = self.idle.lock().unwrap_or_else(|e| e.into_inner()).pop() {
                if still_open(&mut conn) {
                    pooled = Some(conn);
                    break;
                }
            }
            let mut conn = match pooled {
                Some(conn) => conn,
                None => self.connect(deadline)?,
            };
            match run_on(&mut conn, request, deadline) {
                Ok(head) => {
                    if !conn.closed {
                        self.idle.lock().unwrap_or_else(|e| e.into_inner()).push(conn);
                    }
                    return Ok(head);
                }
                Err(Failure::Unsent(unsent)) => request = unsent,
                Err(other) => return Err(other),
            }
        }
        Err(Failure::Transport("connection closed before the request could be sent".into()))
    }
}

#[pymethods]
impl SyncPool {
    #[new]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        host: &str,
        port: u16,
        origin: String,
        user_agent: &str,
        response: Py<PyAny>,
        request_info: Py<PyAny>,
        lazy_headers: Py<PyAny>,
        status_error: Py<PyAny>,
        connection_error: Py<PyAny>,
        timeout_error: Py<PyAny>,
    ) -> PyResult<Self> {
        let netloc = origin.split("://").nth(1).unwrap_or(host).to_owned();
        let mut default_headers = HeaderMap::new();
        default_headers.insert(USER_AGENT, HeaderValue::from_str(user_agent).map_err(|e| PyValueError::new_err(e.to_string()))?);
        default_headers.insert(ACCEPT, HeaderValue::from_static("*/*"));
        Ok(Self {
            addr: (host.to_owned(), port),
            host: HeaderValue::from_str(&netloc).map_err(|e| PyValueError::new_err(e.to_string()))?,
            host_str: PyString::new(py, host).unbind(),
            origin,
            default_headers,
            idle: Mutex::new(Vec::new()),
            types: Types {
                response,
                request_info,
                lazy_headers,
                status_error,
                connection_error,
                timeout_error,
                http_11: PyString::intern(py, "HTTP/1.1").unbind(),
            },
        })
    }

    /// One blocking request: same arguments as `Pool.request`, returns lothc's `Response`.
    #[pyo3(signature = (method, path, query, headers, auth, body, json_body, decode, timeout, raises))]
    #[allow(clippy::too_many_arguments)]
    fn request<'py>(
        &self,
        py: Python<'py>,
        method: Bound<'py, PyString>,
        path: &str,
        query: Option<Bound<'py, PyAny>>,
        headers: Option<Bound<'py, PyDict>>,
        auth: Option<&str>,
        body: Option<Bound<'py, PyBytes>>,
        json_body: bool,
        decode: Option<Bound<'py, PyAny>>,
        timeout: Option<f64>,
        raises: Option<Bound<'py, PyAny>>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let (request, uri, path_end) =
            build_request(&self.host, &self.default_headers, &method, path, query, headers, auth, body, json_body)?;
        let started = Instant::now();
        let deadline = timeout.map(|t| started + Duration::from_secs_f64(t));
        let result = py.detach(|| self.send_blocking(request, deadline));
        let t = &self.types;
        let head = match result {
            Ok(head) => head,
            Err(Failure::Timeout) => return Err(PyErr::from_value(t.timeout_error.bind(py).call1(("request timed out",))?)),
            Err(Failure::Transport(msg)) => return Err(PyErr::from_value(t.connection_error.bind(py).call1((msg,))?)),
            Err(Failure::Unsent(_)) => unreachable!("send_blocking retries unsent requests"),
        };
        let elapsed = started.elapsed().as_secs_f64();
        let url = format!("{}{}", self.origin, uri);
        let request_info = t.request_info.bind(py).call1((&method, url, &uri[..path_end], self.host_str.bind(py)))?;
        let raw = PyBytes::new(py, &head.body);
        let headers = t.lazy_headers.bind(py).call1((RawHeaders { map: head.headers },))?;
        if head.status >= 400 {
            if let Some(raises) = &raises {
                let error = t.status_error.bind(py).call1((raises, head.status, raw, request_info, headers))?;
                return Err(PyErr::from_value(error));
            }
        }
        let data = match &decode {
            Some(decode) => decode.call1((raw,))?,
            None => raw.into_any(),
        };
        t.response.bind(py).call1((data, head.status, headers, py.None(), request_info, t.http_11.bind(py), elapsed))
    }
}

#[pymodule]
fn _poc(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Pool>()?;
    m.add_class::<SyncPool>()?;
    m.add_class::<RawHeaders>()
}
