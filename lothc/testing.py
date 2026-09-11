"""pytest plugin for mocking `HTTPClient`/`SyncHTTPClient` requests — install the `testing` extra
(`lothc[testing]`) to get the `lothc_mocker` fixture, auto-registered via this module's
`pytest11` entry point (see `pyproject.toml`).

This is a thin, lothc-flavored adapter over pyreqwest's own `client_mocker` fixture
(`pyreqwest.pytest_plugin`) rather than a separate interception mechanism — pyreqwest's plugin
already monkeypatches `RequestBuilder.build`/`build_streamed` (exactly what `_send`/`_send_sync`
and the streaming verbs call in `_client.py`), so it transparently intercepts every request a
`HTTPClient`/`SyncHTTPClient` sends with no changes to how the client under test is built.

**No pyreqwest type is ever part of this module's public API — not even re-exported.** A custom
matcher/handler receives lothc's own `MockRequest` (a plain snapshot: `method`/`path`/
`query_string`/`headers`/`body`), never pyreqwest's `Request`; `get_requests()` returns
`list[MockRequest]`; `url=` matching takes `str | re.Pattern[str]` only (pyreqwest's own
`Url`-object alternative is dropped — a `str` already matches the exact URL, so nothing real is
lost); and `match_request_with_response` returns `MockResponse`, not pyreqwest's `Response`/
`SyncResponse`/`ResponseBuilder`. Two private helpers still touch pyreqwest's real types
internally, never at the boundary a caller reaches: `_wrap_custom_handler`/`_wrap_custom_matcher`
convert a real `Request` to `MockRequest` via `_mock_request_from` before a caller's own code ever
sees it, and `_query_param_match_values` uses pyreqwest's own `Url.parse_with_params` to derive
`params=`'s query-string encoding rather than reimplementing it by hand.

`ClientMocker`/`Mock` (and the `Matcher`/`PathMatcher`/`QueryMatcher`/`JsonMatcher` type aliases
their methods are typed against) are re-typed here as local Protocols (`_ClientMockerTyping`/
`_MockTyping`), the same reasoning as `_compat.py`'s `StructTyping`/`BaseModelTyping`: pyreqwest's
own `pytest_plugin/types.py` builds those aliases via a runtime `try/except ImportError` around an
optional `dirty_equals` import, which confirmed live to leave every method typed against them
`Unknown` under basedpyright strict mode — not merely when `dirty_equals` is absent (the expected
case, and the one this project's own venv is in), but even with it installed, since a bare
variable assigned across two `try`/`except` branches isn't resolved as a type alias by
basedpyright at all (`Variable not allowed in type expression`) rather than a real precision gap
in a dependency's stubs, so a `cast` at this module's boundaries is the fix, not a workaround for
a limitation to live with. Only the `str | Pattern[str]` baseline is carried over — `DirtyEquals`
matcher support is dropped from these signatures accordingly.

`params=`/`data=`/`headers=`'s encoding are the places this module has real lothc-specific
behaviour, all three reusing `_client.py`'s own encoding helpers so a mock always matches/responds
exactly the way a real request would encode the same value: `params=` accepts lothc's `Params`
union (via `_encode_params`, shared with `_apply_params`) and is applied as an exact
`match_query_param` per key — narrowing which requests an `add_*_response` rule matches, the same
way a real call's `params=` would build the query string (each value's match string derived via
`_query_param_match_values` from pyreqwest's own real encoder, not reimplemented by hand — e.g.
`True` becomes `"true"`, not Python's `str(True)` == `"True"`, confirmed live); `data=`
accepts lothc's `Data` union (`bytes | dict | BaseModel | Struct`, via `_encode_json_payload`,
shared with `_attach_body`/`_attach_body_sync`); and `headers=` accepts lothc's `Headers` union
(`Mapping[str, str] | BaseModel | Struct`, via `_encode_headers`, shared with `_apply_headers`).
The same typed params/headers class used for a real request, or for a `response_headers_type=`
decode on the read side, can be constructed once and passed straight into `add_*_response(...)`.
The import below is the one deliberate `reportPrivateUsage` suppression in this module: these
three encoding helpers keep this file's otherwise-universal `_`-prefix-for-module-private-functions
convention rather than losing it to cross-module sharing, since basedpyright's module-boundary
check has no way to express "private to the package, shared between two of its own files."
"""

