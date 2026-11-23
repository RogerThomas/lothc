//! Why an exchange failed, before it becomes one of lothc's exceptions.
use std::error::Error;
use std::io;

use http::Request;
use pyo3::prelude::*;

use crate::body::RequestBody;

pyo3::create_exception!(
    _native,
    ClientBuildError,
    pyo3::exceptions::PyValueError,
    "Settings a client can't be built with, raised where reqwest's `build()` raises its builder \
     error (a TLS version range rustls can't satisfy, a `user_agent` that isn't a header value), \
     with reqwest's message. The transport re-raises it as a plain `ValueError`, as the pyreqwest \
     one does pyreqwest's `BuilderError`."
);

pub(crate) enum Failure {
    /// hyper never sent it (the connection was already closing): safe to send again.
    /// Boxed: a request is far bigger than the other variants.
    Unsent(Box<Request<RequestBody>>),
    /// The connection failed (refused, reset, TLS, cut short): `HTTPConnectionError`.
    Transport(String),
    /// The request's total timeout elapsed: `HTTPTimeoutError`.
    Timeout,
    /// The whole body arrived but couldn't be decompressed: `HTTPConnectionError`, as reqwest's
    /// decode error is, but kept apart from `Transport` (where a body cut short lands), since
    /// only a cut-short body is worth retrying.
    Decode(String),
    /// A redirect that can't be followed (too many, or to plain http under `https_only`):
    /// `HTTPTransportError`, as reqwest's redirect error is.
    Redirect(String),
    /// A request that can never be sent (a URL scheme other than http(s)): `HTTPTransportError`,
    /// as reqwest's builder error is, marked permanent so `sse()` won't reconnect for it.
    Builder(String),
    /// The client closed while the request was running: the `RuntimeError` a request made after
    /// it closed raises.
    Closed,
    /// A streamed `form=` part's iterator raised it (`chunks.rs`): raised as it was, as
    /// pyreqwest re-raises an exception from a body stream.
    Raised(PyErr),
}

impl Failure {
    /// A failed exchange as hyper reports it: the exception a streamed part raised, if that's
    /// what failed its body, else the error with its causes.
    pub(crate) fn of_hyper(error: &hyper::Error) -> Self {
        match raised(error) {
            Some(error) => Self::Raised(error),
            None => Self::Transport(error_chain(error)),
        }
    }

    /// Whether the connection is still good after this failure: a redirect that wasn't followed
    /// only drops its response, which hyper drains (or closes) as for any hop.
    pub(crate) fn keeps_connection(&self) -> bool {
        matches!(self, Self::Redirect(_) | Self::Builder(_))
    }
}

/// The Python exception among `error`'s causes, if any: the `io::Error` a body's stream failed
/// with holds one when pulling a streamed part raised it (as pyreqwest finds one in reqwest's).
fn raised(error: &(dyn Error + 'static)) -> Option<PyErr> {
    let mut cause = Some(error);
    while let Some(error) = cause {
        let inner = error.downcast_ref::<io::Error>().and_then(io::Error::get_ref);
        if let Some(raised) = inner.and_then(|inner| inner.downcast_ref::<PyErr>()) {
            return Some(Python::attach(|py| raised.clone_ref(py)));
        }
        cause = error.source();
    }
    None
}

/// An error and its causes as one line. hyper's `Display` omits the causes, and lothc's tests
/// match on them (e.g. rustls's `UnknownIssuer` under "client error (Connect)").
pub(crate) fn error_chain(error: &dyn std::error::Error) -> String {
    let mut message = error.to_string();
    let mut source = error.source();
    while let Some(cause) = source {
        message.push_str(": ");
        message.push_str(&cause.to_string());
        source = cause.source();
    }
    message
}
