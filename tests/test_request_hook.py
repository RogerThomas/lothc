"""A client's `request_hook=`: entered once per attempt at a request, shown its `RequestInfo`,
its headers sent on that attempt and its `status` set once a response arrives, and whatever the
attempt ends in raised into it."""

import _thread
import json
import threading
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import pytest
from _helpers import echoed_header_values, release, sync_arrived, sync_release

from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPDecodeError,
    HTTPResponseError,
    HTTPTimeoutError,
    RequestHook,
    RequestInfo,
    SyncHTTPClient,
)
from lothc.testing import LOTHCMocker, MockRequest, MockResponse


@dataclass(slots=True)
class _Attempt:
    """One time the hook was entered: the request it was shown, what it yielded, and how its
    context ended (`exited`, with `error` if an exception was raised into it)."""

    request: RequestInfo
    hook: RequestHook
    exited: bool = False
    error: BaseException | None = None


@dataclass(slots=True)
class _Recorder:
    """A `request_hook=` that records every attempt in `attempts` (and each entry and exit in
    `events`), adding `headers` and `x-attempt`, the attempt's number from 1, to each."""

    headers: dict[str, str] = field(default_factory=dict[str, str])
    attempts: list[_Attempt] = field(default_factory=list[_Attempt])
    events: list[str] = field(default_factory=list[str])

    @contextmanager
    def __call__(self, request: RequestInfo) -> Generator[RequestHook]:
        number = len(self.attempts) + 1
        hook = RequestHook({**self.headers, "x-attempt": str(number)})
        attempt = _Attempt(request, hook)
        self.attempts.append(attempt)
        self.events.append(f"enter {number}")
        try:
            yield hook
        except BaseException as error:
            attempt.error = error
            raise
        finally:
            attempt.exited = True
            self.events.append(f"exit {number}")


@dataclass(slots=True)
class _FailsFirst:
    """A mock handler answering 503 to its first request and 200 to every later one."""

    calls: int = 0

    def __call__(self, _request: MockRequest) -> MockResponse:
        self.calls += 1
        return MockResponse(status=503 if self.calls == 1 else 200, data={})


@dataclass(slots=True)
class _ContextReader:
    """A `request_hook=` that records `variable`'s value each time it's entered."""

    variable: ContextVar[str]
    seen: list[str] = field(default_factory=list[str])

    @contextmanager
    def __call__(self, _request: RequestInfo) -> Generator[RequestHook]:
        self.seen.append(self.variable.get("unset"))
        yield RequestHook()


@dataclass(slots=True)
class _WrongYield:
    """A `request_hook=` whose context manager yields something other than a `RequestHook`,
    recording what its context exits with."""

    exits: list[BaseException | None] = field(default_factory=list[BaseException | None])

    @contextmanager
    def __call__(self, _request: RequestInfo) -> Generator[RequestHook]:
        wrong: Any = "not-a-hook"
        try:
            yield wrong
        except BaseException as error:
            self.exits.append(error)
            raise
        self.exits.append(None)


def _wait_until_exited(attempt: _Attempt) -> None:
    """Wait for the sync stream's worker thread to exit `attempt`'s context, which it does once
    the stalled open returns."""
    deadline = time.monotonic() + 5
    while not attempt.exited and time.monotonic() < deadline:
        time.sleep(0.01)


def _interrupt_once_arrived(base_url: str, key: str) -> None:
    """Ctrl-C, on the main thread, once the request counting `key` is waiting on the server."""
    sync_arrived(base_url, key)
    _thread.interrupt_main()


@contextmanager
def _swallowing_hook(_request: RequestInfo) -> Generator[RequestHook]:
    with suppress(HTTPResponseError):
        yield RequestHook()


@asynccontextmanager
async def _async_hook(_request: RequestInfo) -> AsyncGenerator[RequestHook]:
    yield RequestHook()


@pytest.fixture(name="recorder")
def _recorder() -> _Recorder:
    return _Recorder({"x-hook": "hook"})