import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from re import Pattern
from typing import Any, Literal, Protocol, Self, cast

import pytest
from pyreqwest.http import Url
from pyreqwest.pytest_plugin.mock import ClientMocker
from pyreqwest.request import Request
from pyreqwest.response import Response, ResponseBuilder, SyncResponse

from ._client import (
    Data,
    Headers,
    Params,
    _encode_headers,  # pyright: ignore[reportPrivateUsage]
    _encode_json_payload,  # pyright: ignore[reportPrivateUsage]
    _encode_params,  # pyright: ignore[reportPrivateUsage]
)

__all__ = ["LOTHCMock", "LOTHCMocker", "MockRequest", "MockResponse"]

type Matcher = str | Pattern[str]
type MethodMatcher = Matcher
type PathMatcher = Matcher
type UrlMatcher = Matcher
type QueryMatcher = dict[str, Matcher | list[str]] | Matcher
type JsonMatcher = Any


@dataclass(slots=True)
class MockRequest:
    """The request a `match_request`/`match_request_with_response` handler receives, or
    `get_requests()` returns — lothc's own snapshot (`method`/`path`/`query_string`/`headers`/
    `body`), built from pyreqwest's real `Request` by `_mock_request_from` but never itself
    pyreqwest's type. `body` is already the fully-read bytes — pyreqwest's own mock middleware
    reads any streamed body into bytes before a mock rule ever sees the request.

    Deliberately not `frozen=True`: `headers` holds a plain `dict`, which a frozen dataclass can't
    make genuinely immutable or hashable anyway (confirmed live — `frozen=True` here still let
    `.headers[...] = ...` mutate in place, and `hash(...)` still raised from deep inside dataclass
    machinery); a plain mutable dataclass is the honest shape, matching `hash(MockRequest(...))`
    raising a direct, expected `TypeError` instead. `headers` is single-value-per-key — matching
    every other place lothc reads real headers (`Result.headers`, see `_client.py`) — so a
    genuinely repeated header collapses to its first value, same as pyreqwest's own
    `HeaderMap.__getitem__`/`dict(HeaderMap(...))` already do."""

    method: str
    path: str
    query_string: str
    headers: Mapping[str, str]
    body: bytes | None


@dataclass(slots=True)
class MockResponse:
    """The response a `match_request_with_response` custom handler returns — lothc's own plain
    description (`data`/`headers`/`status`, encoded the same way `add_*_response` encodes them),
    not pyreqwest's `Response`/`SyncResponse`/`ResponseBuilder`. lothc never exposes pyreqwest's
    own response types in its public API; `LOTHCMock.match_request_with_response` builds the real
    pyreqwest response internally from this. `slots=True` per `style-guide.md` §7 (a DTO, like its
    sibling `MockRequest`) — not `frozen=True`, matching `MockRequest`'s own reasoning: `data`/
    `headers` can hold a mutable `dict`, so a frozen dataclass couldn't deliver real immutability
    here either."""

    data: Data = b""
    headers: Headers | None = None
    status: int = 200


type CustomMatcher = Callable[[MockRequest], Awaitable[bool]] | Callable[[MockRequest], bool]
type CustomHandler = (
    Callable[[MockRequest], Awaitable[MockResponse | None]]
    | Callable[[MockRequest], MockResponse | None]
)
# What pyreqwest's real `Mock.match_request`/`match_request_with_response` actually accept —
# `LOTHCMock` wraps a `CustomMatcher`/`CustomHandler` into one of these before registering it, so
# pyreqwest's real `Request`/`Response`/`SyncResponse` types never reach a caller.
type _PyreqwestCustomMatcher = Callable[[Request], Awaitable[bool]] | Callable[[Request], bool]
type _PyreqwestCustomHandler = (
    Callable[[Request], Awaitable[Response | None]] | Callable[[Request], SyncResponse | None]
)


