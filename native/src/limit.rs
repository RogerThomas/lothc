//! `max_connections`/`pool_timeout`, as pyreqwest 0.15 implements them (reqwest 0.13 and
//! hyper-util have no such limit; `ConnectionLimiter` in pyreqwest's `client/internal`):
//!
//! - a semaphore of `max_connections` slots over the client's requests, all origins together,
//!   not its connections: idle connections don't count, and neither does a background connect;
//! - a request takes a slot before anything is sent (pyreqwest's `Spawner::limit_connections`),
//!   holds it through every redirect hop, and gives it back once its body has been read (a
//!   stream's once its body ends, fails or is closed). A retry or a re-authenticated request is
//!   a new request, taking a slot of its own;
//! - requests get the slots in the order they asked (tokio's semaphore is fair);
//! - a request waits at most `pool_timeout`, and never past its own timeout (the shorter one
//!   wins), then fails with `HTTPTimeoutError` (pyreqwest's `PoolTimeoutError`). Waiting counts
//!   against its timeout, which runs from when the request started;
//! - `pool_timeout` without `max_connections` does nothing;
//! - closing the client fails every waiting request with the `RuntimeError` a request after it
//!   raises (pyreqwest's `ClientClosedError`).
//!
//! The async pool keeps its waiting requests in `Permits` (single-threaded, on the loop), the
//! sync pool its waiting threads in `SyncLimiter`.
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use pyo3::exceptions::PyValueError;
use pyo3::panic::PanicException;
use pyo3::PyResult;

use crate::error::Failure;
use crate::request::DeadlineClock;

/// `max_connections` as pyreqwest's builder takes it: at least one (`ValueError` otherwise), and
/// no more than tokio's semaphore can hold, past which its `Semaphore::new` panics on entry,
/// which pyo3 raises as `PanicException`.
pub(crate) fn checked_max_connections(max_connections: Option<usize>) -> PyResult<Option<usize>> {
    // tokio's `MAX_PERMITS`.
    let most = usize::MAX >> 3;
    match max_connections {
        Some(0) => Err(PyValueError::new_err("max_connections must be greater than 0")),
        Some(max) if max > most => {
            Err(PanicException::new_err(format!("a semaphore may not have more than MAX_PERMITS permits ({most})")))
        }
        max => Ok(max),
    }
}

/// The earlier of two optional deadlines; `None` (no deadline) only if both are.
fn earliest(a: Option<Instant>, b: Option<Instant>) -> Option<Instant> {
    match (a, b) {
        (Some(a), Some(b)) => Some(a.min(b)),
        (a, b) => a.or(b),
    }
}

/// The async pool's slots, on the loop's thread. A waiting request is its slab slot and
/// generation, with when its `pool_timeout` is up; its own timeout is in the pool's deadline heap.
pub(crate) struct Permits {
    available: usize,
    timeout: Option<Duration>,
    /// Oldest first. Every request waits the same `timeout` from when it asked, so their
    /// `pool_timeout` deadlines are in this order too, and only the first one's is ever due.
    waiting: VecDeque<(usize, u32, Option<Instant>)>,
}

impl Permits {
    pub(crate) fn new(max_connections: Option<usize>, timeout: Option<Duration>) -> Option<Self> {
        max_connections.map(|available| Self { available, timeout, waiting: VecDeque::new() })
    }

    /// Take a slot for a request started at `started`, if one is free and nobody is waiting for
    /// one already; otherwise queue it, `Err` when its `pool_timeout` is up.
    pub(crate) fn take(&mut self, slot: usize, generation: u32, started: Instant, clock: DeadlineClock) -> Result<(), Option<Instant>> {
        if self.waiting.is_empty() && self.available > 0 {
            self.available -= 1;
            return Ok(());
        }
        let until = self.timeout.and_then(|timeout| clock.deadline(started, timeout));
        self.waiting.push_back((slot, generation, until));
        Err(until)
    }

    /// A request gave its slot back.
    pub(crate) fn give_back(&mut self) {
        self.available += 1;
    }

    /// The oldest waiting request, if there is a slot for it now (taken for it).
    pub(crate) fn grant(&mut self) -> Option<(usize, u32)> {
        if self.available == 0 {
            return None;
        }
        let (slot, generation, _) = self.waiting.pop_front()?;
        self.available -= 1;
        Some((slot, generation))
    }

    /// Stop waiting for the first request (slab slot) `matches` picks, if one is waiting.
    pub(crate) fn remove_where(&mut self, matches: impl Fn(usize) -> bool) -> Option<usize> {
        let at = self.waiting.iter().position(|(slot, _, _)| matches(*slot))?;
        self.waiting.remove(at).map(|(slot, _, _)| slot)
    }

