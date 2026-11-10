//! `lothc._native`: lothc's own HTTP/1.1 transport, selected with `native=True` (see
//! `lothc/_transport_native.py`'s `_NativeTransport`). hyper's low-level `client::conn::http1`, after
//! fastpg's design: no tokio and no I/O thread. The async `Pool` runs on the asyncio loop's thread
//! over sockets the loop watches; the `SyncPool` drives the same futures on the calling thread.
//!
//! Requests are built exactly as lothc's pyreqwest path builds them (`request.rs`), and what a
//! caller gets back is built from lothc's own classes (`response.rs`), so the two transports agree
//! on the wire and in Python.
//!
//! - `connect`: how a connection's socket is set up (`resolve`, `local_address`, keepalive), and
//!   `connect_timeout`/`read_timeout`, as reqwest's connector does it;
//! - `io`: non-blocking sockets for hyper, and where their readiness comes from;
//! - `tls`: rustls over those sockets, configured as reqwest 0.13 configures it;
//! - `request`: URL joining, query/form/JSON encoding, header order;
//! - `response`: one exchange (`fetch`) and its result as lothc's `Response` or exception;
//! - `decompress`: `Accept-Encoding` and gzip/deflate/br/zstd response bodies, as reqwest's;
//! - `stream`: a streamed response body, read (and decoded) as it arrives;
//! - `pool`/`sync_pool`: connection pooling, timeouts and cancellation;
//! - `idle`: when an idle connection may be reused, as reqwest's pool decides it;
//! - `limit`: `max_connections`/`pool_timeout`, the slots pyreqwest gives a client's requests;
//! - `proxy`: which proxy a request goes through, and the proxy handshakes, as reqwest's;
//! - `redirect`: following redirects, as reqwest's default policy does;
//! - `cookie`: `cookie_store=True`'s cookie jar, as reqwest's;
//! - `headers`: `RawHeaders`, a response's headers until `_LazyHeaders` reads them.
mod body;
mod connect;
mod cookie;
mod decompress;
mod error;
mod headers;
mod idle;
mod io;
mod limit;
mod multipart;
mod pool;
mod proxy;
mod redirect;
mod request;
mod response;
mod stream;
mod sync_pool;
mod tls;

use pyo3::prelude::*;

// Free-threading safe without the GIL: `Pool` keeps its state in a `ThreadBound`, usable only on
// the thread that created it (another thread gets a `RuntimeError`), and a `Stream` reaches
// only that state; `SyncPool` is frozen, with its mutable state (idle connections, the lazily
// built TLS config, the cookie jar) behind locks and atomics, and so is a `SyncStream`, whose
// state no call holds locked while it waits; `RawHeaders` is frozen and immutable.
#[pymodule(gil_used = false)]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<pool::Pool>()?;
    m.add_class::<pool::AsyncStream>()?;
    m.add_class::<sync_pool::SyncPool>()?;
    m.add_class::<sync_pool::SyncStream>()?;
    m.add_class::<headers::RawHeaders>()?;
    m.add_class::<connect::ConnectOptions>()?;
    m.add("ClientBuildError", m.py().get_type::<error::ClientBuildError>())
}
