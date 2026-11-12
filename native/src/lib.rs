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
//! - `mock`: how `lothc.testing`'s mocker answers a request a pool built, instead of the network;
//! - `headers`: `RawHeaders`, a response's headers until `_LazyHeaders` reads them.
//!
//! All of that is POSIX only (`poll(2)`, raw fds, `loop.add_reader`). On Windows, where
//! `native=True` raises, the module is `unsupported`: the same names, with nothing behind them.
#[cfg(unix)]
mod body;
#[cfg(unix)]
mod connect;
#[cfg(unix)]
mod cookie;
#[cfg(unix)]
mod decompress;
#[cfg(unix)]
mod error;
#[cfg(unix)]
mod headers;
#[cfg(unix)]
mod idle;
#[cfg(unix)]
mod io;
#[cfg(unix)]
mod limit;
#[cfg(unix)]
mod mock;
#[cfg(unix)]
mod multipart;
#[cfg(unix)]
mod pool;
#[cfg(unix)]
mod proxy;
#[cfg(unix)]
mod redirect;
#[cfg(unix)]
mod request;
#[cfg(unix)]
mod response;
#[cfg(unix)]
mod stream;
#[cfg(unix)]
mod sync_pool;
#[cfg(unix)]
mod tls;
#[cfg(not(unix))]
mod unsupported;

use pyo3::prelude::*;

// Free-threading safe without the GIL: `Pool` keeps its state in a `ThreadBound`, usable only on
// the thread that created it (another thread gets a `RuntimeError`), and a `Stream` and an
// `Intercepted` reach only that state; `SyncPool` is frozen, with its mutable state (idle
// connections, the lazily built TLS config, the cookie jar) behind locks and atomics, and so are
// a `SyncStream`, whose state no call holds locked while it waits, and a `SyncIntercepted`;
// `RawHeaders` is frozen and immutable, and the mock hook sits behind a lock and an atomic.
#[cfg(unix)]
#[pymodule(gil_used = false)]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<pool::Pool>()?;
    m.add_class::<pool::AsyncStream>()?;
    m.add_class::<sync_pool::SyncPool>()?;
    m.add_class::<sync_pool::SyncStream>()?;
    m.add_class::<headers::RawHeaders>()?;
    m.add_class::<connect::ConnectOptions>()?;
    m.add_class::<pool::Intercepted>()?;
    m.add_class::<sync_pool::SyncIntercepted>()?;
    m.add_function(wrap_pyfunction!(mock::set_mock_hook, m)?)?;
    m.add("ClientBuildError", m.py().get_type::<error::ClientBuildError>())
}

// The same names as above, so lothc imports this module unchanged on every platform.
#[cfg(not(unix))]
#[pymodule(gil_used = false)]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<unsupported::Pool>()?;
    m.add_class::<unsupported::Stream>()?;
    m.add_class::<unsupported::SyncPool>()?;
    m.add_class::<unsupported::SyncStream>()?;
    m.add_class::<unsupported::RawHeaders>()?;
    m.add_class::<unsupported::ConnectOptions>()?;
    m.add_class::<unsupported::Intercepted>()?;
    m.add_class::<unsupported::SyncIntercepted>()?;
    m.add_function(wrap_pyfunction!(unsupported::set_mock_hook, m)?)?;
    m.add("ClientBuildError", m.py().get_type::<unsupported::ClientBuildError>())
}
