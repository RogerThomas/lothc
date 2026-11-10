//! The wire request, built exactly as lothc's pyreqwest path builds it, so both transports put the
//! same bytes on the wire. The order of operations is pyreqwest 0.15's and reqwest 0.13's:
//!
//! 1. the URL: `base_url.join(path)` (RFC 3986, the `url` crate), else `Url::parse(path)`, then
//!    any `user:pass@` taken out of it as a basic-auth header (reqwest's `RequestBuilder::new`);
//! 2. the query: appended through `Url::query_pairs_mut` (reqwest's `.query()`, which drops a
//!    `?` left empty), values written as serde_urlencoded writes them;
//! 3. headers, in pyreqwest's two maps: the client's `default_headers` and the request's own.
//!    Setting a header on the request (auth first, then `headers=`) removes it from the defaults,
//!    and the request's headers are appended to reqwest's own (which only a URL's userinfo, a
//!    `data=` body's content type, or a `form=` body's content type and length put there):
//!    `reqwest, then defaults, then the request`;
//! 4. a `json=` dict/list's content type, then reqwest's client defaults (`accept`, `user-agent`)
//!    where absent, then an HTTP proxy's `proxy-authorization` where absent (reqwest's
//!    `proxy_auth`, for an `http://` target only), then `accept-encoding` where absent
//!    (tower-http's `Decompression`), then `host` where absent (hyper-util's). hyper adds
//!    `content-length` last. The request target is origin-form, or the whole URI when the
//!    request goes to an HTTP proxy (`proxy.rs`).
//!
//! A redirect's hop (`redirect.rs`) starts from step 4's headers before `host`, as reqwest's
//! redirect layer copies them, and gets its own `host` and request target.
use std::net::IpAddr;
use std::time::{Duration, Instant};

use base64::prelude::BASE64_STANDARD;
use base64::write::EncoderWriter;
use bytes::Bytes;
use form_urlencoded::{Serializer, Target};
use http::header::{
    Entry, HeaderMap, HeaderName, HeaderValue, ACCEPT, ACCEPT_ENCODING, AUTHORIZATION, CONTENT_LENGTH, CONTENT_TYPE,
    HOST, PROXY_AUTHORIZATION, USER_AGENT,
};
use http::{Method, Request, Uri};
use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PySequence, PyString, PyTuple};
use rustls::pki_types::ServerName;
use url::{Host, Url};

use crate::body::RequestBody;
use crate::decompress;
use crate::error::ClientBuildError;
use crate::multipart::Form;
use crate::proxy::{Proxies, Route};
use crate::redirect::{Hop, Policy, Redirect};
use crate::response::Glue;

/// `body` is raw `bytes`/`str` (`content=`): no content type.
pub(crate) const BODY_RAW: u8 = 0;
/// `body` is a model's or Struct's JSON, already encoded: content type set like `headers=`.
pub(crate) const BODY_ENCODED_JSON: u8 = 1;
/// `body` is a `dict`/`list` to serialize here (pyreqwest's `body_json`).
pub(crate) const BODY_JSON: u8 = 2;
/// `body` is `data=` (a `dict`, or `(name, value)` pairs) to urlencode (reqwest's `.form()`).
pub(crate) const BODY_FORM: u8 = 3;
/// `body` is `form=` as `_NativeForm` describes it, a multipart body (`multipart.rs`).
pub(crate) const BODY_MULTIPART: u8 = 4;

/// Far below `HeaderMap`'s own limit, which panics rather than erroring in `extend`.
const MAX_HEADERS: usize = 8192;

/// Requests' deadlines as reqwest's timer sets them. A request's total `timeout` arrives as the
/// `Duration` pyo3 makes of a `timedelta`, as pyreqwest's builder takes it. tokio rounds each
/// deadline up to the end of its millisecond, counted from when its clock started, so a zero or
/// sub-millisecond timeout still leaves an exchange until that millisecond ends: on pyreqwest a
/// `timeout=0` request to a quick server usually succeeds, and to a slower one always times out.
#[derive(Clone, Copy)]
pub(crate) struct DeadlineClock {
    epoch: Instant,
}

