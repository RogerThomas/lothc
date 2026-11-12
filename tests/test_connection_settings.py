"""How a connection is set up and bounded, as reqwest's connector does it: `resolve=` (which hosts
it overrides, through a proxy too), `local_address=` (which addresses it lets a connection try),
`connect_timeout=` (DNS, TCP, a proxy's handshake and TLS's, but never the wait for a response)
and `read_timeout=` (a deadline from the start until the response's head, every redirect hop
included, then a limit on each gap in the body).

A "silent" port accepts TCP connections (the kernel completes them) but never answers, so what
times out there is whatever comes after the TCP connect: a TLS handshake, a proxy's, or the wait
for a response.
"""

import socket
import sys
import time
from collections.abc import Generator
from functools import partial
from urllib.parse import urlsplit

import pytest
import trustme
from _helpers import (
    EventsParams,
    clear_proxy_variables,
    sync_unless_the_server_stalled,
    unless_the_server_stalled,
)
from _proxy_server import (
    ProxyBehaviour,
    SocksBehaviour,
    serve_http_proxy,
    serve_socks5_proxy,
)
from _tls_server import serve_tls, tls_server_context

from lothc import HTTPClient, HTTPConnectionError, HTTPTimeoutError, SyncHTTPClient


@pytest.fixture(name="no_proxy_variables", autouse=True)
def _no_proxy_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_proxy_variables(monkeypatch)


@pytest.fixture(name="silent_port")
def _silent_port() -> Generator[int]:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield listener.getsockname()[1]


@pytest.fixture(scope="module", name="lothc_test_https_port")
def _lothc_test_https_port(tls_ca: trustme.CA) -> Generator[int]:
    """An HTTPS server whose certificate names only `lothc.test`."""
    with serve_tls(tls_server_context(tls_ca, ("lothc.test",))) as url:
        yield _port(url)


def _port(url: str) -> int:
    port = urlsplit(url).port
    if port is None:
        raise ValueError(f"{url} has no port")
    return port


def _keepalive_idle_option() -> int:
    # macOS names the idle time `TCP_KEEPALIVE`.
    if sys.platform == "darwin":
        return socket.TCP_KEEPALIVE
    return socket.TCP_KEEPIDLE


def _keepalive_of(local_port: int) -> tuple[bool, int, int, int]:
    """The TCP keepalive settings of this process's socket from `local_port`: on, idle time,
    probe interval and probe count."""
    idle = _keepalive_idle_option()
    for fd in range(3, 4096):
        try:
            sock = socket.fromfd(fd, socket.AF_INET, socket.SOCK_STREAM)
        except OSError:
            continue
        with sock:
            try:
                address = sock.getsockname()
            except (OSError, ValueError):
                continue
            if isinstance(address, tuple) and address[1] == local_port:
                return (
                    bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)),
                    sock.getsockopt(socket.IPPROTO_TCP, idle),
                    sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL),
                    sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT),
                )
    raise LookupError(f"no socket from port {local_port}")


# --- resolve= ---------------------------------------------------------------------------------