class _MockTyping(Protocol):
    def with_status(self, status: int) -> "_MockTyping": ...
    def with_headers(self, headers: Mapping[str, str]) -> "_MockTyping": ...
    def with_body_bytes(self, body: bytes) -> "_MockTyping": ...
    def with_body_json(self, _json_body: Any) -> "_MockTyping": ...  # noqa: ANN401
    def match_query(self, query: QueryMatcher) -> "_MockTyping": ...
    def match_query_param(self, name: str, value: Matcher) -> "_MockTyping": ...
    def match_header(self, name: str, value: Matcher) -> "_MockTyping": ...
    def match_body_json(self, matcher: JsonMatcher) -> "_MockTyping": ...
    def match_request(self, matcher: _PyreqwestCustomMatcher) -> "_MockTyping": ...
    def match_request_with_response(self, handler: _PyreqwestCustomHandler) -> "_MockTyping": ...
    def assert_called(
        self,
        *,
        count: int | None = None,
        min_count: int | None = None,
        max_count: int | None = None,
    ) -> None: ...
    def get_requests(self) -> list[Request]: ...
    def get_call_count(self) -> int: ...
    def reset_requests(self) -> None: ...


class _ClientMockerTyping(Protocol):
    def mock(
        self,
        method: MethodMatcher | None = None,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
    ) -> _MockTyping: ...
    def get(
        self, *, path: PathMatcher | None = None, url: UrlMatcher | None = None
    ) -> _MockTyping: ...
    def post(
        self, *, path: PathMatcher | None = None, url: UrlMatcher | None = None
    ) -> _MockTyping: ...
    def put(
        self, *, path: PathMatcher | None = None, url: UrlMatcher | None = None
    ) -> _MockTyping: ...
    def patch(
        self, *, path: PathMatcher | None = None, url: UrlMatcher | None = None
    ) -> _MockTyping: ...
    def delete(
        self, *, path: PathMatcher | None = None, url: UrlMatcher | None = None
    ) -> _MockTyping: ...
    def head(
        self, *, path: PathMatcher | None = None, url: UrlMatcher | None = None
    ) -> _MockTyping: ...
    def strict(self, *, enabled: bool = True) -> "_ClientMockerTyping": ...
    def get_requests(self) -> list[Request]: ...
    def get_call_count(self) -> int: ...
    def clear(self) -> None: ...
    def reset_requests(self) -> None: ...


def _mock_request_from(request: Request) -> MockRequest:
    body = request.body
    body_bytes = None
    if body is not None:
        # pyreqwest's own mock middleware (ClientMocker._create_middleware/_create_sync_middleware)
        # already reads any streamed body into bytes before a mock rule ever sees the request, so
        # `copy_bytes()` genuinely never returns `None` here in practice — but its own stub says it
        # can (for a still-streaming body), so this stays a real check, not an assumed invariant.
        raw = body.copy_bytes()
        body_bytes = raw.to_bytes() if raw is not None else None
    return MockRequest(
        method=request.method,
        path=request.url.path,
        query_string=request.url.query_string or "",
        headers=dict(request.headers),
        body=body_bytes,
    )


def _apply_data(
    data: Data, *, with_bytes: Callable[[bytes], object], with_json: Callable[[Any], object]
) -> None:
    """Shared `isinstance(data, bytes)` dispatch for `_encode_response_data`/`_apply_mock_response`
    — the two objects they each mutate (pyreqwest's `Mock`/`ResponseBuilder`) name their
    bytes/JSON-body setters differently (`with_body_bytes`/`with_body_json` vs `body_bytes`/
    `body_json`), so the dispatch itself is shared via these two bound-method callbacks rather
    than a common Protocol — both objects mutate themselves in place and return `self` (confirmed
    live for `ResponseBuilder`), so discarding the return value here is safe for both."""
    if isinstance(data, bytes):
        with_bytes(data)
    else:
        with_json(_encode_json_payload(data))


def _encode_response_data(mock: _MockTyping, data: Data) -> None:
    _apply_data(data, with_bytes=mock.with_body_bytes, with_json=mock.with_body_json)


def _query_param_match_values(
    params: Mapping[str, str | int | float | bool],
) -> dict[str, str]:
    """The exact string each `params` value matches against in `match_query_param`, derived from
    pyreqwest's own real URL/query encoder (`Url.parse_with_params` + `.query_dict_multi_value`)
    rather than a hand-reimplementation of it — reimplementing risked silently diverging from
    pyreqwest for any type it encodes differently from Python's own `str()` (confirmed live for
    `bool`: `str(True)` == `"True"` vs pyreqwest's real `"true"`), and would only ever be caught by
    manually re-discovering the next such mismatch. This also means an invalid value (e.g. `None`
    — not a valid `Params` value, but nothing stops it arriving at runtime) raises the exact same
    `ValueError` a real request would, confirmed live, instead of silently registering a mock that
    a real call could never actually produce. `_encode_params`'s value union never includes a list
    (`Params` is `Mapping[str, str | int | float | bool] | BaseModel | Struct`, one scalar per
    key), so `query_dict_multi_value`'s per-key result is always the plain `str` case, never the
    `list[str]` one it also allows for genuinely repeated query keys."""
    encoded = Url.parse_with_params("http://mock.invalid/", params).query_dict_multi_value
    return {name: value if isinstance(value, str) else value[0] for name, value in encoded.items()}