impl DeadlineClock {
    pub(crate) fn new() -> Self {
        Self { epoch: Instant::now() }
    }

    /// When a request started at `started` with total `timeout` times out; `None` (no deadline)
    /// beyond what `Instant` can hold.
    pub(crate) fn deadline(self, started: Instant, timeout: Duration) -> Option<Instant> {
        let since = started.checked_add(timeout)?.saturating_duration_since(self.epoch);
        let millis = since.checked_add(Duration::from_nanos(999_999))?.as_millis();
        self.epoch.checked_add(Duration::from_millis(u64::try_from(millis).ok()?))
    }
}

/// Where a request's connection goes: pooled per scheme, host and port.
#[derive(Clone, PartialEq, Eq)]
pub(crate) struct OriginKey {
    pub(crate) https: bool,
    /// As the URL serializes it (an IPv6 address in brackets).
    pub(crate) host: String,
    pub(crate) port: u16,
}

impl OriginKey {
    pub(crate) fn matches(&self, url: &Url) -> bool {
        self.port == url.port_or_known_default().unwrap_or(0)
            && (self.https == (url.scheme() == "https"))
            && Some(self.host.as_str()) == url.host_str()
    }

    pub(crate) fn of(url: &Url) -> Self {
        Self {
            https: url.scheme() == "https",
            host: url.host_str().unwrap_or_default().to_owned(),
            port: url.port_or_known_default().unwrap_or(0),
        }
    }
}

/// The host to connect to (an IPv6 address without brackets) and the port.
pub(crate) fn connect_address(url: &Url) -> (String, u16) {
    let host = match url.host() {
        Some(Host::Domain(domain)) => domain.to_owned(),
        Some(Host::Ipv4(address)) => address.to_string(),
        Some(Host::Ipv6(address)) => address.to_string(),
        None => String::new(),
    };
    (host, url.port_or_known_default().unwrap_or(0))
}

/// The name an https origin's certificate is verified against (SNI too, for a domain).
pub(crate) fn server_name(url: &Url) -> Result<ServerName<'static>, String> {
    match url.host() {
        Some(Host::Domain(domain)) => ServerName::try_from(domain.to_owned()).map_err(|e| format!("invalid DNS name {domain:?}: {e}")),
        Some(Host::Ipv4(address)) => Ok(ServerName::IpAddress(IpAddr::V4(address).into())),
        Some(Host::Ipv6(address)) => Ok(ServerName::IpAddress(IpAddr::V6(address).into())),
        None => Err("the URL has no host".to_owned()),
    }
}

/// Headers a verb sets itself, each dropped from the defaults first: `before` ahead of the auth
/// and `headers=` (`sse()`'s `accept`), appended as pyreqwest's `.header()` does; `after` behind
/// them (its `last-event-id`), replacing a same-named `headers=` value as `.headers()` does.
#[derive(Default)]
pub(crate) struct VerbHeaders {
    pub(crate) before: Option<(HeaderName, HeaderValue)>,
    pub(crate) after: Option<(HeaderName, HeaderValue)>,
}

impl VerbHeaders {
    /// `sse()`'s: an `accept` first, a `last-event-id` last (each only when given).
    pub(crate) fn sse(accept: Option<&str>, last_event_id: Option<&str>) -> PyResult<Self> {
        let value = |value: &str| HeaderValue::from_str(value).map_err(|e| PyValueError::new_err(e.to_string()));
        Ok(Self {
            before: accept.map(value).transpose()?.map(|value| (ACCEPT, value)),
            after: last_event_id.map(value).transpose()?.map(|value| (HeaderName::from_static("last-event-id"), value)),
        })
    }
}

pub(crate) struct Prepared {
    pub(crate) request: Request<RequestBody>,
    pub(crate) url: Url,
    /// How its connection is made: direct, or through which proxy.
    pub(crate) route: Route,
    /// What a redirect is followed from, when the client follows them (`None` for a hop, whose
    /// request keeps the state it came from).
    pub(crate) redirect: Option<Box<Redirect>>,
}

