"""What lothc puts on the wire, pinned exactly: the request line, every header in order with its
casing, and the body, as the test server's `/echo-request` received them, plus each response's
status and `RequestInfo`. Through a proxy, what the proxy received (`_proxy_server.py` records
it): the absolute-form request to an HTTP proxy, the `CONNECT` before a tunnel, the SOCKS5
handshake.

Every case runs on both clients, against the same expected value. A port anywhere in a value is
written `<port>`, and lothc's default user agent `python-lothc/<version>`, since both change from
run to run or release to release. Every test starts with no proxy variables set.
"""

import json
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
from _helpers import clear_proxy_variables
from _proxy_server import (
    HTTPProxy,
    ProxiedRequest,
    SocksHandshake,
    serve_http_proxy,
    serve_socks5_proxy,
)
from msgspec import Struct
from pydantic import BaseModel, Field

from lothc import HTTPClient, RequestInfo, SyncHTTPClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _QueryStruct(Struct):
    page: int
    tag: str | None = None
    tags: list[str] = []


class _HeadersStruct(Struct):
    x_request_id: str
    x_skip: str | None = None


class _ItemStruct(Struct):
    item_id: int
    note: str | None = None


class _QueryModel(BaseModel):
    page: int
    tag: str | None = None


class _HeadersModel(BaseModel):
    x_request_id: str


class _AliasedModel(BaseModel):
    item_id: int = Field(alias="itemId")


@dataclass(slots=True)
class _Wire:
    """One request as it arrived."""

    line: str
    headers: list[tuple[str, str]]
    body: bytes = b""


@dataclass(slots=True)
class _Seen:
    status: int
    request: RequestInfo
    # What `/echo-request` (or the proxy) received; `None` for any other route.
    wire: _Wire | None = None


@dataclass(slots=True)
class _Case:
    id: str
    verb: str = "get"
    # `{base_url}`, `{origin}` (`//host:port/`) and `{userinfo_base_url}` (`base_url` with
    # `user:pass@`) are filled in from the test server's URL.
    path: str = "/echo-request"
    call: dict[str, Any] = field(default_factory=dict[str, Any])
    client: dict[str, Any] = field(default_factory=dict[str, Any])
    # Appended to the server's URL to make the client's `base_url`.
    base_suffix: str = ""
    # Through an HTTP proxy, the client's credentials for it, as they go in the proxy URL.
    proxy_userinfo: str = ""
    # `None` for a case that only describes a request, its expected value kept elsewhere.
    expected: _Seen | None = field(default=None, kw_only=True)


@dataclass(slots=True)
class _TunnelCase:
    id: str
    client: dict[str, Any]
    # The client's, as they go in the proxy URL.
    userinfo: str
    # What the proxy received (the `CONNECT`), then what the server did through the tunnel.
    expected: tuple[_Wire, _Seen]


@dataclass(slots=True)
class _SocksCase:
    scheme: str
    credentials: tuple[str, str] | None
    # The origin's host: `socks5` is given an address, so nothing depends on how this machine
    # resolves `localhost`.
    host: str
    # Each handshake's `port` is written 0 and stands for the test server's.
    expected: tuple[list[SocksHandshake], _Seen]


@pytest.fixture(name="no_proxy_variables", autouse=True)
def _no_proxy_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_proxy_variables(monkeypatch)


def _masked(text: str, ports: list[int]) -> str:
    for port in ports:
        text = text.replace(f":{port}", ":<port>")
    return text.replace(f"python-lothc/{version('lothc')}", "python-lothc/<version>")


def _masked_wire(wire: _Wire, ports: list[int]) -> _Wire:
    headers = [(name, _masked(value, ports)) for name, value in wire.headers]
    return _Wire(_masked(wire.line, ports), headers, wire.body)


def _masked_request(request: RequestInfo, ports: list[int]) -> RequestInfo:
    return RequestInfo(request.method, _masked(request.url, ports), request.path, request.host)


def _proxied_wire(request: ProxiedRequest) -> _Wire:
    return _Wire(request.line, request.headers, request.body)


def _seen(response: Any, ports: list[int]) -> _Seen:  # noqa: ANN401 — either client's `Response`
    wire = None
    if response.request.path == "/echo-request" and response.status == 200:
        echoed = json.loads(response.data)
        headers = [(name, value) for name, value in echoed["headers"]]
        body = echoed["body"].encode("latin-1")
        wire = _masked_wire(_Wire(echoed["line"], headers, body), ports)
    return _Seen(response.status, _masked_request(response.request, ports), wire)


def _port(url: str) -> int:
    port = urlsplit(url).port
    if port is None:
        raise ValueError(f"{url} has no port")
    return port


def _path(case: _Case, base_url: str) -> str:
    return case.path.format(
        base_url=base_url,
        origin=base_url.removeprefix("http:"),
        userinfo_base_url=base_url.replace("://", "://user:pass@"),
    )


async def _async_seen(client: HTTPClient, case: _Case, base_url: str, ports: list[int]) -> _Seen:
    async with client:
        send: Callable[..., Awaitable[Any]] = getattr(client, case.verb)
        response = await send(_path(case, base_url), error_for_status=False, **case.call)
    return _seen(response, ports)