def _apply_mock_response(builder: ResponseBuilder, mock_response: MockResponse) -> ResponseBuilder:
    builder = builder.status(mock_response.status)
    # `data` before `headers`, deliberately: pyreqwest's `.body_json()` sets its own Content-Type
    # by *appending* a header value (confirmed live — its own `.header()` docstring says "Append
    # single header value (multiple allowed)"), while `.headers()` *merges with same-key replace*
    # (confirmed live: calling it after `.body_json()` correctly replaces the auto-set
    # Content-Type; calling it before left both values on the wire — two Content-Type headers for
    # one response). Applying `data` first means a caller's own `headers={"content-type": ...}`
    # always wins over `.body_json()`'s default, matching real request precedence.
    _apply_data(mock_response.data, with_bytes=builder.body_bytes, with_json=builder.body_json)
    if mock_response.headers is not None:
        builder = builder.headers(_encode_headers(mock_response.headers))
    return builder


async def _call_async_matcher(
    request: Request, *, matcher: Callable[[MockRequest], Awaitable[bool]]
) -> bool:
    return await matcher(_mock_request_from(request))


def _call_sync_matcher(request: Request, *, matcher: Callable[[MockRequest], bool]) -> bool:
    return matcher(_mock_request_from(request))


def _wrap_custom_matcher(matcher: CustomMatcher) -> _PyreqwestCustomMatcher:
    """Same `functools.partial`-over-a-nested-closure and async/sync-dispatch reasoning as
    `_wrap_custom_handler` below — see its docstring for both. Deliberately not unified with it
    into one generic helper despite the identical dispatch *shape*: a matcher returns a plain
    `bool` (used as-is), a handler returns `MockResponse | None` that still needs converting to a
    real pyreqwest `Response`/`SyncResponse` via `_apply_mock_response` — genuinely different
    return types and post-processing, unlike `_apply_data` above (which really was the same
    dispatch with only the target object's method names differing). Forcing these two through one
    generic, parametrized helper would trade a small amount of duplication for real type-signature
    complexity, the same tradeoff this codebase already makes deliberately for the six near-
    identical `add_*_response` methods further down."""
    if inspect.iscoroutinefunction(matcher):
        async_matcher = cast("Callable[[MockRequest], Awaitable[bool]]", matcher)
        return functools.partial(_call_async_matcher, matcher=async_matcher)

    sync_matcher = cast("Callable[[MockRequest], bool]", matcher)
    return functools.partial(_call_sync_matcher, matcher=sync_matcher)


async def _call_async_handler(
    request: Request, *, handler: Callable[[MockRequest], Awaitable[MockResponse | None]]
) -> Response | None:
    mock_response = await handler(_mock_request_from(request))
    if mock_response is None:
        return None
    return await _apply_mock_response(ResponseBuilder(), mock_response).build()


def _call_sync_handler(
    request: Request, *, handler: Callable[[MockRequest], MockResponse | None]
) -> SyncResponse | None:
    mock_response = handler(_mock_request_from(request))
    if mock_response is None:
        return None
    return _apply_mock_response(ResponseBuilder(), mock_response).build_sync()