/// A client's request-building settings, fixed when it is opened.
pub(crate) struct ClientConfig {
    base_url: Option<Url>,
    /// pyreqwest's per-client `default_headers` (lothc's `default_headers=`).
    default_headers: Option<HeaderMap>,
    /// reqwest's own client defaults: `accept: */*`, then the user agent.
    client_headers: HeaderMap,
    https_only: bool,
    proxies: Proxies,
    /// `None` when redirects aren't followed.
    redirects: Option<Policy>,
}

enum QueryFailure {
    /// A value pyreqwest's extraction refuses: a `ValueError` naming it.
    Invalid(PyErr),
    /// A sequence where serde_urlencoded wants a scalar: reqwest's builder error.
    Unsupported,
}

impl From<PyErr> for QueryFailure {
    fn from(error: PyErr) -> Self {
        Self::Invalid(error)
    }
}

enum Scalar<'a, 'py> {
    Str(&'a str),
    Bool(bool),
    Int(i64),
    /// An `int` outside i64, sent as its own `str()`, as `_exact_query_value` gives pyreqwest.
    Digits(&'a Bound<'py, PyAny>),
    Float(f64),
}

/// A query value as pyreqwest's `QueryVal` extracts one: `bool`, then an `i64`, then an `f64`,
/// then `str`, except that an `int` beyond i64 goes out as its digits, not a float, as the
/// pyreqwest transport's `_exact_query_value` makes it. The common types are checked directly
/// first; the fallbacks are pyo3's own extractions, so `__index__`/`__float__` objects behave as
/// they do there (only a real `int` gets the exact digits, as in `_exact_query_value`).
fn scalar<'a, 'py>(value: &'a Bound<'py, PyAny>) -> Option<Scalar<'a, 'py>> {
    if let Ok(text) = value.cast::<PyString>() {
        return text.to_str().ok().map(Scalar::Str);
    }
    if let Ok(flag) = value.cast::<PyBool>() {
        return Some(Scalar::Bool(flag.is_true()));
    }
    if value.is_instance_of::<PyInt>() {
        return match value.extract::<i64>() {
            Ok(number) => Some(Scalar::Int(number)),
            Err(_) => Some(Scalar::Digits(value)),
        };
    }
    if let Ok(number) = value.cast::<PyFloat>() {
        return Some(Scalar::Float(number.value()));
    }
    if let Ok(flag) = value.extract::<bool>() {
        return Some(Scalar::Bool(flag));
    }
    if let Ok(number) = value.extract::<i64>() {
        return Some(Scalar::Int(number));
    }
    value.extract::<f64>().ok().map(Scalar::Float)
}

/// Fails only for `Scalar::Digits`, with `str()`'s own error (an `int` past
/// `sys.get_int_max_str_digits()`), as `_exact_query_value` would raise it.
fn append_scalar<T: Target>(serializer: &mut Serializer<'_, T>, name: &str, value: Scalar<'_, '_>) -> PyResult<()> {
    match value {
        Scalar::Str(text) => serializer.append_pair(name, text),
        Scalar::Bool(flag) => serializer.append_pair(name, if flag { "true" } else { "false" }),
        Scalar::Int(number) => serializer.append_pair(name, itoa::Buffer::new().format(number)),
        Scalar::Digits(number) => serializer.append_pair(name, number.str()?.to_str()?),
        Scalar::Float(number) => serializer.append_pair(name, ryu::Buffer::new().format(number)),
    };
    Ok(())
}

/// One `(name, value)` pair. A sequence of scalars is pyreqwest's `QueryValue::Array`, which
/// extracts fine and then fails in serde_urlencoded; anything else is refused at extraction, with
/// pyreqwest's message (`context` is "query" or "form").
fn append_query_value<T: Target>(
    serializer: &mut Serializer<'_, T>,
    name: &str,
    value: &Bound<'_, PyAny>,
    context: &str,
) -> Result<(), QueryFailure> {
    if let Some(scalar) = scalar(value) {
        append_scalar(serializer, name, scalar)?;
        return Ok(());
    }
    if !value.is_instance_of::<PyString>() {
        if let Ok(sequence) = value.cast::<PySequence>() {
            let mut all_scalars = true;
            for item in sequence.try_iter()? {
                all_scalars &= scalar(&item?).is_some();
            }
            if all_scalars {
                return Err(QueryFailure::Unsupported);
            }
        }
    }
    Err(QueryFailure::Invalid(PyValueError::new_err(format!("Invalid {context} value: {value}"))))
}

fn query_key<'a>(key: &'a Bound<'_, PyAny>, context: &str) -> Result<&'a str, QueryFailure> {
    key.cast::<PyString>()
        .ok()
        .and_then(|text| text.to_str().ok())
        .ok_or_else(|| QueryFailure::Invalid(PyValueError::new_err(format!("Invalid {context} key: {key}"))))
}

/// `pairs` is a plain `dict` (lothc's `Params` mapping, whose `list`/`tuple` values repeat the
/// key, as `_query_pairs` expands them) or the `(name, value)` pairs `_query_pairs` produced.
fn append_pairs<T: Target>(
    serializer: &mut Serializer<'_, T>,
    pairs: &Bound<'_, PyAny>,
    context: &str,
) -> Result<(), QueryFailure> {
    if let Ok(dict) = pairs.cast::<PyDict>() {
        for (key, value) in dict.iter() {
            let name = query_key(&key, context)?;
            if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
                for item in value.try_iter()? {
                    append_query_value(serializer, name, &item?, context)?;
                }
            } else {
                append_query_value(serializer, name, &value, context)?;
            }
        }
        return Ok(());
    }
    for pair in pairs.try_iter()? {
        let pair = pair?;
        let (key, value) = pair.extract::<(Bound<'_, PyAny>, Bound<'_, PyAny>)>()?;
        append_query_value(serializer, query_key(&key, context)?, &value, context)?;
    }
    Ok(())
}

