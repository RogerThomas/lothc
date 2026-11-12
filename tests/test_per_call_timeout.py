"""A per-call `timeout=` at its edges, on the buffered verbs and as the streaming verbs' total cap.

It goes to pyreqwest as a `timedelta`, so it is rounded to the microsecond: anything under half a
microsecond either side of zero is zero, and a NaN, infinite or out-of-range value fails to become
one. A negative `timedelta` can't become a Rust `Duration`. The deadline itself is tokio's, which
rounds it up to the end of its millisecond: a zero timeout is not expired on arrival but leaves
the request whatever is left of that millisecond, enough for a quick exchange, rarely for a
slower one, never for one that takes longer than a millisecond.
"""

import math
import time
from collections.abc import Awaitable, Callable

import pytest

from lothc import HTTPClient, HTTPTimeoutError, SyncHTTPClient

type _Params = dict[str, float]
type _Call = Callable[[HTTPClient, str, _Params, float], Awaitable[object]]
type _SyncCall = Callable[[SyncHTTPClient, str, _Params, float], object]


async def _get(client: HTTPClient, path: str, params: _Params, timeout: float) -> object:
    return (await client.get(path, params=params, timeout=timeout)).data


async def _stream_get(client: HTTPClient, path: str, params: _Params, timeout: float) -> object:
    return [chunk async for chunk in client.stream_get(path, params=params, timeout=timeout)]


async def _download(client: HTTPClient, path: str, params: _Params, timeout: float) -> object:
    return (await client.download(path, params=params, timeout=timeout)).data


def _sync_get(client: SyncHTTPClient, path: str, params: _Params, timeout: float) -> object:
    return client.get(path, params=params, timeout=timeout).data


def _sync_stream_get(client: SyncHTTPClient, path: str, params: _Params, timeout: float) -> object:
    return list(client.stream_get(path, params=params, timeout=timeout))


def _sync_download(client: SyncHTTPClient, path: str, params: _Params, timeout: float) -> object:
    return client.download(path, params=params, timeout=timeout).data


async def _succeeds_once(client: HTTPClient, call: _Call) -> bool:
    # Whether one try succeeds depends on where in its millisecond it starts, so this keeps trying
    # for a while; a timeout that is expired on arrival never succeeds. Each try goes out on a
    # connection a request without a timeout has just left open: one that times out takes its
    # connection with it, and opening the next (connect, accept, the server's handler thread)
    # outlasted the millisecond on every try on a slow machine.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        await client.get("items/7")
        try:
            await call(client, "items/7", {}, 0.0)
        except HTTPTimeoutError:
            continue
        return True
    return False


def _sync_succeeds_once(client: SyncHTTPClient, call: _SyncCall) -> bool:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        client.get("items/7")
        try:
            call(client, "items/7", {}, 0.0)
        except HTTPTimeoutError:
            continue
        return True
    return False


_calls = pytest.mark.parametrize(
    "call", [_get, _stream_get, _download], ids=["get", "stream_get", "download"]
)
_sync_calls = pytest.mark.parametrize(
    "call", [_sync_get, _sync_stream_get, _sync_download], ids=["get", "stream_get", "download"]
)
# Each is a zero `timedelta`, or a sub-millisecond one.
_zero_or_sub_millisecond = pytest.mark.parametrize(
    "timeout",
    [0.0, -0.0, 1e-9, -1e-9, 4e-7, -4e-7, 1e-6, 5e-4],
    ids=["zero", "negative-zero", "1ns", "minus-1ns", "400ns", "minus-400ns", "1us", "500us"],
)
# Each is a negative `timedelta`: -6e-7 rounds to minus one microsecond.
_negative = pytest.mark.parametrize("timeout", [-1.0, -1e-6, -6e-7], ids=["-1s", "-1us", "-600ns"])
_unrepresentable = pytest.mark.parametrize(
    ("timeout", "error", "match"),
    [
        (math.nan, ValueError, "NaN"),
        (math.inf, OverflowError, "infinity"),
        (-math.inf, OverflowError, "infinity"),
        (1e15, OverflowError, "too large"),
    ],
    ids=["nan", "inf", "minus-inf", "1e15s"],
)


@_calls
@_zero_or_sub_millisecond
async def test_a_zero_or_sub_millisecond_timeout_times_out_a_slower_request(
    client: HTTPClient, call: _Call, timeout: float
) -> None:
    with pytest.raises(HTTPTimeoutError):
        await call(client, "slow", {"seconds": 0.5}, timeout)


@_sync_calls
@_zero_or_sub_millisecond
def test_sync_a_zero_or_sub_millisecond_timeout_times_out_a_slower_request(
    sync_client: SyncHTTPClient, call: _SyncCall, timeout: float
) -> None:
    with pytest.raises(HTTPTimeoutError):
        call(sync_client, "slow", {"seconds": 0.5}, timeout)


@_calls
async def test_a_zero_timeout_leaves_a_quick_request_the_rest_of_its_millisecond(
    client: HTTPClient, call: _Call
) -> None:
    assert await _succeeds_once(client, call)


@_sync_calls
def test_sync_a_zero_timeout_leaves_a_quick_request_the_rest_of_its_millisecond(
    sync_client: SyncHTTPClient, call: _SyncCall
) -> None:
    assert _sync_succeeds_once(sync_client, call)


@_calls
@_negative
async def test_a_negative_timeout_raises_value_error(
    client: HTTPClient, call: _Call, timeout: float
) -> None:
    with pytest.raises(ValueError, match="negative timedelta"):
        await call(client, "items/7", {}, timeout)


@_sync_calls
@_negative
def test_sync_a_negative_timeout_raises_value_error(
    sync_client: SyncHTTPClient, call: _SyncCall, timeout: float
) -> None:
    with pytest.raises(ValueError, match="negative timedelta"):
        call(sync_client, "items/7", {}, timeout)


@_calls
@_unrepresentable
async def test_a_timeout_no_timedelta_can_hold_raises(
    client: HTTPClient, call: _Call, timeout: float, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        await call(client, "items/7", {}, timeout)


@_sync_calls
@_unrepresentable
def test_sync_a_timeout_no_timedelta_can_hold_raises(
    sync_client: SyncHTTPClient,
    call: _SyncCall,
    timeout: float,
    error: type[Exception],
    match: str,
) -> None:
    with pytest.raises(error, match=match):
        call(sync_client, "items/7", {}, timeout)


@_calls
async def test_a_timeout_too_long_to_reach_never_fires(client: HTTPClient, call: _Call) -> None:
    # About 31,700 years: a valid `timedelta`, past any deadline the clock can reach.
    assert await call(client, "items/7", {}, 1e12)


@_sync_calls
def test_sync_a_timeout_too_long_to_reach_never_fires(
    sync_client: SyncHTTPClient, call: _SyncCall
) -> None:
    assert call(sync_client, "items/7", {}, 1e12)
