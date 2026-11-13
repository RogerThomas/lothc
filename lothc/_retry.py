"""Retrying as both transports do it (`max_retries=` and friends): which statuses are retried, how
long to wait before the next attempt, and when a server's `Retry-After` ends retrying instead.

The pyreqwest transport retries in a middleware around each `send()` (`_RetryMiddleware`); the
native one runs `_retried` around each exchange instead, the same loop over the same helpers, so
the two retry exactly alike.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from ._errors import HTTPTransportError
from ._settings import _TransportSettings


@dataclass(slots=True, frozen=True)
class _RetryPolicy:
    """A client's `max_retries`/`retry_methods`/`backoff_base`/`max_retry_after`, when it retries
    at all (`_retry_policy`)."""

    max_retries: int
    # The upper-case methods that are retried; any other request goes out once.
    methods: frozenset[str]
    backoff_base: float
    # The longest `Retry-After` honoured; a longer one ends retrying instead (`None`: no limit).
    max_retry_after: float | None


def _retry_policy(settings: _TransportSettings) -> _RetryPolicy | None:
    """The client's retry policy, `None` without retries (`max_retries` below 1)."""
    if settings.max_retries <= 0:
        return None
    return _RetryPolicy(
        settings.max_retries,
        settings.retry_methods,
        settings.backoff_base,
        settings.max_retry_after,
    )


def _is_retryable_status(status: int) -> bool:
    """Whether a response's status is worth another attempt: rate limiting and the server errors a
    retry can outlast, never a 501 or 505, which no retry changes.

    >>> [code for code in (408, 429, 500, 501, 502, 503, 504, 505) if _is_retryable_status(code)]
    [429, 500, 502, 503, 504]
    """
    return status in {429, 500, 502, 503, 504}


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
    attempt: Callable[[], Awaitable[T]],
    retry_signal: Callable[[T], tuple[int, str | None]],
    retryable: Callable[[HTTPTransportError], bool],
) -> T:
    """`_RetryMiddleware`, for the native transport: `attempt()` until one succeeds, fails in a
    way no retry fixes (an error `retryable` turns down), or the attempts run out, waiting as
    `_RetryMiddleware` waits in between. `retry_signal` reads the status and `Retry-After` off
    what an attempt returned."""
    for number in range(retry.max_retries + 1):
        try:
            outcome = await attempt()
        except HTTPTransportError as error:
            if number == retry.max_retries or not retryable(error):
                raise
            await asyncio.sleep(_backoff_delay(number, retry.backoff_base, retry_after=None))
            continue
        status, retry_after_header = retry_signal(outcome)
        if number == retry.max_retries or not _is_retryable_status(status):
            return outcome
        retry_after = _parse_retry_after(retry_after_header)
        if _retry_after_exceeds(retry_after, retry.max_retry_after):
            return outcome
        await asyncio.sleep(_backoff_delay(number, retry.backoff_base, retry_after))
    # range(max_retries+1) is never empty, so this is never actually reached.
    raise AssertionError("unreachable")  # pragma: no cover


def _retried_sync[T](
    retry: _RetryPolicy,
    attempt: Callable[[], T],
    retry_signal: Callable[[T], tuple[int, str | None]],
    retryable: Callable[[HTTPTransportError], bool],
) -> T:
    """Sync mirror of `_retried`."""
    for number in range(retry.max_retries + 1):
        try:
            outcome = attempt()
        except HTTPTransportError as error:
            if number == retry.max_retries or not retryable(error):
                raise
            time.sleep(_backoff_delay(number, retry.backoff_base, retry_after=None))
            continue
        status, retry_after_header = retry_signal(outcome)
        if number == retry.max_retries or not _is_retryable_status(status):
            return outcome
        retry_after = _parse_retry_after(retry_after_header)
        if _retry_after_exceeds(retry_after, retry.max_retry_after):
            return outcome
        time.sleep(_backoff_delay(number, retry.backoff_base, retry_after))
    # range(max_retries+1) is never empty, so this is never actually reached.
    raise AssertionError("unreachable")  # pragma: no cover