fn header_name(key: &Bound<'_, PyAny>) -> PyResult<HeaderName> {
    key.cast::<PyString>()
        .ok()
        .and_then(|text| text.to_str().ok())
        .and_then(|text| HeaderName::from_bytes(text.as_bytes()).ok())
        .ok_or_else(|| PyValueError::new_err(format!("Invalid header key: {key}")))
}

fn header_value(value: &Bound<'_, PyAny>) -> PyResult<HeaderValue> {
    value
        .cast::<PyString>()
        .ok()
        .and_then(|text| text.to_str().ok())
        .and_then(|text| HeaderValue::from_str(text).ok())
        .ok_or_else(|| PyValueError::new_err(format!("Invalid header value: {value}")))
}

fn too_many_headers<E>(_error: E) -> PyErr {
    PyValueError::new_err("too many headers")
}

/// A `dict` of headers as pyreqwest's `HeaderMap` extracts one: every pair appended, so two keys
/// differing only in case both go out.
pub(crate) fn header_map(headers: &Bound<'_, PyAny>) -> PyResult<HeaderMap> {
    let dict = headers.cast::<PyDict>().map_err(|_| PyTypeError::new_err("headers must be a dict of str to str"))?;
    let mut map = HeaderMap::try_with_capacity(dict.len()).map_err(too_many_headers)?;
    for (key, value) in dict.iter() {
        map.try_append(header_name(&key)?, header_value(&value)?).map_err(too_many_headers)?;
    }
    Ok(map)
}

/// `HeaderMap::extend` (the first value of each name replaces, the rest append), guarded so its
/// size limit can't be reached.
fn extend_headers(dst: &mut HeaderMap, src: HeaderMap) -> PyResult<()> {
    if dst.len() + src.len() > MAX_HEADERS {
        return Err(too_many_headers(()));
    }
    dst.extend(src);
    Ok(())
}

/// pyreqwest's `RequestBuilderHeaders::header`/`headers`: setting a name drops it from the
/// defaults (`remove_entry_mult`, a swap-remove, which is why order shifts).
fn remove_default(defaults: &mut Option<HeaderMap>, name: &HeaderName) -> PyResult<()> {
    if let Some(defaults) = defaults {
        if let Entry::Occupied(entry) = defaults.try_entry(name).map_err(too_many_headers)? {
            entry.remove_entry_mult();
        }
    }
    Ok(())
}

