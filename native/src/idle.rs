//! When an idle keep-alive connection may still be reused, as hyper-util 0.1.21's legacy client
//! pool (reqwest 0.13's) decides it, so both native pools keep the same connections reqwest would:
//!
//! - a connection's idle clock starts when it goes back to the pool, and it has expired once it
//!   has been idle for longer than the timeout (strictly: idle for exactly the timeout is still
//!   reusable). An expired connection is closed when a checkout finds it, never reused;
//! - checkout takes the most recently idled connection first (LIFO), so spare connections age out;
//! - a connection going back to a pool already holding `max_per_host` idle ones for its origin is
//!   closed (the ones already idle stay), and `max_per_host = 0` turns pooling off entirely;
//! - the async pool also reaps: every `max(timeout, 90ms)` from when a connection first goes idle
//!   it closes the expired ones, stopping once none are idle (hyper-util's `IdleTask`).
use std::time::{Duration, Instant};

pub(crate) struct IdlePolicy {
    timeout: Duration,
    max_per_host: usize,
}

impl IdlePolicy {
    /// `None`s are reqwest's defaults: 90s, and no cap on idle connections per origin.
    pub(crate) fn new(timeout: Option<Duration>, max_per_host: Option<usize>) -> Self {
        Self { timeout: timeout.unwrap_or(Duration::from_secs(90)), max_per_host: max_per_host.unwrap_or(usize::MAX) }
    }

    /// Whether a connection idle since `idle_at` is too old to reuse at `now`.
    pub(crate) fn expired(&self, idle_at: Instant, now: Instant) -> bool {
        now.saturating_duration_since(idle_at) > self.timeout
    }

    /// Whether an origin holding `idle` idle connections may keep one more.
    pub(crate) fn has_room(&self, idle: usize) -> bool {
        idle < self.max_per_host
    }

    /// Whether a finished connection may serve another request at all.
    pub(crate) fn pools(&self) -> bool {
        self.max_per_host > 0
    }

    /// When the reaper next runs, if it starts at `now`: never for a zero timeout (checkout alone
    /// expires those), and no more often than every 90ms however short the timeout.
    pub(crate) fn next_reap(&self, now: Instant) -> Option<Instant> {
        if self.timeout.is_zero() {
            return None;
        }
        now.checked_add(self.timeout.max(Duration::from_millis(90)))
    }
}
