//! What a finished exchange becomes: exactly what lothc's pyreqwest path returns or raises, built
//! in one pass from lothc's own classes and helpers (handed over as `glue`).
use std::time::Instant;

use bytes::Bytes;
use http::{HeaderMap, Response, Version};
use http_body_util::BodyExt;
use hyper::body::Incoming;
use hyper::client::conn::http1::SendRequest;
use pyo3::exceptions::PyRuntimeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString, PyTuple};
use url::Url;

use crate::body::RequestBody;
use crate::decompress;
use crate::error::{error_chain, Failure};
use crate::headers::RawHeaders;
use crate::redirect::{Hop, Redirect};

pub(crate) type Sender = SendRequest<RequestBody>;

/// The status line, headers and whole body of a response.
pub(crate) struct Head {
    pub(crate) status: u16,
    pub(crate) version: Version,
    pub(crate) headers: HeaderMap,
    pub(crate) body: Bytes,
}

/// Send one request on a ready connection and wait for the response's head. A request hyper
/// hands back unsent (the connection was already closing) comes back as `Failure::Unsent`.
pub(crate) async fn send(sender: &mut Sender, request: http::Request<RequestBody>) -> Result<Response<Incoming>, Failure> {
    if sender.ready().await.is_err() {
        return Err(Failure::Unsent(Box::new(request)));
    }
    match sender.try_send_request(request).await {
        Ok(response) => Ok(response),
        Err(mut error) => Err(match error.take_message() {
            Some(request) => Failure::Unsent(Box::new(request)),
            None => Failure::Transport(error_chain(&error.into_error())),
        }),
    }
}

/// What one exchange of a request came to: its final response, or a redirect to follow.
pub(crate) enum Fetched {
    Head(Head),
    /// `redirect` now describes the hop's request; the redirect response went unread.
    Hop(Hop),
}

/// Send one request on a ready connection and read the whole response (see `send`), unless
/// `redirect` (the request's redirect state, when the client follows redirects) says to follow it.
pub(crate) async fn fetch(sender: &mut Sender, request: http::Request<RequestBody>, redirect: Option<&mut Redirect>) -> Result<Fetched, Failure> {
    let response = send(sender, request).await?;
    if let Some(redirect) = redirect {
        if let Some(hop) = redirect.next(response.status().as_u16(), response.headers())? {
            return Ok(Fetched::Hop(hop));
        }
    }
    let (mut parts, body) = response.into_parts();
    let body = match decompress::coding(&parts.headers) {
        None => body.collect().await.map_err(|e| Failure::Transport(error_chain(&e)))?.to_bytes(),
        Some(coding) => decompress::read(coding, &mut parts.headers, body).await?,
    };
    Ok(Fetched::Head(Head { status: parts.status.as_u16(), version: parts.version, headers: parts.headers, body }))
}

/// One request's Python-facing settings, carried from `request()` to its completion.
pub(crate) struct Exchange {
    pub(crate) method: Py<PyString>,
    pub(crate) url: Url,
    pub(crate) decode: Option<Py<PyAny>>,
    pub(crate) raises: Option<Py<PyAny>>,
    pub(crate) typed_headers: Option<Py<PyAny>>,
    pub(crate) headers_only: bool,
    pub(crate) started: Instant,
}

/// lothc's own classes and helpers, read once from the `glue` object the client passes in.
pub(crate) struct Glue {
    response: Py<PyAny>,
    request_info: Py<PyAny>,
    lazy_headers: Py<PyAny>,
    eager_headers: Py<PyAny>,
    status_error: Py<PyAny>,
    parse_typed_headers: Py<PyAny>,
    connection_error: Py<PyAny>,
    timeout_error: Py<PyAny>,
    transport_error: Py<PyAny>,
    builder_error: Py<PyAny>,
    decode_error: Py<PyAny>,
    http_10: Py<PyString>,
    http_11: Py<PyString>,
}

impl Glue {
    pub(crate) fn new(py: Python<'_>, glue: &Bound<'_, PyAny>) -> PyResult<Self> {
        let get = |name: &str| glue.getattr(name).map(Bound::unbind);
        Ok(Self {
            response: get("response")?,
            request_info: get("request_info")?,
            lazy_headers: get("lazy_headers")?,
            eager_headers: get("eager_headers")?,
            status_error: get("status_error")?,
            parse_typed_headers: get("parse_typed_headers")?,
            connection_error: get("connection_error")?,
            timeout_error: get("timeout_error")?,
            transport_error: get("transport_error")?,
            builder_error: get("builder_error")?,
            decode_error: get("decode_error")?,
            http_10: PyString::intern(py, "HTTP/1.0").unbind(),
            http_11: PyString::intern(py, "HTTP/1.1").unbind(),
        })
    }

    fn error(py: Python<'_>, class: &Py<PyAny>, message: String) -> PyErr {
        match class.bind(py).call1((message,)) {
            Ok(error) => PyErr::from_value(error),
            Err(error) => error,
        }
    }

    /// `HTTPTransportError`: no usable response (a redirect that can't be followed).
    pub(crate) fn transport_error(&self, py: Python<'_>, message: String) -> PyErr {
        Self::error(py, &self.transport_error, message)
    }

