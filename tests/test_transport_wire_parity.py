"""The native transport (`native=True`) puts the same request on the wire as the pyreqwest one:
every call below goes out through both, async and sync, and the test server's `/echo-request`
reports what arrived (request line, every header in order with its casing, body), alongside each
response's `RequestInfo`.

Through a proxy, what's compared is what the proxy received (`_proxy_server.py` records it): the
absolute-form request to an HTTP proxy, the `CONNECT` before a tunnel, the SOCKS5 handshake. That
is also how every hop of a followed redirect and every attempt of a retried request is compared:
the proxy sees them all.

A multipart `form=` body's boundary is random, so its cases mask each one (`_boundaries_masked`)
and compare everything else byte for byte.
"""

import io
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from _helpers import clear_proxy_variables, partial_body_seen
from _proxy_server import HTTPProxy, SocksProxy, serve_http_proxy, serve_socks5_proxy
from msgspec import Struct
from pydantic import BaseModel, Field

from lothc import HTTPClient, HTTPConnectionError, RequestInfo, SyncHTTPClient


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
class _Case:
    id: str
    verb: str = "get"
    # `{base_url}`, `{origin}` (`//host:port/`) and `{other}` (`base_url` as `localhost`, another
    # origin on the same server) are filled in from the test server's URL.
    path: str = "/echo-request"
    call: dict[str, Any] = field(default_factory=dict[str, Any])
    client: dict[str, Any] = field(default_factory=dict[str, Any])
    # Appended to the server's URL to make the client's `base_url`.
    base_suffix: str = ""


@dataclass(slots=True)
class _Seen:
    status: int
    request: RequestInfo
    # What `/echo-request` reports; the raw body for any other route.
    wire: Any


def _wire_cases() -> list[_Case]:
    return [
        _Case("plain-get"),
        _Case(
            "params-and-headers",
            call={
                "params": {"a": "x y", "b": [1, 2], "c": True, "d": 1.5, "e": "ü&=*~"},
                "headers": {"X-Custom": "value"},
            },
        ),
        _Case(
            "params-onto-an-existing-query",
            path="/echo-request?q=1",
            call={"params": {"z": 2}, "headers": {"User-Agent": "user-agent"}},
        ),
        _Case(
            "params-repeated-and-out-of-range",
            call={"params": {"t": ("a", "b"), "n": -3, "u": 2**63, "big": 2**70, "neg": -(2**70)}},
        ),
        _Case(
            "params-floats-and-specials",
            call={"params": {"f": 1.0, "inf": float("inf"), "nan": float("nan"), "small": 1e-7}},
        ),
        _Case(
            "struct-params-and-headers",
            call={
                "params": _QueryStruct(page=1, tags=["x", "y"]),
                "headers": _HeadersStruct(x_request_id="request-id"),
            },
        ),
        _Case(
            "model-params-and-headers",
            call={
                "params": _QueryModel(page=2, tag="tag"),
                "headers": _HeadersModel(x_request_id="request-id"),
            },
        ),
        _Case("empty-params-drop-the-question-mark", path="/echo-request?", call={"params": {}}),
        _Case("a-bare-question-mark-is-kept", path="/echo-request?"),
        _Case(
            "a-fragment-stays-off-the-wire",
            path="/echo-request?a=1#fragment",
            call={"params": {"b": 2}},
        ),
        _Case("headers-differing-only-in-case", call={"headers": {"X-Same": "1", "x-same": "2"}}),
        _Case("host-header-override", call={"headers": {"Host": "host"}}),
        _Case("accept-encoding-override", call={"headers": {"Accept-Encoding": "identity"}}),
        _Case(
            "default-accept-encoding",
            client={"default_headers": {"Accept-Encoding": "gzip", "X-Default": "0"}},
        ),
        _Case("bearer-token", client={"bearer_token": "token"}),
        _Case("skip-auth", client={"bearer_token": "token"}, call={"skip_auth": True}),
        _Case("basic-auth", client={"basic_auth": ("user", "pa:ss")}),
        _Case("basic-auth-without-password", client={"basic_auth": ("user", None)}),
        _Case(
            "authorization-header-overrides-auth",
            client={"bearer_token": "token"},
            call={"headers": {"X-First": "1", "Authorization": "authorization"}},
        ),
        _Case(
            "userinfo-in-the-url",
            path="{base_url}echo-request".replace("http://", "http://user:pass@"),
        ),
        _Case("user-agent", client={"user_agent": "user-agent"}),
        _Case(
            "default-headers",
            client={"default_headers": {"X-Default": "default", "Accept": "accept"}},
            call={"headers": {"x-default": "override"}},
        ),
        _Case(
            "default-headers-user-agent-wins-over-user-agent",
            client={"default_headers": {"User-Agent": "default"}, "user_agent": "user-agent"},
        ),
        _Case(
            "default-authorization-and-bearer-token",
            client={
                "default_headers": {"Authorization": "default", "X-Default": "0"},
                "bearer_token": "token",
            },
        ),
        _Case("json-dict", verb="post", call={"json": {"k": [1, 2.5, "s", None, True], "u": "ü"}}),
        _Case(
            "json-dict-keys-and-numbers",
            verb="post",
            call={
                "json": {"b": {"d": 1, "c": 2}, "a": [1e20, 0.1, -0.0, 1e-7, 1e16, 1e15, 5e-324]}
            },
        ),
        _Case("json-list", verb="put", call={"json": [1e20, float("nan"), chr(0x2028), "\x00"]}),
        _Case("json-struct", verb="post", call={"json": _ItemStruct(item_id=1)}),
        _Case("json-model-by-alias", verb="patch", call={"json": _AliasedModel(itemId=1)}),
        _Case(
            "json-dict-replaces-a-content-type-header",
            verb="post",
            call={
                "json": {"a": 1},
                "headers": {"X-1": "1", "Content-Type": "text/plain", "X-2": "2"},
            },
        ),
        _Case(
            "json-struct-replaces-a-content-type-header",
            verb="post",
            client={"default_headers": {"Content-Type": "default", "X-Default": "0"}},
            call={"json": _ItemStruct(item_id=1), "headers": {"X-1": "1"}},
        ),
        _Case(
            "data-form",
            verb="post",
            client={"bearer_token": "token"},
            call={
                "data": {"a": "x y", "b": [1, 2], "c": "ü&=*~", "d": True},
                "headers": {"X-1": "1"},
            },
        ),
        _Case("data-model", verb="post", call={"data": _QueryModel(page=1)}),
        _Case(
            "data-keeps-a-content-type-header",
            verb="post",
            call={"data": {"a": "b"}, "headers": {"content-type": "text/plain"}},
        ),
        _Case("data-empty", verb="post", call={"data": {}}),
        _Case("content-text", verb="post", call={"content": "héllo"}),
        _Case("content-bytes", verb="delete", call={"content": b"\x00\xffraw"}),
        _Case("content-empty", verb="put", call={"content": b""}),
        _Case("post-without-a-body", verb="post"),
        _Case("delete", verb="delete"),
        _Case("base-path-prefix-kept", path="items/7", base_suffix="prefix/"),
        _Case("leading-slash-drops-the-base-path", base_suffix="prefix/"),
        _Case(
            "absolute-url-overrides-the-base", path="{base_url}echo-request", base_suffix="prefix/"
        ),
        _Case("protocol-relative-url", path="{origin}echo-request", base_suffix="prefix/"),
        _Case("relative-path-without-leading-slash", path="echo-request"),
        _Case("percent-encoding-and-dot-segments", path="/échø/../echo%20x/./y"),
        _Case("head", verb="head", path="/items/7"),
        _Case("options", verb="options", path="/items"),
        _Case("download", verb="download", call={"params": {"a": 1}, "headers": {"X-1": "1"}}),
        _Case(
            "download-auth-and-defaults",
            verb="download",
            client={"basic_auth": ("user", "pass"), "default_headers": {"X-Default": "0"}},
        ),
    ]