@pytest.fixture(name="hook_client")
async def _hook_client(base_url: str, recorder: _Recorder) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, request_hook=recorder) as client:
        yield client


@pytest.fixture(name="sync_hook_client")
def _sync_hook_client(base_url: str, recorder: _Recorder) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, request_hook=recorder) as client:
        yield client


@pytest.fixture(name="retrying_hook_client")
async def _retrying_hook_client(base_url: str, recorder: _Recorder) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(
        base_url=base_url, max_retries=2, backoff_base=0.001, request_hook=recorder
    ) as client:
        yield client


@pytest.fixture(name="sync_retrying_hook_client")
def _sync_retrying_hook_client(base_url: str, recorder: _Recorder) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(
        base_url=base_url, max_retries=2, backoff_base=0.001, request_hook=recorder
    ) as client:
        yield client


# --- The request it's shown, the headers it sends -----------------------------------------------


async def test_the_hook_is_shown_the_full_url_and_its_headers_are_sent(
    base_url: str, recorder: _Recorder, hook_client: HTTPClient
) -> None:
    response = await hook_client.get("echo-headers", params={"a": "1"}, into=dict)
    [attempt] = recorder.attempts

    assert (attempt.request.method, attempt.request.url) == (
        "GET",
        f"{base_url}echo-headers?a=1",
    )
    assert attempt.request == response.request
    assert echoed_header_values(response.data, "x-hook") == ["hook"]
    assert (attempt.hook.status, attempt.exited, attempt.error) == (200, True, None)


