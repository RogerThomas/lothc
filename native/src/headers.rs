//! A response's headers, kept in Rust until `_LazyHeaders` first reads them.
use http::HeaderMap;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString, PyTuple};

#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct RawHeaders {
    map: HeaderMap,
}

impl RawHeaders {
    pub(crate) fn new(map: HeaderMap) -> Self {
        Self { map }
    }
}

#[pymethods]
impl RawHeaders {
    /// Every `(name, value)` in arrival order, a repeated name once per value. Names are
    /// lowercase (the `http` crate's `HeaderName`); a value that isn't visible ASCII is a
    /// `ValueError`, as reading one from pyreqwest's `HeaderMap` is.
    fn items<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(py);
        for (name, value) in &self.map {
            let value = value.to_str().map_err(|e| PyValueError::new_err(e.to_string()))?;
            list.append(PyTuple::new(py, [PyString::new(py, name.as_str()), PyString::new(py, value)])?)?;
        }
        Ok(list)
    }

    fn __len__(&self) -> usize {
        self.map.len()
    }
}