def _sync_seen(client: SyncHTTPClient, case: _Case, base_url: str, ports: list[int]) -> _Seen:
    with client:
        send: Callable[..., Any] = getattr(client, case.verb)
        response = send(_path(case, base_url), error_for_status=False, **case.call)
    return _seen(response, ports)


def _taken(proxy: HTTPProxy, ports: list[int]) -> list[_Wire]:
    return [_masked_wire(_proxied_wire(request), ports) for request in proxy.requests]


def _with_proxy_wire(seen: _Seen, proxy: HTTPProxy, ports: list[int]) -> _Seen:
    """`seen` with what the proxy received in place of what the server did."""
    (wire,) = _taken(proxy, ports)
    return _Seen(seen.status, seen.request, wire)


def _wire_cases() -> list[_Case]:
    return [
        _Case(
            "plain-get",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "params-and-headers",
            call={
                "params": {"a": "x y", "b": [1, 2], "c": True, "d": 1.5, "e": "ü&=*~"},
                "headers": {"X-Custom": "value"},
            },
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?a=x+y&b=1&b=2&c=true&d=1.5&e=%C3%BC%26%3D*%7E",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request?a=x+y&b=1&b=2&c=true&d=1.5&e=%C3%BC%26%3D*%7E HTTP/1.1",
                    headers=[
                        ("X-Custom", "value"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "params-onto-an-existing-query",
            path="/echo-request?q=1",
            call={"params": {"z": 2}, "headers": {"User-Agent": "user-agent"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?q=1&z=2",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request?q=1&z=2 HTTP/1.1",
                    headers=[
                        ("User-Agent", "user-agent"),
                        ("Accept", "*/*"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "params-repeated-and-signed",
            call={"params": {"t": ("a", "b"), "n": -3, "i64-max": 2**63 - 1, "f53": 2**53 + 1}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?t=a&t=b&n=-3&i64-max=9223372036854775807&f53=9007199254740993",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line=(
                        "GET /echo-request?t=a&t=b&n=-3&i64-max=9223372036854775807"
                        "&f53=9007199254740993 HTTP/1.1"
                    ),
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "params-floats",
            call={"params": {"f": 1.0, "small": 1e-7, "big": 1e20}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?f=1.0&small=1e-7&big=1e20",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request?f=1.0&small=1e-7&big=1e20 HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "struct-params-and-headers",
            call={
                "params": _QueryStruct(page=1, tags=["x", "y"]),
                "headers": _HeadersStruct(x_request_id="request-id"),
            },
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?page=1&tags=x&tags=y",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request?page=1&tags=x&tags=y HTTP/1.1",
                    headers=[
                        ("X-Request-Id", "request-id"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "model-params-and-headers",
            call={
                "params": _QueryModel(page=2, tag="tag"),
                "headers": _HeadersModel(x_request_id="request-id"),
            },
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?page=2&tag=tag",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request?page=2&tag=tag HTTP/1.1",
                    headers=[
                        ("X-Request-Id", "request-id"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "empty-params-drop-the-question-mark",
            path="/echo-request?",
            call={"params": {}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "a-bare-question-mark-is-kept",
            path="/echo-request?",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request? HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "a-fragment-stays-off-the-wire",
            path="/echo-request?a=1#fragment",
            call={"params": {"b": 2}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?a=1&b=2#fragment",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request?a=1&b=2 HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "headers-differing-only-in-case",
            call={"headers": {"X-Same": "1", "x-same": "2"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("X-Same", "1"),
                        ("X-Same", "2"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "host-header-override",
            call={"headers": {"Host": "host"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Host", "host"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "accept-encoding-override",
            call={"headers": {"Accept-Encoding": "identity"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept-Encoding", "identity"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "default-accept-encoding",
            client={"default_headers": {"Accept-Encoding": "gzip", "X-Default": "0"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept-Encoding", "gzip"),
                        ("X-Default", "0"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "bearer-token",
            client={"bearer_token": "token"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "Bearer token"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "skip-auth",
            client={"bearer_token": "token"},
            call={"skip_auth": True},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "basic-auth",
            client={"basic_auth": ("user", "pa:ss")},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "Basic dXNlcjpwYTpzcw=="),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "basic-auth-without-password",
            client={"basic_auth": ("user", None)},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "Basic dXNlcjo="),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "authorization-header-overrides-auth",
            client={"bearer_token": "token"},
            call={"headers": {"X-First": "1", "Authorization": "authorization"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "authorization"),
                        ("X-First", "1"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "userinfo-in-the-url",
            path="{userinfo_base_url}echo-request",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "Basic dXNlcjpwYXNz"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "userinfo-in-the-url-and-a-bearer-token",
            path="{userinfo_base_url}echo-request",
            client={"bearer_token": "token"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "Bearer token"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "user-agent",
            client={"user_agent": "user-agent"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "user-agent"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "default-headers",
            client={"default_headers": {"X-Default": "default", "Accept": "accept"}},
            call={"headers": {"x-default": "override"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "accept"),
                        ("X-Default", "override"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "default-headers-user-agent-wins-over-user-agent",
            client={"default_headers": {"User-Agent": "default"}, "user_agent": "user-agent"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("User-Agent", "default"),
                        ("Accept", "*/*"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "default-authorization-and-bearer-token",
            client={
                "default_headers": {"Authorization": "default", "X-Default": "0"},
                "bearer_token": "token",
            },
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("X-Default", "0"),
                        ("Authorization", "Bearer token"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "json-dict",
            verb="post",
            call={"json": {"k": [1, 2.5, "s", None, True], "u": "ü"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "36"),
                    ],
                    body=b'{"k":[1,2.5,"s",null,true],"u":"\xc3\xbc"}',
                ),
            ),
        ),
        _Case(
            "json-dict-keys-and-numbers",
            verb="post",
            call={
                "json": {"b": {"d": 1, "c": 2}, "a": [1e20, 0.1, -0.0, 1e-7, 1e16, 1e15, 5e-324]}
            },
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "77"),
                    ],
                    body=b'{"a":[1e+20,0.1,-0.0,1e-7,1e+16,1000000000000000.0,5e-324],"b":{"c":2,"d":1}}',
                ),
            ),
        ),
        _Case(
            "json-list",
            verb="put",
            call={"json": [1e20, float("nan"), chr(0x2028), "\x00"]},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="PUT",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="PUT /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "27"),
                    ],
                    body=b'[1e+20,null,"\xe2\x80\xa8","\\u0000"]',
                ),
            ),
        ),
        _Case(
            "json-struct",
            verb="post",
            call={"json": _ItemStruct(item_id=1)},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "25"),
                    ],
                    body=b'{"item_id":1,"note":null}',
                ),
            ),
        ),
        _Case(
            "json-model-by-alias",
            verb="patch",
            call={"json": _AliasedModel(itemId=1)},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="PATCH",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="PATCH /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "12"),
                    ],
                    body=b'{"itemId":1}',
                ),
            ),
        ),
        _Case(
            "json-dict-replaces-a-content-type-header",
            verb="post",
            call={
                "json": {"a": 1},
                "headers": {"X-1": "1", "Content-Type": "text/plain", "X-2": "2"},
            },
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("X-1", "1"),
                        ("Content-Type", "application/json"),
                        ("X-2", "2"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "7"),
                    ],
                    body=b'{"a":1}',
                ),
            ),
        ),
        _Case(
            "json-struct-replaces-a-content-type-header",
            verb="post",
            client={"default_headers": {"Content-Type": "default", "X-Default": "0"}},
            call={"json": _ItemStruct(item_id=1), "headers": {"X-1": "1"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("X-Default", "0"),
                        ("X-1", "1"),
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "25"),
                    ],
                    body=b'{"item_id":1,"note":null}',
                ),
            ),
        ),
        _Case(
            "data-form",
            verb="post",
            client={"bearer_token": "token"},
            call={
                "data": {"a": "x y", "b": [1, 2], "c": "ü&=*~", "d": True},
                "headers": {"X-1": "1"},
            },
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/x-www-form-urlencoded"),
                        ("Authorization", "Bearer token"),
                        ("X-1", "1"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "39"),
                    ],
                    body=b"a=x+y&b=1&b=2&c=%C3%BC%26%3D*%7E&d=true",
                ),
            ),
        ),
        _Case(
            "data-model",
            verb="post",
            call={"data": _QueryModel(page=1)},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/x-www-form-urlencoded"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "6"),
                    ],
                    body=b"page=1",
                ),
            ),
        ),
        _Case(
            "data-keeps-a-content-type-header",
            verb="post",
            call={"data": {"a": "b"}, "headers": {"content-type": "text/plain"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "text/plain"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "3"),
                    ],
                    body=b"a=b",
                ),
            ),
        ),
        _Case(
            "data-empty",
            verb="post",
            call={"data": {}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/x-www-form-urlencoded"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "content-text",
            verb="post",
            call={"content": "héllo"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "6"),
                    ],
                    body=b"h\xc3\xa9llo",
                ),
            ),
        ),
        _Case(
            "content-bytes",
            verb="delete",
            call={"content": b"\x00\xffraw"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="DELETE",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="DELETE /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "5"),
                    ],
                    body=b"\x00\xffraw",
                ),
            ),
        ),
        _Case(
            "content-empty",
            verb="put",
            call={"content": b""},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="PUT",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="PUT /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "post-without-a-body",
            verb="post",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "delete",
            verb="delete",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="DELETE",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="DELETE /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "base-path-prefix-kept",
            path="items/7",
            base_suffix="prefix/",
            expected=_Seen(
                status=404,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/prefix/items/7",
                    path="/prefix/items/7",
                    host="127.0.0.1",
                ),
                wire=None,
            ),
        ),
        _Case(
            "leading-slash-drops-the-base-path",
            base_suffix="prefix/",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "absolute-url-overrides-the-base",
            path="{base_url}echo-request",
            base_suffix="prefix/",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "protocol-relative-url",
            path="{origin}echo-request",
            base_suffix="prefix/",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "relative-path-without-leading-slash",
            path="echo-request",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "percent-encoding-and-dot-segments",
            path="/échø/../echo%20x/./y",
            expected=_Seen(
                status=404,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo%20x/y",
                    path="/echo%20x/y",
                    host="127.0.0.1",
                ),
                wire=None,
            ),
        ),
        _Case(
            "head",
            verb="head",
            path="/items/7",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="HEAD",
                    url="http://127.0.0.1:<port>/items/7",
                    path="/items/7",
                    host="127.0.0.1",
                ),
                wire=None,
            ),
        ),
        _Case(
            "options",
            verb="options",
            path="/items",
            expected=_Seen(
                status=204,
                request=RequestInfo(
                    method="OPTIONS",
                    url="http://127.0.0.1:<port>/items",
                    path="/items",
                    host="127.0.0.1",
                ),
                wire=None,
            ),
        ),
    ]