/// pyreqwest's `RequestBuilderHeaders::header`: drop `name` from the defaults, then append it to
/// the request's own headers.
fn set_header(defaults: &mut Option<HeaderMap>, own: &mut Option<HeaderMap>, name: HeaderName, value: HeaderValue) -> PyResult<()> {
    remove_default(defaults, &name)?;
    match own {
        Some(map) => {
            map.try_append(name, value).map_err(too_many_headers)?;
        }
        None => {
            let mut map = HeaderMap::new();
            map.insert(name, value);
            *own = Some(map);
        }
    }
    Ok(())
}

fn set_request_headers(defaults: &mut Option<HeaderMap>, own: &mut Option<HeaderMap>, headers: HeaderMap) -> PyResult<()> {
    for name in headers.keys() {
        remove_default(defaults, name)?;
    }
    match own {
        Some(existing) => extend_headers(existing, headers),
        None => {
            *own = Some(headers);
            Ok(())
        }
    }
}

/// reqwest's (and pyreqwest's) `basic_auth`: `user:` then the password if there is one.
fn basic_auth(username: &str, password: Option<&str>) -> PyResult<HeaderValue> {
    let mut buf = b"Basic ".to_vec();
    {
        let mut encoder = EncoderWriter::new(&mut buf, &BASE64_STANDARD);
        let _ = std::io::Write::write_all(&mut encoder, username.as_bytes());
        let _ = std::io::Write::write_all(&mut encoder, b":");
        if let Some(password) = password {
            let _ = std::io::Write::write_all(&mut encoder, password.as_bytes());
        }
    }
    let mut value = HeaderValue::from_maybe_shared(Bytes::from(buf)).map_err(|e| PyValueError::new_err(e.to_string()))?;
    value.set_sensitive(true);
    Ok(value)
}

/// reqwest's `extract_authority`: a URL's `user:pass@` becomes a basic-auth header.
fn authority_auth(url: &mut Url) -> PyResult<Option<HeaderValue>> {
    if !url.has_authority() {
        return Ok(None);
    }
    let Ok(username) = percent_encoding::percent_decode(url.username().as_bytes()).decode_utf8() else {
        return Ok(None);
    };
    let username = username.into_owned();
    let password = url
        .password()
        .and_then(|password| percent_encoding::percent_decode(password.as_bytes()).decode_utf8().ok().map(|p| p.into_owned()));
    if username.is_empty() && password.is_none() {
        return Ok(None);
    }
    // Neither fails on a URL with an authority.
    let _ = url.set_username("");
    let _ = url.set_password(None);
    basic_auth(&username, password.as_deref()).map(Some)
}

fn method(method: &str) -> PyResult<Method> {
    Ok(match method {
        "GET" => Method::GET,
        "POST" => Method::POST,
        "PUT" => Method::PUT,
        "PATCH" => Method::PATCH,
        "DELETE" => Method::DELETE,
        "HEAD" => Method::HEAD,
        "OPTIONS" => Method::OPTIONS,
        other => Method::from_bytes(other.as_bytes()).map_err(|e| PyValueError::new_err(e.to_string()))?,
    })
}

/// hyper-util's `get_non_default_port`.
fn non_default_port(uri: &Uri) -> Option<u16> {
    match (uri.port_u16(), uri.scheme_str() == Some("https")) {
        (Some(443), true) | (Some(80), false) => None,
        (port, _) => port,
    }
}

impl ClientConfig {
    pub(crate) fn new(
        base_url: Option<&str>,
        default_headers: Option<&Bound<'_, PyAny>>,
        user_agent: &str,
        https_only: bool,
    ) -> PyResult<Self> {
        let base_url = match base_url {
            Some(base) => {
                let url = Url::parse(base).map_err(|e| PyValueError::new_err(e.to_string()))?;
                if !url.as_str().ends_with('/') {
                    return Err(PyValueError::new_err("base_url must end with a trailing slash '/'"));
                }
                Some(url)
            }
            None => None,
        };
        let default_headers = match default_headers {
            Some(headers) => Some(header_map(headers)?).filter(|map| !map.is_empty()),
            None => None,
        };
        let mut client_headers = HeaderMap::with_capacity(2);
        client_headers.insert(ACCEPT, HeaderValue::from_static("*/*"));
        // reqwest's `user_agent()` keeps the error for `build()`, which raises it as its own.
        let user_agent = HeaderValue::from_str(user_agent).map_err(|e| ClientBuildError::new_err(format!("builder error: {e}")))?;
        client_headers.insert(USER_AGENT, user_agent);
        Ok(Self { base_url, default_headers, client_headers, https_only, proxies: Proxies::none(), redirects: None })
    }

