//! `lothc._native` where the native transport can't run: Windows, which has neither `poll(2)` nor
//! the `loop.add_reader` the pools wait on, so `native="on"` raises there before any pool is built
//! (`lothc/_settings.py`). lothc imports these names on every platform (`_transport_native.py`,
//! `_native_loop.py`, `_mocking.py`), so they exist here with nothing behind them: none of the
//! classes can be constructed, and the mock hook has no pool to ask.
use pyo3::prelude::*;

pyo3::create_exception!(_native, ClientBuildError, pyo3::exceptions::PyValueError);

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct Pool;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct Stream;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct SyncPool;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct SyncStream;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct RawHeaders;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct ConnectOptions;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct Intercepted;

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct SyncIntercepted;

/// The mocker installs its hook whichever transport a client uses (`lothc/_mocking.py`'s
/// `_install`); with no pool to ask it, there's nothing to keep.
#[pyfunction]
pub(crate) fn set_mock_hook(hook: Option<Py<PyAny>>) {
    drop(hook);
}
