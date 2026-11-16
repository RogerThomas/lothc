"""Retrying as both transports do it (`max_retries=` and friends): which statuses are retried, how
long to wait before the next attempt, and when a server's `Retry-After` ends retrying instead.

Both transports run `_retried` around each exchange (a whole redirect chain, re-authentication
included), one loop over the same helpers, so the two retry exactly alike. Not a pyreqwest client
middleware: that cost every async request ~57us, retried or not.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ._errors import HTTPTransportError
from ._settings import _TransportSettings, _validate_max_retries


@dataclass(slots=True, frozen=True)
class _RetryPolicy:
    """A client's `max_retries`/`retry_methods`/`retry_statuses`/`backoff_base`/
    `max_retry_after` (`_retry_policy`)."""

    max_retries: int
    # The upper-case methods the client retries: none when `max_retries` is 0, so a method's
    # membership alone says whether a call that doesn't pass its own `max_retries` retries.
    methods: frozenset[str]
    # The statuses that are retried; any other comes back from the first attempt.
    statuses: frozenset[int]
    backoff_base: float
    # The longest `Retry-After` honoured; a longer one ends retrying instead (`None`: no limit).
    max_retry_after: float | None


def _retry_policy(settings: _TransportSettings) -> _RetryPolicy:
    return _RetryPolicy(
        settings.max_retries,
        settings.retry_methods if settings.max_retries > 0 else frozenset(),
        settings.retry_statuses,
        settings.backoff_base,
        settings.max_retry_after,
    )


def _call_retries(retry: _RetryPolicy, method: str, max_retries: int | None) -> int:
    """How many times one call may be retried: its own `max_retries`, else the client's for a
    method the client retries, else none. A call's own count opts it in whatever its method,
    since the caller asked for it; it's refused here, before sending, if it isn't a count. The
    multipart `form=` send-once rule is the caller's to apply on top.

    >>> policy = _RetryPolicy(2, frozenset({"GET"}), frozenset({503}), 0.1, 60.0)
    >>> _call_retries(policy, "GET", None), _call_retries(policy, "POST", None)
    (2, 0)
    >>> _call_retries(policy, "GET", 0), _call_retries(policy, "POST", 5)
    (0, 5)
    """
    if max_retries is None:
        return retry.max_retries if method in retry.methods else 0
    _validate_max_retries(max_retries)
    return max_retries


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    if value.isdigit():
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _backoff_delay(attempt: int, backoff_base: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return backoff_base * (2**attempt)


def _retry_after_exceeds(retry_after: float | None, limit: float | None) -> bool:
    """Whether the server asked for a longer wait than the client will spend inside a call.

    If so retrying stops and that response is handed back, so the caller gets an
    `HTTPResponseError` whose `.headers["Retry-After"]` says when to try again, rather than a
    call silently blocking for as long as the server liked (`Retry-After: 3600` slept an hour).
    Waiting only part of the requested time and retrying anyway would just be rejected again.

    >>> _retry_after_exceeds(5.0, 60.0), _retry_after_exceeds(3600.0, 60.0)
    (False, True)
    >>> _retry_after_exceeds(3600.0, None), _retry_after_exceeds(None, 60.0)
    (False, False)
    """
    return retry_after is not None and limit is not None and retry_after > limit


async def _retried[T](
    retry: _RetryPolicy,
    retries: int,
    attempt: Callable[[], Awaitable[T]],
    retry_signal: Callable[[T], tuple[int, str | None]],
    retryable: Callable[[HTTPTransportError], bool],
) -> T:
    """`attempt()` until one succeeds, fails in a way no retry fixes (an error `retryable` turns
    down, a status outside `retry.statuses`), or the `retries` after the first run out, with
    `retry`'s backoff in between. `retry_signal` reads the status and `Retry-After` off what an
    attempt returned; an outcome the loop turns down is the attempt's own to release (the next
    `attempt()` call)."""
    for number in range(retries + 1):
        try:
            outcome = await attempt()
        except HTTPTransportError as error:
            if number == retries or not retryable(error):
                raise
            await asyncio.sleep(_backoff_delay(number, retry.backoff_base, retry_after=None))
            continue
        status, retry_after_header = retry_signal(outcome)
        if number == retries or status not in retry.statuses:
            return outcome
        retry_after = _parse_retry_after(retry_after_header)
        if _retry_after_exceeds(retry_after, retry.max_retry_after):
            return outcome
        await asyncio.sleep(_backoff_delay(number, retry.backoff_base, retry_after))
    # range(retries+1) is never empty, so this is never actually reached.
    raise AssertionError("unreachable")  # pragma: no cover


def _retried_sync[T](
    retry: _RetryPolicy,
    retries: int,
    attempt: Callable[[], T],
    retry_signal: Callable[[T], tuple[int, str | None]],
    retryable: Callable[[HTTPTransportError], bool],
) -> T:
    """Sync mirror of `_retried`."""
    for number in range(retries + 1):
        try:
            outcome = attempt()
        except HTTPTransportError as error:
            if number == retries or not retryable(error):
                raise
            time.sleep(_backoff_delay(number, retry.backoff_base, retry_after=None))
            continue
        status, retry_after_header = retry_signal(outcome)
        if number == retries or status not in retry.statuses:
            return outcome
        retry_after = _parse_retry_after(retry_after_header)
        if _retry_after_exceeds(retry_after, retry.max_retry_after):
            return outcome
        time.sleep(_backoff_delay(number, retry.backoff_base, retry_after))
    # range(retries+1) is never empty, so this is never actually reached.
    raise AssertionError("unreachable")  # pragma: no cover