def _bearer_auth_case() -> _Case:
    return _Case(
        "bearer-auth",
        verb="post",
        call={"json": {"a": 1}},
        expected=_Seen(
            status=200,
            request=RequestInfo(
                method="POST",
                url="http://127.0.0.1:<port>/echo-request",
                path="/echo-request",
                host="127.0.0.1",
            ),
            wire=_Wire(
                line="POST /echo-request HTTP/1.1",
                headers=[
                    ("Authorization", "Bearer token"),
                    ("Content-Type", "application/json"),
                    ("Accept", "*/*"),
                    ("User-Agent", "python-lothc/<version>"),
                    ("Accept-Encoding", "zstd,gzip,deflate,br"),
                    ("Host", "127.0.0.1:<port>"),
                    ("Content-Length", "7"),
                ],
                body=b'{"a":1}',
            ),
        ),
    )


def _no_base_url_case() -> _Case:
    return _Case(
        "no-base-url",
        path="{base_url}echo-request",
        call={"params": {"a": "b"}},
        expected=_Seen(
            status=200,
            request=RequestInfo(
                method="GET",
                url="http://127.0.0.1:<port>/echo-request?a=b",
                path="/echo-request",
                host="127.0.0.1",
            ),
            wire=_Wire(
                line="GET /echo-request?a=b HTTP/1.1",
                headers=[
                    ("Accept", "*/*"),
                    ("User-Agent", "python-lothc/<version>"),
                    ("Accept-Encoding", "zstd,gzip,deflate,br"),
                    ("Host", "127.0.0.1:<port>"),
                ],
                body=b"",
            ),
        ),
    )


