//! `lothc.testing`'s way into the native transport. While its mocker is active it installs a hook
//! (`set_mock_hook`), and each request a pool would send stops where pyreqwest's mock middleware
//! sees one (`ClientConfig::stage`): built from the call by the same code as any other request,
//! but before reqwest's sending adds anything. The pool hands it to the hook as an `Intercepted`
//! (`pool.rs`, `sync_pool.rs`), which the hook either answers from the mock's rules (its
//! response then goes through the pool's own response path, `Glue::outcome`) or sends on for
//! real, as pyreqwest's middleware falls through to the network. A mocked answer uses nothing
//! else of the pool: no connection, `max_connections` slot, timeout, redirect, cookie jar or
//! decompression, as on pyreqwest, whose mock answers before reqwest has the request.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use bytes::Bytes;
use http::header::{HeaderName, HeaderValue};
use http::{HeaderMap, Version};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyString, PyTuple};
use url::Url;

use crate::request::{ClientConfig, Staged, VerbHeaders};
use crate::response::{Exchange, Glue, Head};

/// Whether `HOOK` holds a hook: the one check an unmocked request pays.
static ACTIVE: AtomicBool = AtomicBool::new(false);
static HOOK: Mutex<Option<Py<PyAny>>> = Mutex::new(None);

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Install `hook` (or, with `None`, remove the one installed): every pool, in every thread, asks
/// it about each request from now on, through its `request`/`stream` (an async pool's, with an
/// `Intercepted`) and `request_sync`/`stream_sync` (a sync pool's, with a `SyncIntercepted`).
#[pyfunction]
pub(crate) fn set_mock_hook(hook: Option<Py<PyAny>>) {
    let previous = {
        let mut current = lock(&HOOK);
        ACTIVE.store(hook.is_some(), Ordering::Release);
        std::mem::replace(&mut *current, hook)
    };
    // Released outside the lock: dropping it may run Python code, which may install a hook.
    drop(previous);
}

/// The installed hook, if any.
pub(crate) fn hook(py: Python<'_>) -> Option<Py<PyAny>> {
    if !ACTIVE.load(Ordering::Acquire) {
        return None;
    }
    lock(&HOOK).as_ref().map(|hook| hook.clone_ref(py))
}

/// The request a pool was asked to send, as its `request`/`stream` take it.
pub(crate) struct Call<'a, 'py> {
    pub(crate) path: &'a str,
    pub(crate) query: Option<&'a Bound<'py, PyAny>>,
    pub(crate) headers: Option<&'a Bound<'py, PyAny>>,
    pub(crate) auth: Option<&'a str>,
    pub(crate) body: Option<&'a Bound<'py, PyAny>>,
    pub(crate) body_kind: u8,
    pub(crate) verb_headers: VerbHeaders,
}

/// What a pool's `request` takes besides the request, kept for its answer; a stream has only its
/// `timeout`.
#[derive(Default)]
pub(crate) struct Settings {
    pub(crate) decode: Option<Py<PyAny>>,
    pub(crate) timeout: Option<Duration>,
    pub(crate) raises: Option<Py<PyAny>>,
    pub(crate) defer_error: bool,
    pub(crate) typed_headers: Option<Py<PyAny>>,
    pub(crate) headers_only: bool,
}

impl Settings {
    pub(crate) fn clone_ref(&self, py: Python<'_>) -> Self {
        let copy = |value: &Option<Py<PyAny>>| value.as_ref().map(|value| value.clone_ref(py));
        Self {
            decode: copy(&self.decode),
            timeout: self.timeout,
            raises: copy(&self.raises),
            defer_error: self.defer_error,
            typed_headers: copy(&self.typed_headers),
            headers_only: self.headers_only,
        }
    }
}