def _wrap_custom_handler(handler: CustomHandler) -> _PyreqwestCustomHandler:
    """Adapts a lothc-flavored `CustomHandler` (takes `MockRequest`, returns `MockResponse |
    None`) into whichever shape pyreqwest's real `Mock.match_request_with_response` actually
    needs — an `async def` taking `Request` and returning `Response | None`, or a plain function
    taking `Request` and returning `SyncResponse | None` — so a caller never has to import
    pyreqwest's `Request`/`Response`/`SyncResponse`/`ResponseBuilder` themselves. Which of the two
    pyreqwest expects depends on which client (`HTTPClient` vs `SyncHTTPClient`) ends up invoking
    it, which this module has no way to know ahead of time — so this dispatches on whether the
    caller's own `handler` is itself an `async def`, exactly the same constraint the raw pyreqwest
    API already imposes (an async handler for a `HTTPClient` test, a plain one for
    `SyncHTTPClient`), just without requiring pyreqwest's own types to do it. Binds `handler` into
    `_call_async_handler`/`_call_sync_handler` via `functools.partial` rather than a nested
    closure — this project doesn't nest `def`s, even small wrappers like this one.

    A single `LOTHCMocker`/`ClientMocker` patches both `HTTPClient` and `SyncHTTPClient`
    transports, so nothing here stops a sync `handler` from being registered on a mock that a
    `HTTPClient` request also matches (or vice versa) within the same test. If that happens,
    pyreqwest's own internal `assert isinstance(res, Awaitable)` (or the sync-side equivalent)
    raises a raw, unhelpful `AssertionError` — that mismatch happens deep inside pyreqwest's own
    call chain, after this wrapper has already returned, so it can't be intercepted here. Keep a
    given `handler`'s async-ness matched to whichever client will actually exercise that mock."""
    if inspect.iscoroutinefunction(handler):
        async_handler = cast("Callable[[MockRequest], Awaitable[MockResponse | None]]", handler)
        return functools.partial(_call_async_handler, handler=async_handler)

    sync_handler = cast("Callable[[MockRequest], MockResponse | None]", handler)
    return functools.partial(_call_sync_handler, handler=sync_handler)


@dataclass
class LOTHCMock:
    """A single registered mock rule, returned by every `LOTHCMocker.add_*_response`/`mock` call
    so it can be further narrowed with matchers, given a response, or asserted on afterwards."""

    _mock: _MockTyping
    _committed: Literal["response", "handler"] | None = None
    """A canned response (`with_status`/`with_headers`/`with_data`, or the `add_*_response`
    methods that call them) and `match_request_with_response`'s custom handler are mutually
    exclusive on the same pyreqwest `Mock` rule, in either order — pyreqwest itself guards both
    directions, but via two independent assertions (one on `Mock._response_builder`, one on
    `Mock.match_request_with_response`), each only catching its own ordering. Every setter for
    either side here goes through `_commit`, so the guard can't be bypassed by a future setter
    that forgets to check it — `_commit` raises lothc's own clear `ValueError` for both orderings
    instead of leaking pyreqwest's internal `AssertionError`."""
    _last_encoded_headers: dict[str, str] | None = None
    """Set by `with_headers`, re-applied by `with_data` — see `with_data`'s own comment for why:
    this is what makes `.with_headers(...).with_data(...)` (the natural order to chain them in)
    produce the correct single Content-Type header on the wire, not just `add_*_response`'s own
    internal (already data-then-headers) call order."""

    def _commit(self, kind: Literal["response", "handler"]) -> None:
        if self._committed is not None and self._committed != kind:
            raise ValueError(
                "Cannot add a canned response (with_status/with_headers/with_data, or an "
                "add_*_response call) and a match_request_with_response custom handler to the "
                "same mock — pyreqwest treats these as mutually exclusive on one rule. Register "
                "a separate lothc_mocker.mock() for whichever side is missing."
            )
        self._committed = kind

    def with_status(self, status: int) -> Self:
        self._commit("response")
        self._mock.with_status(status)
        return self

    def with_headers(self, headers: Headers) -> Self:
        self._commit("response")
        self._last_encoded_headers = _encode_headers(headers)
        self._mock.with_headers(self._last_encoded_headers)
        return self

    def with_data(self, data: Data) -> Self:
        self._commit("response")
        _encode_response_data(self._mock, data)
        if self._last_encoded_headers is not None:
            # pyreqwest's own `.body_json()` sets its own Content-Type by *appending* a header
            # value, never replacing — confirmed live — so if `with_headers` was already called
            # (this method running second, the natural order to chain them in), its headers must
            # be re-applied now: `.headers()` merges with same-key *replace* semantics (also
            # confirmed live), so re-applying here correctly overwrites whatever Content-Type
            # `_encode_response_data` just set, matching what a real request would send.
            self._mock.with_headers(self._last_encoded_headers)
        return self

    def match_query(self, query: QueryMatcher) -> Self:
        self._mock.match_query(query)
        return self

    def match_query_param(self, name: str, value: Matcher) -> Self:
        self._mock.match_query_param(name, value)
        return self

    def match_header(self, name: str, value: Matcher) -> Self:
        self._mock.match_header(name, value)
        return self

    def match_body_json(self, matcher: JsonMatcher) -> Self:
        self._mock.match_body_json(matcher)
        return self

    def match_request(self, matcher: CustomMatcher) -> Self:
        self._mock.match_request(_wrap_custom_matcher(matcher))
        return self

    def match_request_with_response(self, handler: CustomHandler) -> Self:
        self._commit("handler")
        self._mock.match_request_with_response(_wrap_custom_handler(handler))
        return self

    def assert_called(
        self,
        *,
        count: int | None = None,
        min_count: int | None = None,
        max_count: int | None = None,
    ) -> None:
        self._mock.assert_called(count=count, min_count=min_count, max_count=max_count)

    def get_requests(self) -> list[MockRequest]:
        # pylint can't infer through _MockTyping's Protocol stub here (confirmed a false
        # positive: all four type checkers agree this is list[Request], and it iterates fine at
        # runtime — see test_get_requests_returns_mock_request_snapshots).
        return [_mock_request_from(r) for r in self._mock.get_requests()]  # pylint: disable=not-an-iterable

    def get_call_count(self) -> int:
        return self._mock.get_call_count()

    def reset_requests(self) -> None:
        self._mock.reset_requests()