def _tls_cases() -> list[_Case]:
    return [
        _Case(
            "plain-get",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="https://localhost:<port>/echo-request",
                    path="/echo-request",
                    host="localhost",
                ),
                wire=_Wire(
                    line="GET /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "localhost:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "params-and-headers",
            call={"params": {"a": "x y"}, "headers": {"X-Custom": "value"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="https://localhost:<port>/echo-request?a=x+y",
                    path="/echo-request",
                    host="localhost",
                ),
                wire=_Wire(
                    line="GET /echo-request?a=x+y HTTP/1.1",
                    headers=[
                        ("X-Custom", "value"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "localhost:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "json-dict",
            verb="post",
            call={"json": {"a": 1}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="https://localhost:<port>/echo-request",
                    path="/echo-request",
                    host="localhost",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "localhost:<port>"),
                        ("Content-Length", "7"),
                    ],
                    body=b'{"a":1}',
                ),
            ),
        ),
        _Case(
            "data-form",
            verb="post",
            call={"data": {"a": "x y"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="https://localhost:<port>/echo-request",
                    path="/echo-request",
                    host="localhost",
                ),
                wire=_Wire(
                    line="POST /echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/x-www-form-urlencoded"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "localhost:<port>"),
                        ("Content-Length", "5"),
                    ],
                    body=b"a=x+y",
                ),
            ),
        ),
        _Case(
            "content-bytes",
            verb="delete",
            call={"content": b"\x00\xffraw"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="DELETE",
                    url="https://localhost:<port>/echo-request",
                    path="/echo-request",
                    host="localhost",
                ),
                wire=_Wire(
                    line="DELETE /echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "localhost:<port>"),
                        ("Content-Length", "5"),
                    ],
                    body=b"\x00\xffraw",
                ),
            ),
        ),
        _Case(
            "head",
            verb="head",
            path="/items/7",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="HEAD",
                    url="https://localhost:<port>/items/7",
                    path="/items/7",
                    host="localhost",
                ),
                wire=None,
            ),
        ),
    ]


def _tunnelled_case() -> _Case:
    return _Case("tunnelled", call={"params": {"a": "b"}})