def test_sync_the_hook_is_shown_the_full_url_and_its_headers_are_sent(
    base_url: str, recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    response = sync_hook_client.get("echo-headers", params={"a": "1"}, into=dict)
    [attempt] = recorder.attempts

    assert (attempt.request.method, attempt.request.url) == (
        "GET",
        f"{base_url}echo-headers?a=1",
    )
    assert attempt.request == response.request
    assert echoed_header_values(response.data, "x-hook") == ["hook"]
    assert (attempt.hook.status, attempt.exited, attempt.error) == (200, True, None)


async def test_a_hook_header_replaces_one_of_the_same_name(base_url: str) -> None:
    recorder = _Recorder({"X-Call": "hook", "x-default": "hook"})
    async with HTTPClient(
        base_url=base_url, default_headers={"X-Default": "default"}, request_hook=recorder
    ) as client:
        response = await client.get("echo-headers", headers={"x-call": "call"}, into=dict)

    assert echoed_header_values(response.data, "x-call") == ["hook"]
    assert echoed_header_values(response.data, "x-default") == ["hook"]


def test_sync_a_hook_header_replaces_one_of_the_same_name(base_url: str) -> None:
    recorder = _Recorder({"X-Call": "hook", "x-default": "hook"})
    with SyncHTTPClient(
        base_url=base_url, default_headers={"X-Default": "default"}, request_hook=recorder
    ) as client:
        response = client.get("echo-headers", headers={"x-call": "call"}, into=dict)

    assert echoed_header_values(response.data, "x-call") == ["hook"]
    assert echoed_header_values(response.data, "x-default") == ["hook"]


async def test_a_mocked_request_carries_the_hooks_headers(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_post_response(path="/items", data={})
    async with HTTPClient(base_url="http://testserver/", request_hook=_Recorder()) as client:
        await client.post("items", json={"a": 1})

    [seen] = mock.get_requests()
    assert seen.headers["x-attempt"] == "1"


def test_sync_a_mocked_request_carries_the_hooks_headers(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_post_response(path="/items", data={})
    with SyncHTTPClient(base_url="http://testserver/", request_hook=_Recorder()) as client:
        client.post("items", json={"a": 1})

    [seen] = mock.get_requests()
    assert seen.headers["x-attempt"] == "1"


async def test_a_mocked_stream_carries_the_hooks_headers(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_get_response(path="/items", data={})
    async with HTTPClient(base_url="http://testserver/", request_hook=_Recorder()) as client:
        _ = [chunk async for chunk in client.stream_get("items")]

    [seen] = mock.get_requests()
    assert seen.headers["x-attempt"] == "1"


def test_sync_a_mocked_stream_carries_the_hooks_headers(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_get_response(path="/items", data={})
    with SyncHTTPClient(base_url="http://testserver/", request_hook=_Recorder()) as client:
        _ = list(client.stream_get("items"))

    [seen] = mock.get_requests()
    assert seen.headers["x-attempt"] == "1"


# --- One context per attempt --------------------------------------------------------------------


async def test_each_retry_is_an_attempt_of_its_own(
    server_key: str, recorder: _Recorder, retrying_hook_client: HTTPClient
) -> None:
    response = await retrying_hook_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 2}, into=dict
    )

    assert response.data["attempts"] == 3
    assert recorder.events == ["enter 1", "exit 1", "enter 2", "exit 2", "enter 3", "exit 3"]
    assert [attempt.hook.status for attempt in recorder.attempts] == [503, 503, 200]
    assert [attempt.error for attempt in recorder.attempts] == [None, None, None]


def test_sync_each_retry_is_an_attempt_of_its_own(
    server_key: str, recorder: _Recorder, sync_retrying_hook_client: SyncHTTPClient
) -> None:
    response = sync_retrying_hook_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 2}, into=dict
    )

    assert response.data["attempts"] == 3
    assert recorder.events == ["enter 1", "exit 1", "enter 2", "exit 2", "enter 3", "exit 3"]
    assert [attempt.hook.status for attempt in recorder.attempts] == [503, 503, 200]
    assert [attempt.error for attempt in recorder.attempts] == [None, None, None]


async def test_each_retry_sends_its_own_headers(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.mock("GET").match_request_with_response(_FailsFirst())
    async with HTTPClient(
        base_url="http://testserver/", max_retries=1, backoff_base=0.001, request_hook=_Recorder()
    ) as client:
        await client.get("items")

    assert [seen.headers["x-attempt"] for seen in mock.get_requests()] == ["1", "2"]


def test_sync_each_retry_sends_its_own_headers(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.mock("GET").match_request_with_response(_FailsFirst())
    with SyncHTTPClient(
        base_url="http://testserver/", max_retries=1, backoff_base=0.001, request_hook=_Recorder()
    ) as client:
        client.get("items")

    assert [seen.headers["x-attempt"] for seen in mock.get_requests()] == ["1", "2"]


async def test_a_redirect_chain_is_one_attempt(
    base_url: str, recorder: _Recorder, hook_client: HTTPClient
) -> None:
    await hook_client.get("redirect-chain", params={"hops": 2})
    [attempt] = recorder.attempts

    assert attempt.request.url == f"{base_url}redirect-chain?hops=2"
    assert attempt.hook.status == 200


def test_sync_a_redirect_chain_is_one_attempt(
    base_url: str, recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    sync_hook_client.get("redirect-chain", params={"hops": 2})
    [attempt] = recorder.attempts

    assert attempt.request.url == f"{base_url}redirect-chain?hops=2"
    assert attempt.hook.status == 200


# --- What an attempt ends in goes into it -------------------------------------------------------


async def test_a_retried_transport_error_is_raised_into_its_attempt(
    server_key: str, recorder: _Recorder, retrying_hook_client: HTTPClient
) -> None:
    await retrying_hook_client.get("connection-flaky", params={"key": server_key, "fail_times": 1})
    first, second = recorder.attempts

    assert isinstance(first.error, HTTPConnectionError)
    assert (first.hook.status, first.exited) == (None, True)
    assert (second.hook.status, second.error) == (200, None)


def test_sync_a_retried_transport_error_is_raised_into_its_attempt(
    server_key: str, recorder: _Recorder, sync_retrying_hook_client: SyncHTTPClient
) -> None:
    sync_retrying_hook_client.get("connection-flaky", params={"key": server_key, "fail_times": 1})
    first, second = recorder.attempts

    assert isinstance(first.error, HTTPConnectionError)
    assert (first.hook.status, first.exited) == (None, True)
    assert (second.hook.status, second.error) == (200, None)


async def test_a_status_error_is_raised_into_the_hook_with_its_status_set(
    recorder: _Recorder, hook_client: HTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as raised:
        await hook_client.get("boom")
    [attempt] = recorder.attempts

    assert attempt.error is raised.value
    assert attempt.hook.status == 500


def test_sync_a_status_error_is_raised_into_the_hook_with_its_status_set(
    recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as raised:
        sync_hook_client.get("boom")
    [attempt] = recorder.attempts

    assert attempt.error is raised.value
    assert attempt.hook.status == 500


async def test_a_decode_error_is_raised_into_the_hook(
    recorder: _Recorder, hook_client: HTTPClient
) -> None:
    with pytest.raises(HTTPDecodeError) as raised:
        await hook_client.get("invalid-json", into=dict)
    [attempt] = recorder.attempts

    assert attempt.error is raised.value
    assert attempt.hook.status == 200


def test_sync_a_decode_error_is_raised_into_the_hook(
    recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    with pytest.raises(HTTPDecodeError) as raised:
        sync_hook_client.get("invalid-json", into=dict)
    [attempt] = recorder.attempts

    assert attempt.error is raised.value
    assert attempt.hook.status == 200


async def test_a_hook_cannot_swallow_the_error(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, request_hook=_swallowing_hook) as client:
        with pytest.raises(HTTPResponseError):
            await client.get("boom")


def test_sync_a_hook_cannot_swallow_the_error(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, request_hook=_swallowing_hook) as client,
        pytest.raises(HTTPResponseError),
    ):
        client.get("boom")


async def test_a_request_that_cannot_be_built_never_enters_the_hook(
    recorder: _Recorder, hook_client: HTTPClient
) -> None:
    with pytest.raises(ValueError, match="header"):
        await hook_client.get("echo-headers", headers={"x-bad": "bad\n"})
    with pytest.raises(ValueError, match="header"):
        _ = [chunk async for chunk in hook_client.stream_get("echo-headers", headers={"x": "\n"})]

    assert recorder.attempts == []


def test_sync_a_request_that_cannot_be_built_never_enters_the_hook(
    recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    with pytest.raises(ValueError, match="header"):
        sync_hook_client.get("echo-headers", headers={"x-bad": "bad\n"})
    with pytest.raises(ValueError, match="header"):
        _ = list(sync_hook_client.stream_get("echo-headers", headers={"x": "\n"}))

    assert recorder.attempts == []


async def test_a_hook_that_yields_no_request_hook_is_exited_with_the_type_error(
    base_url: str,
) -> None:
    hook = _WrongYield()
    async with HTTPClient(base_url=base_url, request_hook=hook) as client:
        with pytest.raises(TypeError, match="yields a RequestHook") as raised:
            await client.get("echo-headers")

    assert hook.exits == [raised.value]


def test_sync_a_hook_that_yields_no_request_hook_is_exited_with_the_type_error(
    base_url: str,
) -> None:
    hook = _WrongYield()
    with (
        SyncHTTPClient(base_url=base_url, request_hook=hook) as client,
        pytest.raises(TypeError, match="yields a RequestHook") as raised,
    ):
        client.get("echo-headers")

    assert hook.exits == [raised.value]


# --- Streaming verbs ----------------------------------------------------------------------------


async def test_a_stream_exits_the_hook_once_its_headers_arrive(
    server_key: str, recorder: _Recorder, hook_client: HTTPClient
) -> None:
    chunks = hook_client.stream_get("endless", params={"key": server_key})
    await anext(chunks)
    [attempt] = recorder.attempts
    await chunks.aclose()

    assert (attempt.hook.status, attempt.exited, attempt.error) == (200, True, None)


def test_sync_a_stream_exits_the_hook_once_its_headers_arrive(
    server_key: str, recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    chunks = sync_hook_client.stream_get("endless", params={"key": server_key})
    next(chunks)
    [attempt] = recorder.attempts
    chunks.close()

    assert (attempt.hook.status, attempt.exited, attempt.error) == (200, True, None)


async def test_a_streams_idle_timeout_while_opening_is_raised_into_the_hook(
    base_url: str, server_key: str, recorder: _Recorder
) -> None:
    async with HTTPClient(base_url=base_url, timeout=0.05, request_hook=recorder) as client:
        with pytest.raises(HTTPTimeoutError) as raised:
            _ = [
                chunk
                async for chunk in client.stream_get(
                    "slow", params={"key": server_key, "seconds": 5}
                )
            ]
    await release(base_url, server_key)
    [attempt] = recorder.attempts

    assert attempt.error is raised.value


def test_sync_a_streams_idle_timeout_while_opening_is_raised_into_the_hook(
    base_url: str, server_key: str, recorder: _Recorder
) -> None:
    with SyncHTTPClient(base_url=base_url, timeout=0.05, request_hook=recorder) as client:
        with pytest.raises(HTTPTimeoutError) as raised:
            _ = list(client.stream_get("slow", params={"key": server_key, "seconds": 5}))
        # The worker thread is still waiting for the head: answered now, it exits the hook.
        sync_release(base_url, server_key)
        [attempt] = recorder.attempts
        _wait_until_exited(attempt)

    # Its own instance: exiting the hook from the worker thread leaves the caller's alone.
    assert isinstance(attempt.error, HTTPTimeoutError)
    assert attempt.error is not raised.value
    assert str(attempt.error) == str(raised.value)


async def test_a_stream_given_up_on_while_opening_is_not_retried(
    base_url: str, server_key: str, recorder: _Recorder
) -> None:
    async with HTTPClient(
        base_url=base_url, timeout=0.05, max_retries=3, backoff_base=0.001, request_hook=recorder
    ) as client:
        with pytest.raises(HTTPTimeoutError) as raised:
            _ = [
                chunk
                async for chunk in client.stream_get(
                    "slow", params={"key": server_key, "seconds": 5, "status": 503}
                )
            ]
        await release(base_url, server_key)
    [attempt] = recorder.attempts

    # The idle limit cancels the retry loop itself, whether or not the request had gone out.
    assert attempt.error is raised.value
    assert recorder.events == ["enter 1", "exit 1"]


def test_sync_a_stream_given_up_on_while_opening_is_not_retried(
    base_url: str, server_key: str, recorder: _Recorder
) -> None:
    with SyncHTTPClient(
        base_url=base_url, timeout=0.05, max_retries=3, backoff_base=0.001, request_hook=recorder
    ) as client:
        with pytest.raises(HTTPTimeoutError) as raised:
            _ = list(
                client.stream_get("slow", params={"key": server_key, "seconds": 5, "status": 503})
            )
        traceback = raised.value.__traceback__
        # Answered once it's there, with a status the client retries: the worker thread gives up
        # instead.
        sync_arrived(base_url, server_key)
        sync_release(base_url, server_key)
        [attempt] = recorder.attempts
        _wait_until_exited(attempt)
    with SyncHTTPClient(base_url=base_url) as client:
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert isinstance(attempt.error, HTTPTimeoutError)
    assert attempt.error is not raised.value
    assert raised.value.__traceback__ is traceback
    assert (recorder.events, hits) == (["enter 1", "exit 1"], {"hits": 1})


def test_sync_a_stream_interrupted_while_opening_is_not_retried(
    base_url: str, server_key: str, recorder: _Recorder
) -> None:
    interrupter = threading.Thread(target=_interrupt_once_arrived, args=(base_url, server_key))
    with SyncHTTPClient(
        base_url=base_url, max_retries=3, backoff_base=0.001, request_hook=recorder
    ) as client:
        interrupter.start()
        with pytest.raises(KeyboardInterrupt):
            _ = list(
                client.stream_get(
                    "slow",
                    params={"key": server_key, "seconds": 5, "status": 503},
                    interruptible=True,
                )
            )
        interrupter.join()
        sync_release(base_url, server_key)
        [attempt] = recorder.attempts
        _wait_until_exited(attempt)
    with SyncHTTPClient(base_url=base_url) as client:
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert isinstance(attempt.error, KeyboardInterrupt)
    assert (recorder.events, hits) == (["enter 1", "exit 1"], {"hits": 1})


async def test_a_stream_sends_the_hooks_headers(
    recorder: _Recorder, hook_client: HTTPClient
) -> None:
    body = b"".join([chunk async for chunk in hook_client.stream_get("echo-headers")])

    assert echoed_header_values(json.loads(body), "x-hook") == ["hook"]
    assert recorder.attempts[0].request.path == "/echo-headers"


def test_sync_a_stream_sends_the_hooks_headers(
    recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    body = b"".join(sync_hook_client.stream_get("echo-headers"))

    assert echoed_header_values(json.loads(body), "x-hook") == ["hook"]
    assert recorder.attempts[0].request.path == "/echo-headers"


async def test_a_streams_hook_runs_in_the_callers_context(base_url: str) -> None:
    variable = ContextVar[str]("variable")
    reader = _ContextReader(variable)
    variable.set("caller")
    async with HTTPClient(base_url=base_url, request_hook=reader) as client:
        _ = [chunk async for chunk in client.stream_get("echo-headers")]

    assert reader.seen == ["caller"]


def test_sync_a_streams_hook_runs_in_the_callers_context(base_url: str) -> None:
    # The sync client opens a stream on a worker thread, which must still see the caller's
    # context variables (a tracer's active span, say).
    variable = ContextVar[str]("variable")
    reader = _ContextReader(variable)
    token = variable.set("caller")
    with SyncHTTPClient(base_url=base_url, request_hook=reader) as client:
        _ = list(client.stream_get("echo-headers"))
    variable.reset(token)

    assert reader.seen == ["caller"]


async def test_each_sse_reconnect_is_an_attempt_of_its_own(
    server_key: str, recorder: _Recorder, hook_client: HTTPClient
) -> None:
    events = hook_client.sse(
        "events-drops", params={"key": server_key, "drops": 1}, reconnect_delay=0.001
    )
    data = [event.data async for event in events]

    assert data == ["done"]
    assert recorder.events == ["enter 1", "exit 1", "enter 2", "exit 2"]
    assert [attempt.hook.status for attempt in recorder.attempts] == [200, 200]


def test_sync_each_sse_reconnect_is_an_attempt_of_its_own(
    server_key: str, recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    events = sync_hook_client.sse(
        "events-drops", params={"key": server_key, "drops": 1}, reconnect_delay=0.001
    )
    data = [event.data for event in events]

    assert data == ["done"]
    assert recorder.events == ["enter 1", "exit 1", "enter 2", "exit 2"]
    assert [attempt.hook.status for attempt in recorder.attempts] == [200, 200]


async def test_a_download_status_error_is_raised_into_the_hook(
    recorder: _Recorder, hook_client: HTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as raised:
        await hook_client.download("boom")
    [attempt] = recorder.attempts

    assert attempt.error is raised.value
    assert attempt.hook.status == 500


def test_sync_a_download_status_error_is_raised_into_the_hook(
    recorder: _Recorder, sync_hook_client: SyncHTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as raised:
        sync_hook_client.download("boom")
    [attempt] = recorder.attempts

    assert attempt.error is raised.value
    assert attempt.hook.status == 500


# --- Refused at construction --------------------------------------------------------------------


def test_an_async_hook_is_refused_at_construction() -> None:
    settings: dict[str, Any] = {"request_hook": _async_hook}

    with pytest.raises(TypeError, match="not an async one: _async_hook"):
        HTTPClient(**settings)
    with pytest.raises(TypeError, match="not an async one: _async_hook"):
        SyncHTTPClient(**settings)


def test_a_hook_that_is_not_callable_is_refused_at_construction() -> None:
    settings: dict[str, Any] = {"request_hook": RequestHook()}

    with pytest.raises(TypeError, match="takes a function of a RequestInfo"):
        HTTPClient(**settings)
    with pytest.raises(TypeError, match="takes a function of a RequestInfo"):
        SyncHTTPClient(**settings)
