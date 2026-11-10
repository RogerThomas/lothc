import asyncio
import queue
import socket
import threading
import time
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any
from urllib.parse import urlsplit

import pytest
from _helpers import echoed_header_values, echoed_headers
from pydantic import BaseModel

from lothc import HTTPClient, HTTPConnectionError, HTTPTimeoutError, SyncHTTPClient


class _DefaultHeaders(BaseModel):
    x_api_key: str
    x_unset: str | None = None


async def _async_bearer_auth() -> str:
    return "token-value"


def _sync_bearer_auth() -> str:
    return "token-value"


@dataclass(slots=True)
class _TwoHalvesServer:
    """A server for one request, its answer sent in two halves: the head and the body's first
    half at once, the rest once `release` is set. `after_body` then gets what the client did with
    the connection: `b""` for closing it, the start of another request, or `None` for leaving it
    open."""

    url: str
    release: threading.Event
    after_body: queue.Queue[bytes | None]


def _answer_in_two_halves(
    listener: socket.socket, release: threading.Event, after_body: queue.Queue[bytes | None]
) -> None:
    conn, _ = listener.accept()
    with conn:
        conn.settimeout(2.0)
        request = b""
        while b"\r\n\r\n" not in request:
            request += conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nab")
        release.wait(2.0)
        conn.sendall(b"cd")
        try:
            after_body.put(conn.recv(65536))
        except TimeoutError:
            after_body.put(None)


@pytest.fixture(name="two_halves_server")
def _two_halves_server() -> Generator[_TwoHalvesServer]:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(2.0)
        server = _TwoHalvesServer(
            f"http://127.0.0.1:{listener.getsockname()[1]}/", threading.Event(), queue.Queue()
        )
        thread = threading.Thread(
            target=_answer_in_two_halves, args=(listener, server.release, server.after_body)
        )
        thread.start()
        yield server
        server.release.set()
        thread.join()


@pytest.fixture(name="user_agent_client")
async def _user_agent_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, user_agent="agent-name/1.0") as client:
        yield client


@pytest.fixture(name="sync_user_agent_client")
def _sync_user_agent_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, user_agent="agent-name/1.0") as client:
        yield client


async def test_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with pytest.raises(ValueError, match="at most one"):
        async with HTTPClient(
            base_url=base_url, bearer_token="token-value", bearer_auth=_async_bearer_auth
        ):
            pass