/// The request a pool's mock hook gets, shared by `Intercepted` and `SyncIntercepted`.
pub(crate) struct Core {
    pub(crate) method: Py<PyString>,
    pub(crate) url: Url,
    headers: HeaderMap,
    body: Option<Bytes>,
    pub(crate) request_info: Py<PyAny>,
    pub(crate) started: Instant,
    pub(crate) settings: Settings,
    /// Taken by the one `send`/`stream` that sends it for real.
    staged: Mutex<Option<Staged>>,
}

impl Core {
    /// Stage `call` as `config` builds it, failing as building it fails.
    pub(crate) fn new(py: Python<'_>, config: &ClientConfig, glue: &Glue, method: Bound<'_, PyString>, call: Call<'_, '_>, settings: Settings) -> PyResult<Self> {
        let started = Instant::now();
        let Call { path, query, headers, auth, body, body_kind, verb_headers } = call;
        let staged = config.stage(py, glue, method.to_str()?, path, query, headers, auth, body, body_kind, verb_headers, true)?;
        let request_info = glue.request_info_of(py, &method, &staged.url)?.unbind();
        Ok(Self {
            method: method.unbind(),
            url: staged.url.clone(),
            headers: staged.headers.clone(),
            body: staged.seen_body.clone(),
            request_info,
            started,
            settings,
            staged: Mutex::new(Some(staged)),
        })
    }

    /// The staged request, to send for real (once).
    pub(crate) fn take(&self) -> PyResult<Staged> {
        lock(&self.staged).take().ok_or_else(|| PyRuntimeError::new_err("internal error: an intercepted request was sent twice"))
    }

    /// Its headers as `(name, value)` pairs, in order; a value that isn't visible ASCII is a
    /// `ValueError`, as reading one from pyreqwest's `HeaderMap` is.
    pub(crate) fn headers<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(py);
        for (name, value) in &self.headers {
            let value = value.to_str().map_err(|e| PyValueError::new_err(e.to_string()))?;
            list.append(PyTuple::new(py, [PyString::new(py, name.as_str()), PyString::new(py, value)])?)?;
        }
        Ok(list)
    }

    pub(crate) fn body<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        self.body.as_ref().map(|body| PyBytes::new(py, body))
    }

    /// The exchange its answer completes, as the pool's own requests carry one.
    fn exchange(&self, py: Python<'_>) -> Exchange {
        let Settings { decode, raises, defer_error, typed_headers, headers_only, .. } = self.settings.clone_ref(py);
        Exchange { method: self.method.clone_ref(py), url: self.url.clone(), decode, raises, defer_error, typed_headers, headers_only, started: self.started }
    }

    /// The mock's answer as the pool's response path takes it: lothc's `Response` (a deferred
    /// error response included), or what a status error raises, built as for any response.
    pub(crate) fn respond<'py>(&self, py: Python<'py>, glue: &Glue, status: u16, headers: &Bound<'_, PyAny>, body: &[u8]) -> PyResult<Bound<'py, PyAny>> {
        let head = Head { status, version: Version::HTTP_11, headers: header_map(headers)?, body: Bytes::copy_from_slice(body) };
        glue.outcome(py, &self.exchange(py), Ok(head))
    }

    /// A mocked stream's head, `(status, RawHeaders, http_version)`, as a pool's stream opens.
    pub(crate) fn head<'py>(&self, py: Python<'py>, glue: &Glue, status: u16, headers: &Bound<'_, PyAny>) -> PyResult<Bound<'py, PyTuple>> {
        glue.stream_head(py, status, Version::HTTP_11, header_map(headers)?)
    }
}

/// A response's `(name, value)` pairs, each appended in order.
fn header_map(headers: &Bound<'_, PyAny>) -> PyResult<HeaderMap> {
    let mut map = HeaderMap::new();
    for pair in headers.try_iter()? {
        let (name, value) = pair?.extract::<(String, String)>()?;
        let name = HeaderName::from_bytes(name.as_bytes()).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let value = HeaderValue::from_str(&value).map_err(|e| PyValueError::new_err(e.to_string()))?;
        map.append(name, value);
    }
    Ok(map)
}
