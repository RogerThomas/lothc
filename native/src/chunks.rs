//! A `form=` file part streamed from Python (`_ChunkStream`, `_AsyncChunkStream`: see
//! `lothc/_form.py`), pulled a chunk at a time as hyper writes the body, as reqwest polls
//! pyreqwest's `BodyStream` for a `Part::stream`:
//!
//! - from an iterator with `next()`, on the thread driving the request (an async pool's loop
//!   thread, or a sync request's own, attached to Python for each pull);
//! - from an async iterable with its `pull`, a task on the loop: the request waits (`Pending`)
//!   until the task's done callback (`ChunkReady`) wakes it and runs the pool (`Pool.wake`), so
//!   the loop is never blocked waiting for a chunk.
//!
//! An exception a pull raises fails the body with it as the `io::Error`'s inner error, which the
//! request then raises as it was (`Failure::of_hyper`), as pyreqwest re-raises it.
use std::io;
use std::sync::{Arc, Mutex, MutexGuard, PoisonError};
use std::task::{Context, Poll, Waker};

use bytes::Bytes;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyIterator};

/// The waker of the request waiting for an async pull, taken by whichever comes first: the pull's
/// `ChunkReady`, or the body being dropped (after which a pull that finishes runs nothing).
type Waiting = Arc<Mutex<Option<Waker>>>;

fn lock(waiting: &Waiting) -> MutexGuard<'_, Option<Waker>> {
    waiting.lock().unwrap_or_else(PoisonError::into_inner)
}

/// An async pull in flight: its asyncio task, and who to wake when it's done.
pub(crate) struct Pull {
    task: Py<PyAny>,
    waiting: Waiting,
}

/// A streamed part's chunks, from `_ChunkStream` (`Iter`) or `_AsyncChunkStream` (`Async`, with
/// the pool's own `wake` and the pull in flight, if any).
pub(crate) enum Chunks {
    Iter(Py<PyIterator>),
    Async { stream: Py<PyAny>, wake: Py<PyAny>, pull: Option<Pull> },
}

/// What a chunk is sent as: its bytes, copied (`_checked_chunk` has made sure they're `bytes`).
fn chunk_bytes(chunk: &Bound<'_, PyAny>) -> PyResult<Bytes> {
    Ok(Bytes::copy_from_slice(chunk.cast::<PyBytes>()?.as_bytes()))
}

/// The iterator's next chunk, `None` at its end.
fn next_chunk(iter: &Bound<'_, PyIterator>) -> PyResult<Option<Bytes>> {
    iter.clone().next().map(|chunk| chunk_bytes(&chunk?)).transpose()
}

/// The async iterable's next chunk: the finished pull's result, else a pull started (or still
/// running) for the request to wait on.
fn poll_async(py: Python<'_>, stream: &Py<PyAny>, wake: &Py<PyAny>, pull: &mut Option<Pull>, waker: &Waker) -> PyResult<Poll<Option<Bytes>>> {
    if let Some(running) = pull {
        let task = running.task.bind(py);
        if !task.call_method0(intern!(py, "done"))?.is_truthy()? {
            *lock(&running.waiting) = Some(waker.clone());
            return Ok(Poll::Pending);
        }
        let chunk = task.call_method0(intern!(py, "result"));
        // Its callback, if it hasn't run yet, finds no one to wake.
        lock(&running.waiting).take();
        *pull = None;
        let chunk = chunk?;
        return Ok(Poll::Ready(if chunk.is_none() { None } else { Some(chunk_bytes(&chunk)?) }));
    }
    let waiting = Arc::new(Mutex::new(Some(waker.clone())));
    let ready = Py::new(py, ChunkReady { waiting: waiting.clone(), wake: wake.clone_ref(py) })?;
    let task = stream.bind(py).call_method1(intern!(py, "pull"), (ready,))?;
    *pull = Some(Pull { task: task.unbind(), waiting });
    Ok(Poll::Pending)
}

impl Chunks {
    /// A `_ChunkStream` (an iterator) or an `_AsyncChunkStream`, which only an async pool (one
    /// with a `wake`) is given.
    pub(crate) fn extract(content: &Bound<'_, PyAny>, wake: Option<&Py<PyAny>>) -> PyResult<Option<Self>> {
        let py = content.py();
        if let Ok(iter) = content.cast::<PyIterator>() {
            return Ok(Some(Self::Iter(iter.clone().unbind())));
        }
        if !content.hasattr(intern!(py, "pull"))? {
            return Ok(None);
        }
        let wake = wake.ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("internal error: an async stream on a sync pool"))?;
        Ok(Some(Self::Async { stream: content.clone().unbind(), wake: wake.clone_ref(py), pull: None }))
    }

    /// The next chunk, `None` at the end, or the exception pulling it raised.
    pub(crate) fn poll_chunk(&mut self, cx: &mut Context<'_>) -> Poll<Option<io::Result<Bytes>>> {
        let polled = Python::attach(|py| match self {
            Self::Iter(iter) => next_chunk(iter.bind(py)).map(Poll::Ready),
            Self::Async { stream, wake, pull } => poll_async(py, stream, wake, pull, cx.waker()),
        });
        match polled {
            Ok(Poll::Ready(chunk)) => Poll::Ready(chunk.map(Ok)),
            Ok(Poll::Pending) => Poll::Pending,
            Err(error) => Poll::Ready(Some(Err(io::Error::other(error)))),
        }
    }
}

impl Drop for Chunks {
    /// A body dropped mid-pull (its request failed, was cancelled, or its client closed): the
    /// pull is cancelled, or, if it already finished, its exception (if any) marked as seen, so
    /// asyncio doesn't log it as never retrieved.
    fn drop(&mut self) {
        let Self::Async { pull: Some(running), .. } = self else { return };
        lock(&running.waiting).take();
        let task = &running.task;
        Python::try_attach(|py| {
            let task = task.bind(py);
            if let Ok(false) = task.call_method0(intern!(py, "cancel")).and_then(|cancelled| cancelled.is_truthy()) {
                drop(task.call_method0(intern!(py, "exception")));
            }
        });
    }
}

/// An async pull's done callback: wakes the request waiting on it, then runs the pool, which
/// polls it (a waker alone only marks it for the pool's next run).
#[pyclass(frozen, module = "lothc._native")]
struct ChunkReady {
    waiting: Waiting,
    wake: Py<PyAny>,
}

#[pymethods]
impl ChunkReady {
    fn __call__(&self, py: Python<'_>, _task: &Bound<'_, PyAny>) -> PyResult<()> {
        let waker = lock(&self.waiting).take();
        let Some(waker) = waker else { return Ok(()) };
        waker.wake();
        self.wake.call0(py)?;
        Ok(())
    }
}