@dataclass
class LOTHCMocker:
    """Mocks HTTP requests made by any `HTTPClient`/`SyncHTTPClient` for the duration of a test.
    Get one via the `lothc_mocker` fixture (requires the `testing` extra)."""

    _client_mocker: _ClientMockerTyping

    def _add_response(
        self,
        verb: Callable[..., _MockTyping],
        *,
        path: PathMatcher | None,
        url: UrlMatcher | None,
        params: Params | None,
        data: Data,
        headers: Headers | None,
        status: int,
    ) -> LOTHCMock:
        # Encode everything that can raise BEFORE calling `verb(...)` — pyreqwest registers a
        # `Mock` the instant that call returns, so raising after it would leave a bare,
        # unconfigured `Mock` behind: its response builder defaults to status 200/empty body, so
        # it would silently match ANY later request to this method/path (confirmed live) — not
        # "no mock got registered", which is what a caller raised past would expect. The encoded
        # results are applied directly below (bypassing `LOTHCMock.with_data`/`with_headers`,
        # which would otherwise re-derive them a second time) rather than discarded — avoids
        # wasted work, and means the mock is provably built from the exact values validated here.
        query_matches = (
            _query_param_match_values(_encode_params(params)) if params is not None else None
        )
        encoded_headers = _encode_headers(headers) if headers is not None else None
        encoded_data: bytes | Any = data if isinstance(data, bytes) else _encode_json_payload(data)

        mock = verb(path=path, url=url)
        if query_matches is not None:
            for name, value in query_matches.items():
                mock.match_query_param(name, value)
        mock.with_status(status)
        # `data` before `headers` — see `_apply_mock_response`'s comment for why.
        if isinstance(data, bytes):
            mock.with_body_bytes(encoded_data)
        else:
            mock.with_body_json(encoded_data)
        if encoded_headers is not None:
            mock.with_headers(encoded_headers)
        return LOTHCMock(mock, _committed="response", _last_encoded_headers=encoded_headers)

    def mock(
        self,
        method: MethodMatcher | None = None,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
    ) -> LOTHCMock:
        """Register a bare mock rule with no canned response yet — the low-level escape hatch for
        a `match_request_with_response` custom handler, or manual `with_status`/`with_data`
        building, beyond what the `add_*_response` methods below cover."""
        return LOTHCMock(self._client_mocker.mock(method, path=path, url=url))

    def add_get_response(
        self,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
        params: Params | None = None,
        data: Data = b"",
        headers: Headers | None = None,
        status: int = 200,
    ) -> LOTHCMock:
        return self._add_response(
            self._client_mocker.get,
            path=path,
            url=url,
            params=params,
            data=data,
            headers=headers,
            status=status,
        )

    def add_post_response(
        self,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
        params: Params | None = None,
        data: Data = b"",
        headers: Headers | None = None,
        status: int = 200,
    ) -> LOTHCMock:
        return self._add_response(
            self._client_mocker.post,
            path=path,
            url=url,
            params=params,
            data=data,
            headers=headers,
            status=status,
        )

    def add_put_response(
        self,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
        params: Params | None = None,
        data: Data = b"",
        headers: Headers | None = None,
        status: int = 200,
    ) -> LOTHCMock:
        return self._add_response(
            self._client_mocker.put,
            path=path,
            url=url,
            params=params,
            data=data,
            headers=headers,
            status=status,
        )

    def add_patch_response(
        self,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
        params: Params | None = None,
        data: Data = b"",
        headers: Headers | None = None,
        status: int = 200,
    ) -> LOTHCMock:
        return self._add_response(
            self._client_mocker.patch,
            path=path,
            url=url,
            params=params,
            data=data,
            headers=headers,
            status=status,
        )

    def add_delete_response(
        self,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
        params: Params | None = None,
        data: Data = b"",
        headers: Headers | None = None,
        status: int = 200,
    ) -> LOTHCMock:
        return self._add_response(
            self._client_mocker.delete,
            path=path,
            url=url,
            params=params,
            data=data,
            headers=headers,
            status=status,
        )

    def add_head_response(
        self,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
        params: Params | None = None,
        headers: Headers | None = None,
        status: int = 200,
    ) -> LOTHCMock:
        # No `data=` — lothc's `head()` never decodes a body (`Result[None]`, see `_client.py`).
        return self._add_response(
            self._client_mocker.head,
            path=path,
            url=url,
            params=params,
            data=b"",
            headers=headers,
            status=status,
        )

    def strict(self, *, enabled: bool = True) -> Self:
        self._client_mocker.strict(enabled=enabled)
        return self

    def get_requests(self) -> list[MockRequest]:
        # Same pylint Protocol-inference false positive as LOTHCMock.get_requests above.
        return [_mock_request_from(r) for r in self._client_mocker.get_requests()]  # pylint: disable=not-an-iterable

    def get_call_count(self) -> int:
        return self._client_mocker.get_call_count()

    def clear(self) -> None:
        self._client_mocker.clear()

    def reset_requests(self) -> None:
        self._client_mocker.reset_requests()