    /// The client's proxies (`proxy.rs`), which `prepare` routes every request by.
    pub(crate) fn with_proxies(self, proxies: Proxies) -> Self {
        Self { proxies, ..self }
    }

    /// The client's redirect policy (`redirect.rs`): `follow_redirects` and `max_redirects`, which
    /// reqwest's builder takes, validated as pyo3 extracts them there.
    pub(crate) fn with_redirects(self, follow: bool, max: Option<usize>) -> Self {
        let redirects = Policy::new(follow, max, self.https_only);
        Self { redirects, ..self }
    }

    /// The user agent reqwest's connector also sends on a `CONNECT`.
    pub(crate) fn user_agent(&self) -> Option<&HeaderValue> {
        self.client_headers.get(USER_AGENT)
    }

    fn url(&self, path: &str) -> PyResult<Url> {
        let url = match &self.base_url {
            Some(base) => base.join(path),
            None => Url::parse(path),
        };
        url.map_err(|e| PyValueError::new_err(e.to_string()))
    }

    /// A body that's only bytes, and the copy reqwest's redirect layer keeps of it (made only
    /// when the client follows redirects).
    fn copyable(&self, bytes: Bytes) -> (RequestBody, Option<Bytes>) {
        let copy = self.redirects.is_some().then(|| bytes.clone());
        (RequestBody::full(bytes), copy)
    }

