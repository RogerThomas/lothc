"""`lothc_mocker` sees the same request on both transports: every call below goes out through a
pyreqwest client and a `native=True` one, async and sync, and what the mock was handed (method,
path, query, every header in order, body) is compared, along with the `RequestInfo` of the
response. A multipart body's boundary is random, so it's masked in its `Content-Type`.
"""

import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from msgspec import Struct
from pydantic import BaseModel, Field

from lothc import HTTPClient, RequestInfo, SyncHTTPClient
from lothc.testing import LOTHCMocker, MockRequest

# Every test here sends through the native transport, which is POSIX only.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the native transport is POSIX only"
)


class _ItemStruct(Struct):
    item_id: int


class _AliasedModel(BaseModel):
    item_id: int = Field(alias="itemId")


class _QueryModel(BaseModel):
    page: int
    tag: str | None = None


@dataclass(slots=True)
class _Case:
    id: str
    verb: str = "get"
    # `{base_url}` and `{origin}` (`//host:port/`) are filled in from the mock's `base_url`.
    path: str = "items"
    call: dict[str, Any] = field(default_factory=dict[str, Any])
    client: dict[str, Any] = field(default_factory=dict[str, Any])
    base_suffix: str = ""


@dataclass(slots=True)
class _Snapshot:
    method: str
    path: str
    query_string: str
    headers: list[tuple[str, str]]
    body: bytes | None
    request: RequestInfo | None


def _cases() -> list[_Case]:
    form_file = {"field": "v", "bytes": b"\x00b", "file": "{file}", "list": ["a", 1]}
    return [
        _Case("plain-get"),
        _Case(
            "params-and-headers",
            call={
                "params": {"a": "x y", "b": [1, 2], "c": True, "d": 1.5, "e": "ü&=*~", "f": 2**70},
                "headers": {"X-Custom": "value", "x-custom": "again"},
            },
        ),
        _Case(
            "params-onto-an-existing-query",
            path="items?q=1#fragment",
            call={"params": {"z": 2}},
        ),
        _Case("empty-params", path="items?", call={"params": {}}),
        _Case("model-params", call={"params": _QueryModel(page=2, tag="t")}),
        _Case("bearer-token", client={"bearer_token": "token"}),
        _Case("skip-auth", client={"bearer_token": "token"}, call={"skip_auth": True}),
        _Case("basic-auth", client={"basic_auth": ("user", "pa:ss")}),
        _Case("basic-auth-without-password", client={"basic_auth": ("user", None)}),
        _Case(
            "authorization-header-overrides-auth",
            client={"bearer_token": "token"},
            call={"headers": {"X-First": "1", "Authorization": "authorization"}},
        ),
        _Case("userinfo-in-the-url", path="{base_url}items".replace("http://", "http://u:p@")),
        _Case(
            "default-headers",
            client={
                "default_headers": {"X-Default": "default", "Accept": "a", "Authorization": "d"},
                "bearer_token": "token",
                "user_agent": "user-agent",
            },
            call={"headers": {"x-default": "override"}},
        ),
        _Case("json-dict", verb="post", call={"json": {"b": {"d": 1, "c": 2}, "a": [1e20, None]}}),
        _Case("json-list", verb="put", call={"json": [1, "ü", chr(0x2028)]}),
        _Case("json-struct", verb="post", call={"json": _ItemStruct(item_id=1)}),
        _Case("json-model-by-alias", verb="patch", call={"json": _AliasedModel(itemId=1)}),
        _Case(
            "json-replaces-a-content-type",
            verb="post",
            client={"default_headers": {"Content-Type": "default", "X-Default": "0"}},
            call={"json": {"a": 1}, "headers": {"X-1": "1", "Content-Type": "text/plain"}},
        ),
        _Case(
            "data-form",
            verb="post",
            client={"default_headers": {"Content-Type": "default"}},
            call={"data": {"a": "x y", "b": [1, 2]}, "headers": {"X-1": "1"}},
        ),
        _Case("data-empty", verb="post", call={"data": {}}),
        _Case("multipart-form", verb="post", call={"form": form_file}),
        _Case("multipart-empty", verb="post", call={"form": {}}),
        _Case("content-text", verb="post", call={"content": "héllo"}),
        _Case("content-empty", verb="delete", call={"content": b""}),
        _Case("post-without-a-body", verb="post"),
        _Case("head", verb="head"),
        _Case("options", verb="options"),
        _Case("base-path-prefix-kept", path="items/7", base_suffix="prefix/"),
        _Case("leading-slash-drops-the-base-path", path="/items", base_suffix="prefix/"),
        _Case("protocol-relative-url", path="{origin}items", base_suffix="prefix/"),
        _Case("percent-encoding-and-dot-segments", path="/échø/../x%20y/./z"),
        _Case("download", verb="download", call={"params": {"a": 1}, "headers": {"X-1": "1"}}),
        _Case("stream-get", verb="stream_get", client={"bearer_token": "token"}),
        _Case("stream-post-json", verb="stream_post", call={"json": {"b": 1, "a": 2}}),
        _Case("sse", verb="sse", call={"headers": {"Last-Event-ID": "own", "X-1": "1"}}),
    ]