def _tunnel_cases() -> list[_TunnelCase]:
    return [
        _TunnelCase(
            "defaults",
            {},
            "",
            (
                _Wire(
                    line="CONNECT localhost:<port> HTTP/1.1",
                    headers=[
                        ("Host", "localhost:<port>"),
                        ("user-agent", "python-lothc/<version>"),
                    ],
                    body=b"",
                ),
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="https://localhost:<port>/echo-request?a=b",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request?a=b HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "python-lothc/<version>"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        _TunnelCase(
            "defaults-credentials",
            {},
            "us%20er:p%40ss@",
            (
                _Wire(
                    line="CONNECT localhost:<port> HTTP/1.1",
                    headers=[
                        ("Host", "localhost:<port>"),
                        ("user-agent", "python-lothc/<version>"),
                        ("proxy-authorization", "Basic dXMgZXI6cEBzcw=="),
                    ],
                    body=b"",
                ),
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="https://localhost:<port>/echo-request?a=b",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request?a=b HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "python-lothc/<version>"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        _TunnelCase(
            "user-agent",
            {"user_agent": "user-agent"},
            "",
            (
                _Wire(
                    line="CONNECT localhost:<port> HTTP/1.1",
                    headers=[("Host", "localhost:<port>"), ("user-agent", "user-agent")],
                    body=b"",
                ),
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="https://localhost:<port>/echo-request?a=b",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request?a=b HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "user-agent"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        _TunnelCase(
            "user-agent-credentials",
            {"user_agent": "user-agent"},
            "us%20er:p%40ss@",
            (
                _Wire(
                    line="CONNECT localhost:<port> HTTP/1.1",
                    headers=[
                        ("Host", "localhost:<port>"),
                        ("user-agent", "user-agent"),
                        ("proxy-authorization", "Basic dXMgZXI6cEBzcw=="),
                    ],
                    body=b"",
                ),
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="https://localhost:<port>/echo-request?a=b",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request?a=b HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "user-agent"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        # The request's user agent is the default header's; the `CONNECT`'s is `user_agent`'s.
        _TunnelCase(
            "default-user-agent",
            {"default_headers": {"User-Agent": "default"}},
            "",
            (
                _Wire(
                    line="CONNECT localhost:<port> HTTP/1.1",
                    headers=[
                        ("Host", "localhost:<port>"),
                        ("user-agent", "python-lothc/<version>"),
                    ],
                    body=b"",
                ),
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="https://localhost:<port>/echo-request?a=b",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request?a=b HTTP/1.1",
                        headers=[
                            ("User-Agent", "default"),
                            ("Accept", "*/*"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        _TunnelCase(
            "default-user-agent-credentials",
            {"default_headers": {"User-Agent": "default"}},
            "us%20er:p%40ss@",
            (
                _Wire(
                    line="CONNECT localhost:<port> HTTP/1.1",
                    headers=[
                        ("Host", "localhost:<port>"),
                        ("user-agent", "python-lothc/<version>"),
                        ("proxy-authorization", "Basic dXMgZXI6cEBzcw=="),
                    ],
                    body=b"",
                ),
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="https://localhost:<port>/echo-request?a=b",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request?a=b HTTP/1.1",
                        headers=[
                            ("User-Agent", "default"),
                            ("Accept", "*/*"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
    ]


def _socks_cases() -> list[_SocksCase]:
    return [
        _SocksCase(
            "socks5",
            None,
            "127.0.0.1",
            (
                [
                    SocksHandshake(
                        methods=b"\x00", credentials=None, address_type=1, host="127.0.0.1", port=0
                    )
                ],
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="http://127.0.0.1:<port>/echo-request",
                        path="/echo-request",
                        host="127.0.0.1",
                    ),
                    wire=_Wire(
                        line="GET /echo-request HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "python-lothc/<version>"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "127.0.0.1:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        _SocksCase(
            "socks5",
            ("user", "pass"),
            "127.0.0.1",
            (
                [
                    SocksHandshake(
                        methods=b"\x02",
                        credentials=("user", "pass"),
                        address_type=1,
                        host="127.0.0.1",
                        port=0,
                    )
                ],
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="http://127.0.0.1:<port>/echo-request",
                        path="/echo-request",
                        host="127.0.0.1",
                    ),
                    wire=_Wire(
                        line="GET /echo-request HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "python-lothc/<version>"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "127.0.0.1:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        _SocksCase(
            "socks5h",
            None,
            "localhost",
            (
                [
                    SocksHandshake(
                        methods=b"\x00", credentials=None, address_type=3, host="localhost", port=0
                    )
                ],
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="http://localhost:<port>/echo-request",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "python-lothc/<version>"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
        _SocksCase(
            "socks5h",
            ("user", "pass"),
            "localhost",
            (
                [
                    SocksHandshake(
                        methods=b"\x02",
                        credentials=("user", "pass"),
                        address_type=3,
                        host="localhost",
                        port=0,
                    )
                ],
                _Seen(
                    status=200,
                    request=RequestInfo(
                        method="GET",
                        url="http://localhost:<port>/echo-request",
                        path="/echo-request",
                        host="localhost",
                    ),
                    wire=_Wire(
                        line="GET /echo-request HTTP/1.1",
                        headers=[
                            ("Accept", "*/*"),
                            ("User-Agent", "python-lothc/<version>"),
                            ("Accept-Encoding", "zstd,gzip,deflate,br"),
                            ("Host", "localhost:<port>"),
                        ],
                        body=b"",
                    ),
                ),
            ),
        ),
    ]


def _large_body() -> bytes:
    return bytes(range(256)) * 16_384


def _large_body_case(server_url_fixture: str) -> _Case:
    """The body itself is compared on its own, and left out of `expected`."""
    expected = {
        "base_url": _Seen(
            status=200,
            request=RequestInfo(
                method="POST",
                url="http://127.0.0.1:<port>/echo-request",
                path="/echo-request",
                host="127.0.0.1",
            ),
            wire=_Wire(
                line="POST /echo-request HTTP/1.1",
                headers=[
                    ("Accept", "*/*"),
                    ("User-Agent", "python-lothc/<version>"),
                    ("Accept-Encoding", "zstd,gzip,deflate,br"),
                    ("Host", "127.0.0.1:<port>"),
                    ("Content-Length", "4194304"),
                ],
                body=b"",
            ),
        ),
        "https_base_url": _Seen(
            status=200,
            request=RequestInfo(
                method="POST",
                url="https://localhost:<port>/echo-request",
                path="/echo-request",
                host="localhost",
            ),
            wire=_Wire(
                line="POST /echo-request HTTP/1.1",
                headers=[
                    ("Accept", "*/*"),
                    ("User-Agent", "python-lothc/<version>"),
                    ("Accept-Encoding", "zstd,gzip,deflate,br"),
                    ("Host", "localhost:<port>"),
                    ("Content-Length", "4194304"),
                ],
                body=b"",
            ),
        ),
    }
    return _Case(
        "large-body",
        verb="post",
        call={"content": _large_body()},
        expected=expected[server_url_fixture],
    )


def _proxied_cases() -> list[_Case]:
    return [
        _Case(
            "plain-get",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "params-and-headers",
            call={"params": {"a": "x y"}, "headers": {"X-Custom": "value"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?a=x+y",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request?a=x+y HTTP/1.1",
                    headers=[
                        ("X-Custom", "value"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "host-header-override",
            call={"headers": {"Host": "host"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Host", "host"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "bearer-token",
            client={"bearer_token": "token"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "Bearer token"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "basic-auth",
            client={"basic_auth": ("user", "pa:ss")},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Authorization", "Basic dXNlcjpwYTpzcw=="),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "user-agent",
            client={"user_agent": "user-agent"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "user-agent"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "default-headers",
            client={"default_headers": {"X-Default": "default", "Accept": "accept"}},
            call={"headers": {"x-default": "override"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "accept"),
                        ("X-Default", "override"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "json-dict",
            verb="post",
            call={"json": {"a": 1}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "7"),
                    ],
                    body=b'{"a":1}',
                ),
            ),
        ),
        _Case(
            "data-form",
            verb="post",
            call={"data": {"a": "x y"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/x-www-form-urlencoded"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "5"),
                    ],
                    body=b"a=x+y",
                ),
            ),
        ),
        _Case(
            "content-bytes",
            verb="delete",
            call={"content": b"\x00\xffraw"},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="DELETE",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="DELETE http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "5"),
                    ],
                    body=b"\x00\xffraw",
                ),
            ),
        ),
        _Case(
            "post-without-a-body",
            verb="post",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "a-fragment-stays-off-the-wire",
            path="/echo-request?a=1#fragment",
            call={"params": {"b": 2}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request?a=1&b=2#fragment",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request?a=1&b=2 HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "head",
            verb="head",
            path="/items/7",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="HEAD",
                    url="http://127.0.0.1:<port>/items/7",
                    path="/items/7",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="HEAD http://127.0.0.1:<port>/items/7 HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "options",
            verb="options",
            path="/items",
            expected=_Seen(
                status=204,
                request=RequestInfo(
                    method="OPTIONS",
                    url="http://127.0.0.1:<port>/items",
                    path="/items",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="OPTIONS http://127.0.0.1:<port>/items HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "a-proxy-authorization-header",
            call={"headers": {"Proxy-Authorization": "proxy-authorization"}},
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Proxy-Authorization", "proxy-authorization"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        # The proxy sends these on to the test server's port, whatever they ask for.
        _Case(
            "the-default-port-left-out",
            path="http://host:80/echo-request",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET", url="http://host/echo-request", path="/echo-request", host="host"
                ),
                wire=_Wire(
                    line="GET http://host/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "host"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "no-port",
            path="http://host/echo-request",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET", url="http://host/echo-request", path="/echo-request", host="host"
                ),
                wire=_Wire(
                    line="GET http://host/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "host"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "another-port-kept",
            path="http://host:8080/echo-request",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://host:8080/echo-request",
                    path="/echo-request",
                    host="host",
                ),
                wire=_Wire(
                    line="GET http://host:8080/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "host:8080"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "credentials",
            proxy_userinfo="user:pass@",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Proxy-Authorization", "Basic dXNlcjpwYXNz"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "credentials-and-a-host-header",
            call={"headers": {"Host": "host"}},
            proxy_userinfo="user:pass@",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Host", "host"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Proxy-Authorization", "Basic dXNlcjpwYXNz"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "credentials-and-a-body",
            verb="post",
            call={"json": {"a": 1}},
            proxy_userinfo="user:pass@",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="POST",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="POST http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Content-Type", "application/json"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Proxy-Authorization", "Basic dXNlcjpwYXNz"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                        ("Content-Length", "7"),
                    ],
                    body=b'{"a":1}',
                ),
            ),
        ),
        _Case(
            "credentials-and-a-proxy-authorization-header",
            call={"headers": {"Proxy-Authorization": "proxy-authorization"}},
            proxy_userinfo="user:pass@",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET",
                    url="http://127.0.0.1:<port>/echo-request",
                    path="/echo-request",
                    host="127.0.0.1",
                ),
                wire=_Wire(
                    line="GET http://127.0.0.1:<port>/echo-request HTTP/1.1",
                    headers=[
                        ("Proxy-Authorization", "proxy-authorization"),
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "127.0.0.1:<port>"),
                    ],
                    body=b"",
                ),
            ),
        ),
        _Case(
            "credentials-and-the-default-port",
            path="http://host:80/echo-request",
            proxy_userinfo="us%20er:p%40ss@",
            expected=_Seen(
                status=200,
                request=RequestInfo(
                    method="GET", url="http://host/echo-request", path="/echo-request", host="host"
                ),
                wire=_Wire(
                    line="GET http://host/echo-request HTTP/1.1",
                    headers=[
                        ("Accept", "*/*"),
                        ("User-Agent", "python-lothc/<version>"),
                        ("Proxy-Authorization", "Basic dXMgZXI6cEBzcw=="),
                        ("Accept-Encoding", "zstd,gzip,deflate,br"),
                        ("Host", "host"),
                    ],
                    body=b"",
                ),
            ),
        ),
    ]


# --- Direct ---------------------------------------------------------------------------------


@pytest.mark.parametrize("case", _wire_cases(), ids=lambda case: case.id)
async def test_request_goes_out_as_pinned(base_url: str, case: _Case) -> None:
    client = HTTPClient(base_url=f"{base_url}{case.base_suffix}", **case.client)

    assert await _async_seen(client, case, base_url, [_port(base_url)]) == case.expected


@pytest.mark.parametrize("case", _wire_cases(), ids=lambda case: case.id)
def test_sync_request_goes_out_as_pinned(base_url: str, case: _Case) -> None:
    client = SyncHTTPClient(base_url=f"{base_url}{case.base_suffix}", **case.client)

    assert _sync_seen(client, case, base_url, [_port(base_url)]) == case.expected


async def _bearer() -> str:
    return "token"


def _sync_bearer() -> str:
    return "token"


async def test_a_bearer_auth_token_goes_out_as_pinned(base_url: str) -> None:
    case = _bearer_auth_case()
    client = HTTPClient(base_url=base_url, bearer_auth=_bearer)

    assert await _async_seen(client, case, base_url, [_port(base_url)]) == case.expected


def test_sync_a_bearer_auth_token_goes_out_as_pinned(base_url: str) -> None:
    case = _bearer_auth_case()
    client = SyncHTTPClient(base_url=base_url, bearer_auth=_sync_bearer)

    assert _sync_seen(client, case, base_url, [_port(base_url)]) == case.expected


async def test_without_a_base_url_an_absolute_url_goes_out_as_pinned(base_url: str) -> None:
    case = _no_base_url_case()

    assert await _async_seen(HTTPClient(), case, base_url, [_port(base_url)]) == case.expected


def test_sync_without_a_base_url_an_absolute_url_goes_out_as_pinned(base_url: str) -> None:
    case = _no_base_url_case()

    assert _sync_seen(SyncHTTPClient(), case, base_url, [_port(base_url)]) == case.expected


# --- TLS --------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", _tls_cases(), ids=lambda case: case.id)
async def test_request_over_tls_goes_out_as_pinned(
    https_base_url: str, tls_ca_pem: bytes, case: _Case
) -> None:
    client = HTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem])
    seen = await _async_seen(client, case, https_base_url, [_port(https_base_url)])

    assert seen == case.expected


@pytest.mark.parametrize("case", _tls_cases(), ids=lambda case: case.id)
def test_sync_request_over_tls_goes_out_as_pinned(
    https_base_url: str, tls_ca_pem: bytes, case: _Case
) -> None:
    client = SyncHTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem])
    seen = _sync_seen(client, case, https_base_url, [_port(https_base_url)])

    assert seen == case.expected


# A body far bigger than a socket's send buffer, so sending it has to wait for the socket to
# drain, in plain HTTP and over TLS.
@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
async def test_a_large_body_goes_out_whole(
    server_url_fixture: str, tls_ca_pem: bytes, request: pytest.FixtureRequest
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    case = _large_body_case(server_url_fixture)
    client = HTTPClient(base_url=server_url, root_certificates=[tls_ca_pem])

    seen = await _async_seen(client, case, server_url, [_port(server_url)])

    assert seen.wire is not None
    assert seen.wire.body == _large_body()
    seen.wire.body = b""
    assert seen == case.expected


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
def test_sync_a_large_body_goes_out_whole(
    server_url_fixture: str, tls_ca_pem: bytes, request: pytest.FixtureRequest
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    case = _large_body_case(server_url_fixture)
    client = SyncHTTPClient(base_url=server_url, root_certificates=[tls_ca_pem])

    seen = _sync_seen(client, case, server_url, [_port(server_url)])

    assert seen.wire is not None
    assert seen.wire.body == _large_body()
    seen.wire.body = b""
    assert seen == case.expected


# --- Through an HTTP proxy --------------------------------------------------------------------


@pytest.mark.parametrize("case", _proxied_cases(), ids=lambda case: case.id)
async def test_request_to_an_http_proxy_goes_out_as_pinned(base_url: str, case: _Case) -> None:
    with serve_http_proxy(upstream_port=_port(base_url)) as proxy:
        ports = [_port(base_url), _port(proxy.url)]
        proxy_url = proxy.url.replace("://", f"://{case.proxy_userinfo}")
        client = HTTPClient(base_url=base_url, proxy=proxy_url, **case.client)
        seen = _with_proxy_wire(await _async_seen(client, case, base_url, ports), proxy, ports)

    assert seen == case.expected


@pytest.mark.parametrize("case", _proxied_cases(), ids=lambda case: case.id)
def test_sync_request_to_an_http_proxy_goes_out_as_pinned(base_url: str, case: _Case) -> None:
    with serve_http_proxy(upstream_port=_port(base_url)) as proxy:
        ports = [_port(base_url), _port(proxy.url)]
        proxy_url = proxy.url.replace("://", f"://{case.proxy_userinfo}")
        client = SyncHTTPClient(base_url=base_url, proxy=proxy_url, **case.client)
        seen = _with_proxy_wire(_sync_seen(client, case, base_url, ports), proxy, ports)

    assert seen == case.expected


# --- Through a CONNECT tunnel -----------------------------------------------------------------


def _tunnel_case_id(case: _TunnelCase) -> str:
    return case.id


@pytest.mark.parametrize("case", _tunnel_cases(), ids=_tunnel_case_id)
async def test_a_tunnel_goes_out_as_pinned(
    https_base_url: str, tls_ca_pem: bytes, case: _TunnelCase
) -> None:
    with serve_http_proxy() as proxy:
        ports = [_port(https_base_url), _port(proxy.url)]
        client = HTTPClient(
            base_url=https_base_url,
            root_certificates=[tls_ca_pem],
            proxy=proxy.url.replace("://", f"://{case.userinfo}"),
            **case.client,
        )
        seen = await _async_seen(client, _tunnelled_case(), https_base_url, ports)
        (connect,) = _taken(proxy, ports)

    assert (connect, seen) == case.expected


@pytest.mark.parametrize("case", _tunnel_cases(), ids=_tunnel_case_id)
def test_sync_a_tunnel_goes_out_as_pinned(
    https_base_url: str, tls_ca_pem: bytes, case: _TunnelCase
) -> None:
    with serve_http_proxy() as proxy:
        ports = [_port(https_base_url), _port(proxy.url)]
        client = SyncHTTPClient(
            base_url=https_base_url,
            root_certificates=[tls_ca_pem],
            proxy=proxy.url.replace("://", f"://{case.userinfo}"),
            **case.client,
        )
        seen = _sync_seen(client, _tunnelled_case(), https_base_url, ports)
        (connect,) = _taken(proxy, ports)

    assert (connect, seen) == case.expected


# --- Through SOCKS5 ---------------------------------------------------------------------------


def _socks_case_id(case: _SocksCase) -> str:
    return case.scheme + ("" if case.credentials is None else "-credentials")


def _socks_handshakes(handshakes: list[SocksHandshake], port: int) -> list[SocksHandshake]:
    return [
        SocksHandshake(
            h.methods, h.credentials, h.address_type, h.host, 0 if h.port == port else h.port
        )
        for h in handshakes
    ]


def _socks_proxy_url(case: _SocksCase, address: str) -> str:
    userinfo = "" if case.credentials is None else ":".join(case.credentials) + "@"
    return f"{case.scheme}://{userinfo}{address}"


@pytest.mark.parametrize("case", _socks_cases(), ids=_socks_case_id)
async def test_socks5_goes_out_as_pinned(base_url: str, case: _SocksCase) -> None:
    origin = base_url.replace("127.0.0.1", case.host)
    with serve_socks5_proxy(credentials=case.credentials) as proxy:
        client = HTTPClient(base_url=origin, proxy=_socks_proxy_url(case, proxy.address))
        seen = await _async_seen(client, _Case("socks"), origin, [_port(base_url)])
        handshakes = _socks_handshakes(proxy.handshakes, _port(base_url))

    assert (handshakes, seen) == case.expected


@pytest.mark.parametrize("case", _socks_cases(), ids=_socks_case_id)
def test_sync_socks5_goes_out_as_pinned(base_url: str, case: _SocksCase) -> None:
    origin = base_url.replace("127.0.0.1", case.host)
    with serve_socks5_proxy(credentials=case.credentials) as proxy:
        client = SyncHTTPClient(base_url=origin, proxy=_socks_proxy_url(case, proxy.address))
        seen = _sync_seen(client, _Case("socks"), origin, [_port(base_url)])
        handshakes = _socks_handshakes(proxy.handshakes, _port(base_url))

    assert (handshakes, seen) == case.expected
