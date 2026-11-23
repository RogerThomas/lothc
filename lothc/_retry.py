"""Retrying as both transports do it (`max_retries=` and friends): which statuses are retried, how
long to wait before the next attempt, when a server's `Retry-After` ends retrying instead, and
asking a client's `retry=` rule about each failure that would be retried.

Both transports run `_retried` around each exchange (a whole redirect chain, re-authentication
included), one loop over the same helpers, so the two retry exactly alike. Not a pyreqwest client
middleware: that cost every async request ~57us, retried or not. A client's `request_hook=` is
entered per attempt (`_AttemptHook`), so the loop exits each attempt it retries.
"""

import asyncio
import inspect
import math
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any

from ._errors import HTTPResponseError, HTTPTransportError, RequestInfo
from ._settings import (
    RequestHook,
    RetryAttempt,
    _RequestHookFactory,
    _RetryRule,
    _TransportSettings,
    _validate_max_retries,
)


@dataclass(slots=True, frozen=True)
class _RetryPolicy:
    """A client's `max_retries`/`retry_methods`/`retry_statuses`/`backoff_base`/
    `max_retry_after`/`retry` (`_retry_policy`)."""

    max_retries: int
    # The upper-case methods the client retries: none when `max_retries` is 0, so a method's
    # membership alone says whether a call that doesn't pass its own `max_retries` retries.
    methods: frozenset[str]
    # The statuses that are retried; any other comes back from the first attempt.
    statuses: frozenset[int]
    backoff_base: float
    # The longest `Retry-After` honoured; a longer one ends retrying instead (`None`: no limit).
    max_retry_after: float | None
    # The client's `retry=`, asked about each failure the loop would retry (`None`: none).
    rule: _RetryRule | None


@dataclass(slots=True)
class _RetryRuling[T]:
    """What `_retried` needs to ask the client's `retry=` rule about an attempt: the rule, the
    call's method, and `status_error`, the `HTTPResponseError` an outcome's status would raise
    (its body read whole). Built per call, and only when the client has a rule, so a client
    without one pays nothing per attempt."""

    rule: _RetryRule
    method: str
    status_error: Callable[[T], Awaitable[HTTPResponseError[Any]]]


@dataclass(slots=True)
class _SyncRetryRuling[T]:
    """Sync mirror of `_RetryRuling`."""

    rule: _RetryRule
    method: str
    status_error: Callable[[T], HTTPResponseError[Any]]