def _path(case: _Case, base_url: str) -> str:
    origin = base_url.removeprefix("http:").removesuffix("/") + "/"
    return case.path.format(base_url=base_url, origin=origin)


def _call_kwargs(case: _Case, tmp_path: Path) -> dict[str, Any]:
    call = dict(case.call)
    if "form" in call:
        (tmp_path / "f.txt").write_bytes(b"file")
        call["form"] = {
            name: tmp_path / "f.txt" if value == "{file}" else value
            for name, value in call["form"].items()
        }
    if case.verb == "sse":
        call["max_reconnects"] = 0
    return call


def _snapshot(seen: MockRequest, request: RequestInfo | None) -> _Snapshot:
    pattern = r"\b[0-9a-f]{16}(?:-[0-9a-f]{16}){3}\b"
    headers = [(name, re.sub(pattern, "<boundary>", value)) for name, value in seen.headers.items()]
    return _Snapshot(seen.method, seen.path, seen.query_string, headers, seen.body, request)


async def _drain(results: AsyncIterator[Any]) -> None:
    async for _ in results:
        pass


def _drain_sync(results: Iterator[Any]) -> None:
    for _ in results:
        pass


async def _async_request(client: HTTPClient, case: _Case, path: str, call: dict[str, Any]) -> Any:  # noqa: ANN401
    async with client:
        send: Callable[..., Any] = getattr(client, case.verb)
        if case.verb in {"stream_get", "stream_post", "sse"}:
            await _drain(send(path, **call))
            return None
        returned: Awaitable[Any] = send(path, error_for_status=False, **call)
        return await returned


def _sync_request(client: SyncHTTPClient, case: _Case, path: str, call: dict[str, Any]) -> Any:  # noqa: ANN401
    with client:
        send: Callable[..., Any] = getattr(client, case.verb)
        if case.verb in {"stream_get", "stream_post", "sse"}:
            _drain_sync(send(path, **call))
            return None
        return send(path, error_for_status=False, **call)


def _request_info(result: object) -> RequestInfo | None:
    request = getattr(result, "request", None)
    return request if isinstance(request, RequestInfo) else None


@pytest.fixture(name="answer_everything")
def _answer_everything(lothc_mocker: LOTHCMocker, monkeypatch: pytest.MonkeyPatch) -> LOTHCMocker:
    """One mock for every request (an event stream, so `sse()` reads it too). `LOTHC_TRANSPORT`
    is unset because it wins over `native=`: left set, both clients would be native."""
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)
    lothc_mocker.mock().with_headers({"content-type": "text/event-stream"})
    return lothc_mocker


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.id)
async def test_a_mock_sees_the_same_request_on_both_transports(
    base_url: str, answer_everything: LOTHCMocker, case: _Case, tmp_path: Path
) -> None:
    path, call = _path(case, base_url), _call_kwargs(case, tmp_path)
    infos: list[RequestInfo | None] = []
    for native in (False, True):
        client = HTTPClient(base_url=base_url + case.base_suffix, native=native, **case.client)
        infos.append(_request_info(await _async_request(client, case, path, call)))

    on_pyreqwest, on_native = answer_everything.get_requests()
    assert _snapshot(on_native, infos[1]) == _snapshot(on_pyreqwest, infos[0])


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case.id)
def test_sync_a_mock_sees_the_same_request_on_both_transports(
    base_url: str, answer_everything: LOTHCMocker, case: _Case, tmp_path: Path
) -> None:
    path, call = _path(case, base_url), _call_kwargs(case, tmp_path)
    infos: list[RequestInfo | None] = []
    for native in (False, True):
        client = SyncHTTPClient(base_url=base_url + case.base_suffix, native=native, **case.client)
        infos.append(_request_info(_sync_request(client, case, path, call)))

    on_pyreqwest, on_native = answer_everything.get_requests()
    assert _snapshot(on_native, infos[1]) == _snapshot(on_pyreqwest, infos[0])
