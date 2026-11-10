"""Proxies: `proxy=`/`no_proxy=`, the proxy environment variables, proxy credentials, `CONNECT`
tunnels and SOCKS5, against `_proxy_server.py`'s stdlib proxies, which record what reached them.

Every test starts with no proxy variables set (`_no_proxy_variables`), and `monkeypatch` restores
the caller's afterwards, so none leaks into another test. `test_wire_format.py` pins the exact
bytes a proxy receives.
"""

import asyncio
import socket
import time
from base64 import b64encode
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from urllib.parse import urlsplit

import pytest
from _helpers import clear_proxy_variables, echoed_headers
from _proxy_server import (
    HTTPProxy,
    ProxiedRequest,
    ProxyBehaviour,
    SocksBehaviour,
    SocksProxy,
    serve_http_proxy,
    serve_socks5_proxy,
)

from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    SyncHTTPClient,
)


@pytest.fixture(name="no_proxy_variables", autouse=True)
def _no_proxy_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_proxy_variables(monkeypatch)


@pytest.fixture(name="http_proxy")
def _http_proxy() -> Generator[HTTPProxy]:
    with serve_http_proxy() as proxy:
        yield proxy


@pytest.fixture(name="socks_proxy")
def _socks_proxy() -> Generator[SocksProxy]:
    with serve_socks5_proxy() as proxy:
        yield proxy


@pytest.fixture(name="socks_proxy_on_1080")
def _socks_proxy_on_1080() -> Generator[SocksProxy]:
    with ExitStack() as stack:
        try:
            proxy = stack.enter_context(serve_socks5_proxy(port=1080))
        except OSError:
            pytest.skip("port 1080, SOCKS's default, is in use")
        yield proxy


@pytest.fixture(name="tunnel_client")
async def _tunnel_client(
    https_base_url: str, http_proxy: HTTPProxy, tls_ca_pem: bytes
) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(
        base_url=https_base_url, proxy=http_proxy.url, root_certificates=[tls_ca_pem]
    ) as client:
        yield client


@pytest.fixture(name="sync_tunnel_client")
def _sync_tunnel_client(
    https_base_url: str, http_proxy: HTTPProxy, tls_ca_pem: bytes
) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(
        base_url=https_base_url, proxy=http_proxy.url, root_certificates=[tls_ca_pem]
    ) as client:
        yield client


@pytest.fixture(name="socks_client")
async def _socks_client(base_url: str, socks_proxy: SocksProxy) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, proxy=f"socks5://{socks_proxy.address}") as client:
        yield client


@pytest.fixture(name="sync_socks_client")
def _sync_socks_client(base_url: str, socks_proxy: SocksProxy) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, proxy=f"socks5://{socks_proxy.address}") as client:
        yield client


def _port(url: str) -> int:
    port = urlsplit(url).port
    if port is None:
        raise ValueError(f"{url} has no port")
    return port


def _lines(proxy: HTTPProxy) -> list[str]:
    return [request.line for request in proxy.requests]


def _proxied_schemes(proxy: HTTPProxy) -> set[str]:
    """Which schemes' requests went through `proxy`: a `CONNECT` is an https one."""
    return {"https" if line.startswith("CONNECT ") else "http" for line in _lines(proxy)}


def _headers_named(request: ProxiedRequest, name: str) -> list[str]:
    return [value for header, value in request.headers if header.lower() == name.lower()]


def _basic(username: str, password: str) -> str:
    return "Basic " + b64encode(f"{username}:{password}".encode()).decode()


def _with_userinfo(proxy_url: str, userinfo: str) -> str:
    return proxy_url.replace("://", f"://{userinfo}", 1)


def _first_address(host: str) -> str:
    """The address a client resolving `host` itself picks: `getaddrinfo`'s first."""
    return str(socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)[0][4][0])


# --- proxy= and no_proxy= ---------------------------------------------------------------------


async def test_proxy_forwards_an_http_request_in_absolute_form(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    async with HTTPClient(base_url=base_url, proxy=http_proxy.url) as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]
    assert _headers_named(http_proxy.requests[0], "Host") == [urlsplit(base_url).netloc]


def test_sync_proxy_forwards_an_http_request_in_absolute_form(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    with SyncHTTPClient(base_url=base_url, proxy=http_proxy.url) as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]
    assert _headers_named(http_proxy.requests[0], "Host") == [urlsplit(base_url).netloc]


async def test_proxy_tunnels_an_https_request_with_connect(
    https_base_url: str, http_proxy: HTTPProxy, tunnel_client: HTTPClient
) -> None:
    result = await tunnel_client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _lines(http_proxy) == [f"CONNECT localhost:{_port(https_base_url)} HTTP/1.1"]


def test_sync_proxy_tunnels_an_https_request_with_connect(
    https_base_url: str, http_proxy: HTTPProxy, sync_tunnel_client: SyncHTTPClient
) -> None:
    result = sync_tunnel_client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _lines(http_proxy) == [f"CONNECT localhost:{_port(https_base_url)} HTTP/1.1"]


async def test_a_tunnel_still_verifies_the_origin_certificate(
    https_base_url: str, http_proxy: HTTPProxy
) -> None:
    async with HTTPClient(base_url=https_base_url, proxy=http_proxy.url) as client:
        with pytest.raises(HTTPConnectionError, match="UnknownIssuer"):
            await client.get("items/7")


def test_sync_a_tunnel_still_verifies_the_origin_certificate(
    https_base_url: str, http_proxy: HTTPProxy
) -> None:
    with (
        SyncHTTPClient(base_url=https_base_url, proxy=http_proxy.url) as client,
        pytest.raises(HTTPConnectionError, match="UnknownIssuer"),
    ):
        client.get("items/7")