def pytest_configure(config: pytest.Config) -> None:  # pyright: ignore[reportUnusedFunction]
    config.addinivalue_line(
        "markers",
        "lothc_mocker(strict=True): configure the lothc_mocker fixture for this test "
        "(e.g. @pytest.mark.lothc_mocker(strict=False) to allow requests with no matching mock "
        "to fall through to the real network instead of raising AssertionError).",
    )


def _strict_marker_value(request: pytest.FixtureRequest) -> bool:
    # `FixtureRequest.node`'s property has no return annotation in pytest's own stubs, leaving it
    # `Unknown` under basedpyright strict — `pytest.Item` (a `Node` subclass) is the real runtime
    # type and is what gives `get_closest_marker` its properly-typed `Mark | None` return.
    node = cast("pytest.Item", request.node)
    marker = node.get_closest_marker("lothc_mocker")
    if marker is None:
        return True
    if marker.args and "strict" in marker.kwargs:
        raise TypeError(
            "@pytest.mark.lothc_mocker: pass strict= either positionally "
            "(@pytest.mark.lothc_mocker(False)) or as a keyword "
            "(@pytest.mark.lothc_mocker(strict=False)), not both."
        )
    # @pytest.mark.lothc_mocker(False) — positional, not just the documented strict= keyword.
    value = marker.args[0] if marker.args else marker.kwargs.get("strict", True)
    if not isinstance(value, bool):
        raise TypeError(f"@pytest.mark.lothc_mocker's strict value must be a bool, got {value!r}.")
    return value


@pytest.fixture(name="lothc_mocker")
def _lothc_mocker(  # pyright: ignore[reportUnusedFunction]
    client_mocker: ClientMocker, request: pytest.FixtureRequest
) -> LOTHCMocker:
    """Only ever resolved by pytest via fixture-name matching, never called directly. Strict by
    default (unlike pyreqwest's own `client_mocker`, which defaults to passthrough) — opt out
    per-test with `@pytest.mark.lothc_mocker(strict=False)`, or call `.strict(enabled=False)` on
    the returned `LOTHCMocker` mid-test."""
    mocker = LOTHCMocker(cast(_ClientMockerTyping, client_mocker))
    mocker.strict(enabled=_strict_marker_value(request))
    return mocker