    /// Everything about a request but the connection. Fails before any I/O: a `ValueError` for
    /// what pyreqwest rejects while building (an unparseable URL, an invalid header or query
    /// value), `HTTPTransportError` for what reqwest rejects when sending (a scheme other than
    /// http(s), plain http under `https_only`, a sequence as a query value).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn prepare(
        &self,
        py: Python<'_>,
        glue: &Glue,
        method_name: &str,
        path: &str,
        query: Option<&Bound<'_, PyAny>>,
        headers: Option<&Bound<'_, PyAny>>,
        auth: Option<&str>,
        body: Option<&Bound<'_, PyAny>>,
        body_kind: u8,
    ) -> PyResult<Prepared> {
        self.prepare_with(py, glue, method_name, path, query, headers, auth, body, body_kind, VerbHeaders::default())
    }

    /// `prepare`, plus the headers a verb sets itself (`VerbHeaders`).
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn prepare_with(
        &self,
        py: Python<'_>,
        glue: &Glue,
        method_name: &str,
        path: &str,
        query: Option<&Bound<'_, PyAny>>,
        headers: Option<&Bound<'_, PyAny>>,
        auth: Option<&str>,
        body: Option<&Bound<'_, PyAny>>,
        body_kind: u8,
        verb_headers: VerbHeaders,
    ) -> PyResult<Prepared> {
        let method = method(method_name)?;
        let mut url = self.url(path)?;
        let mut reqwest_headers = HeaderMap::new();
        if let Some(value) = authority_auth(&mut url)? {
            reqwest_headers.append(AUTHORIZATION, value);
        }
        let mut unsupported = false;
        if let Some(query) = query {
            let result = {
                let mut serializer = url.query_pairs_mut();
                append_pairs(&mut serializer, query, "query")
            };
            match result {
                Ok(()) => {}
                Err(QueryFailure::Invalid(error)) => return Err(error),
                Err(QueryFailure::Unsupported) => unsupported = true,
            }
            if url.query() == Some("") {
                url.set_query(None);
            }
        }

        let mut defaults = self.default_headers.clone();
        let mut own: Option<HeaderMap> = None;
        let VerbHeaders { before, after } = verb_headers;
        if let Some((name, value)) = before {
            set_header(&mut defaults, &mut own, name, value)?;
        }
        if let Some(auth) = auth {
            let mut value = HeaderValue::from_str(auth).map_err(|e| PyValueError::new_err(e.to_string()))?;
            value.set_sensitive(true);
            set_header(&mut defaults, &mut own, AUTHORIZATION, value)?;
        }
        if let Some(headers) = headers {
            set_request_headers(&mut defaults, &mut own, header_map(headers)?)?;
        }
        if let Some((name, value)) = after {
            // `.headers()`, not `.header()`: a caller's own `Last-Event-ID` gives way to the
            // tracked one instead of going out beside it.
            let mut replacement = HeaderMap::with_capacity(1);
            replacement.insert(name, value);
            set_request_headers(&mut defaults, &mut own, replacement)?;
        }
        // The body as sent, and a copy for a redirect's next hop: `None` for a multipart body
        // with parts, which reqwest streams, so its redirect layer can't copy it.
        let (body, copy) = match body {
            None => self.copyable(Bytes::new()),
            Some(body) => match body_kind {
                BODY_RAW => match body.cast::<PyBytes>() {
                    Ok(bytes) => self.copyable(Bytes::copy_from_slice(bytes.as_bytes())),
                    Err(_) => self.copyable(Bytes::from(body.extract::<String>()?)),
                },
                BODY_ENCODED_JSON => {
                    let mut content_type = HeaderMap::with_capacity(1);
                    content_type.insert(CONTENT_TYPE, HeaderValue::from_static("application/json"));
                    set_request_headers(&mut defaults, &mut own, content_type)?;
                    self.copyable(Bytes::copy_from_slice(body.cast::<PyBytes>()?.as_bytes()))
                }
                BODY_JSON => {
                    let value: serde_json::Value = pythonize::depythonize(body)?;
                    self.copyable(Bytes::from(serde_json::to_vec(&value).map_err(|e| PyValueError::new_err(e.to_string()))?))
                }
                BODY_FORM => {
                    let mut serializer = Serializer::new(String::new());
                    match append_pairs(&mut serializer, body, "form") {
                        Ok(()) => {}
                        Err(QueryFailure::Invalid(error)) => return Err(error),
                        Err(QueryFailure::Unsupported) => unsupported = true,
                    }
                    if let Entry::Vacant(entry) = reqwest_headers.try_entry(CONTENT_TYPE).map_err(too_many_headers)? {
                        entry.insert(HeaderValue::from_static("application/x-www-form-urlencoded"));
                    }
                    self.copyable(Bytes::from(serializer.finish()))
                }
                BODY_MULTIPART => multipart_body(body, &mut reqwest_headers)?,
                other => return Err(PyValueError::new_err(format!("unknown body kind {other}"))),
            },
        };
        if unsupported {
            return Err(glue.builder_error(py, "builder error: unsupported value".to_owned()));
        }
        // reqwest's `IntoUrl` (a URL without a host) and `execute_request` (the scheme) checks.
        let scheme = url.scheme();
        if !url.has_host() || (scheme != "http" && scheme != "https") || (self.https_only && scheme != "https") {
            return Err(glue.builder_error(py, format!("builder error for url ({url}): URL scheme is not allowed")));
        }

        let mut headers = reqwest_headers;
        if let Some(defaults) = defaults {
            extend_headers(&mut headers, defaults)?;
        }
        if let Some(own) = own {
            extend_headers(&mut headers, own)?;
        }
        if body_kind == BODY_JSON {
            headers.try_insert(CONTENT_TYPE, HeaderValue::from_static("application/json")).map_err(too_many_headers)?;
        }
        for (name, value) in &self.client_headers {
            if let Entry::Vacant(entry) = headers.try_entry(name).map_err(too_many_headers)? {
                entry.try_insert(value.clone()).map_err(too_many_headers)?;
            }
        }
        let invalid_uri = || glue.builder_error(py, format!("builder error for url ({url}): Parsed Url is not a valid Uri"));
        let uri: Uri = url.as_str().parse().map_err(|_| invalid_uri())?;
        let route = self.proxies.route(&uri)?;
        if let Route::Forward { auth: Some(auth), .. } = &route {
            if let Entry::Vacant(entry) = headers.try_entry(PROXY_AUTHORIZATION).map_err(too_many_headers)? {
                entry.try_insert(auth.clone()).map_err(too_many_headers)?;
            }
        }
        // tower-http's `Decompression`, reqwest's outermost layer: after reqwest's defaults and
        // the proxy credentials `execute_request` adds before calling it.
        if let Entry::Vacant(entry) = headers.try_entry(ACCEPT_ENCODING).map_err(too_many_headers)? {
            entry.try_insert(decompress::accept_encoding()).map_err(too_many_headers)?;
        }
        // What reqwest's redirect layer copies, between `Decompression` and hyper-util.
        let redirect = self
            .redirects
            .map(|policy| Box::new(Redirect::new(policy, method.clone(), uri.clone(), headers.clone(), copy)));
        let request = sendable(method, &uri, &route, headers, body, invalid_uri)?;
        Ok(Prepared { request, url, route, redirect })
    }

    /// The request a redirect's `hop` makes from `redirect` (`redirect.rs`), routed and given its
    /// `host` and target as hyper-util does any request, but not the proxy credentials reqwest
    /// adds only for a first request (`execute_request`), which the hop already carries if any.
    pub(crate) fn hop(&self, py: Python<'_>, glue: &Glue, redirect: &Redirect, hop: Hop) -> PyResult<Prepared> {
        let Hop { url, headers, body } = hop;
        let route = self.proxies.route(&redirect.uri)?;
        let invalid_uri = || glue.builder_error(py, format!("builder error for url ({url}): Parsed Url is not a valid Uri"));
        let request = sendable(redirect.method.clone(), &redirect.uri, &route, headers, RequestBody::full(body), invalid_uri)?;
        Ok(Prepared { request, url, route, redirect: None })
    }
}