async def test_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    async with HTTPClient(timeout=None) as client:
        result = (await client.get(f"{base_url}items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


async def test_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_redirects=5) as client:
        result = await client.get("redirect")

    assert result.status == 200


def test_sync_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with (
        pytest.raises(ValueError, match="at most one"),
        SyncHTTPClient(
            base_url=base_url, bearer_token="token-value", bearer_auth=_sync_bearer_auth
        ),
    ):
        pass


def test_sync_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    with SyncHTTPClient(timeout=None) as client:
        result = client.get(f"{base_url}items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_build_with_default_headers_sent_on_every_request(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, default_headers={"x-api-key": "secret"}) as client:
        result = client.get("echo-headers", into=dict).data

    headers = echoed_headers(result)
    assert headers["x-api-key"] == "secret"


def test_sync_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_redirects=5) as client:
        result = client.get("redirect")

    assert result.status == 200


async def test_build_accepts_a_model_as_default_headers(base_url: str) -> None:
    # Encoded like per-request `headers=`: `_` becomes `-`, and a `None` field is omitted.
    async with HTTPClient(
        base_url=base_url, default_headers=_DefaultHeaders(x_api_key="secret")
    ) as client:
        result = (await client.get("echo-headers", into=dict)).data

    headers = echoed_headers(result)
    assert headers["x-api-key"] == "secret"
    assert "x-unset" not in headers


async def _fetch_once(client: HTTPClient) -> dict[str, object]:
    async with client:
        return (await client.get("items/7", into=dict)).data


async def test_client_used_before_it_is_opened_raises_a_clear_error(base_url: str) -> None:
    client = HTTPClient(base_url=base_url)

    with pytest.raises(RuntimeError, match="never opened"):
        await client.get("items/7")


def test_sync_client_used_before_it_is_opened_raises_a_clear_error(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)

    with pytest.raises(RuntimeError, match="never opened"):
        client.get("items/7")


async def test_client_can_be_opened_again_after_it_closes(base_url: str) -> None:
    # Each entry builds a fresh pyreqwest client from the stored settings.
    client = HTTPClient(base_url=base_url)
    async with client:
        first = (await client.get("items/7", into=dict)).data
    async with client:
        second = (await client.get("items/7", into=dict)).data

    assert first == second == {"id": 7, "name": "item-7"}


def test_sync_client_can_be_opened_again_after_it_closes(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)
    with client:
        first = client.get("items/7", into=dict).data
    with client:
        second = client.get("items/7", into=dict).data

    assert first == second


async def test_entering_an_already_open_client_raises(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        with pytest.raises(RuntimeError, match="already open"):
            await client.__aenter__()


def test_sync_entering_an_already_open_client_raises(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url) as client,
        pytest.raises(RuntimeError, match="already open"),
    ):
        client.__enter__()


def test_conflicting_auth_is_rejected_when_the_client_is_constructed() -> None:
    # At construction, before anything is opened, not on entry.
    with pytest.raises(ValueError, match="at most one"):
        HTTPClient(bearer_token="bearer-token", basic_auth=("user", "pass"))


def test_one_client_works_across_separate_event_loops(base_url: str) -> None:
    # A module-level client reused by separate `asyncio.run()` calls, each opening it afresh.
    client = HTTPClient(base_url=base_url)

    assert asyncio.run(_fetch_once(client)) == asyncio.run(_fetch_once(client))


async def test_a_request_in_flight_when_the_client_closes_raises_runtime_error(
    base_url: str,
) -> None:
    # pyreqwest's own ClientClosedError for the in-flight request is translated, not leaked.
    client = HTTPClient(base_url=base_url)
    await client.__aenter__()
    request = asyncio.create_task(client.get("slow", params={"seconds": 0.5}))
    await asyncio.sleep(0.1)
    await client.__aexit__(None, None, None)

    with pytest.raises(RuntimeError, match="client is closed"):
        await request


@pytest.mark.parametrize(
    "path", ["stall-after-first-chunk", "stall-unframed"], ids=["chunked", "ends-with-connection"]
)
async def test_a_request_whose_body_is_still_arriving_when_the_client_closes_raises_runtime_error(
    base_url: str, path: str
) -> None:
    # Promptly, and not with the part that arrived: an unframed body would even look complete.
    client = HTTPClient(base_url=base_url)
    await client.__aenter__()
    request = asyncio.create_task(client.get(path, params={"seconds": 1.0}))
    await asyncio.sleep(0.1)
    await client.__aexit__(None, None, None)
    closed_at = time.monotonic()

    with pytest.raises(RuntimeError, match="client is closed"):
        await request
    assert time.monotonic() - closed_at < 0.5


async def _first_chunk(client: HTTPClient, path: str, params: dict[str, Any]) -> bytes:
    stream = client.stream_get(path, params=params)
    try:
        return await anext(stream)
    finally:
        await stream.aclose()


async def test_a_stream_still_waiting_for_its_head_when_the_client_closes_raises_runtime_error(
    base_url: str,
) -> None:
    client = HTTPClient(base_url=base_url)
    await client.__aenter__()
    request = asyncio.create_task(_first_chunk(client, "slow", {"seconds": 1.0}))
    await asyncio.sleep(0.1)
    await client.__aexit__(None, None, None)
    closed_at = time.monotonic()

    with pytest.raises(RuntimeError, match="client is closed"):
        await request
    assert time.monotonic() - closed_at < 0.5


async def test_a_stream_whose_head_has_arrived_reads_on_after_the_client_closes(
    base_url: str, server_key: str
) -> None:
    client = HTTPClient(base_url=base_url)
    await client.__aenter__()
    stream = client.stream_get("large", params={"key": server_key, "size": 2**20})
    try:
        received = len(await anext(stream))
        await client.__aexit__(None, None, None)
        received += sum([len(chunk) async for chunk in stream])
    finally:
        await stream.aclose()

    assert received == 2**20


async def test_a_stream_read_to_its_end_after_the_client_closes_closes_its_connection(
    two_halves_server: _TwoHalvesServer,
) -> None:
    # Closed, not pooled for a client that will never send on it again.
    client = HTTPClient(base_url=two_halves_server.url)
    await client.__aenter__()
    stream = client.stream_get("two-halves")
    try:
        received = await anext(stream)
        await client.__aexit__(None, None, None)
        two_halves_server.release.set()
        received += b"".join([chunk async for chunk in stream])
    finally:
        await stream.aclose()

    assert received == b"abcd"
    assert two_halves_server.after_body.get(timeout=3.0) == b""


async def test_a_stream_whose_head_has_arrived_still_times_out_after_the_client_closes(
    base_url: str,
) -> None:
    client = HTTPClient(base_url=base_url)
    await client.__aenter__()
    stream = client.stream_get("stall-after-first-chunk", params={"seconds": 1.0}, timeout=0.2)
    try:
        await anext(stream)
        await client.__aexit__(None, None, None)
        with pytest.raises(HTTPTimeoutError):
            await anext(stream)
    finally:
        await stream.aclose()


async def test_exiting_a_client_that_is_not_open_is_harmless(base_url: str) -> None:
    client = HTTPClient(base_url=base_url)
    await client.__aexit__(None, None, None)
    async with client:
        pass
    await client.__aexit__(None, None, None)


def test_sync_exiting_a_client_that_is_not_open_is_harmless(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__exit__(None, None, None)
    with client:
        pass
    client.__exit__(None, None, None)


async def test_user_agent_is_sent_on_every_request(user_agent_client: HTTPClient) -> None:
    result = (await user_agent_client.get("echo-headers", into=dict)).data

    headers = echoed_headers(result)
    assert headers["user-agent"] == "agent-name/1.0"


def test_sync_user_agent_is_sent_on_every_request(sync_user_agent_client: SyncHTTPClient) -> None:
    result = sync_user_agent_client.get("echo-headers", into=dict).data

    headers = echoed_headers(result)
    assert headers["user-agent"] == "agent-name/1.0"


async def test_a_per_request_user_agent_header_overrides_user_agent(
    user_agent_client: HTTPClient,
) -> None:
    result = (
        await user_agent_client.get(
            "echo-headers", headers={"user-agent": "override/2.0"}, into=dict
        )
    ).data

    values = [h["value"] for h in result["headers"] if h["name"].lower() == "user-agent"]
    assert values == ["override/2.0"]


async def test_a_proxy_that_is_down_fails_requests(base_url: str) -> None:
    # The control for the `no_proxy` tests below: without an exclusion, requests go through the
    # (unreachable) proxy and fail.
    async with HTTPClient(base_url=base_url, proxy="http://127.0.0.1:1") as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("items/7")


async def test_no_proxy_hosts_bypass_the_proxy(base_url: str) -> None:
    async with HTTPClient(
        base_url=base_url, proxy="http://127.0.0.1:1", no_proxy=["127.0.0.1"]
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_no_proxy_hosts_bypass_the_proxy(base_url: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, proxy="http://127.0.0.1:1", no_proxy=["localhost", "127.0.0.1"]
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_no_proxy_that_does_not_match_still_uses_the_proxy(base_url: str) -> None:
    async with HTTPClient(
        base_url=base_url, proxy="http://127.0.0.1:1", no_proxy=["other-host"]
    ) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("items/7")


@pytest.mark.parametrize("client_class", [HTTPClient, SyncHTTPClient], ids=["async", "sync"])
def test_no_proxy_without_a_proxy_is_rejected(
    client_class: type[HTTPClient] | type[SyncHTTPClient],
) -> None:
    with pytest.raises(ValueError, match="'no_proxy' needs a 'proxy'"):
        client_class(no_proxy=["localhost"])


async def test_http2_falls_back_to_http1_against_an_http1_only_server(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, http2=True) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_http2_falls_back_to_http1_against_an_http1_only_server(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, http2=True) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_the_default_user_agent_names_lothc_and_its_version(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        result = (await client.get("echo-headers", into=dict)).data

    headers = echoed_headers(result)
    assert headers["user-agent"] == f"python-lothc/{version('lothc')}"


def test_sync_the_default_user_agent_names_lothc_and_its_version(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url) as client:
        result = client.get("echo-headers", into=dict).data

    headers = echoed_headers(result)
    assert headers["user-agent"] == f"python-lothc/{version('lothc')}"


async def test_resolve_sends_a_hostname_to_the_given_address(base_url: str) -> None:
    # `lothc.test` exists only through the override; the URL's own port is still used.
    port = urlsplit(base_url).port
    async with HTTPClient(resolve={"lothc.test": "127.0.0.1"}) as client:
        result = (await client.get(f"http://lothc.test:{port}/items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_resolve_sends_a_hostname_to_the_given_address(base_url: str) -> None:
    port = urlsplit(base_url).port
    with SyncHTTPClient(resolve={"lothc.test": "127.0.0.1"}) as client:
        result = client.get(f"http://lothc.test:{port}/items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_local_address_is_the_address_connections_come_from(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, local_address="127.0.0.1") as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


async def test_a_local_address_this_machine_does_not_have_fails_to_connect(base_url: str) -> None:
    # 192.0.2.1 is reserved for documentation (TEST-NET-1), so it can't be bound: proof that
    # `local_address` really reaches the socket.
    async with HTTPClient(base_url=base_url, local_address="192.0.2.1") as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("items/7")


async def test_tcp_keepalive_is_accepted(base_url: str) -> None:
    # Keepalive probes aren't observable from here; this only checks the setting is wired in
    # without breaking requests.
    async with HTTPClient(base_url=base_url, tcp_keepalive=30.0) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_tcp_keepalive_is_accepted(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, tcp_keepalive=30.0) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("client_class", [HTTPClient, SyncHTTPClient], ids=["async", "sync"])
@pytest.mark.parametrize("base_url", ["http://host/api", "http://host/api/?key=value"])
def test_an_unjoinable_base_url_is_rejected_at_construction(
    client_class: type[HTTPClient] | type[SyncHTTPClient], base_url: str
) -> None:
    with pytest.raises(ValueError, match="trailing slash"):
        client_class(base_url=base_url)


async def test_a_per_request_header_replaces_a_default_header_of_the_same_name(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, default_headers={"X-Api-Key": "default"}) as client:
        result = (
            await client.get("echo-headers", headers={"x-api-key": "override"}, into=dict)
        ).data

    assert echoed_header_values(result, "x-api-key") == ["override"]


def test_sync_a_per_request_header_replaces_a_default_header_of_the_same_name(
    base_url: str,
) -> None:
    with SyncHTTPClient(base_url=base_url, default_headers={"X-Api-Key": "default"}) as client:
        result = client.get("echo-headers", headers={"x-api-key": "override"}, into=dict).data

    assert echoed_header_values(result, "x-api-key") == ["override"]


async def _open_and_close(client: HTTPClient) -> None:
    async with client:
        pass


def _open_and_close_sync(client: SyncHTTPClient) -> None:
    with client:
        pass


@pytest.mark.parametrize(
    "settings",
    [
        {"local_address": "not-an-ip"},
        {"resolve": {"lothc.test": "not-an-ip"}},
        {"max_connections": 0},
        {"default_headers": {"bad name": "value"}},
        {"timeout": -1.0},
        {"user_agent": "a\nb"},
        {"identity_pem": b"identity-pem"},
        {"max_tls_version": "TLSv1.1"},
        {"min_tls_version": "TLSv1.3", "max_tls_version": "TLSv1.2"},
        {"timeout": float("nan")},
        {"tcp_keepalive": -1.0},
        {"connect_timeout": -1.0},
        {"read_timeout": -1.0},
        {"tcp_keepalive": float("nan")},
    ],
    ids=[
        "local_address",
        "resolve",
        "max_connections",
        "default_headers",
        "timeout",
        "user_agent",
        "identity_pem",
        "max_tls_version",
        "min_above_max_tls_version",
        "nan",
        "tcp_keepalive",
        "connect_timeout",
        "read_timeout",
        "nan_tcp_keepalive",
    ],
)
async def test_unusable_settings_raise_value_error_by_the_time_the_client_opens(
    settings: dict[str, Any],
) -> None:
    # Construction or entry, whichever notices first; never a pyreqwest exception type.
    with pytest.raises(ValueError, match=r".+") as exc_info:
        await _open_and_close(HTTPClient(**settings))

    assert not type(exc_info.value).__module__.startswith("pyreqwest")


@pytest.mark.parametrize(
    "settings",
    [
        {"local_address": "not-an-ip"},
        {"resolve": {"lothc.test": "not-an-ip"}},
        {"max_connections": 0},
        {"default_headers": {"bad name": "value"}},
        {"timeout": -1.0},
        {"user_agent": "a\nb"},
        {"identity_pem": b"identity-pem"},
        {"max_tls_version": "TLSv1.1"},
        {"min_tls_version": "TLSv1.3", "max_tls_version": "TLSv1.2"},
        {"timeout": float("nan")},
        {"tcp_keepalive": -1.0},
        {"connect_timeout": -1.0},
        {"read_timeout": -1.0},
        {"tcp_keepalive": float("nan")},
    ],
    ids=[
        "local_address",
        "resolve",
        "max_connections",
        "default_headers",
        "timeout",
        "user_agent",
        "identity_pem",
        "max_tls_version",
        "min_above_max_tls_version",
        "nan",
        "tcp_keepalive",
        "connect_timeout",
        "read_timeout",
        "nan_tcp_keepalive",
    ],
)
def test_sync_unusable_settings_raise_value_error_by_the_time_the_client_opens(
    settings: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match=r".+") as exc_info:
        _open_and_close_sync(SyncHTTPClient(**settings))

    assert not type(exc_info.value).__module__.startswith("pyreqwest")


@pytest.mark.parametrize("timeout", [float("inf"), 1e20], ids=["infinite", "huge"])
async def test_a_timeout_too_large_to_represent_raises_overflow_error(timeout: float) -> None:
    with pytest.raises(OverflowError):
        await _open_and_close(HTTPClient(timeout=timeout))


@pytest.mark.parametrize("timeout", [float("inf"), 1e20], ids=["infinite", "huge"])
def test_sync_a_timeout_too_large_to_represent_raises_overflow_error(timeout: float) -> None:
    with pytest.raises(OverflowError):
        _open_and_close_sync(SyncHTTPClient(timeout=timeout))


@pytest.mark.parametrize("setting", ["tcp_keepalive", "connect_timeout", "read_timeout"])
async def test_a_connection_duration_too_large_to_represent_raises_overflow_error(
    setting: str,
) -> None:
    settings: dict[str, Any] = {setting: 1e20}

    with pytest.raises(OverflowError):
        await _open_and_close(HTTPClient(**settings))


@pytest.mark.parametrize("setting", ["tcp_keepalive", "connect_timeout", "read_timeout"])
def test_sync_a_connection_duration_too_large_to_represent_raises_overflow_error(
    setting: str,
) -> None:
    settings: dict[str, Any] = {setting: 1e20}

    with pytest.raises(OverflowError):
        _open_and_close_sync(SyncHTTPClient(**settings))


# pyreqwest's `BuilderError` from building the client is re-raised as a plain `ValueError` with
# its message and causes in one line, chained to the original.
@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"max_tls_version": "TLSv1.1"}, "builder error: empty supported tls versions"),
        ({"user_agent": "a\nb"}, "builder error: failed to parse header value"),
    ],
    ids=["max_tls_version", "user_agent"],
)
async def test_a_client_build_error_is_a_plain_value_error_with_pyreqwests_message(
    settings: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match="builder error") as exc_info:
        await _open_and_close(HTTPClient(**settings))

    assert type(exc_info.value) is ValueError
    assert str(exc_info.value) == message
    assert exc_info.value.__cause__ is not None


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"max_tls_version": "TLSv1.1"}, "builder error: empty supported tls versions"),
        ({"user_agent": "a\nb"}, "builder error: failed to parse header value"),
    ],
    ids=["max_tls_version", "user_agent"],
)
def test_sync_a_client_build_error_is_a_plain_value_error_with_pyreqwests_message(
    settings: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match="builder error") as exc_info:
        _open_and_close_sync(SyncHTTPClient(**settings))

    assert type(exc_info.value) is ValueError
    assert str(exc_info.value) == message
    assert exc_info.value.__cause__ is not None