    /// The oldest waiting request whose `pool_timeout` was up by `now`, no longer waiting.
    pub(crate) fn expired(&mut self, now: Instant) -> Option<(usize, u32)> {
        let (slot, generation, until) = *self.waiting.front()?;
        if until.is_some_and(|until| until <= now) {
            self.waiting.pop_front();
            return Some((slot, generation));
        }
        None
    }

    /// When the next waiting request's `pool_timeout` is up.
    pub(crate) fn next_deadline(&self) -> Option<Instant> {
        self.waiting.front().and_then(|(_, _, until)| *until)
    }

    /// The pool closed: nobody waits any more (each request was failed with its slot).
    pub(crate) fn clear(&mut self) {
        self.waiting.clear();
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// One thread waiting for a slot, woken alone when it is given one (or the pool closes).
#[derive(Default)]
struct Waiter {
    /// Set, under the limiter's lock, once a slot is its.
    granted: AtomicBool,
    wake: Condvar,
}

struct SyncSlots {
    available: usize,
    waiting: VecDeque<Arc<Waiter>>,
    closed: bool,
}

/// The sync pool's slots, shared by every thread using the client. A waiting thread blocks on
/// its own condition variable, which releases the lock while it waits; the GIL is released by
/// the caller.
pub(crate) struct SyncLimiter {
    timeout: Option<Duration>,
    slots: Mutex<SyncSlots>,
}

impl SyncLimiter {
    pub(crate) fn new(max_connections: Option<usize>, timeout: Option<Duration>) -> Option<Arc<Self>> {
        max_connections.map(|available| {
            Arc::new(Self { timeout, slots: Mutex::new(SyncSlots { available, waiting: VecDeque::new(), closed: false }) })
        })
    }

    /// A slot for a request started at `started` whose own timeout is up at `deadline`, waiting
    /// for one (in turn) if none is free: `Failure::Timeout` once its wait is over first,
    /// `Failure::Closed` if the pool closes first.
    pub(crate) fn acquire(self: &Arc<Self>, started: Instant, clock: DeadlineClock, deadline: Option<Instant>) -> Result<SyncPermit, Failure> {
        let mut slots = lock(&self.slots);
        if slots.closed {
            return Err(Failure::Closed);
        }
        if slots.waiting.is_empty() && slots.available > 0 {
            slots.available -= 1;
            return Ok(SyncPermit(self.clone()));
        }
        let until = earliest(self.timeout.and_then(|timeout| clock.deadline(started, timeout)), deadline);
        let waiter = Arc::new(Waiter::default());
        slots.waiting.push_back(waiter.clone());
        loop {
            // A slot given just as the wait ran out still counts, as tokio's `timeout` polls the
            // acquire before its timer.
            if waiter.granted.load(Ordering::Relaxed) {
                return Ok(SyncPermit(self.clone()));
            }
            let left = until.map(|until| until.saturating_duration_since(Instant::now()));
            let failure = if slots.closed {
                Some(Failure::Closed)
            } else if left.is_some_and(|left| left.is_zero()) {
                Some(Failure::Timeout)
            } else {
                None
            };
            if let Some(failure) = failure {
                slots.waiting.retain(|other| !Arc::ptr_eq(other, &waiter));
                return Err(failure);
            }
            slots = match left {
                None => waiter.wake.wait(slots).unwrap_or_else(PoisonError::into_inner),
                Some(left) => waiter.wake.wait_timeout(slots, left).unwrap_or_else(PoisonError::into_inner).0,
            };
        }
    }

    /// A slot comes back: to the thread that has waited longest, else it is free again.
    fn give_back(&self) {
        let mut slots = lock(&self.slots);
        match slots.waiting.pop_front() {
            Some(waiter) => {
                waiter.granted.store(true, Ordering::Relaxed);
                waiter.wake.notify_one();
            }
            None => slots.available += 1,
        }
    }

    /// The pool closed: every waiting thread wakes and fails, and nobody waits from now on.
    pub(crate) fn close(&self) {
        let mut slots = lock(&self.slots);
        slots.closed = true;
        for waiter in &slots.waiting {
            waiter.wake.notify_one();
        }
    }
}

/// A slot a sync request holds, given back when dropped.
pub(crate) struct SyncPermit(Arc<SyncLimiter>);

impl Drop for SyncPermit {
    fn drop(&mut self) {
        self.0.give_back();
    }
}