async def test_no_proxy_hosts_go_direct_and_the_rest_through_the_proxy(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")
    async with HTTPClient(proxy=http_proxy.url, no_proxy=["other-host", "127.0.0.1"]) as client:
        await client.get(f"{base_url}items/7")
        await client.get(f"{other_origin}items/7")

    assert _lines(http_proxy) == [f"GET {other_origin}items/7 HTTP/1.1"]


def test_sync_no_proxy_hosts_go_direct_and_the_rest_through_the_proxy(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")
    with SyncHTTPClient(proxy=http_proxy.url, no_proxy=["other-host", "127.0.0.1"]) as client:
        client.get(f"{base_url}items/7")
        client.get(f"{other_origin}items/7")

    assert _lines(http_proxy) == [f"GET {other_origin}items/7 HTTP/1.1"]


async def test_a_proxy_without_a_scheme_is_an_http_proxy(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    # A bare `host:port` parses as a scheme and a path; reqwest retries it with `http://`.
    async with HTTPClient(base_url=base_url, proxy=f"localhost:{_port(http_proxy.url)}") as client:
        await client.get("items/7")

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]


def test_sync_a_proxy_without_a_scheme_is_an_http_proxy(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    with SyncHTTPClient(base_url=base_url, proxy=f"localhost:{_port(http_proxy.url)}") as client:
        client.get("items/7")

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]


async def test_a_proxy_with_an_unknown_scheme_is_no_proxy_at_all(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The proxy can't be used, and `proxy=` still turns the environment's off.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    async with HTTPClient(base_url=base_url, proxy="ftp://127.0.0.1:1") as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_a_proxy_with_an_unknown_scheme_is_no_proxy_at_all(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    with SyncHTTPClient(base_url=base_url, proxy="ftp://127.0.0.1:1") as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize(
    ("proxy", "message"),
    [
        ("not a url", "relative URL without a base"),
        ("127.0.0.1:8080", "relative URL without a base"),
        ("http://", "empty host"),
    ],
)
async def test_a_proxy_that_is_not_a_url_is_refused_on_entry(proxy: str, message: str) -> None:
    client = HTTPClient(proxy=proxy)

    with pytest.raises(ValueError, match=message):
        async with client:
            pass


@pytest.mark.parametrize(
    ("proxy", "message"),
    [
        ("not a url", "relative URL without a base"),
        ("127.0.0.1:8080", "relative URL without a base"),
        ("http://", "empty host"),
    ],
)
def test_sync_a_proxy_that_is_not_a_url_is_refused_on_entry(proxy: str, message: str) -> None:
    client = SyncHTTPClient(proxy=proxy)

    with pytest.raises(ValueError, match=message), client:
        pass


# --- The default port -------------------------------------------------------------------------


def _default_port_cases() -> list[tuple[str, str, str]]:
    """(the URL asked for, the request line the proxy gets, its `Host`): a scheme's own port is
    left out of both, any other port kept."""
    return [
        ("http://host:80/items/7", "GET http://host/items/7 HTTP/1.1", "host"),
        ("http://host/items/7", "GET http://host/items/7 HTTP/1.1", "host"),
        ("http://host:8080/items/7", "GET http://host:8080/items/7 HTTP/1.1", "host:8080"),
        ("http://host:443/items/7", "GET http://host:443/items/7 HTTP/1.1", "host:443"),
    ]


@pytest.mark.parametrize(("url", "line", "host"), _default_port_cases())
async def test_a_default_port_is_left_out_of_the_request_target_and_host(
    base_url: str, url: str, line: str, host: str
) -> None:
    # The proxy sends every request on to the test server, whatever port was asked for.
    with serve_http_proxy(upstream_port=_port(base_url)) as proxy:
        async with HTTPClient(proxy=proxy.url) as client:
            result = await client.get(url, into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _lines(proxy) == [line]
    assert _headers_named(proxy.requests[0], "Host") == [host]


@pytest.mark.parametrize(("url", "line", "host"), _default_port_cases())
def test_sync_a_default_port_is_left_out_of_the_request_target_and_host(
    base_url: str, url: str, line: str, host: str
) -> None:
    with (
        serve_http_proxy(upstream_port=_port(base_url)) as proxy,
        SyncHTTPClient(proxy=proxy.url) as client,
    ):
        result = client.get(url, into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _lines(proxy) == [line]
    assert _headers_named(proxy.requests[0], "Host") == [host]


def _tunnelled_default_port_cases() -> list[tuple[str, str, str]]:
    """(the URL asked for, the `CONNECT` line, the tunnelled request's `Host`): a `CONNECT` always
    names the port, 443 when the URL has none."""
    return [
        ("https://localhost:443/echo-headers", "CONNECT localhost:443 HTTP/1.1", "localhost"),
        ("https://localhost/echo-headers", "CONNECT localhost:443 HTTP/1.1", "localhost"),
        (
            "https://localhost:8443/echo-headers",
            "CONNECT localhost:8443 HTTP/1.1",
            "localhost:8443",
        ),
        ("https://localhost:80/echo-headers", "CONNECT localhost:80 HTTP/1.1", "localhost:80"),
    ]


@pytest.mark.parametrize(("url", "line", "host"), _tunnelled_default_port_cases())
async def test_a_tunnel_names_the_port_and_the_host_header_leaves_out_443(
    https_base_url: str, tls_ca_pem: bytes, url: str, line: str, host: str
) -> None:
    with serve_http_proxy(upstream_port=_port(https_base_url)) as proxy:
        async with HTTPClient(proxy=proxy.url, root_certificates=[tls_ca_pem]) as client:
            result = await client.get(url, into=dict)

    assert _lines(proxy) == [line]
    assert echoed_headers(result.data)["host"] == host


@pytest.mark.parametrize(("url", "line", "host"), _tunnelled_default_port_cases())
def test_sync_a_tunnel_names_the_port_and_the_host_header_leaves_out_443(
    https_base_url: str, tls_ca_pem: bytes, url: str, line: str, host: str
) -> None:
    with (
        serve_http_proxy(upstream_port=_port(https_base_url)) as proxy,
        SyncHTTPClient(proxy=proxy.url, root_certificates=[tls_ca_pem]) as client,
    ):
        result = client.get(url, into=dict)

    assert _lines(proxy) == [line]
    assert echoed_headers(result.data)["host"] == host


# --- Proxy credentials ------------------------------------------------------------------------


async def test_proxy_credentials_are_sent_as_proxy_authorization(base_url: str) -> None:
    with serve_http_proxy(credentials=("us er", "p@ss")) as proxy:
        async with HTTPClient(
            base_url=base_url, proxy=_with_userinfo(proxy.url, "us%20er:p%40ss@")
        ) as client:
            result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _headers_named(proxy.requests[0], "Proxy-Authorization") == [_basic("us er", "p@ss")]


def test_sync_proxy_credentials_are_sent_as_proxy_authorization(base_url: str) -> None:
    with (
        serve_http_proxy(credentials=("us er", "p@ss")) as proxy,
        SyncHTTPClient(
            base_url=base_url, proxy=_with_userinfo(proxy.url, "us%20er:p%40ss@")
        ) as client,
    ):
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _headers_named(proxy.requests[0], "Proxy-Authorization") == [_basic("us er", "p@ss")]


async def test_a_proxy_authorization_header_replaces_the_proxy_credentials(base_url: str) -> None:
    with serve_http_proxy(credentials=("user", "pass")) as proxy:
        async with HTTPClient(
            base_url=base_url, proxy=_with_userinfo(proxy.url, "user:wrong@")
        ) as client:
            result = await client.get(
                "items/7", headers={"Proxy-Authorization": _basic("user", "pass")}
            )

    assert result.status == 200
    assert _headers_named(proxy.requests[0], "Proxy-Authorization") == [_basic("user", "pass")]


def test_sync_a_proxy_authorization_header_replaces_the_proxy_credentials(base_url: str) -> None:
    with (
        serve_http_proxy(credentials=("user", "pass")) as proxy,
        SyncHTTPClient(base_url=base_url, proxy=_with_userinfo(proxy.url, "user:wrong@")) as client,
    ):
        result = client.get("items/7", headers={"Proxy-Authorization": _basic("user", "pass")})

    assert result.status == 200
    assert _headers_named(proxy.requests[0], "Proxy-Authorization") == [_basic("user", "pass")]


async def test_a_forwarding_proxy_refusing_credentials_answers_with_its_407(base_url: str) -> None:
    with serve_http_proxy(credentials=("user", "pass")) as proxy:
        async with HTTPClient(base_url=base_url, proxy=proxy.url) as client:
            with pytest.raises(HTTPResponseError) as caught:
                await client.get("items/7")

    assert caught.value.status == 407


def test_sync_a_forwarding_proxy_refusing_credentials_answers_with_its_407(base_url: str) -> None:
    with (
        serve_http_proxy(credentials=("user", "pass")) as proxy,
        SyncHTTPClient(base_url=base_url, proxy=proxy.url) as client,
        pytest.raises(HTTPResponseError) as caught,
    ):
        client.get("items/7")

    assert caught.value.status == 407


async def test_connect_sends_the_proxy_credentials(https_base_url: str, tls_ca_pem: bytes) -> None:
    with serve_http_proxy(credentials=("user", "pass")) as proxy:
        async with HTTPClient(
            base_url=https_base_url,
            proxy=_with_userinfo(proxy.url, "user:pass@"),
            root_certificates=[tls_ca_pem],
        ) as client:
            result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _headers_named(proxy.requests[0], "Proxy-Authorization") == [_basic("user", "pass")]


def test_sync_connect_sends_the_proxy_credentials(https_base_url: str, tls_ca_pem: bytes) -> None:
    with (
        serve_http_proxy(credentials=("user", "pass")) as proxy,
        SyncHTTPClient(
            base_url=https_base_url,
            proxy=_with_userinfo(proxy.url, "user:pass@"),
            root_certificates=[tls_ca_pem],
        ) as client,
    ):
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert _headers_named(proxy.requests[0], "Proxy-Authorization") == [_basic("user", "pass")]


# --- A refused or broken CONNECT --------------------------------------------------------------


def _connect_failures() -> list[tuple[int, ProxyBehaviour, str]]:
    return [
        (403, ProxyBehaviour.TUNNEL, "tunnel error: unsuccessful"),
        (407, ProxyBehaviour.TUNNEL, "tunnel error: proxy authorization required"),
        (200, ProxyBehaviour.HANG_UP, "tunnel error: unexpected end of file"),
        (200, ProxyBehaviour.GARBAGE, "tunnel error: unsuccessful"),
        (200, ProxyBehaviour.ENDLESS_HEADERS, "tunnel error: proxy response headers too long"),
        (200, ProxyBehaviour.TOO_MANY_HEADERS, "tunnel error: unsuccessful"),
    ]


@pytest.mark.parametrize(
    ("connect_status", "behaviour", "message"),
    _connect_failures(),
    ids=[
        "refused",
        "needs-credentials",
        "hang-up",
        "garbage",
        "endless-headers",
        "too-many-headers",
    ],
)
async def test_a_failed_connect_fails_the_request(
    https_base_url: str,
    tls_ca_pem: bytes,
    connect_status: int,
    behaviour: ProxyBehaviour,
    message: str,
) -> None:
    with serve_http_proxy(connect_status=connect_status, behaviour=behaviour) as proxy:
        async with HTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem]
        ) as client:
            with pytest.raises(HTTPConnectionError, match=message):
                await client.get("items/7")


@pytest.mark.parametrize(
    ("connect_status", "behaviour", "message"),
    _connect_failures(),
    ids=[
        "refused",
        "needs-credentials",
        "hang-up",
        "garbage",
        "endless-headers",
        "too-many-headers",
    ],
)
def test_sync_a_failed_connect_fails_the_request(
    https_base_url: str,
    tls_ca_pem: bytes,
    connect_status: int,
    behaviour: ProxyBehaviour,
    message: str,
) -> None:
    with (
        serve_http_proxy(connect_status=connect_status, behaviour=behaviour) as proxy,
        SyncHTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem]
        ) as client,
        pytest.raises(HTTPConnectionError, match=message),
    ):
        client.get("items/7")


async def test_a_connect_response_arriving_in_parts_opens_the_tunnel(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with serve_http_proxy(behaviour=ProxyBehaviour.SPLIT_HEAD) as proxy:
        async with HTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem]
        ) as client:
            result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_a_connect_response_arriving_in_parts_opens_the_tunnel(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        serve_http_proxy(behaviour=ProxyBehaviour.SPLIT_HEAD) as proxy,
        SyncHTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem]
        ) as client,
    ):
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