def test_sync_local_address_is_the_address_connections_come_from(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, local_address="127.0.0.1") as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_a_local_address_this_machine_does_not_have_fails_to_connect(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, local_address="192.0.2.1") as client,
        pytest.raises(HTTPConnectionError),
    ):
        client.get("items/7")


def test_sync_a_request_in_flight_when_the_client_closes_raises_runtime_error(
    base_url: str,
) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__enter__()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.get, "slow", params={"seconds": 0.5})
        time.sleep(0.1)
        client.__exit__(None, None, None)

        with pytest.raises(RuntimeError, match="client is closed"):
            request.result()


@pytest.mark.parametrize(
    "path", ["stall-after-first-chunk", "stall-unframed"], ids=["chunked", "ends-with-connection"]
)
def test_sync_a_request_whose_body_is_still_arriving_when_the_client_closes_raises_runtime_error(
    base_url: str, path: str
) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__enter__()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.get, path, params={"seconds": 1.0})
        time.sleep(0.1)
        client.__exit__(None, None, None)
        closed_at = time.monotonic()

        with pytest.raises(RuntimeError, match="client is closed"):
            request.result()
        assert time.monotonic() - closed_at < 0.5


def _sync_first_chunk(client: SyncHTTPClient, path: str, params: dict[str, Any]) -> bytes:
    stream = client.stream_get(path, params=params)
    try:
        return next(stream)
    finally:
        stream.close()