def _streamed_cases() -> list[_Case]:
    """`stream_get`/`stream_post`, whose `RequestInfo` isn't returned: only the wire is compared."""
    return [
        _Case(
            "stream-get", verb="stream_get", call={"params": {"a": "x y"}, "headers": {"X": "1"}}
        ),
        _Case(
            "stream-get-auth-and-defaults",
            verb="stream_get",
            client={"bearer_token": "token", "default_headers": {"Accept": "a", "X-Default": "0"}},
        ),
        _Case("stream-post-json", verb="stream_post", call={"json": {"b": 1, "a": [1.5, None]}}),
        _Case("stream-post-data", verb="stream_post", call={"data": {"a": "x y"}}),
        _Case("stream-post-content", verb="stream_post", call={"content": b"\x00raw"}),
        _Case(
            "stream-get-redirected",
            verb="stream_get",
            path="/redirect-to?to=/echo-request",
            client={"bearer_token": "token"},
            call={"headers": {"X": "1"}},
        ),
        _Case(
            "stream-post-redirected-by-308",
            verb="stream_post",
            path="/redirect-to?status=308&to=/echo-request",
            call={"json": {"a": 1}},
        ),
        _Case(
            "stream-post-redirected-by-302",
            verb="stream_post",
            path="/redirect-to?status=302&to={other}echo-request",
            call={"json": {"a": 1}, "headers": {"Cookie": "c"}},
        ),
    ]


def _redirect_cases() -> list[_Case]:
    """A followed redirect: what the last hop's `/echo-request` saw, the hops before it having
    shaped it (method and body kept or dropped, credentials dropped across origins, `Referer`)."""
    return [
        _Case(
            "redirect-same-origin",
            path="/redirect-to?to=/echo-request",
            client={"bearer_token": "token"},
            call={"headers": {"Cookie": "c", "X-1": "1", "Referer": "referer"}},
        ),
        _Case(
            "redirect-to-another-origin",
            path="/redirect-to?to={other}echo-request",
            client={"bearer_token": "token"},
            call={
                "headers": {
                    "Cookie": "c",
                    "X-1": "1",
                    "Proxy-Authorization": "p",
                    "WWW-Authenticate": "w",
                    "Cookie2": "c2",
                }
            },
        ),
        _Case(
            "redirect-on-within-another-origin",
            path="/redirect-to?to={other}redirect-to%3Fto%3D/echo-request",
            client={"basic_auth": ("user", "pass")},
        ),
        _Case(
            "redirect-there-and-back",
            path="/redirect-to?to={other}redirect-to%3Fto%3D{base_url}echo-request",
            client={"bearer_token": "token"},
        ),
        _Case(
            "redirect-twice",
            path="/redirect-to?to=/redirect-to%3Fto%3D/echo-request",
            call={"params": {"a": 1}},
        ),
        _Case("redirect-relative-path", path="/redirect-to?to=echo-request%3Fq%3D1"),
        _Case("redirect-protocol-relative", path="/redirect-to?to={origin}echo-request"),
        _Case("redirect-with-a-fragment", path="/redirect-to?to=/echo-request%23fragment"),
        _Case(
            "redirect-from-userinfo-in-the-url",
            path="{base_url}redirect-to?to=/echo-request".replace("http://", "http://u:p@"),
        ),
        _Case(
            "redirect-keeps-defaults-and-overrides",
            path="/redirect-to?to=/echo-request",
            client={"default_headers": {"X-Default": "0", "Accept": "a"}, "user_agent": "ua"},
            call={"headers": {"Accept-Encoding": "identity", "Host": "host"}},
        ),
        _Case(
            "redirect-post-json-302",
            verb="post",
            path="/redirect-to?status=302&to=/echo-request",
            call={"json": {"a": 1}, "headers": {"Content-Encoding": "identity", "X-1": "1"}},
        ),
        _Case(
            "redirect-post-data-301",
            verb="post",
            path="/redirect-to?status=301&to=/echo-request",
            call={"data": {"a": "b"}},
        ),
        _Case(
            "redirect-post-json-303",
            verb="post",
            path="/redirect-to?status=303&to=/echo-request",
            call={"json": _ItemStruct(item_id=1)},
        ),
        _Case(
            "redirect-put-303",
            verb="put",
            path="/redirect-to?status=303&to=/echo-request",
            call={"content": b"body", "headers": {"Content-Type": "text/plain"}},
        ),
        _Case(
            "redirect-put-302",
            verb="put",
            path="/redirect-to?status=302&to=/echo-request",
            call={"content": "body"},
        ),
        _Case(
            "redirect-patch-307",
            verb="patch",
            path="/redirect-to?status=307&to=/echo-request",
            call={"json": [1, 2]},
        ),
        _Case(
            "redirect-post-data-308",
            verb="post",
            path="/redirect-to?status=308&to=/echo-request",
            call={"data": {"a": "b"}},
        ),
        _Case(
            "redirect-delete-301",
            verb="delete",
            path="/redirect-to?status=301&to=/echo-request",
            call={"content": b"x"},
        ),
        _Case(
            "redirect-post-empty-307", verb="post", path="/redirect-to?status=307&to=/echo-request"
        ),
        _Case("redirect-download", verb="download", path="/redirect-to?to=/echo-request"),
    ]


def _proxied_redirect_cases() -> list[_Case]:
    """Redirects through an HTTP proxy, which sees every hop (another host needs no DNS there,
    since the proxy sends everything to the test server's port)."""
    return [
        _Case("redirect-same-origin", path="/redirect-to?to=/echo-request"),
        _Case(
            "redirect-to-another-host",
            path="/redirect-to?to=http://other.test/echo-request",
            client={"bearer_token": "token"},
            call={"headers": {"Cookie": "c", "X-1": "1"}},
        ),
        _Case(
            "redirect-post-json-302",
            verb="post",
            path="/redirect-to?status=302&to=/echo-request",
            call={"json": {"a": 1}},
        ),
        _Case(
            "redirect-head-303",
            verb="head",
            path="/redirect-to?status=303&to=/items/7",
            call={"headers": {"Content-Type": "text/plain"}},
        ),
    ]