async def test_a_stalled_connect_is_bound_by_the_request_timeout(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with serve_http_proxy(behaviour=ProxyBehaviour.SILENT) as proxy:
        async with HTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem], timeout=0.05
        ) as client:
            with pytest.raises(HTTPTimeoutError):
                await client.get("items/7")


def test_sync_a_stalled_connect_is_bound_by_the_request_timeout(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        serve_http_proxy(behaviour=ProxyBehaviour.SILENT) as proxy,
        SyncHTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem], timeout=0.05
        ) as client,
        pytest.raises(HTTPTimeoutError),
    ):
        client.get("items/7")


async def test_a_connect_still_waiting_on_its_proxy_when_the_client_closes_raises_runtime_error(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with serve_http_proxy(behaviour=ProxyBehaviour.SILENT) as proxy:
        client = HTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem]
        )
        await client.__aenter__()
        request = asyncio.create_task(client.get("items/7"))
        await asyncio.sleep(0.1)
        await client.__aexit__(None, None, None)
        closed_at = time.monotonic()

        with pytest.raises(RuntimeError, match="client is closed"):
            await request
        assert time.monotonic() - closed_at < 0.5


def test_sync_a_connect_still_waiting_on_its_proxy_when_the_client_closes_raises_runtime_error(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        serve_http_proxy(behaviour=ProxyBehaviour.SILENT) as proxy,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        client = SyncHTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem]
        )
        client.__enter__()
        request = pool.submit(client.get, "items/7")
        time.sleep(0.1)
        client.__exit__(None, None, None)
        closed_at = time.monotonic()

        with pytest.raises(RuntimeError, match="client is closed"):
            request.result()
        assert time.monotonic() - closed_at < 0.5