/// reqwest's `RequestBuilder::multipart`: the form's parts built (`multipart.rs`), its
/// `Content-Type`, then its `Content-Length` when every part's length is known, appended to
/// reqwest's own headers; the body, and its copy for a redirect (an empty form's only).
fn multipart_body(form: &Bound<'_, PyAny>, reqwest_headers: &mut HeaderMap) -> PyResult<(RequestBody, Option<Bytes>)> {
    let form = Form::extract(form)?;
    let copy = form.is_empty().then(Bytes::new);
    let encoded = form.encode();
    let content_type = HeaderValue::from_str(&encoded.content_type).map_err(|e| PyValueError::new_err(e.to_string()))?;
    reqwest_headers.try_append(CONTENT_TYPE, content_type).map_err(too_many_headers)?;
    if let Some(length) = encoded.length {
        reqwest_headers.try_append(CONTENT_LENGTH, HeaderValue::from(length)).map_err(too_many_headers)?;
    }
    Ok((encoded.body, copy))
}

/// hyper-util's last touches to a request for `uri` (absolute) along `route`: `host` where absent,
/// then `absolute_form` to an HTTP proxy (the URI as is), else `origin_form` (the path and query
/// alone) as the request target; `invalid_uri` if the URI can't be written that way.
fn sendable(
    method: Method,
    uri: &Uri,
    route: &Route,
    mut headers: HeaderMap,
    body: RequestBody,
    invalid_uri: impl Fn() -> PyErr,
) -> PyResult<Request<RequestBody>> {
    if let Entry::Vacant(entry) = headers.try_entry(HOST).map_err(too_many_headers)? {
        let host = uri.host().ok_or_else(&invalid_uri)?;
        let host = match non_default_port(uri) {
            Some(port) => format!("{host}:{port}"),
            None => host.to_owned(),
        };
        entry.try_insert(HeaderValue::from_str(&host).map_err(|_| invalid_uri())?).map_err(too_many_headers)?;
    }
    let target = match uri.path_and_query() {
        _ if route.is_forward() => uri.clone(),
        Some(path_and_query) if path_and_query.as_str() != "/" => {
            let mut parts = http::uri::Parts::default();
            parts.path_and_query = Some(path_and_query.clone());
            Uri::from_parts(parts).map_err(|_| invalid_uri())?
        }
        _ => Uri::default(),
    };
    let mut request = Request::new(body);
    *request.method_mut() = method;
    *request.uri_mut() = target;
    *request.headers_mut() = headers;
    Ok(request)
}