    /// `HTTPTransportError` for a request that can never be sent (reqwest's builder error),
    /// marked so neither a retry nor `sse()` tries it again.
    pub(crate) fn builder_error(&self, py: Python<'_>, message: String) -> PyErr {
        Self::error(py, &self.builder_error, message)
    }

    /// `HTTPConnectionError`: the connection failed, or was lost mid-request.
    pub(crate) fn connection_error(&self, py: Python<'_>, message: String) -> PyErr {
        Self::error(py, &self.connection_error, message)
    }

    /// `HTTPTimeoutError`: the request's total timeout elapsed.
    pub(crate) fn timeout_error(&self, py: Python<'_>) -> PyErr {
        Self::error(py, &self.timeout_error, "request timed out".to_owned())
    }

    /// The `RuntimeError` a request still in flight gets when its client closes.
    pub(crate) fn closed_error() -> PyErr {
        PyRuntimeError::new_err("The client is closed; it was used after its context exited")
    }

    /// lothc's `RequestInfo` for a request of `method` to `url`.
    pub(crate) fn request_info_of<'py>(&self, py: Python<'py>, method: &Bound<'py, PyString>, url: &Url) -> PyResult<Bound<'py, PyAny>> {
        self.request_info.bind(py).call1((method, url.as_str(), url.path(), url.host_str()))
    }

    fn request_info<'py>(&self, py: Python<'py>, exchange: &Exchange) -> PyResult<Bound<'py, PyAny>> {
        self.request_info_of(py, exchange.method.bind(py), &exchange.url)
    }

    /// The exception a failed exchange raises.
    pub(crate) fn failure(&self, py: Python<'_>, failure: Failure) -> PyErr {
        match failure {
            Failure::Timeout => self.timeout_error(py),
            Failure::Transport(message) => self.connection_error(py, message),
            // Marked, since the retry middleware doesn't retry it.
            Failure::Decode(message) => Self::error(py, &self.decode_error, message),
            Failure::Redirect(message) => self.transport_error(py, message),
            Failure::Builder(message) => self.builder_error(py, message),
            Failure::Unsent(_) => self.connection_error(py, "connection closed before the request could be sent".to_owned()),
            Failure::Closed => Self::closed_error(),
        }
    }

    /// A streamed response's head as `(status, RawHeaders, http_version)`.
    pub(crate) fn stream_head<'py>(&self, py: Python<'py>, status: u16, version: Version, headers: HeaderMap) -> PyResult<Bound<'py, PyTuple>> {
        let version = if version == Version::HTTP_10 { &self.http_10 } else { &self.http_11 };
        let headers = Bound::new(py, RawHeaders::new(headers))?;
        PyTuple::new(py, [status.into_pyobject(py)?.into_any(), headers.into_any(), version.bind(py).clone().into_any()])
    }

    /// What lothc's pyreqwest path returns (a `Response`) or raises for this exchange.
    pub(crate) fn outcome<'py>(
        &self,
        py: Python<'py>,
        exchange: &Exchange,
        result: Result<Head, Failure>,
    ) -> PyResult<Bound<'py, PyAny>> {
        let head = match result {
            Ok(head) => head,
            Err(failure) => return Err(self.failure(py, failure)),
        };
        let elapsed = exchange.started.elapsed().as_secs_f64();
        let request_info = self.request_info(py, exchange)?;
        let body = PyBytes::new(py, &head.body);
        if head.status >= 400 {
            if let Some(raises) = &exchange.raises {
                let headers = self.eager_headers.bind(py).call1((RawHeaders::new(head.headers),))?;
                let error = self.status_error.bind(py).call1((raises.bind(py), head.status, body, request_info, headers))?;
                return Err(PyErr::from_value(error));
            }
        }
        let headers = self.lazy_headers.bind(py).call1((RawHeaders::new(head.headers),))?;
        let data = if exchange.headers_only {
            py.None().into_bound(py)
        } else {
            match &exchange.decode {
                Some(decode) => decode.bind(py).call1((body,))?,
                None => body.into_any(),
            }
        };
        let typed_headers = match &exchange.typed_headers {
            Some(header_type) => self.parse_typed_headers.bind(py).call1((&headers, header_type.bind(py)))?,
            None => py.None().into_bound(py),
        };
        let version = if head.version == Version::HTTP_10 { &self.http_10 } else { &self.http_11 };
        self.response
            .bind(py)
            .call1((data, head.status, headers, typed_headers, request_info, version.bind(py), elapsed))
    }

    /// Resolve `fut` with `outcome`, unless its caller already gave up on it (cancelled).
    pub(crate) fn settle(py: Python<'_>, fut: &Py<PyAny>, outcome: PyResult<Bound<'_, PyAny>>) -> PyResult<()> {
        let fut = fut.bind(py);
        if fut.call_method0(intern!(py, "done"))?.is_truthy()? {
            return Ok(());
        }
        match outcome {
            Ok(value) => fut.call_method1(intern!(py, "set_result"), (value,))?,
            Err(error) => fut.call_method1(intern!(py, "set_exception"), (error.into_value(py),))?,
        };
        Ok(())
    }
}