# --- The proxy environment variables ----------------------------------------------------------


@pytest.mark.parametrize(
    ("variable", "proxied"),
    [
        ("HTTP_PROXY", {"http"}),
        ("http_proxy", {"http"}),
        ("HTTPS_PROXY", {"https"}),
        ("https_proxy", {"https"}),
        ("ALL_PROXY", {"http", "https"}),
        ("all_proxy", {"http", "https"}),
    ],
)
async def test_a_proxy_variable_proxies_its_own_scheme(
    base_url: str,
    https_base_url: str,
    tls_ca_pem: bytes,
    http_proxy: HTTPProxy,
    variable: str,
    proxied: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(variable, http_proxy.url)

    async with HTTPClient(root_certificates=[tls_ca_pem]) as client:
        await client.get(f"{base_url}items/7")
        await client.get(f"{https_base_url}items/7")

    assert _proxied_schemes(http_proxy) == proxied


@pytest.mark.parametrize(
    ("variable", "proxied"),
    [
        ("HTTP_PROXY", {"http"}),
        ("http_proxy", {"http"}),
        ("HTTPS_PROXY", {"https"}),
        ("https_proxy", {"https"}),
        ("ALL_PROXY", {"http", "https"}),
        ("all_proxy", {"http", "https"}),
    ],
)
def test_sync_a_proxy_variable_proxies_its_own_scheme(
    base_url: str,
    https_base_url: str,
    tls_ca_pem: bytes,
    http_proxy: HTTPProxy,
    variable: str,
    proxied: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(variable, http_proxy.url)

    with SyncHTTPClient(root_certificates=[tls_ca_pem]) as client:
        client.get(f"{base_url}items/7")
        client.get(f"{https_base_url}items/7")

    assert _proxied_schemes(http_proxy) == proxied


@pytest.mark.parametrize(
    ("uppercase", "lowercase", "server_url_fixture"),
    [
        ("HTTP_PROXY", "http_proxy", "base_url"),
        ("HTTPS_PROXY", "https_proxy", "https_base_url"),
        ("ALL_PROXY", "all_proxy", "base_url"),
    ],
)
async def test_an_uppercase_proxy_variable_wins_over_the_lowercase_one(
    tls_ca_pem: bytes,
    http_proxy: HTTPProxy,
    uppercase: str,
    lowercase: str,
    server_url_fixture: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    monkeypatch.setenv(uppercase, http_proxy.url)
    monkeypatch.setenv(lowercase, "http://127.0.0.1:1")

    async with HTTPClient(root_certificates=[tls_ca_pem]) as client:
        result = await client.get(f"{server_url}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert len(http_proxy.requests) == 1


@pytest.mark.parametrize(
    ("uppercase", "lowercase", "server_url_fixture"),
    [
        ("HTTP_PROXY", "http_proxy", "base_url"),
        ("HTTPS_PROXY", "https_proxy", "https_base_url"),
        ("ALL_PROXY", "all_proxy", "base_url"),
    ],
)
def test_sync_an_uppercase_proxy_variable_wins_over_the_lowercase_one(
    tls_ca_pem: bytes,
    http_proxy: HTTPProxy,
    uppercase: str,
    lowercase: str,
    server_url_fixture: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    monkeypatch.setenv(uppercase, http_proxy.url)
    monkeypatch.setenv(lowercase, "http://127.0.0.1:1")

    with SyncHTTPClient(root_certificates=[tls_ca_pem]) as client:
        result = client.get(f"{server_url}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert len(http_proxy.requests) == 1


async def test_an_uppercase_no_proxy_wins_over_the_lowercase_one(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", http_proxy.url)
    monkeypatch.setenv("NO_PROXY", "other-host")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    async with HTTPClient(base_url=base_url) as client:
        await client.get("items/7")

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]


def test_sync_an_uppercase_no_proxy_wins_over_the_lowercase_one(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", http_proxy.url)
    monkeypatch.setenv("NO_PROXY", "other-host")
    monkeypatch.setenv("no_proxy", "127.0.0.1")

    with SyncHTTPClient(base_url=base_url) as client:
        client.get("items/7")

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]


def _no_proxy_cases() -> list[tuple[str, str, str, bool]]:
    """(variable, its value, the target host, whether the request goes through the proxy)."""
    return [
        ("NO_PROXY", "127.0.0.1", "127.0.0.1", False),
        ("no_proxy", "127.0.0.1", "127.0.0.1", False),
        ("NO_PROXY", "127.0.0.0/8", "127.0.0.1", False),
        ("NO_PROXY", "10.0.0.0/8", "127.0.0.1", True),
        ("NO_PROXY", "other-host, 127.0.0.1", "127.0.0.1", False),
        ("NO_PROXY", "localhost", "localhost", False),
        ("NO_PROXY", ".localhost", "localhost", False),
        ("NO_PROXY", "LOCALHOST", "localhost", False),
        # A name matches its subdomains, with or without a leading dot.
        ("NO_PROXY", "localhost", "sub.localhost", False),
        ("NO_PROXY", ".localhost", "sub.localhost", False),
        ("NO_PROXY", "sub.localhost", "localhost", True),
        # A suffix only matches at a dot.
        ("NO_PROXY", "host", "localhost", True),
        # `*` matches every name, but no IP address (hyper-util's, as curl's).
        ("NO_PROXY", "*", "localhost", False),
        ("NO_PROXY", "*", "127.0.0.1", True),
    ]


def _skip_unless_resolvable(host: str) -> None:
    """`sub.localhost` is loopback on macOS and under systemd-resolved (ubuntu, CI's runners), as
    RFC 6761 has it, but not in every container: without `nss-myhostname` it goes to DNS."""
    try:
        socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        pytest.skip(f"{host} doesn't resolve to loopback here")


def _no_proxy_case_id(case: tuple[str, str, str, bool]) -> str:
    variable, value, host, _ = case
    return f"{variable}={value}->{host}"


@pytest.mark.parametrize("case", _no_proxy_cases(), ids=_no_proxy_case_id)
async def test_no_proxy_matches_hosts_as_curl_does(
    base_url: str,
    http_proxy: HTTPProxy,
    case: tuple[str, str, str, bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable, value, host, proxied = case
    _skip_unless_resolvable(host)
    monkeypatch.setenv("HTTP_PROXY", http_proxy.url)
    monkeypatch.setenv(variable, value)

    async with HTTPClient() as client:
        result = await client.get(f"http://{host}:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert len(http_proxy.requests) == (1 if proxied else 0)


@pytest.mark.parametrize("case", _no_proxy_cases(), ids=_no_proxy_case_id)
def test_sync_no_proxy_matches_hosts_as_curl_does(
    base_url: str,
    http_proxy: HTTPProxy,
    case: tuple[str, str, str, bool],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variable, value, host, proxied = case
    _skip_unless_resolvable(host)
    monkeypatch.setenv("HTTP_PROXY", http_proxy.url)
    monkeypatch.setenv(variable, value)

    with SyncHTTPClient() as client:
        result = client.get(f"http://{host}:{_port(base_url)}/items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert len(http_proxy.requests) == (1 if proxied else 0)


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
async def test_a_cgi_request_method_turns_every_proxy_variable_off(
    base_url: str,
    https_base_url: str,
    tls_ca_pem: bytes,
    http_proxy: HTTPProxy,
    variable: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Under CGI, `HTTP_PROXY` could be a request's `Proxy:` header (httpoxy).
    monkeypatch.setenv("REQUEST_METHOD", "GET")
    monkeypatch.setenv(variable, http_proxy.url)

    async with HTTPClient(root_certificates=[tls_ca_pem]) as client:
        await client.get(f"{base_url}items/7")
        await client.get(f"{https_base_url}items/7")

    assert http_proxy.requests == []


@pytest.mark.parametrize("variable", ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"])
def test_sync_a_cgi_request_method_turns_every_proxy_variable_off(
    base_url: str,
    https_base_url: str,
    tls_ca_pem: bytes,
    http_proxy: HTTPProxy,
    variable: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REQUEST_METHOD", "GET")
    monkeypatch.setenv(variable, http_proxy.url)

    with SyncHTTPClient(root_certificates=[tls_ca_pem]) as client:
        client.get(f"{base_url}items/7")
        client.get(f"{https_base_url}items/7")

    assert http_proxy.requests == []


async def test_proxy_variables_are_read_when_the_client_is_entered(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = HTTPClient(base_url=base_url)
    monkeypatch.setenv("HTTP_PROXY", http_proxy.url)

    async with client:
        await client.get("items/7")

    assert len(http_proxy.requests) == 1


def test_sync_proxy_variables_are_read_when_the_client_is_entered(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = SyncHTTPClient(base_url=base_url)
    monkeypatch.setenv("HTTP_PROXY", http_proxy.url)

    with client:
        client.get("items/7")

    assert len(http_proxy.requests) == 1


async def test_proxy_variables_set_after_entering_are_not_read(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with HTTPClient(base_url=base_url) as client:
        monkeypatch.setenv("HTTP_PROXY", http_proxy.url)
        await client.get("items/7")

    assert http_proxy.requests == []


def test_sync_proxy_variables_set_after_entering_are_not_read(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    with SyncHTTPClient(base_url=base_url) as client:
        monkeypatch.setenv("HTTP_PROXY", http_proxy.url)
        client.get("items/7")

    assert http_proxy.requests == []


async def test_proxy_turns_the_proxy_variables_off(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    async with HTTPClient(base_url=base_url, proxy=http_proxy.url) as client:
        await client.get("items/7")
    async with HTTPClient(
        base_url=base_url, proxy=http_proxy.url, no_proxy=["127.0.0.1"]
    ) as client:
        await client.get("items/7")

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]


def test_sync_proxy_turns_the_proxy_variables_off(
    base_url: str, http_proxy: HTTPProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")

    with SyncHTTPClient(base_url=base_url, proxy=http_proxy.url) as client:
        client.get("items/7")
    with SyncHTTPClient(base_url=base_url, proxy=http_proxy.url, no_proxy=["127.0.0.1"]) as client:
        client.get("items/7")

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"]


# --- SOCKS5 -----------------------------------------------------------------------------------


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
async def test_socks5h_leaves_resolving_the_host_to_the_proxy(
    tls_ca_pem: bytes,
    socks_proxy: SocksProxy,
    server_url_fixture: str,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    target = server_url.replace("127.0.0.1", "localhost")

    async with HTTPClient(
        proxy=f"socks5h://{socks_proxy.address}", root_certificates=[tls_ca_pem]
    ) as client:
        result = await client.get(f"{target}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host, h.port) for h in socks_proxy.handshakes] == [
        (3, "localhost", _port(server_url))
    ]


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
def test_sync_socks5h_leaves_resolving_the_host_to_the_proxy(
    tls_ca_pem: bytes,
    socks_proxy: SocksProxy,
    server_url_fixture: str,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    target = server_url.replace("127.0.0.1", "localhost")

    with SyncHTTPClient(
        proxy=f"socks5h://{socks_proxy.address}", root_certificates=[tls_ca_pem]
    ) as client:
        result = client.get(f"{target}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host, h.port) for h in socks_proxy.handshakes] == [
        (3, "localhost", _port(server_url))
    ]


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
async def test_socks5_resolves_the_host_itself(
    tls_ca_pem: bytes,
    socks_proxy: SocksProxy,
    server_url_fixture: str,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    target = server_url.replace("127.0.0.1", "localhost")

    async with HTTPClient(
        proxy=f"socks5://{socks_proxy.address}", root_certificates=[tls_ca_pem]
    ) as client:
        result = await client.get(f"{target}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.host, h.port) for h in socks_proxy.handshakes] == [
        (_first_address("localhost"), _port(server_url))
    ]
    assert socks_proxy.handshakes[0].address_type in {1, 4}


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
def test_sync_socks5_resolves_the_host_itself(
    tls_ca_pem: bytes,
    socks_proxy: SocksProxy,
    server_url_fixture: str,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    target = server_url.replace("127.0.0.1", "localhost")

    with SyncHTTPClient(
        proxy=f"socks5://{socks_proxy.address}", root_certificates=[tls_ca_pem]
    ) as client:
        result = client.get(f"{target}items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.host, h.port) for h in socks_proxy.handshakes] == [
        (_first_address("localhost"), _port(server_url))
    ]
    assert socks_proxy.handshakes[0].address_type in {1, 4}


async def test_socks5_sends_an_ip_address_as_is(
    socks_proxy: SocksProxy, socks_client: HTTPClient
) -> None:
    await socks_client.get("items/7")

    assert [(h.address_type, h.host) for h in socks_proxy.handshakes] == [(1, "127.0.0.1")]


def test_sync_socks5_sends_an_ip_address_as_is(
    socks_proxy: SocksProxy, sync_socks_client: SyncHTTPClient
) -> None:
    sync_socks_client.get("items/7")

    assert [(h.address_type, h.host) for h in socks_proxy.handshakes] == [(1, "127.0.0.1")]


async def test_socks5h_sends_an_ipv6_address_as_is(base_url: str, socks_proxy: SocksProxy) -> None:
    # The proxy connects on to the test server on 127.0.0.1, whatever address it was asked for.
    async with HTTPClient(proxy=f"socks5h://{socks_proxy.address}") as client:
        await client.get(f"http://[::1]:{_port(base_url)}/items/7")

    assert [(h.address_type, h.host) for h in socks_proxy.handshakes] == [(4, "::1")]


def test_sync_socks5h_sends_an_ipv6_address_as_is(base_url: str, socks_proxy: SocksProxy) -> None:
    with SyncHTTPClient(proxy=f"socks5h://{socks_proxy.address}") as client:
        client.get(f"http://[::1]:{_port(base_url)}/items/7")

    assert [(h.address_type, h.host) for h in socks_proxy.handshakes] == [(4, "::1")]


async def test_socks5_authenticates_with_the_proxy_credentials(base_url: str) -> None:
    with serve_socks5_proxy(credentials=("us er", "pass")) as proxy:
        async with HTTPClient(
            base_url=base_url, proxy=f"socks5://us%20er:pass@{proxy.address}"
        ) as client:
            result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.methods, h.credentials) for h in proxy.handshakes] == [(b"\x02", ("us er", "pass"))]


def test_sync_socks5_authenticates_with_the_proxy_credentials(base_url: str) -> None:
    with (
        serve_socks5_proxy(credentials=("us er", "pass")) as proxy,
        SyncHTTPClient(base_url=base_url, proxy=f"socks5://us%20er:pass@{proxy.address}") as client,
    ):
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.methods, h.credentials) for h in proxy.handshakes] == [(b"\x02", ("us er", "pass"))]


async def test_socks5_without_credentials_offers_no_authentication(
    socks_proxy: SocksProxy, socks_client: HTTPClient
) -> None:
    await socks_client.get("items/7")

    assert [(h.methods, h.credentials) for h in socks_proxy.handshakes] == [(b"\x00", None)]


def test_sync_socks5_without_credentials_offers_no_authentication(
    socks_proxy: SocksProxy, sync_socks_client: SyncHTTPClient
) -> None:
    sync_socks_client.get("items/7")

    assert [(h.methods, h.credentials) for h in socks_proxy.handshakes] == [(b"\x00", None)]


async def test_a_socks_proxy_variable_is_used(
    base_url: str, socks_proxy: SocksProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALL_PROXY", f"socks5h://{socks_proxy.address}")

    async with HTTPClient(base_url=base_url.replace("127.0.0.1", "localhost")) as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host) for h in socks_proxy.handshakes] == [(3, "localhost")]


def test_sync_a_socks_proxy_variable_is_used(
    base_url: str, socks_proxy: SocksProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALL_PROXY", f"socks5h://{socks_proxy.address}")

    with SyncHTTPClient(base_url=base_url.replace("127.0.0.1", "localhost")) as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}
    assert [(h.address_type, h.host) for h in socks_proxy.handshakes] == [(3, "localhost")]


async def test_a_socks_proxy_without_a_port_is_on_1080(
    base_url: str, socks_proxy_on_1080: SocksProxy
) -> None:
    async with HTTPClient(base_url=base_url, proxy="socks5://127.0.0.1") as client:
        await client.get("items/7")

    assert len(socks_proxy_on_1080.handshakes) == 1


def test_sync_a_socks_proxy_without_a_port_is_on_1080(
    base_url: str, socks_proxy_on_1080: SocksProxy
) -> None:
    with SyncHTTPClient(base_url=base_url, proxy="socks5://127.0.0.1") as client:
        client.get("items/7")

    assert len(socks_proxy_on_1080.handshakes) == 1


@dataclass(slots=True)
class _SocksFailure:
    id: str
    message: str
    # The server's.
    credentials: tuple[str, str] | None = None
    behaviour: SocksBehaviour = SocksBehaviour.SERVE
    # The client's, as they go in the proxy URL.
    userinfo: str = ""
    scheme: str = "socks5"
    # `{port}` is the test server's.
    target: str = "http://127.0.0.1:{port}/items/7"


def _socks_failures() -> list[_SocksFailure]:
    long_host = ".".join(["a" * 60] * 5)
    return [
        _SocksFailure(
            "wrong-credentials",
            "credentials not accepted",
            credentials=("user", "pass"),
            userinfo="user:wrong@",
        ),
        _SocksFailure(
            "missing-credentials",
            "server does not support user/pass authentication",
            credentials=("user", "pass"),
        ),
        _SocksFailure(
            "method-mismatch",
            "server implements authentication incorrectly",
            behaviour=SocksBehaviour.WRONG_METHOD,
        ),
        _SocksFailure(
            "garbage-reply", "failed parsing server response", behaviour=SocksBehaviour.GARBAGE
        ),
        _SocksFailure(
            "hang-up", "io error during SOCKS handshake", behaviour=SocksBehaviour.HANG_UP
        ),
        _SocksFailure(
            "target-refused", "SOCKS error: connection refused", target="http://127.0.0.1:1/"
        ),
        _SocksFailure(
            "host-too-long",
            "host address is more than 255 characters",
            scheme="socks5h",
            target=f"http://{long_host}:{{port}}/",
        ),
        _SocksFailure(
            "unresolvable-host",
            "error resolving for socks proxy",
            target="http://nonexistent.invalid:{port}/",
        ),
    ]


@pytest.mark.parametrize("case", _socks_failures(), ids=lambda case: case.id)
async def test_a_failed_socks5_handshake_fails_the_request(
    base_url: str, case: _SocksFailure
) -> None:
    with serve_socks5_proxy(credentials=case.credentials, behaviour=case.behaviour) as proxy:
        async with HTTPClient(
            proxy=f"{case.scheme}://{case.userinfo}{proxy.address}", timeout=5.0
        ) as client:
            with pytest.raises(HTTPConnectionError, match=case.message):
                await client.get(case.target.format(port=_port(base_url)))


@pytest.mark.parametrize("case", _socks_failures(), ids=lambda case: case.id)
def test_sync_a_failed_socks5_handshake_fails_the_request(
    base_url: str, case: _SocksFailure
) -> None:
    with (
        serve_socks5_proxy(credentials=case.credentials, behaviour=case.behaviour) as proxy,
        SyncHTTPClient(
            proxy=f"{case.scheme}://{case.userinfo}{proxy.address}", timeout=5.0
        ) as client,
        pytest.raises(HTTPConnectionError, match=case.message),
    ):
        client.get(case.target.format(port=_port(base_url)))


def _socks_reply(code: int, bound: bytes = b"\x01\x00\x00\x00\x00", reserved: int = 0) -> bytes:
    """A SOCKS5 `CONNECT` reply: version 5, `code`, `reserved`, then the bound address (its type
    byte and the address) and port 0."""
    return bytes([0x05, code, reserved]) + bound + b"\x00\x00"


def _socks_reply_failures() -> list[tuple[bytes, str]]:
    """(a `CONNECT` reply, the error it fails the request with)."""
    unparsable = "failed parsing server response"
    return [
        (_socks_reply(0x01), "SOCKS error: general server failure"),
        (_socks_reply(0x02), "SOCKS error: connection not allowed"),
        (_socks_reply(0x03), "SOCKS error: network unreachable"),
        (_socks_reply(0x04), "SOCKS error: host unreachable"),
        (_socks_reply(0x06), "SOCKS error: ttl expired"),
        (_socks_reply(0x07), "SOCKS error: command not supported"),
        (_socks_reply(0x08), "SOCKS error: address type not supported"),
        (_socks_reply(0x09), unparsable),
        (b"\x04" + _socks_reply(0x00)[1:], unparsable),
        (_socks_reply(0x00, reserved=1), unparsable),
        (_socks_reply(0x00, bound=b"\x03\x00"), unparsable),
        (_socks_reply(0x00, bound=b"\x03\x01\xff"), unparsable),
        (_socks_reply(0x00, bound=b"\x05"), unparsable),
    ]


def _socks_reply_failure_id(case: tuple[bytes, str]) -> str:
    return case[0].hex()


@pytest.mark.parametrize("case", _socks_reply_failures(), ids=_socks_reply_failure_id)
async def test_a_socks5_connect_reply_that_is_not_a_success_fails_the_request(
    base_url: str, case: tuple[bytes, str]
) -> None:
    connect_reply, message = case
    with serve_socks5_proxy(connect_reply=[connect_reply]) as proxy:
        async with HTTPClient(
            base_url=base_url, proxy=f"socks5://{proxy.address}", timeout=5.0
        ) as client:
            with pytest.raises(HTTPConnectionError, match=message):
                await client.get("items/7")


@pytest.mark.parametrize("case", _socks_reply_failures(), ids=_socks_reply_failure_id)
def test_sync_a_socks5_connect_reply_that_is_not_a_success_fails_the_request(
    base_url: str, case: tuple[bytes, str]
) -> None:
    connect_reply, message = case
    with (
        serve_socks5_proxy(connect_reply=[connect_reply]) as proxy,
        SyncHTTPClient(base_url=base_url, proxy=f"socks5://{proxy.address}", timeout=5.0) as client,
        pytest.raises(HTTPConnectionError, match=message),
    ):
        client.get("items/7")


def _socks_bound_addresses() -> list[bytes]:
    """A successful reply's bound address, of each type: an IPv4 address, a domain name, an IPv6
    address. The reply comes in two reads, its port last, so a client reading a bound address
    as shorter than it is passes those two bytes on as the start of the HTTP response."""
    return [b"\x01" + bytes(4), b"\x03\x04host", b"\x04" + bytes(16)]


def _split_socks_reply(bound: bytes) -> list[bytes]:
    reply = _socks_reply(0x00, bound=bound)
    return [reply[:-2], reply[-2:]]


@pytest.mark.parametrize("bound", _socks_bound_addresses(), ids=["ipv4", "domain", "ipv6"])
async def test_a_socks5_success_reply_may_bind_any_address_type(
    base_url: str, bound: bytes
) -> None:
    with serve_socks5_proxy(connect_reply=_split_socks_reply(bound)) as proxy:
        async with HTTPClient(base_url=base_url, proxy=f"socks5://{proxy.address}") as client:
            result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("bound", _socks_bound_addresses(), ids=["ipv4", "domain", "ipv6"])
def test_sync_a_socks5_success_reply_may_bind_any_address_type(base_url: str, bound: bytes) -> None:
    with (
        serve_socks5_proxy(connect_reply=_split_socks_reply(bound)) as proxy,
        SyncHTTPClient(base_url=base_url, proxy=f"socks5://{proxy.address}") as client,
    ):
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


async def test_a_stalled_socks5_handshake_is_bound_by_the_request_timeout(base_url: str) -> None:
    with serve_socks5_proxy(behaviour=SocksBehaviour.SILENT) as proxy:
        async with HTTPClient(
            base_url=base_url, proxy=f"socks5://{proxy.address}", timeout=0.05
        ) as client:
            with pytest.raises(HTTPTimeoutError):
                await client.get("items/7")


def test_sync_a_stalled_socks5_handshake_is_bound_by_the_request_timeout(base_url: str) -> None:
    with (
        serve_socks5_proxy(behaviour=SocksBehaviour.SILENT) as proxy,
        SyncHTTPClient(
            base_url=base_url, proxy=f"socks5://{proxy.address}", timeout=0.05
        ) as client,
        pytest.raises(HTTPTimeoutError),
    ):
        client.get("items/7")


# --- Pooling ----------------------------------------------------------------------------------


async def test_each_proxied_origin_reuses_its_own_connection(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")
    async with HTTPClient(proxy=http_proxy.url) as client:
        for url in (base_url, other_origin, base_url, other_origin):
            await client.get(f"{url}items/7")

    first, second, third, fourth = (request.client_port for request in http_proxy.requests)
    assert (first, second) == (third, fourth)
    assert first != second


def test_sync_each_proxied_origin_reuses_its_own_connection(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")
    with SyncHTTPClient(proxy=http_proxy.url) as client:
        for url in (base_url, other_origin, base_url, other_origin):
            client.get(f"{url}items/7")

    first, second, third, fourth = (request.client_port for request in http_proxy.requests)
    assert (first, second) == (third, fourth)
    assert first != second


async def test_a_tunnel_is_reused_like_a_direct_connection(
    http_proxy: HTTPProxy, tunnel_client: HTTPClient
) -> None:
    await tunnel_client.get("items/7")
    await tunnel_client.get("items/7")

    assert len(http_proxy.requests) == 1


def test_sync_a_tunnel_is_reused_like_a_direct_connection(
    http_proxy: HTTPProxy, sync_tunnel_client: SyncHTTPClient
) -> None:
    sync_tunnel_client.get("items/7")
    sync_tunnel_client.get("items/7")

    assert len(http_proxy.requests) == 1


async def test_direct_and_proxied_origins_keep_separate_connections(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    direct_origin = base_url.replace("127.0.0.1", "localhost")
    direct_ports: list[int] = []
    async with HTTPClient(proxy=http_proxy.url, no_proxy=["localhost"]) as client:
        for _ in range(2):
            await client.get(f"{base_url}items/7")
            direct_ports.append((await client.get(f"{direct_origin}peer", into=dict)).data["port"])

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"] * 2
    assert len({request.client_port for request in http_proxy.requests}) == 1
    assert direct_ports[0] == direct_ports[1]


def test_sync_direct_and_proxied_origins_keep_separate_connections(
    base_url: str, http_proxy: HTTPProxy
) -> None:
    direct_origin = base_url.replace("127.0.0.1", "localhost")
    direct_ports: list[int] = []
    with SyncHTTPClient(proxy=http_proxy.url, no_proxy=["localhost"]) as client:
        for _ in range(2):
            client.get(f"{base_url}items/7")
            direct_ports.append(client.get(f"{direct_origin}peer", into=dict).data["port"])

    assert _lines(http_proxy) == [f"GET {base_url}items/7 HTTP/1.1"] * 2
    assert len({request.client_port for request in http_proxy.requests}) == 1
    assert direct_ports[0] == direct_ports[1]