def _retried_cases() -> list[_Case]:
    """Retried requests through an HTTP proxy, which sees every attempt: `{key}` is each run's own
    `/status-then-ok` key, its first attempt failing."""
    retries: dict[str, Any] = {"max_retries": 1, "backoff_base": 0.001}
    return [
        _Case("retried-get", path="/status-then-ok?key={key}&fail_times=1", client=retries),
        _Case(
            "retried-put-with-auth",
            verb="put",
            path="/status-then-ok?key={key}&fail_times=1",
            client={**retries, "bearer_token": "token"},
            call={"content": b"body", "headers": {"X-1": "1"}},
        ),
        _Case(
            "retried-post-json",
            verb="post",
            path="/status-then-ok?key={key}&fail_times=1",
            client={**retries, "retry_methods": {"POST"}},
            call={"json": {"a": 1}},
        ),
        _Case(
            "retried-head",
            verb="head",
            path="/status-then-ok?key={key}&fail_times=1",
            client=retries,
        ),
        _Case(
            "retried-after-a-redirect",
            path="/redirect-to?to=/status-then-ok%3Fkey%3D{key}%26fail_times%3D1",
            client=retries,
        ),
        _Case(
            "retried-by-a-retry-after",
            path="/status-then-ok?key={key}&fail_times=1&status=429",
            client=retries,
        ),
        _Case(
            "retried-stream-get",
            verb="stream_get",
            path="/status-then-ok?key={key}&fail_times=1",
            client=retries,
        ),
    ]


def _seen(response: Any) -> _Seen:  # noqa: ANN401 — whichever `Response` the verb returned
    wire: Any = response.data
    if response.request.path == "/echo-request" and response.status == 200:
        wire = json.loads(response.data)
    return _Seen(response.status, response.request, wire)


def _path(case: _Case, base_url: str) -> str:
    return case.path.format(
        base_url=base_url,
        origin=base_url.removeprefix("http:"),
        other=base_url.replace("127.0.0.1", "localhost"),
    )


def _pair[TClient](
    factory: Callable[..., TClient], monkeypatch: pytest.MonkeyPatch
) -> tuple[TClient, TClient]:
    """The same client twice: built with `native=False`, then with `native=True`.

    `LOTHC_TRANSPORT` is unset first because it wins over `native=`: left set, as under
    `task test-native`, it would put both clients on the native transport.
    """
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)
    return factory(native=False), factory(native=True)


async def _async_seen(client: HTTPClient, case: _Case, base_url: str) -> _Seen:
    async with client:
        send: Callable[..., Awaitable[Any]] = getattr(client, case.verb)
        return _seen(await send(_path(case, base_url), error_for_status=False, **case.call))


def _sync_seen(client: SyncHTTPClient, case: _Case, base_url: str) -> _Seen:
    with client:
        send: Callable[..., Any] = getattr(client, case.verb)
        return _seen(send(_path(case, base_url), error_for_status=False, **case.call))