def test_sync_a_stream_still_waiting_for_its_head_when_the_client_closes_raises_runtime_error(
    base_url: str,
) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__enter__()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(_sync_first_chunk, client, "slow", {"seconds": 1.0})
        time.sleep(0.1)
        client.__exit__(None, None, None)
        closed_at = time.monotonic()

        with pytest.raises(RuntimeError, match="client is closed"):
            request.result()
        assert time.monotonic() - closed_at < 0.5


def test_sync_a_stream_whose_head_has_arrived_reads_on_after_the_client_closes(
    base_url: str, server_key: str
) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__enter__()
    stream = client.stream_get("large", params={"key": server_key, "size": 2**20})
    try:
        received = len(next(stream))
        client.__exit__(None, None, None)
        received += sum(len(chunk) for chunk in stream)
    finally:
        stream.close()

    assert received == 2**20


def test_sync_a_stream_read_to_its_end_after_the_client_closes_closes_its_connection(
    two_halves_server: _TwoHalvesServer,
) -> None:
    client = SyncHTTPClient(base_url=two_halves_server.url)
    client.__enter__()
    stream = client.stream_get("two-halves")
    try:
        received = next(stream)
        client.__exit__(None, None, None)
        two_halves_server.release.set()
        received += b"".join(stream)
    finally:
        stream.close()

    assert received == b"abcd"
    assert two_halves_server.after_body.get(timeout=3.0) == b""


def test_sync_a_stream_whose_head_has_arrived_still_times_out_after_the_client_closes(
    base_url: str,
) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__enter__()
    stream = client.stream_get("stall-after-first-chunk", params={"seconds": 1.0}, timeout=0.2)
    try:
        next(stream)
        client.__exit__(None, None, None)
        with pytest.raises(HTTPTimeoutError):
            next(stream)
    finally:
        stream.close()