@dataclass(slots=True)
class _AttemptHook:
    """A client's `request_hook=` over one call's attempts, made per call only when the client
    has one. `enter` opens the hook's context for an attempt, with the request about to go out,
    and gives the headers to add to it; `responded` records the attempt's status. Only the
    attempt in flight is open: `_retried` exits each attempt it retries, before waiting, and the
    call exits the last once its outcome is final (`close`, or `fail` with what it raises).

    An exception goes into the context as a `with` block raising it would, but the hook can't
    swallow it: the call raises it either way. `__exit__` is called directly, not through an
    `ExitStack`, which would make a swallowed exception the call's result."""

    factory: _RequestHookFactory
    # The request the hook is shown: known before the first attempt on the native transport, and
    # set once the request is built on pyreqwest's.
    request_info: RequestInfo | None = None
    _context: AbstractContextManager[RequestHook] | None = field(default=None, init=False)
    _hook: RequestHook | None = field(default=None, init=False)

    def _fail(self, error: BaseException) -> None:
        context, self._context, self._hook = self._context, None, None
        if context is not None:
            context.__exit__(type(error), error, error.__traceback__)

    def _close(self) -> None:
        context, self._context, self._hook = self._context, None, None
        if context is not None:
            context.__exit__(None, None, None)

    def enter(self) -> dict[str, str]:
        if self.request_info is None:
            raise RuntimeError("A request hook was entered before its request was built")
        context = self.factory(self.request_info)
        if not isinstance(context, AbstractContextManager):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"a request_hook returns a context manager (a `with` one), not {context!r}"
            )
        hook = context.__enter__()  # pylint: disable=unnecessary-dunder-call
        if not isinstance(hook, RequestHook):  # pyright: ignore[reportUnnecessaryIsInstance]
            error = TypeError(
                f"a request_hook's context manager yields a RequestHook, not {hook!r}"
            )
            context.__exit__(TypeError, error, None)
            raise error
        self._context = context
        self._hook = hook
        return hook.headers

    @property
    def headers(self) -> dict[str, str] | None:
        """The headers the attempt in flight was entered with, if one is."""
        return None if self._hook is None else self._hook.headers

    def responded(self, status: int) -> None:
        if self._hook is not None:
            self._hook.status = status

    def exit_with(
        self,
        _error_type: type[BaseException] | None,
        error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """`close`, or `fail` with `error`, as an exit callback on an exit stack, so the attempt
        exits with whatever the stack unwinds with (never swallowing it)."""
        if error is None:
            self._close()
        else:
            self._fail(error)

    def fail(self, error: BaseException) -> None:
        self._fail(error)

    def close(self) -> None:
        self._close()


async def _through_hook[T](hook: _AttemptHook, sent: Callable[[], Awaitable[T]]) -> T:
    """`sent()`, a call made with `hook`, whose last attempt then exits: with what the call
    raises (a status or decode error included), else cleanly. Only a call with a `request_hook=`
    comes through here, so a call without one has no frame of it."""
    try:
        outcome = await sent()
    except BaseException as error:
        hook.fail(error)
        raise
    hook.close()
    return outcome


def _through_hook_sync[T](hook: _AttemptHook, sent: Callable[[], T]) -> T:
    """Sync mirror of `_through_hook`."""
    try:
        outcome = sent()
    except BaseException as error:
        hook.fail(error)
        raise
    hook.close()
    return outcome


def _retry_policy(settings: _TransportSettings) -> _RetryPolicy:
    return _RetryPolicy(
        settings.max_retries,
        settings.retry_methods if settings.max_retries > 0 else frozenset(),
        settings.retry_statuses,
        settings.backoff_base,
        settings.max_retry_after,
        settings.retry_rule,
    )


def _call_retries(retry: _RetryPolicy, method: str, max_retries: int | None) -> int:
    """How many times one call may be retried: its own `max_retries`, else the client's for a
    method the client retries, else none. A call's own count opts it in whatever its method,
    since the caller asked for it; it's refused here, before sending, if it isn't a count. The
    multipart `form=` send-once rule is the caller's to apply on top.

    >>> policy = _RetryPolicy(2, frozenset({"GET"}), frozenset({503}), 0.1, 60.0, None)
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


def _ruled_wait(retry: _RetryPolicy, rule: _RetryRule, asked: RetryAttempt) -> float | None:
    """What a `retry=` rule's answer about `asked` means: the seconds to wait (a negative answer
    as 0), or `None` to stop retrying, which an answer above `max_retry_after` also means, as an
    over-long `Retry-After` does. Anything but a number or `None` is the rule's bug, raised from
    the call: no type checker catches a `return False` meant as "stop", which would otherwise
    retry at once.

    >>> policy = _RetryPolicy(2, frozenset({"GET"}), frozenset({503}), 0.1, 60.0, None)
    >>> asked = RetryAttempt(0, HTTPTransportError("x"), 0.1, "GET", 0.0)
    >>> [_ruled_wait(policy, lambda _: answer, asked) for answer in (None, 2, -1.0, 61.0)]
    [None, 2.0, 0.0, None]
    >>> _ruled_wait(policy, lambda _: False, asked)
    Traceback (most recent call last):
    ...
    TypeError: a retry rule returns the seconds to wait or None to stop, not False
    """
    answer = rule(asked)
    if answer is None:
        return None
    if isinstance(answer, bool) or not isinstance(answer, (int, float)):  # pyright: ignore[reportUnnecessaryIsInstance]
        if inspect.iscoroutine(answer):
            answer.close()  # never awaited: closed, so it doesn't warn as well
        raise TypeError(f"a retry rule returns the seconds to wait or None to stop, not {answer!r}")
    if not math.isfinite(answer):
        raise ValueError(f"a retry rule's wait must be a finite number of seconds, not {answer!r}")
    wait = max(0.0, float(answer))
    if _retry_after_exceeds(wait, retry.max_retry_after):
        return None
    return wait


async def _retried[T](
    retry: _RetryPolicy,
    retries: int,
    attempt: Callable[[], Awaitable[T]],
    retry_signal: Callable[[T], tuple[int, str | None]],
    retryable: Callable[[HTTPTransportError], bool],
    ruling: _RetryRuling[T] | None,
    hook: _AttemptHook | None,
) -> tuple[T, HTTPResponseError[Any] | None]:
    """`attempt()` until one succeeds, fails in a way no retry fixes (an error `retryable` turns
    down, a status outside `retry.statuses`), or the `retries` after the first run out, with
    `retry`'s backoff in between. `retry_signal` reads the status and `Retry-After` off what an
    attempt returned; an outcome the loop turns down is the attempt's own to release (the next
    `attempt()` call).

    With a `ruling`, the client's rule is asked about each failure the loop would retry, and its
    answer replaces the wait (`_ruled_wait`). Returns the last outcome, with the status error the
    rule was shown for it when the rule stopped retrying on it (`None` otherwise): its body has
    been read, so the caller raises or returns that error rather than reading the body again.
    With a `hook`, which each `attempt()` enters, an attempt that's retried is exited before the
    wait: with its transport error, or cleanly for a status (already `responded`). The last
    attempt's is the caller's to exit."""
    started = 0.0 if ruling is None else time.monotonic()
    for number in range(retries + 1):
        try:
            outcome = await attempt()
        except HTTPTransportError as error:
            if number == retries or not retryable(error):
                raise
            wait = _backoff_delay(number, retry.backoff_base, retry_after=None)
            if ruling is not None:
                elapsed = time.monotonic() - started
                asked = RetryAttempt(number, error, wait, ruling.method, elapsed)
                ruled = _ruled_wait(retry, ruling.rule, asked)
                if ruled is None:
                    raise
                wait = ruled
            if hook is not None:
                hook.fail(error)
            await asyncio.sleep(wait)
            continue
        status, retry_after_header = retry_signal(outcome)
        if number == retries or status not in retry.statuses:
            return outcome, None
        retry_after = _parse_retry_after(retry_after_header)
        wait = _backoff_delay(number, retry.backoff_base, retry_after)
        if ruling is None:
            if _retry_after_exceeds(retry_after, retry.max_retry_after):
                return outcome, None
        else:
            error = await ruling.status_error(outcome)
            elapsed = time.monotonic() - started
            ruled = _ruled_wait(
                retry, ruling.rule, RetryAttempt(number, error, wait, ruling.method, elapsed)
            )
            if ruled is None:
                return outcome, error
            wait = ruled
        if hook is not None:
            hook.close()
        await asyncio.sleep(wait)
    # range(retries+1) is never empty, so this is never actually reached.
    raise AssertionError("unreachable")  # pragma: no cover


def _retried_sync[T](
    retry: _RetryPolicy,
    retries: int,
    attempt: Callable[[], T],
    retry_signal: Callable[[T], tuple[int, str | None]],
    retryable: Callable[[HTTPTransportError], bool],
    ruling: _SyncRetryRuling[T] | None,
    hook: _AttemptHook | None,
) -> tuple[T, HTTPResponseError[Any] | None]:
    """Sync mirror of `_retried`."""
    started = 0.0 if ruling is None else time.monotonic()
    for number in range(retries + 1):
        try:
            outcome = attempt()
        except HTTPTransportError as error:
            if number == retries or not retryable(error):
                raise
            wait = _backoff_delay(number, retry.backoff_base, retry_after=None)
            if ruling is not None:
                elapsed = time.monotonic() - started
                asked = RetryAttempt(number, error, wait, ruling.method, elapsed)
                ruled = _ruled_wait(retry, ruling.rule, asked)
                if ruled is None:
                    raise
                wait = ruled
            if hook is not None:
                hook.fail(error)
            time.sleep(wait)
            continue
        status, retry_after_header = retry_signal(outcome)
        if number == retries or status not in retry.statuses:
            return outcome, None
        retry_after = _parse_retry_after(retry_after_header)
        wait = _backoff_delay(number, retry.backoff_base, retry_after)
        if ruling is None:
            if _retry_after_exceeds(retry_after, retry.max_retry_after):
                return outcome, None
        else:
            error = ruling.status_error(outcome)
            elapsed = time.monotonic() - started
            ruled = _ruled_wait(
                retry, ruling.rule, RetryAttempt(number, error, wait, ruling.method, elapsed)
            )
            if ruled is None:
                return outcome, error
            wait = ruled
        if hook is not None:
            hook.close()
        time.sleep(wait)
    # range(retries+1) is never empty, so this is never actually reached.
    raise AssertionError("unreachable")  # pragma: no cover