@pytest.mark.parametrize("case", _wire_cases(), ids=lambda case: case.id)
async def test_native_transport_sends_what_pyreqwest_sends(
    base_url: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(HTTPClient, base_url=f"{base_url}{case.base_suffix}", **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = await _async_seen(pyreqwest_client, case, base_url)
    actual = await _async_seen(native_client, case, base_url)

    assert actual == expected


@pytest.mark.parametrize("case", _wire_cases(), ids=lambda case: case.id)
def test_sync_native_transport_sends_what_pyreqwest_sends(
    base_url: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(SyncHTTPClient, base_url=f"{base_url}{case.base_suffix}", **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = _sync_seen(pyreqwest_client, case, base_url)
    actual = _sync_seen(native_client, case, base_url)

    assert actual == expected


async def _async_streamed(client: HTTPClient, case: _Case, base_url: str) -> Any:  # noqa: ANN401 — the echoed JSON
    async with client:
        stream: Callable[..., AsyncIterator[bytes]] = getattr(client, case.verb)
        chunks = stream(_path(case, base_url), error_for_status=False, **case.call)
        return json.loads(b"".join([chunk async for chunk in chunks]))


def _sync_streamed(client: SyncHTTPClient, case: _Case, base_url: str) -> Any:  # noqa: ANN401 — the echoed JSON
    with client:
        stream: Callable[..., Iterator[bytes]] = getattr(client, case.verb)
        return json.loads(
            b"".join(stream(_path(case, base_url), error_for_status=False, **case.call))
        )


@pytest.mark.parametrize("case", _streamed_cases(), ids=lambda case: case.id)
async def test_native_transport_streams_what_pyreqwest_streams(
    base_url: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(HTTPClient, base_url=base_url, **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = await _async_streamed(pyreqwest_client, case, base_url)
    actual = await _async_streamed(native_client, case, base_url)

    assert actual == expected


@pytest.mark.parametrize("case", _streamed_cases(), ids=lambda case: case.id)
def test_sync_native_transport_streams_what_pyreqwest_streams(
    base_url: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(SyncHTTPClient, base_url=base_url, **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = _sync_streamed(pyreqwest_client, case, base_url)
    actual = _sync_streamed(native_client, case, base_url)

    assert actual == expected


def _hop_seen(response: Any) -> _Seen:  # noqa: ANN401 — whichever `Response` the verb returned
    """`_seen` for a redirected request, whose `RequestInfo` is the first URL's."""
    return _Seen(response.status, response.request, json.loads(response.data))


async def _async_hop_seen(client: HTTPClient, case: _Case, base_url: str) -> _Seen:
    async with client:
        send: Callable[..., Awaitable[Any]] = getattr(client, case.verb)
        return _hop_seen(await send(_path(case, base_url), **case.call))


def _sync_hop_seen(client: SyncHTTPClient, case: _Case, base_url: str) -> _Seen:
    with client:
        send: Callable[..., Any] = getattr(client, case.verb)
        return _hop_seen(send(_path(case, base_url), **case.call))


@pytest.mark.parametrize("case", _redirect_cases(), ids=lambda case: case.id)
async def test_native_transport_follows_a_redirect_as_pyreqwest_does(
    base_url: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(HTTPClient, base_url=base_url, **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = await _async_hop_seen(pyreqwest_client, case, base_url)
    actual = await _async_hop_seen(native_client, case, base_url)

    assert actual == expected


@pytest.mark.parametrize("case", _redirect_cases(), ids=lambda case: case.id)
def test_sync_native_transport_follows_a_redirect_as_pyreqwest_does(
    base_url: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(SyncHTTPClient, base_url=base_url, **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = _sync_hop_seen(pyreqwest_client, case, base_url)
    actual = _sync_hop_seen(native_client, case, base_url)

    assert actual == expected


def _without_key(seen: list[tuple[str, Any]], key: str) -> list[tuple[str, Any]]:
    """`/echo-request-sse`'s events with this run's own `key=` taken out of the request line."""
    return [
        (event_id, {**data, "line": data["line"].replace(key, "key")}) for event_id, data in seen
    ]


async def _async_sse_seen(client: HTTPClient, key: str) -> list[tuple[str, Any]]:
    async with client:
        events = client.sse(
            "echo-request-sse",
            params={"key": key},
            headers={"X-1": "1"},
            reconnect_delay=0,
            into=dict,
        )
        return _without_key([(event.id, event.data) async for event in events], key)


def _sync_sse_seen(client: SyncHTTPClient, key: str) -> list[tuple[str, Any]]:
    with client:
        events = client.sse(
            "echo-request-sse",
            params={"key": key},
            headers={"X-1": "1"},
            reconnect_delay=0,
            into=dict,
        )
        return _without_key([(event.id, event.data) for event in events], key)


# `sse()`'s own headers around the caller's: `accept` first (displacing a default one), then
# auth and `headers=`, then `last-event-id` on the reconnect `/echo-request-sse` forces.
async def test_native_transport_sends_what_pyreqwest_sends_for_sse(
    base_url: str, server_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    defaults = {"Accept": "accept", "X-Default": "0", "Last-Event-ID": "default"}
    factory = partial(HTTPClient, base_url=base_url, bearer_token="token", default_headers=defaults)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = await _async_sse_seen(pyreqwest_client, f"{server_key}-pyreqwest")
    actual = await _async_sse_seen(native_client, f"{server_key}-native")

    assert [event_id for event_id, _ in expected] == ["1", "2"]
    assert actual == expected


def test_sync_native_transport_sends_what_pyreqwest_sends_for_sse(
    base_url: str, server_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    defaults = {"Accept": "accept", "X-Default": "0", "Last-Event-ID": "default"}
    factory = partial(
        SyncHTTPClient, base_url=base_url, bearer_token="token", default_headers=defaults
    )
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = _sync_sse_seen(pyreqwest_client, f"{server_key}-pyreqwest")
    actual = _sync_sse_seen(native_client, f"{server_key}-native")

    assert [event_id for event_id, _ in expected] == ["1", "2"]
    assert actual == expected


async def _bearer() -> str:
    return "token"


def _sync_bearer() -> str:
    return "token"


async def test_native_transport_sends_a_bearer_auth_token_as_pyreqwest_does(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _Case("bearer-auth", verb="post", call={"json": {"a": 1}})
    pyreqwest_client, native_client = _pair(
        partial(HTTPClient, base_url=base_url, bearer_auth=_bearer), monkeypatch
    )

    expected = await _async_seen(pyreqwest_client, case, base_url)
    actual = await _async_seen(native_client, case, base_url)

    assert actual == expected


def test_sync_native_transport_sends_a_bearer_auth_token_as_pyreqwest_does(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _Case("bearer-auth", verb="post", call={"json": {"a": 1}})
    pyreqwest_client, native_client = _pair(
        partial(SyncHTTPClient, base_url=base_url, bearer_auth=_sync_bearer), monkeypatch
    )

    expected = _sync_seen(pyreqwest_client, case, base_url)
    actual = _sync_seen(native_client, case, base_url)

    assert actual == expected


async def test_native_transport_without_a_base_url_takes_absolute_urls(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = _Case("no-base-url", path="{base_url}echo-request", call={"params": {"a": "b"}})
    pyreqwest_client, native_client = _pair(HTTPClient, monkeypatch)

    expected = await _async_seen(pyreqwest_client, case, base_url)
    actual = await _async_seen(native_client, case, base_url)

    assert actual == expected


def _tls_cases() -> list[_Case]:
    names = {"plain-get", "params-and-headers", "json-dict", "data-form", "content-bytes", "head"}
    return [case for case in _wire_cases() if case.id in names]


@pytest.mark.parametrize("case", _tls_cases(), ids=lambda case: case.id)
async def test_native_transport_sends_what_pyreqwest_sends_over_tls(
    https_base_url: str, tls_ca_pem: bytes, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(HTTPClient, base_url=https_base_url, root_certificates=[tls_ca_pem])
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = await _async_seen(pyreqwest_client, case, https_base_url)
    actual = await _async_seen(native_client, case, https_base_url)

    assert actual == expected


@pytest.mark.parametrize("case", _tls_cases(), ids=lambda case: case.id)
def test_sync_native_transport_sends_what_pyreqwest_sends_over_tls(
    https_base_url: str, tls_ca_pem: bytes, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(SyncHTTPClient, base_url=https_base_url, root_certificates=[tls_ca_pem])
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = _sync_seen(pyreqwest_client, case, https_base_url)
    actual = _sync_seen(native_client, case, https_base_url)

    assert actual == expected


# A body far bigger than a socket's send buffer, so sending it has to wait for the socket to
# drain (the native transport's `loop.add_writer`/`POLLOUT` path), in plain HTTP and over TLS.
@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
async def test_native_transport_sends_a_large_body_as_pyreqwest_does(
    server_url_fixture: str,
    tls_ca_pem: bytes,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    case = _Case("large-body", verb="post", call={"content": bytes(range(256)) * 16_384})
    factory = partial(HTTPClient, base_url=server_url, root_certificates=[tls_ca_pem])
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = await _async_seen(pyreqwest_client, case, server_url)
    actual = await _async_seen(native_client, case, server_url)

    assert actual == expected


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
def test_sync_native_transport_sends_a_large_body_as_pyreqwest_does(
    server_url_fixture: str,
    tls_ca_pem: bytes,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    case = _Case("large-body", verb="post", call={"content": bytes(range(256)) * 16_384})
    factory = partial(SyncHTTPClient, base_url=server_url, root_certificates=[tls_ca_pem])
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    expected = _sync_seen(pyreqwest_client, case, server_url)
    actual = _sync_seen(native_client, case, server_url)

    assert actual == expected


def _proxied_cases() -> list[_Case]:
    names = {
        "plain-get",
        "params-and-headers",
        "host-header-override",
        "bearer-token",
        "basic-auth",
        "user-agent",
        "default-headers",
        "json-dict",
        "data-form",
        "content-bytes",
        "post-without-a-body",
        "a-fragment-stays-off-the-wire",
        "head",
        "options",
    }
    return [case for case in _wire_cases() if case.id in names] + [
        _Case(
            "a-proxy-authorization-header",
            call={"headers": {"Proxy-Authorization": "proxy-authorization"}},
        ),
        # The proxy sends these on to the test server's port, whatever they ask for.
        _Case("the-default-port-left-out", path="http://host:80/echo-request"),
        _Case("no-port", path="http://host/echo-request"),
        _Case("another-port-kept", path="http://host:8080/echo-request"),
    ]


def _taken(proxy: HTTPProxy) -> list[tuple[str, list[tuple[str, str]], bytes]]:
    """What `proxy` received so far, minus `Accept-Encoding`, forgotten once read."""
    received = [
        (
            request.line,
            [header for header in request.headers if header[0] != "Accept-Encoding"],
            request.body,
        )
        for request in proxy.requests
    ]
    proxy.requests.clear()
    return received


def _taken_handshakes(proxy: SocksProxy) -> list[object]:
    received: list[object] = list(proxy.handshakes)
    proxy.handshakes.clear()
    return received


@pytest.mark.parametrize("userinfo", ["", "user:pass@"], ids=["no-credentials", "credentials"])
@pytest.mark.parametrize("case", _proxied_cases(), ids=lambda case: case.id)
async def test_native_transport_sends_what_pyreqwest_sends_to_an_http_proxy(
    base_url: str, case: _Case, userinfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy:
        proxy_url = proxy.url.replace("://", f"://{userinfo}")
        factory = partial(HTTPClient, base_url=base_url, proxy=proxy_url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = await _async_seen(pyreqwest_client, case, base_url), _taken(proxy)
        actual = await _async_seen(native_client, case, base_url), _taken(proxy)

    assert actual == expected


@pytest.mark.parametrize("userinfo", ["", "user:pass@"], ids=["no-credentials", "credentials"])
@pytest.mark.parametrize("case", _proxied_cases(), ids=lambda case: case.id)
def test_sync_native_transport_sends_what_pyreqwest_sends_to_an_http_proxy(
    base_url: str, case: _Case, userinfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy:
        proxy_url = proxy.url.replace("://", f"://{userinfo}")
        factory = partial(SyncHTTPClient, base_url=base_url, proxy=proxy_url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = _sync_seen(pyreqwest_client, case, base_url), _taken(proxy)
        actual = _sync_seen(native_client, case, base_url), _taken(proxy)

    assert actual == expected


@pytest.mark.parametrize("userinfo", ["", "user:pass@"], ids=["no-credentials", "credentials"])
@pytest.mark.parametrize("case", _proxied_redirect_cases(), ids=lambda case: case.id)
async def test_native_transport_sends_every_hop_to_an_http_proxy_as_pyreqwest_does(
    base_url: str, case: _Case, userinfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy:
        proxy_url = proxy.url.replace("://", f"://{userinfo}")
        factory = partial(HTTPClient, base_url=base_url, proxy=proxy_url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = await _async_seen(pyreqwest_client, case, base_url), _taken(proxy)
        actual = await _async_seen(native_client, case, base_url), _taken(proxy)

    assert len(expected[1]) == 2
    assert actual == expected


@pytest.mark.parametrize("userinfo", ["", "user:pass@"], ids=["no-credentials", "credentials"])
@pytest.mark.parametrize("case", _proxied_redirect_cases(), ids=lambda case: case.id)
def test_sync_native_transport_sends_every_hop_to_an_http_proxy_as_pyreqwest_does(
    base_url: str, case: _Case, userinfo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy:
        proxy_url = proxy.url.replace("://", f"://{userinfo}")
        factory = partial(SyncHTTPClient, base_url=base_url, proxy=proxy_url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = _sync_seen(pyreqwest_client, case, base_url), _taken(proxy)
        actual = _sync_seen(native_client, case, base_url), _taken(proxy)

    assert len(expected[1]) == 2
    assert actual == expected


def _keyed(case: _Case, key: str) -> _Case:
    return _Case(case.id, case.verb, case.path.replace("{key}", key), case.call, case.client)


def _taken_without_key(
    proxy: HTTPProxy, key: str
) -> list[tuple[str, list[tuple[str, str]], bytes]]:
    """`_taken` with this run's own `key` taken out of the request lines and a hop's `Referer`."""
    return [
        (
            line.replace(key, "key"),
            [(name, value.replace(key, "key")) for name, value in headers],
            body,
        )
        for line, headers, body in _taken(proxy)
    ]


def _attempts_seen(taken: list[tuple[str, list[tuple[str, str]], bytes]]) -> int:
    """How many requests for `/status-then-ok` itself the proxy saw."""
    return len([
        line for line, _, _ in taken if urlsplit(line.split()[1]).path == "/status-then-ok"
    ])


async def _async_attempts(client: HTTPClient, case: _Case, base_url: str) -> int:
    async with client:
        if case.verb == "stream_get":
            return len([chunk async for chunk in client.stream_get(_path(case, base_url))])
        send: Callable[..., Awaitable[Any]] = getattr(client, case.verb)
        return (await send(_path(case, base_url), **case.call)).status


def _sync_attempts(client: SyncHTTPClient, case: _Case, base_url: str) -> int:
    with client:
        if case.verb == "stream_get":
            return len(list(client.stream_get(_path(case, base_url))))
        send: Callable[..., Any] = getattr(client, case.verb)
        return send(_path(case, base_url), **case.call).status


@pytest.mark.parametrize("case", _retried_cases(), ids=lambda case: case.id)
async def test_native_transport_retries_as_pyreqwest_does(
    base_url: str, server_key: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy:
        factory = partial(HTTPClient, base_url=base_url, proxy=proxy.url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)
        keys = f"{server_key}-pyreqwest", f"{server_key}-native"

        expected = (
            await _async_attempts(pyreqwest_client, _keyed(case, keys[0]), base_url),
            _taken_without_key(proxy, keys[0]),
        )
        actual = (
            await _async_attempts(native_client, _keyed(case, keys[1]), base_url),
            _taken_without_key(proxy, keys[1]),
        )

    assert _attempts_seen(expected[1]) == 2
    assert actual == expected


@pytest.mark.parametrize("case", _retried_cases(), ids=lambda case: case.id)
def test_sync_native_transport_retries_as_pyreqwest_does(
    base_url: str, server_key: str, case: _Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy:
        factory = partial(SyncHTTPClient, base_url=base_url, proxy=proxy.url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)
        keys = f"{server_key}-pyreqwest", f"{server_key}-native"

        expected = (
            _sync_attempts(pyreqwest_client, _keyed(case, keys[0]), base_url),
            _taken_without_key(proxy, keys[0]),
        )
        actual = (
            _sync_attempts(native_client, _keyed(case, keys[1]), base_url),
            _taken_without_key(proxy, keys[1]),
        )

    assert _attempts_seen(expected[1]) == 2
    assert actual == expected


def _tunnel_clients() -> list[dict[str, Any]]:
    return [
        {},
        {"user_agent": "user-agent"},
        # The request's user agent is the default header's; the `CONNECT`'s is `user_agent`'s.
        {"default_headers": {"User-Agent": "default"}},
    ]


@pytest.mark.parametrize("userinfo", ["", "us%20er:p%40ss@"], ids=["no-credentials", "credentials"])
@pytest.mark.parametrize(
    "client_settings", _tunnel_clients(), ids=["defaults", "user-agent", "default-user-agent"]
)
async def test_native_transport_tunnels_as_pyreqwest_does(
    https_base_url: str,
    tls_ca_pem: bytes,
    client_settings: dict[str, Any],
    userinfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_proxy_variables(monkeypatch)
    case = _Case("tunnelled", call={"params": {"a": "b"}})
    with serve_http_proxy() as proxy:
        factory = partial(
            HTTPClient,
            base_url=https_base_url,
            root_certificates=[tls_ca_pem],
            proxy=proxy.url.replace("://", f"://{userinfo}"),
            **client_settings,
        )
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = await _async_seen(pyreqwest_client, case, https_base_url), _taken(proxy)
        actual = await _async_seen(native_client, case, https_base_url), _taken(proxy)

    assert actual == expected


@pytest.mark.parametrize("userinfo", ["", "us%20er:p%40ss@"], ids=["no-credentials", "credentials"])
@pytest.mark.parametrize(
    "client_settings", _tunnel_clients(), ids=["defaults", "user-agent", "default-user-agent"]
)
def test_sync_native_transport_tunnels_as_pyreqwest_does(
    https_base_url: str,
    tls_ca_pem: bytes,
    client_settings: dict[str, Any],
    userinfo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_proxy_variables(monkeypatch)
    case = _Case("tunnelled", call={"params": {"a": "b"}})
    with serve_http_proxy() as proxy:
        factory = partial(
            SyncHTTPClient,
            base_url=https_base_url,
            root_certificates=[tls_ca_pem],
            proxy=proxy.url.replace("://", f"://{userinfo}"),
            **client_settings,
        )
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = _sync_seen(pyreqwest_client, case, https_base_url), _taken(proxy)
        actual = _sync_seen(native_client, case, https_base_url), _taken(proxy)

    assert actual == expected


@pytest.mark.parametrize(
    "credentials", [None, ("user", "pass")], ids=["no-credentials", "credentials"]
)
@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
async def test_native_transport_speaks_socks5_as_pyreqwest_does(
    base_url: str,
    scheme: str,
    credentials: tuple[str, str] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_proxy_variables(monkeypatch)
    origin = base_url.replace("127.0.0.1", "localhost")
    userinfo = "" if credentials is None else ":".join(credentials) + "@"
    with serve_socks5_proxy(credentials=credentials) as proxy:
        factory = partial(
            HTTPClient, base_url=origin, proxy=f"{scheme}://{userinfo}{proxy.address}"
        )
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = (
            await _async_seen(pyreqwest_client, _Case("socks"), origin),
            _taken_handshakes(proxy),
        )
        actual = await _async_seen(native_client, _Case("socks"), origin), _taken_handshakes(proxy)

    assert actual == expected


@pytest.mark.parametrize(
    "credentials", [None, ("user", "pass")], ids=["no-credentials", "credentials"]
)
@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_sync_native_transport_speaks_socks5_as_pyreqwest_does(
    base_url: str,
    scheme: str,
    credentials: tuple[str, str] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_proxy_variables(monkeypatch)
    origin = base_url.replace("127.0.0.1", "localhost")
    userinfo = "" if credentials is None else ":".join(credentials) + "@"
    with serve_socks5_proxy(credentials=credentials) as proxy:
        factory = partial(
            SyncHTTPClient, base_url=origin, proxy=f"{scheme}://{userinfo}{proxy.address}"
        )
        pyreqwest_client, native_client = _pair(factory, monkeypatch)

        expected = _sync_seen(pyreqwest_client, _Case("socks"), origin), _taken_handshakes(proxy)
        actual = _sync_seen(native_client, _Case("socks"), origin), _taken_handshakes(proxy)

    assert actual == expected


# --- form= ----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _FormFile:
    """A file in `form_files`, put into a form when its case runs: its `Path`, or with `opened` the
    file open for reading (a named `BufferedIOBase`)."""

    name: str
    opened: bool = False


@dataclass(frozen=True, slots=True)
class _Buffered:
    """An unnamed in-memory stream (`io.BytesIO`), made afresh for each transport: one read
    consumes it."""

    content: bytes


@dataclass(slots=True)
class _FormCase:
    id: str
    # `_FormFile`s and `_Buffered`s become real when the case runs (`_resolved`).
    form: dict[str, Any]
    verb: str = "post"
    path: str = "/echo-request"
    call: dict[str, Any] = field(default_factory=dict[str, Any])
    client: dict[str, Any] = field(default_factory=dict[str, Any])


@pytest.fixture(name="form_files")
def _form_files(tmp_path: Path) -> Path:
    files = {
        "a.txt": b"text-file",
        "noext": b"no-ext",
        "a.PNG": b"png",
        "a.weirdext": b"weird",
        "empty.json": b"",
        'q"uo\\te ü.txt': b"quoted",
    }
    for name, content in files.items():
        (tmp_path / name).write_bytes(content)
    return tmp_path


def _resolved(value: object, files: Path, stack: ExitStack) -> object:
    """`value` with every `_FormFile`/`_Buffered` in it made real, files opened on `stack`."""
    match value:
        case _FormFile(name=name, opened=True):
            return stack.enter_context((files / name).open("rb"))
        case _FormFile(name=name):
            return files / name
        case _Buffered(content=content):
            return io.BytesIO(content)
        case tuple():
            items = cast("tuple[object, ...]", value)
            return tuple(_resolved(item, files, stack) for item in items)
        case _:
            return value


def _form_call(case: _FormCase, files: Path, stack: ExitStack) -> _Case:
    form = {name: _resolved(value, files, stack) for name, value in case.form.items()}
    return _Case(case.id, case.verb, case.path, {**case.call, "form": form}, case.client)


def _numbered_boundary(numbers: dict[str, str], match: re.Match[str]) -> str:
    return numbers.setdefault(match[0], f"<boundary-{len(numbers)}>")


def _boundaries_masked(text: str) -> str:
    """`text` with each boundary in reqwest's format (four 16-digit hex numbers joined by dashes)
    numbered by where it first appears, so two runs compare equal only if theirs are in the same
    places."""
    pattern = r"\b[0-9a-f]{16}(?:-[0-9a-f]{16}){3}\b"
    return re.sub(pattern, partial(_numbered_boundary, {}), text)


def _masked_seen(seen: _Seen) -> _Seen:
    if isinstance(seen.wire, bytes):
        return _Seen(seen.status, seen.request, _boundaries_masked(seen.wire.decode("latin-1")))
    return _Seen(seen.status, seen.request, json.loads(_boundaries_masked(json.dumps(seen.wire))))


def _form_parts_by_kind() -> dict[str, Any]:
    return {
        "file-tuple": ("n.txt", b"x"),
        "file-tuple-with-type": ("n.bin", b"y", "Application/X-Foo; Charset=UTF-8"),
        "file-tuple-without-extension": ("noext", b"z"),
        "path-tuple": ("given.png", _FormFile("a.txt")),
        "path-tuple-with-type": ("given.txt", _FormFile("a.txt"), "text/x-c"),
        "path-with-type": (_FormFile("a.txt"), "image/gif"),
        "path": _FormFile("a.txt"),
        "path-without-extension": _FormFile("noext"),
        "path-uppercase-extension": _FormFile("a.PNG"),
        "path-unknown-extension": _FormFile("a.weirdext"),
        "empty-path": _FormFile("empty.json"),
        "named-stream": _FormFile("a.txt", opened=True),
        "unnamed-stream": _Buffered(b"buffered"),
    }


def _form_cases() -> list[_FormCase]:
    redirect = "/redirect-to?status={}&to=/echo-request"
    return [
        _FormCase("text-kinds", {"s": "v", "i": 7, "f": 1.5, "t": True, "e": ""}),
        _FormCase("bytes", {"b": b"\x00\xff\r\n--", "empty": b""}),
        _FormCase(
            "json-parts",
            {
                "list": [1, "a", None],
                "dict": {"b": 1, "a": [1.5]},
                "struct": _ItemStruct(item_id=1),
                "model": _AliasedModel(itemId=1),
            },
        ),
        _FormCase("files", _form_parts_by_kind()),
        _FormCase(
            "files-without-type-inference",
            _form_parts_by_kind(),
            call={"infer_mime_type_from_file_extension": False},
        ),
        _FormCase(
            "names-needing-encoding",
            {
                "a b": "1",
                'q"t': "2",
                "pc%": "3",
                "ü中": "4",
                "sl/ash": "5",
                "ok-_.~!$&'()*+,;=:@": "6",
                "": "7",
                "c\r\nr": "8",
                "#?{}<>`": "9",
                "nul\x00": "10",
            },
        ),
        _FormCase(
            "filenames-needing-escapes",
            {
                "f": ('q"uote\\back\r\nnl ü.txt', b"x"),
                "empty": ("", b"y"),
                "path": _FormFile('q"uo\\te ü.txt'),
            },
        ),
        _FormCase(
            "repeated-names",
            {
                "text": ("a", "b", 3),
                "bytes": (b"1", b"2"),
                "files": (("a.txt", b"1"), ("b.txt", b"2", "text/x-b")),
                "paths": (_FormFile("a.txt"), _FormFile("noext")),
                "json": ([1], {"a": 1}),
            },
        ),
        _FormCase("empty-form", {}),
        _FormCase(
            "auth-defaults-and-headers",
            {"a": "b"},
            call={"headers": {"X-1": "1", "Accept": "accept"}},
            client={"bearer_token": "token", "default_headers": {"X-Default": "0"}},
        ),
        _FormCase(
            "a-default-content-type-replaces-the-forms",
            {"a": "b"},
            client={"default_headers": {"Content-Type": "default", "X-Default": "0"}},
        ),
        _FormCase(
            "a-content-type-header-replaces-the-forms",
            {"a": "b"},
            call={"headers": {"X-1": "1", "content-type": "own"}},
        ),
        _FormCase(
            "userinfo-in-the-url",
            {"a": "b"},
            path="{base_url}echo-request".replace("http://", "http://user:pass@"),
        ),
        _FormCase("put", {"a": _FormFile("a.txt")}, verb="put"),
        _FormCase("patch", {"a": "b"}, verb="patch"),
        _FormCase("delete", {"a": "b"}, verb="delete"),
        _FormCase("post-307-is-not-followed", {"a": "b"}, path=redirect.format(307)),
        _FormCase("post-308-is-not-followed", {"a": _FormFile("a.txt")}, path=redirect.format(308)),
        _FormCase("put-301-is-not-followed", {"a": "b"}, verb="put", path=redirect.format(301)),
        _FormCase("put-302-is-not-followed", {"a": "b"}, verb="put", path=redirect.format(302)),
        _FormCase("post-301-becomes-a-get", {"a": "b"}, path=redirect.format(301)),
        _FormCase("post-302-becomes-a-get", {"a": _FormFile("a.txt")}, path=redirect.format(302)),
        _FormCase("put-303-becomes-a-get", {"a": "b"}, verb="put", path=redirect.format(303)),
        _FormCase("an-empty-form-follows-a-307", {}, path=redirect.format(307)),
        _FormCase("an-empty-form-follows-a-put-302", {}, verb="put", path=redirect.format(302)),
    ]


@pytest.mark.parametrize("case", _form_cases(), ids=lambda case: case.id)
async def test_native_transport_sends_a_form_as_pyreqwest_does(
    base_url: str, form_files: Path, case: _FormCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(HTTPClient, base_url=base_url, **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    with ExitStack() as stack:
        expected = await _async_seen(
            pyreqwest_client, _form_call(case, form_files, stack), base_url
        )
        actual = await _async_seen(native_client, _form_call(case, form_files, stack), base_url)

    assert _masked_seen(actual) == _masked_seen(expected)


@pytest.mark.parametrize("case", _form_cases(), ids=lambda case: case.id)
def test_sync_native_transport_sends_a_form_as_pyreqwest_does(
    base_url: str, form_files: Path, case: _FormCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = partial(SyncHTTPClient, base_url=base_url, **case.client)
    pyreqwest_client, native_client = _pair(factory, monkeypatch)

    with ExitStack() as stack:
        expected = _sync_seen(pyreqwest_client, _form_call(case, form_files, stack), base_url)
        actual = _sync_seen(native_client, _form_call(case, form_files, stack), base_url)

    assert _masked_seen(actual) == _masked_seen(expected)


def _streamed_form_case() -> _FormCase:
    return _FormCase("stream-post-form", {"a": "b", "f": _FormFile("a.txt")}, verb="stream_post")


async def test_native_transport_streams_a_form_as_pyreqwest_does(
    base_url: str, form_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyreqwest_client, native_client = _pair(partial(HTTPClient, base_url=base_url), monkeypatch)
    case = _streamed_form_case()

    with ExitStack() as stack:
        expected = await _async_streamed(
            pyreqwest_client, _form_call(case, form_files, stack), base_url
        )
        actual = await _async_streamed(native_client, _form_call(case, form_files, stack), base_url)

    assert _boundaries_masked(json.dumps(actual)) == _boundaries_masked(json.dumps(expected))


def test_sync_native_transport_streams_a_form_as_pyreqwest_does(
    base_url: str, form_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyreqwest_client, native_client = _pair(partial(SyncHTTPClient, base_url=base_url), monkeypatch)
    case = _streamed_form_case()

    with ExitStack() as stack:
        expected = _sync_streamed(pyreqwest_client, _form_call(case, form_files, stack), base_url)
        actual = _sync_streamed(native_client, _form_call(case, form_files, stack), base_url)

    assert _boundaries_masked(json.dumps(actual)) == _boundaries_masked(json.dumps(expected))


# A file many times one read of it (64 KiB on the native transport), so it's read and sent in
# several chunks, in plain HTTP and over TLS.
def _large_file_case(files: Path) -> _FormCase:
    (files / "large.bin").write_bytes(bytes(range(256)) * 4_096)
    return _FormCase("large-file", {"a": "b", "f": _FormFile("large.bin"), "z": "y"})


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
async def test_native_transport_sends_a_large_file_as_pyreqwest_does(
    server_url_fixture: str,
    form_files: Path,
    tls_ca_pem: bytes,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    factory = partial(HTTPClient, base_url=server_url, root_certificates=[tls_ca_pem])
    pyreqwest_client, native_client = _pair(factory, monkeypatch)
    case = _large_file_case(form_files)

    with ExitStack() as stack:
        expected = await _async_seen(
            pyreqwest_client, _form_call(case, form_files, stack), server_url
        )
        actual = await _async_seen(native_client, _form_call(case, form_files, stack), server_url)

    assert _masked_seen(actual) == _masked_seen(expected)


@pytest.mark.parametrize("server_url_fixture", ["base_url", "https_base_url"])
def test_sync_native_transport_sends_a_large_file_as_pyreqwest_does(
    server_url_fixture: str,
    form_files: Path,
    tls_ca_pem: bytes,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    server_url: str = request.getfixturevalue(server_url_fixture)
    factory = partial(SyncHTTPClient, base_url=server_url, root_certificates=[tls_ca_pem])
    pyreqwest_client, native_client = _pair(factory, monkeypatch)
    case = _large_file_case(form_files)

    with ExitStack() as stack:
        expected = _sync_seen(pyreqwest_client, _form_call(case, form_files, stack), server_url)
        actual = _sync_seen(native_client, _form_call(case, form_files, stack), server_url)

    assert _masked_seen(actual) == _masked_seen(expected)


def _retried_form_cases() -> list[_FormCase]:
    """Through an HTTP proxy, which sees every attempt (`{key}` as in `_retried_cases`): a form
    with parts is sent once, as pyreqwest can't copy it; an empty one is retried, its boundary
    the same each time."""
    retries: dict[str, Any] = {"max_retries": 1, "backoff_base": 0.001}
    path = "/status-then-ok?key={key}&fail_times=1"
    # The 503 a form sent once gets back is the response compared.
    call = {"error_for_status": False}
    return [
        _FormCase("a-form-put-is-sent-once", {"a": "b"}, "put", path, call, retries),
        _FormCase(
            "a-form-post-is-sent-once",
            {"f": _FormFile("a.txt")},
            "post",
            path,
            call,
            {**retries, "retry_methods": {"POST"}},
        ),
        _FormCase("an-empty-form-is-retried", {}, "put", path, call, retries),
    ]


def _masked_taken(taken: list[tuple[str, list[tuple[str, str]], bytes]]) -> str:
    """`taken` as one text, so a boundary is numbered the same across every attempt."""
    text = [(line, headers, body.decode("latin-1")) for line, headers, body in taken]
    return _boundaries_masked(json.dumps(text))


def _keyed_form_call(case: _FormCase, key: str, files: Path, stack: ExitStack) -> _Case:
    keyed = _FormCase(
        case.id, case.form, case.verb, case.path.replace("{key}", key), case.call, case.client
    )
    return _form_call(keyed, files, stack)


@pytest.mark.parametrize("case", _retried_form_cases(), ids=lambda case: case.id)
async def test_native_transport_retries_a_form_as_pyreqwest_does(
    base_url: str,
    server_key: str,
    form_files: Path,
    case: _FormCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy, ExitStack() as stack:
        factory = partial(HTTPClient, base_url=base_url, proxy=proxy.url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)
        keys = f"{server_key}-pyreqwest", f"{server_key}-native"

        expected = (
            await _async_attempts(
                pyreqwest_client, _keyed_form_call(case, keys[0], form_files, stack), base_url
            ),
            _masked_taken(_taken_without_key(proxy, keys[0])),
        )
        actual = (
            await _async_attempts(
                native_client, _keyed_form_call(case, keys[1], form_files, stack), base_url
            ),
            _masked_taken(_taken_without_key(proxy, keys[1])),
        )

    assert actual == expected


@pytest.mark.parametrize("case", _retried_form_cases(), ids=lambda case: case.id)
def test_sync_native_transport_retries_a_form_as_pyreqwest_does(
    base_url: str,
    server_key: str,
    form_files: Path,
    case: _FormCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy, ExitStack() as stack:
        factory = partial(SyncHTTPClient, base_url=base_url, proxy=proxy.url, **case.client)
        pyreqwest_client, native_client = _pair(factory, monkeypatch)
        keys = f"{server_key}-pyreqwest", f"{server_key}-native"

        expected = (
            _sync_attempts(
                pyreqwest_client, _keyed_form_call(case, keys[0], form_files, stack), base_url
            ),
            _masked_taken(_taken_without_key(proxy, keys[0])),
        )
        actual = (
            _sync_attempts(
                native_client, _keyed_form_call(case, keys[1], form_files, stack), base_url
            ),
            _masked_taken(_taken_without_key(proxy, keys[1])),
        )

    assert actual == expected


# A file that opens but can't be read (a directory) fails the request partway through its body:
# what had gone out before it, with the head, is what the server received from either transport.
def _unreadable_file_form(files: Path) -> dict[str, Any]:
    (files / "directory").mkdir()
    return {"a": "b", "f": files / "directory", "z": "y"}


def _partial_seen(base_url: str, key: str) -> str:
    seen = partial_body_seen(base_url, key)
    return _boundaries_masked(json.dumps(seen).replace(key, "key"))


async def _async_post_partial(client: HTTPClient, key: str, form: dict[str, Any]) -> None:
    async with client:
        with pytest.raises(HTTPConnectionError):
            await client.post("partial-body", params={"key": key}, form=form)


def _sync_post_partial(client: SyncHTTPClient, key: str, form: dict[str, Any]) -> None:
    with client, pytest.raises(HTTPConnectionError):
        client.post("partial-body", params={"key": key}, form=form)


async def test_native_transport_sends_what_pyreqwest_sends_of_a_form_it_cant_finish(
    base_url: str, server_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyreqwest_client, native_client = _pair(partial(HTTPClient, base_url=base_url), monkeypatch)
    form = _unreadable_file_form(tmp_path)
    keys = f"{server_key}-pyreqwest", f"{server_key}-native"

    await _async_post_partial(pyreqwest_client, keys[0], form)
    await _async_post_partial(native_client, keys[1], form)

    assert _partial_seen(base_url, keys[1]) == _partial_seen(base_url, keys[0])


def test_sync_native_transport_sends_what_pyreqwest_sends_of_a_form_it_cant_finish(
    base_url: str, server_key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyreqwest_client, native_client = _pair(partial(SyncHTTPClient, base_url=base_url), monkeypatch)
    form = _unreadable_file_form(tmp_path)
    keys = f"{server_key}-pyreqwest", f"{server_key}-native"

    _sync_post_partial(pyreqwest_client, keys[0], form)
    _sync_post_partial(native_client, keys[1], form)

    assert _partial_seen(base_url, keys[1]) == _partial_seen(base_url, keys[0])
