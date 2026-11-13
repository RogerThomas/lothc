//! Why an exchange failed, before it becomes one of lothc's exceptions.
use http::Request;

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
}

impl Failure {
    /// Whether the connection is still good after this failure: a redirect that wasn't followed
    /// only drops its response, which hyper drains (or closes) as for any hop.
    pub(crate) fn keeps_connection(&self) -> bool {
        matches!(self, Self::Redirect(_) | Self::Builder(_))
    }
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