async def test_resolve_matches_a_host_whatever_its_case(base_url: str) -> None:
    async with HTTPClient(resolve={"LOTHC.Test": "127.0.0.1"}) as client:
        result = await client.get(f"http://lothc.test:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_resolve_matches_a_host_whatever_its_case(base_url: str) -> None:
    with SyncHTTPClient(resolve={"LOTHC.Test": "127.0.0.1"}) as client:
        result = client.get(f"http://lothc.test:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


async def test_resolve_never_replaces_an_ip_address(base_url: str) -> None:
    # An IP address is never looked up, so its "override" (TEST-NET-1, which nothing answers)
    # doesn't apply: the request still reaches the server.
    async with HTTPClient(
        base_url=base_url, resolve={"127.0.0.1": "192.0.2.1"}, timeout=2
    ) as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_resolve_never_replaces_an_ip_address(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, resolve={"127.0.0.1": "192.0.2.1"}, timeout=2) as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


async def test_resolve_applies_to_the_proxys_own_host(base_url: str) -> None:
    with serve_http_proxy() as proxy:
        proxy_url = f"http://proxy.test:{_port(proxy.url)}"
        async with HTTPClient(proxy=proxy_url, resolve={"proxy.test": "127.0.0.1"}) as client:
            result = await client.get(f"{base_url}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [request.line for request in proxy.requests] == [f"GET {base_url}items/7 HTTP/1.1"]


def test_sync_resolve_applies_to_the_proxys_own_host(base_url: str) -> None:
    with serve_http_proxy() as proxy:
        proxy_url = f"http://proxy.test:{_port(proxy.url)}"
        with SyncHTTPClient(proxy=proxy_url, resolve={"proxy.test": "127.0.0.1"}) as client:
            result = client.get(f"{base_url}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [request.line for request in proxy.requests] == [f"GET {base_url}items/7 HTTP/1.1"]


async def test_resolve_gives_a_socks5_proxy_the_overridden_address(base_url: str) -> None:
    with serve_socks5_proxy() as socks:
        async with HTTPClient(
            proxy=f"socks5://{socks.address}", resolve={"lothc.test": "127.0.0.1"}
        ) as client:
            result = await client.get(f"http://lothc.test:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host, h.port) for h in socks.handshakes] == [
        (1, "127.0.0.1", _port(base_url))
    ]


def test_sync_resolve_gives_a_socks5_proxy_the_overridden_address(base_url: str) -> None:
    with (
        serve_socks5_proxy() as socks,
        SyncHTTPClient(
            proxy=f"socks5://{socks.address}", resolve={"lothc.test": "127.0.0.1"}
        ) as client,
    ):
        result = client.get(f"http://lothc.test:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host, h.port) for h in socks.handshakes] == [
        (1, "127.0.0.1", _port(base_url))
    ]


async def test_socks5h_sends_the_name_whatever_resolve_says(base_url: str) -> None:
    # The proxy resolves the target itself, so the override (TEST-NET-1) never comes into it.
    with serve_socks5_proxy() as socks:
        async with HTTPClient(
            proxy=f"socks5h://{socks.address}", resolve={"localhost": "192.0.2.1"}
        ) as client:
            result = await client.get(f"http://localhost:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host) for h in socks.handshakes] == [(3, "localhost")]


def test_sync_socks5h_sends_the_name_whatever_resolve_says(base_url: str) -> None:
    with (
        serve_socks5_proxy() as socks,
        SyncHTTPClient(
            proxy=f"socks5h://{socks.address}", resolve={"localhost": "192.0.2.1"}
        ) as client,
    ):
        result = client.get(f"http://localhost:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host) for h in socks.handshakes] == [(3, "localhost")]


async def test_https_through_resolve_checks_the_certificate_against_the_name(
    lothc_test_https_port: int, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(
        resolve={"lothc.test": "127.0.0.1"}, root_certificates=[tls_ca_pem]
    ) as client:
        result = await client.get(f"https://lothc.test:{lothc_test_https_port}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_https_through_resolve_checks_the_certificate_against_the_name(
    lothc_test_https_port: int, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(
        resolve={"lothc.test": "127.0.0.1"}, root_certificates=[tls_ca_pem]
    ) as client:
        result = client.get(f"https://lothc.test:{lothc_test_https_port}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


# --- local_address= ---------------------------------------------------------------------------


async def test_local_address_only_tries_addresses_of_its_own_family(base_url: str) -> None:
    # The server listens on 127.0.0.1 only: bound to ::1, no address `localhost` has will do,
    # since the IPv4 one is never tried.
    async with HTTPClient(local_address="::1") as client:
        with pytest.raises(HTTPConnectionError):
            await client.get(f"http://localhost:{_port(base_url)}/items/7")


def test_sync_local_address_only_tries_addresses_of_its_own_family(base_url: str) -> None:
    with SyncHTTPClient(local_address="::1") as client, pytest.raises(HTTPConnectionError):
        client.get(f"http://localhost:{_port(base_url)}/items/7")


# --- tcp_keepalive= ---------------------------------------------------------------------------


# reqwest's defaults: 15s idle, a probe every 15s, 3 of them; `tcp_keepalive` sets the idle time.
# Not on Windows, where reqwest can't set the probe count, and a socket isn't a file descriptor
# for `_keepalive_of` to find.
_posix_keepalive = pytest.mark.skipif(
    sys.platform == "win32", reason="reqwest sets the keepalive probe count only on POSIX"
)


@_posix_keepalive
@pytest.mark.parametrize(("tcp_keepalive", "idle"), [(None, 15), (30.0, 30)])
async def test_every_connection_keeps_alive_as_reqwest_does(
    base_url: str, tcp_keepalive: float | None, idle: int
) -> None:
    async with HTTPClient(base_url=base_url, tcp_keepalive=tcp_keepalive) as client:
        result = await client.get("peer", into=dict)
        keepalive = _keepalive_of(result.data["port"])

    assert keepalive == (True, idle, 15, 3)


@_posix_keepalive
@pytest.mark.parametrize(("tcp_keepalive", "idle"), [(None, 15), (30.0, 30)])
def test_sync_every_connection_keeps_alive_as_reqwest_does(
    base_url: str, tcp_keepalive: float | None, idle: int
) -> None:
    with SyncHTTPClient(base_url=base_url, tcp_keepalive=tcp_keepalive) as client:
        result = client.get("peer", into=dict)
        keepalive = _keepalive_of(result.data["port"])

    assert keepalive == (True, idle, 15, 3)


# --- connect_timeout= -------------------------------------------------------------------------


async def test_connect_timeout_bounds_a_tls_handshake(silent_port: int) -> None:
    async with HTTPClient(connect_timeout=0.05, timeout=2) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            await client.get(f"https://127.0.0.1:{silent_port}/")

    assert time.monotonic() - started < 1


def test_sync_connect_timeout_bounds_a_tls_handshake(silent_port: int) -> None:
    with SyncHTTPClient(connect_timeout=0.05, timeout=2) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get(f"https://127.0.0.1:{silent_port}/")

    assert time.monotonic() - started < 1


# The connect itself must fit in `connect_timeout`, and nothing outside the client can tell a try
# whose connect a stalled machine made outlast it from one the timeout cut short in the wait: so
# 0.15s, not the 0.05s that one did outlast, still short of the 0.25s the wait reaches, at no
# cost, as the wait is the total `timeout`'s either way.
async def test_connect_timeout_never_bounds_the_wait_for_a_response(silent_port: int) -> None:
    async with HTTPClient(connect_timeout=0.15, timeout=0.3) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            await client.get(f"http://127.0.0.1:{silent_port}/")

    assert time.monotonic() - started >= 0.25


def test_sync_connect_timeout_never_bounds_the_wait_for_a_response(silent_port: int) -> None:
    with SyncHTTPClient(connect_timeout=0.15, timeout=0.3) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get(f"http://127.0.0.1:{silent_port}/")

    assert time.monotonic() - started >= 0.25


async def test_connect_timeout_bounds_a_connect_the_proxy_never_answers() -> None:
    with serve_http_proxy(behaviour=ProxyBehaviour.SILENT) as proxy:
        async with HTTPClient(proxy=proxy.url, connect_timeout=0.05, timeout=2) as client:
            started = time.monotonic()
            with pytest.raises(HTTPTimeoutError):
                await client.get("https://localhost:1/")

    assert time.monotonic() - started < 1


def test_sync_connect_timeout_bounds_a_connect_the_proxy_never_answers() -> None:
    with (
        serve_http_proxy(behaviour=ProxyBehaviour.SILENT) as proxy,
        SyncHTTPClient(proxy=proxy.url, connect_timeout=0.05, timeout=2) as client,
    ):
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get("https://localhost:1/")

    assert time.monotonic() - started < 1


async def test_connect_timeout_bounds_a_socks5_handshake() -> None:
    with serve_socks5_proxy(behaviour=SocksBehaviour.SILENT) as socks:
        async with HTTPClient(
            proxy=f"socks5h://{socks.address}", connect_timeout=0.05, timeout=2
        ) as client:
            started = time.monotonic()
            with pytest.raises(HTTPTimeoutError):
                await client.get("http://localhost:1/")

    assert time.monotonic() - started < 1


def test_sync_connect_timeout_bounds_a_socks5_handshake() -> None:
    with (
        serve_socks5_proxy(behaviour=SocksBehaviour.SILENT) as socks,
        SyncHTTPClient(
            proxy=f"socks5h://{socks.address}", connect_timeout=0.05, timeout=2
        ) as client,
    ):
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get("http://localhost:1/")

    assert time.monotonic() - started < 1


def _ipv4_url(https_url: str) -> str:
    """`https_url` naming the address the test servers listen on rather than `localhost`, which
    also resolves to `::1`: Windows takes about a second to refuse that (it retries the SYN), more
    than a short `connect_timeout` allows. The certificate names `127.0.0.1` too."""
    return https_url.replace("localhost", "127.0.0.1")


async def test_connect_timeout_ends_once_the_tls_handshake_has(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=_ipv4_url(https_base_url), root_certificates=[tls_ca_pem], connect_timeout=0.1
    ) as client:
        result = await client.get("slow", params={"seconds": 0.15}, into=dict)

    assert result.data == {"finally": True}


def test_sync_connect_timeout_ends_once_the_tls_handshake_has(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(
        base_url=_ipv4_url(https_base_url), root_certificates=[tls_ca_pem], connect_timeout=0.1
    ) as client:
        result = client.get("slow", params={"seconds": 0.15}, into=dict)

    assert result.data == {"finally": True}


# --- read_timeout= ----------------------------------------------------------------------------


async def test_read_timeout_lets_a_head_within_it_through(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, read_timeout=0.15) as client:
        result = await client.get("slow", params={"seconds": 0.05}, into=dict)

    assert result.data == {"finally": True}


def test_sync_read_timeout_lets_a_head_within_it_through(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, read_timeout=0.15) as client:
        result = client.get("slow", params={"seconds": 0.05}, into=dict)

    assert result.data == {"finally": True}


async def test_read_timeout_bounds_the_head_across_every_redirect_hop(base_url: str) -> None:
    # Each hop answers within `read_timeout`, but not the two together.
    async with HTTPClient(base_url=base_url, read_timeout=0.1) as client:
        with pytest.raises(HTTPTimeoutError):
            await client.get("slow-redirect", params={"seconds": 0.06, "to": "slow?seconds=0.06"})


def test_sync_read_timeout_bounds_the_head_across_every_redirect_hop(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, read_timeout=0.1) as client,
        pytest.raises(HTTPTimeoutError),
    ):
        client.get("slow-redirect", params={"seconds": 0.06, "to": "slow?seconds=0.06"})


async def test_read_timeout_bounds_connecting_too(silent_port: int) -> None:
    async with HTTPClient(read_timeout=0.05, timeout=2) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            await client.get(f"https://127.0.0.1:{silent_port}/")

    assert time.monotonic() - started < 1


def test_sync_read_timeout_bounds_connecting_too(silent_port: int) -> None:
    with SyncHTTPClient(read_timeout=0.05, timeout=2) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get(f"https://127.0.0.1:{silent_port}/")

    assert time.monotonic() - started < 1


async def test_read_timeout_bounds_a_stall_in_the_body(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, read_timeout=0.05, timeout=2) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            await client.get("stall-after-first-chunk", params={"seconds": 2})

    assert time.monotonic() - started < 1


def test_sync_read_timeout_bounds_a_stall_in_the_body(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, read_timeout=0.05, timeout=2) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get("stall-after-first-chunk", params={"seconds": 2})

    assert time.monotonic() - started < 1


# Six events 0.03s apart: each gap well within `read_timeout`, the body as a whole not, unless
# the machine stalls (`unless_the_server_stalled`).
async def _gapped(client: HTTPClient, params: EventsParams) -> bytes:
    return (await client.get("events", params={"interval": 0.03, "count": 6, **params})).data


def _sync_gapped(client: SyncHTTPClient, params: EventsParams) -> bytes:
    return client.get("events", params={"interval": 0.03, "count": 6, **params}).data


async def _streamed_gapped(client: HTTPClient, params: EventsParams) -> bytes:
    stream = client.stream_get("events", params={"interval": 0.03, "count": 6, **params})
    return b"".join([chunk async for chunk in stream])


def _sync_streamed_gapped(client: SyncHTTPClient, params: EventsParams) -> bytes:
    return b"".join(client.stream_get("events", params={"interval": 0.03, "count": 6, **params}))


async def test_read_timeout_limits_each_gap_in_the_body_not_the_whole(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, read_timeout=0.1) as client:
        body = await unless_the_server_stalled(partial(_gapped, client), base_url, server_key, 0.1)

    assert body.count(b"data: ") == 6


def test_sync_read_timeout_limits_each_gap_in_the_body_not_the_whole(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(base_url=base_url, read_timeout=0.1) as client:
        fetch = partial(_sync_gapped, client)
        body = sync_unless_the_server_stalled(fetch, base_url, server_key, 0.1)

    assert body.count(b"data: ") == 6


async def test_read_timeout_limits_each_gap_in_a_streamed_body_not_the_whole(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, read_timeout=0.1) as client:
        fetch = partial(_streamed_gapped, client)
        body = await unless_the_server_stalled(fetch, base_url, server_key, 0.1)

    assert body.count(b"data: ") == 6


def test_sync_read_timeout_limits_each_gap_in_a_streamed_body_not_the_whole(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(base_url=base_url, read_timeout=0.1) as client:
        fetch = partial(_sync_streamed_gapped, client)
        body = sync_unless_the_server_stalled(fetch, base_url, server_key, 0.1)

    assert body.count(b"data: ") == 6


# `read_timeout` also bounds the connect and the wait for the head, which a stalled machine can
# outlast: a try that times out before its first chunk shows nothing about the stall after it, so
# it is made again, for up to 2s.
async def _first_chunk_before_a_timed_out_stall(client: HTTPClient) -> bytes:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        stream = client.stream_get("stall-after-first-chunk", params={"seconds": 2})
        try:
            first = await anext(stream)
        except HTTPTimeoutError:
            continue
        with pytest.raises(HTTPTimeoutError):
            await anext(stream)
        await stream.aclose()
        return first
    raise AssertionError("no try's first chunk arrived within read_timeout")


def _sync_first_chunk_before_a_timed_out_stall(client: SyncHTTPClient) -> bytes:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        stream = client.stream_get("stall-after-first-chunk", params={"seconds": 2})
        try:
            first = next(stream)
        except HTTPTimeoutError:
            continue
        with pytest.raises(HTTPTimeoutError):
            next(stream)
        stream.close()
        return first
    raise AssertionError("no try's first chunk arrived within read_timeout")


async def test_read_timeout_bounds_a_stall_in_a_streamed_body(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, read_timeout=0.05, timeout=2) as client:
        first = await _first_chunk_before_a_timed_out_stall(client)

    assert first == b"first"


def test_sync_read_timeout_bounds_a_stall_in_a_streamed_body(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, read_timeout=0.05, timeout=2) as client:
        first = _sync_first_chunk_before_a_timed_out_stall(client)

    assert first == b"first"
