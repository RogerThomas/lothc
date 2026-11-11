"""`lothc.testing`'s mock engine, shared by both transports: the mocks a test registers, and what
becomes of each request a client sends while a `lothc_mocker` fixture has them installed.

Each transport hands a request over as the client built it (URL, `default_headers`, auth,
`headers=`, a `json=`/`content=` body), before its sending adds anything: pyreqwest's adds
`_MockMiddleware` to each request it builds (`_transport_pyreqwest._mock_hook`), and the native
pools hand each one to `_NativeMockHook` (`lothc._native.set_mock_hook`). The first mock, in the
order they were registered, that matches it answers it; with none, it's sent for real, or, while
strict, fails with an `AssertionError`, which nothing retries.
"""

import asyncio
import copy
import inspect
import json
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from re import Pattern
from typing import Any

from pyreqwest.http import Url
from pyreqwest.middleware import Next, SyncNext
from pyreqwest.request import Request
from pyreqwest.response import Response as RawResponse
from pyreqwest.response import ResponseBuilder
from pyreqwest.response import SyncResponse as RawSyncResponse

from ._compat import BaseModel, Struct, msgspec
from ._encode import _encode_headers, _pydantic_by_alias
from ._headers import CaseInsensitiveDict
from ._native import Intercepted, RawHeaders, Stream, SyncIntercepted, SyncStream, set_mock_hook
from ._response import RequestInfo, Response
from ._transport_pyreqwest import _mock_hook
from ._types import Data, Headers, JSONPayload

type Matcher = str | Pattern[str]
type MethodMatcher = Matcher
type PathMatcher = Matcher
type UrlMatcher = Matcher
type QueryMatcher = dict[str, Matcher | list[str]] | Matcher
type JsonMatcher = Any


@dataclass(slots=True)
class MockRequest:
    """The request a `match_request`/`match_request_with_response` handler receives, or
    `get_requests()` returns: lothc's own snapshot (`method`/`path`/`query_string`/`query`/
    `headers`/`body`) of the request as the client built it, the same on either transport: auth,
    `default_headers` and `headers=` included, but none of the headers the transport adds while
    sending (`accept`, `user-agent`, `accept-encoding`, cookies, `host`), and with `body` the
    bytes of a `json=`/`content=` body (`None` without one, and for `data=`/`form=`, whose bodies
    the transport encodes itself).

    Deliberately not `frozen=True`: `headers` holds a mutable mapping, which a frozen dataclass
    can't make genuinely immutable or hashable anyway (confirmed live — `frozen=True` here still
    let `.headers[...] = ...` mutate in place, and `hash(...)` still raised from deep inside
    dataclass machinery); a plain mutable dataclass is the honest shape, matching
    `hash(MockRequest(...))` raising a direct, expected `TypeError` instead. `headers` is a
    `CaseInsensitiveDict`, matching `Response.headers` (see `_headers.py`), so
    `headers["Content-Type"]` and `headers["content-type"]` are one lookup and a repeated header
    keeps every value — indexing gives the first, `get_all` gives all of them. `query` is
    `query_string` already parsed into a single-value-per-key shape, so unlike `headers`, a
    genuinely repeated query key really does lose everything but its first value. `query_string`
    stays alongside it (not replaced) for anyone who wants the raw, unparsed string — e.g. to
    match it with a regex. Each handler and each `get_requests()` call gets its own copy, so a
    handler that changes one changes nothing else."""

    method: str
    path: str
    query_string: str
    query: Mapping[str, str]
    headers: CaseInsensitiveDict
    body: bytes | None


@dataclass(slots=True)
class MockResponse:
    """The response a `match_request_with_response` custom handler returns: lothc's own plain
    description (`data`/`headers`/`status`, encoded the same way `add_*_response` encodes them),
    never pyreqwest's response types. `slots=True` per `style-guide.md` §7 (a DTO, like its
    sibling `MockRequest`), and not `frozen=True` for `MockRequest`'s reason: `data`/`headers`
    can hold a mutable `dict`."""

    data: Data = b""
    headers: Headers | None = None
    status: int = 200


type CustomMatcher = Callable[[MockRequest], Awaitable[bool]] | Callable[[MockRequest], bool]
type CustomHandler = (
    Callable[[MockRequest], Awaitable[MockResponse | None]]
    | Callable[[MockRequest], MockResponse | None]
)


@dataclass(slots=True, eq=False)
class _Rule:
    """One mock: what a request must look like for it to answer, and its answer (`handler`'s, if
    it has one, else `response`). `LOTHCMock` (`lothc/testing.py`) fills it in. Compared and
    hashed by identity, so it can key `_Incoming.passed_on`."""

    method: MethodMatcher | None
    path: PathMatcher | None
    url: UrlMatcher | None
    query: Matcher | None = None
    # A `list` matches every value of a repeated key, in order; anything else, a key sent once.
    query_params: dict[str, Matcher | list[str]] = field(
        default_factory=dict[str, Matcher | list[str]]
    )
    # Lowercase names; one of the request's values for that name must match.
    headers: dict[str, Matcher] = field(default_factory=dict[str, Matcher])
    json_bodies: list[JsonMatcher] = field(default_factory=list[JsonMatcher])
    matchers: list[CustomMatcher] = field(default_factory=list[CustomMatcher])
    response: MockResponse = field(default_factory=MockResponse)
    handler: CustomHandler | None = None


@dataclass(slots=True, eq=False)
class _Incoming:
    """A request as every mock sees it, on either transport, and what became of it."""

    method: str
    url: Url
    headers: list[tuple[str, str]]
    body: bytes | None
    answered_by: _Rule | None = None
    # Why a mock that matched the rest of it declined: a custom matcher or handler's verdict.
    passed_on: dict[_Rule, str] = field(default_factory=dict[_Rule, str])


def _first_value_per_key(values: Mapping[str, str | list[str]]) -> dict[str, str]:
    """Collapses one of pyreqwest's `query_dict_multi_value` results (a plain `str`, or a
    `list[str]` for a genuinely repeated key) down to one value per key, first value wins.

    Note this is *not* what headers do: `MockRequest.headers`/`Response.headers` are a
    `CaseInsensitiveDict` and keep every value of a repeated name (see `_headers.py`). Query params
    still collapse, so a repeated key's extra values are genuinely dropped here — `query_string`
    stays alongside `query` for anyone who needs them."""
    return {name: value if isinstance(value, str) else value[0] for name, value in values.items()}


def _mock_request(incoming: _Incoming) -> MockRequest:
    url = incoming.url
    return MockRequest(
        incoming.method,
        url.path,
        url.query_string or "",
        _first_value_per_key(url.query_dict_multi_value),
        CaseInsensitiveDict(incoming.headers),
        incoming.body,
    )


def _text_matches(expected: Matcher, actual: str) -> bool:
    if isinstance(expected, str):
        return expected == actual
    return expected.search(actual) is not None


def _shown(values: list[str]) -> str:
    match values:
        case []:
            return "none"
        case [value]:
            return repr(value)
        case _:
            return repr(values)


def _text_mismatch(part: str, expected: Matcher | None, actual: str) -> str | None:
    if expected is None or _text_matches(expected, actual):
        return None
    return f"{part}: expected {expected!r}, got {actual!r}"


def _query_mismatch(name: str, expected: Matcher | list[str], values: list[str]) -> str | None:
    if isinstance(expected, list):
        matched = values == expected
    else:
        matched = len(values) == 1 and _text_matches(expected, values[0])
    return None if matched else f"query {name!r}: expected {expected!r}, got {_shown(values)}"


def _header_mismatch(name: str, expected: Matcher, values: list[str]) -> str | None:
    if any(_text_matches(expected, value) for value in values):
        return None
    return f"header {name!r}: expected {expected!r}, got {_shown(values)}"


def _json_mismatch(expected: JsonMatcher, body: bytes | None) -> str | None:
    if body is None:
        return f"JSON body: expected {expected!r}, got no body"
    try:
        parsed = json.loads(body)
    except ValueError:
        return f"JSON body: expected {expected!r}, got a body that isn't JSON"
    return None if parsed == expected else f"JSON body: expected {expected!r}, got {parsed!r}"


def _mismatches(rule: _Rule, incoming: _Incoming) -> list[str]:
    """Every part of `incoming` that `rule` doesn't match, short of its custom matchers and
    handler (which may be async, so only a dispatch runs them): empty if it matches."""
    url = incoming.url
    query = {
        name: [value] if isinstance(value, str) else value
        for name, value in url.query_dict_multi_value.items()
    }
    headers = CaseInsensitiveDict(incoming.headers)
    found = [
        _text_mismatch("method", rule.method, incoming.method),
        _text_mismatch("path", rule.path, url.path),
        _text_mismatch("URL", rule.url, str(url)),
        _text_mismatch("query", rule.query, url.query_string or ""),
        *(
            _query_mismatch(name, expected, query.get(name, []))
            for name, expected in rule.query_params.items()
        ),
        *(
            _header_mismatch(name, expected, headers.get_all(name))
            for name, expected in rule.headers.items()
        ),
        *(_json_mismatch(expected, incoming.body) for expected in rule.json_bodies),
    ]
    return [mismatch for mismatch in found if mismatch is not None]


def _reasons(rule: _Rule, incoming: _Incoming) -> list[str]:
    """Why `rule` didn't answer `incoming`."""
    if mismatches := _mismatches(rule, incoming):
        return mismatches
    if rule in incoming.passed_on:
        return [incoming.passed_on[rule]]
    if incoming.answered_by is not None:
        return ["another mock answered it first"]
    return ["it was sent before this mock was registered"]


def _reason_count(rule: _Rule, incoming: _Incoming) -> int:
    return len(_reasons(rule, incoming))


def _described(rule: _Rule) -> str:
    """`rule` as it was registered, e.g. `GET path='/items'`."""
    if isinstance(rule.method, str):
        parts = [rule.method]
    else:
        parts = [] if rule.method is None else [f"method={rule.method!r}"]
    if rule.path is not None:
        parts.append(f"path={rule.path!r}")
    if rule.url is not None:
        parts.append(f"url={rule.url!r}")
    return " ".join(parts) or "for any request"


def _request_line(incoming: _Incoming) -> str:
    return f"{incoming.method} {incoming.url}"


def _indented(reasons: list[str]) -> str:
    return "".join(f"\n  {reason}" for reason in reasons)


def _plural(count: int) -> str:
    return f"{count} request" if count == 1 else f"{count} requests"


@dataclass(slots=True, eq=False, repr=False)
class _Engine:
    """Every mock a `LOTHCMocker` registered, in order, and every request it was handed while
    installed (answered or not, for `assert_called`'s message). Requests can come from any
    thread, so recording one takes the lock; a dispatch reads a snapshot of the rules."""

    strict: bool
    rules: list[_Rule] = field(default_factory=list[_Rule])
    requests: list[_Incoming] = field(default_factory=list[_Incoming])
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def candidates(self, incoming: _Incoming) -> Iterator[_Rule]:
        """The mocks `incoming` matches short of their custom matchers and handlers, in order."""
        return (rule for rule in tuple(self.rules) if not _mismatches(rule, incoming))

    def settle(self, incoming: _Incoming, rule: _Rule | None) -> None:
        """Record that `rule` answered `incoming`, or, with `None`, that no mock did: an
        `AssertionError` while strict."""
        incoming.answered_by = rule
        with self._lock:
            self.requests.append(incoming)
        if rule is not None or not self.strict:
            return
        message = f"No mock answered {_request_line(incoming)}"
        if self.rules:
            closest = min(self.rules, key=partial(_reason_count, incoming=incoming))
            message += f"\nThe closest mock, {_described(closest)}:"
            message += _indented(_reasons(closest, incoming))
        message += (
            "\nlothc_mocker is strict: mock the request, or let unmocked requests through with "
            "@pytest.mark.lothc_mocker(strict=False)."
        )
        raise AssertionError(message)

    def answered(self, rule: _Rule | None = None) -> list[_Incoming]:
        """The requests `rule` answered, or, by default, that any of the current mocks did, in
        the order they were sent (a mock `clear()` removed still has its own)."""
        with self._lock:
            if rule is not None:
                return [incoming for incoming in self.requests if incoming.answered_by is rule]
            return [incoming for incoming in self.requests if incoming.answered_by in self.rules]

    def forget(self, rule: _Rule | None = None) -> None:
        """Forget the requests `rule` answered, or, by default, every request."""
        with self._lock:
            if rule is None:
                self.requests = []
            else:
                self.requests = [r for r in self.requests if r.answered_by is not rule]

    def assert_called(
        self, rule: _Rule, count: int | None, min_count: int | None, max_count: int | None
    ) -> None:
        """That `rule` answered as many requests as every bound given allows (exactly one with
        none given)."""
        if count is None and min_count is None and max_count is None:
            count = 1
        with self._lock:
            answered = [incoming for incoming in self.requests if incoming.answered_by is rule]
            others = [incoming for incoming in self.requests if incoming.answered_by is not rule]
        calls = len(answered)
        too_few = calls < (count or 0) or calls < (min_count or 0)
        too_many = any(bound is not None and calls > bound for bound in (count, max_count))
        if not too_few and not too_many:
            return
        bounds = (("exactly", count), ("at least", min_count), ("at most", max_count))
        expected = " and ".join(f"{word} {bound}" for word, bound in bounds if bound is not None)
        message = f"The mock {_described(rule)} answered {_plural(calls)}, expected {expected}."
        if not too_few:
            message += "\nIt answered:" + _indented([_request_line(r) for r in answered])
        elif others:
            # The fewest mismatched parts; of those, the latest.
            closest = min(reversed(others), key=partial(_reason_count, rule))
            message += f"\nThe closest request it didn't answer, {_request_line(closest)}:"
            message += _indented(_reasons(rule, closest))
        else:
            message += "\nNo other request was sent."
        raise AssertionError(message)


def _callable_name(function: Callable[..., object]) -> str:
    return getattr(function, "__qualname__", None) or type(function).__qualname__


async def _resolved[T](value: Awaitable[T] | T) -> T:
    """A matcher's or handler's result, awaited if it's an `async def`'s."""
    if inspect.isawaitable(value):
        return await value
    return value


def _resolved_sync[T](value: Awaitable[T] | T) -> T:
    """A matcher's or handler's result, which a sync client can't await."""
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise TypeError(
            "An async match_request/match_request_with_response function can't answer a "
            "SyncHTTPClient's request, which can't await it: give that mock a plain function."
        )
    return value


async def _answer(rule: _Rule, incoming: _Incoming) -> MockResponse | None:
    """`rule`'s response to `incoming`, which matches the rest of it, or `None` if one of its
    custom matchers or its handler declines."""
    for matcher in rule.matchers:
        if not await _resolved(matcher(_mock_request(incoming))):
            incoming.passed_on[rule] = f"match_request({_callable_name(matcher)}) returned False"
            return None
    if rule.handler is None:
        return rule.response
    response = await _resolved(rule.handler(_mock_request(incoming)))
    if response is None:
        name = _callable_name(rule.handler)
        incoming.passed_on[rule] = f"match_request_with_response({name}) returned None"
    return response


def _answer_sync(rule: _Rule, incoming: _Incoming) -> MockResponse | None:
    for matcher in rule.matchers:
        if not _resolved_sync(matcher(_mock_request(incoming))):
            incoming.passed_on[rule] = f"match_request({_callable_name(matcher)}) returned False"
            return None
    if rule.handler is None:
        return rule.response
    response = _resolved_sync(rule.handler(_mock_request(incoming)))
    if response is None:
        name = _callable_name(rule.handler)
        incoming.passed_on[rule] = f"match_request_with_response({name}) returned None"
    return response


async def _dispatch(engine: _Engine, incoming: _Incoming) -> MockResponse | None:
    """The first matching mock's response to `incoming`, or `None` to send it for real."""
    for rule in engine.candidates(incoming):
        response = await _answer(rule, incoming)
        if response is not None:
            engine.settle(incoming, rule)
            return response
    engine.settle(incoming, None)
    return None


def _dispatch_sync(engine: _Engine, incoming: _Incoming) -> MockResponse | None:
    for rule in engine.candidates(incoming):
        response = _answer_sync(rule, incoming)
        if response is not None:
            engine.settle(incoming, rule)
            return response
    engine.settle(incoming, None)
    return None


def _snapshot(data: Data) -> Data:
    """`data` as it is now, for a canned response: changing the caller's own `dict` or model
    later changes no mock."""
    return data if isinstance(data, bytes) else copy.deepcopy(data)


def _encode_json_payload(payload: JSONPayload) -> Any:  # noqa: ANN401
    """Any `JSONPayload` as plain JSON values, for `ResponseBuilder.body_json()`."""
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json", by_alias=_pydantic_by_alias(payload))
    if isinstance(payload, Struct):
        return msgspec.to_builtins(payload)
    return payload


def _response_builder(response: MockResponse) -> ResponseBuilder:
    """A mock's response, built as pyreqwest builds one (either transport answers with it)."""
    builder = ResponseBuilder().status(response.status)
    # `data` before `headers`: `.body_json()` appends a Content-Type, which a caller's own then
    # replaces (`.headers()` replaces a same-named value) rather than going out beside it.
    if isinstance(response.data, bytes):
        builder = builder.body_bytes(response.data)
    else:
        builder = builder.body_json(_encode_json_payload(response.data))
    if response.headers is not None:
        builder = builder.headers(_encode_headers(response.headers))
    return builder


def _response_parts(response: MockResponse) -> tuple[int, list[tuple[str, str]], bytes]:
    """A mock's response as the native pools take one: status, header pairs, body."""
    built = _response_builder(response).build_sync()
    return built.status, list(built.headers.items()), built.bytes().to_bytes()


def _incoming_from_pyreqwest(request: Request) -> _Incoming:
    body = request.body
    # `None` for a streamed body: lothc only streams `form=`, which pyreqwest hands reqwest unseen.
    raw = None if body is None else body.copy_bytes()
    body_bytes = None if raw is None else raw.to_bytes()
    return _Incoming(request.method, request.url, list(request.headers.items()), body_bytes)


@dataclass(slots=True)
class _MockMiddleware:
    """What the pyreqwest transport adds to each async request while the mocker is installed."""

    _engine: _Engine

    async def __call__(self, request: Request, next: Next) -> RawResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        response = await _dispatch(self._engine, _incoming_from_pyreqwest(request))
        if response is None:
            return await next.run(request)
        return await _response_builder(response).build()


@dataclass(slots=True)
class _SyncMockMiddleware:
    """`_MockMiddleware` for sync requests."""

    _engine: _Engine

    def __call__(self, request: Request, next: SyncNext) -> RawSyncResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        response = _dispatch_sync(self._engine, _incoming_from_pyreqwest(request))
        if response is None:
            return next.run(request)
        return _response_builder(response).build_sync()


def _incoming_from_native(intercepted: Intercepted | SyncIntercepted) -> _Incoming:
    return _Incoming(
        intercepted.method, Url(intercepted.url), intercepted.headers, intercepted.body
    )


async def _native_answer(engine: _Engine, intercepted: Intercepted) -> Response[Any, Any]:
    """What a mocked request on the async native transport resolves to: a mock's response,
    through the pool's own response path, or the request sent for real."""
    response = await _dispatch(engine, _incoming_from_native(intercepted))
    if response is None:
        fut = intercepted.send()
        try:
            return await fut
        except asyncio.CancelledError:
            intercepted.cancel(fut)
            raise
    return intercepted.respond(*_response_parts(response))


def _native_answer_sync(engine: _Engine, intercepted: SyncIntercepted) -> Response[Any, Any]:
    response = _dispatch_sync(engine, _incoming_from_native(intercepted))
    if response is None:
        return intercepted.send()
    return intercepted.respond(*_response_parts(response))


@dataclass(slots=True, eq=False)
class _NativeMockStream:
    """What the async native pool's `stream` returns while the mocker is installed, with
    `Stream`'s methods: a mock's response, its body read in one chunk, or, if no mock answered,
    the request streamed for real."""

    _engine: _Engine
    _intercepted: Intercepted
    _passed: Stream | None = None
    _body: bytes | None = None

    async def _open(self) -> tuple[int, RawHeaders, str]:
        response = await _dispatch(self._engine, _incoming_from_native(self._intercepted))
        if response is None:
            stream = self._passed = self._intercepted.stream()
            return await stream.open()
        status, headers, body = _response_parts(response)
        self._body = body or None
        return self._intercepted.head(status, headers)

    @property
    def request_info(self) -> RequestInfo:
        return self._intercepted.request_info

    def open(self) -> asyncio.Future[tuple[int, RawHeaders, str]]:
        return asyncio.ensure_future(self._open())

    def read(self) -> asyncio.Future[bytes | None]:
        if self._passed is not None:
            return self._passed.read()
        read: asyncio.Future[bytes | None] = asyncio.get_running_loop().create_future()
        read.set_result(self._body)
        self._body = None
        return read

    def close(self) -> None:
        if self._passed is not None:
            self._passed.close()


@dataclass(slots=True, eq=False)
class _SyncNativeMockStream:
    """`_NativeMockStream` for the sync native pool, with `SyncStream`'s methods."""

    _engine: _Engine
    _intercepted: SyncIntercepted
    _passed: SyncStream | None = None
    _body: bytes | None = None

    def _open(self) -> tuple[int, RawHeaders, str]:
        response = _dispatch_sync(self._engine, _incoming_from_native(self._intercepted))
        if response is None:
            stream = self._passed = self._intercepted.stream()
            return stream.open()
        status, headers, body = _response_parts(response)
        self._body = body or None
        return self._intercepted.head(status, headers)

    @property
    def request_info(self) -> RequestInfo:
        return self._intercepted.request_info

    def open(self) -> tuple[int, RawHeaders, str]:
        return self._open()

    def read(self) -> bytes | None:
        if self._passed is not None:
            return self._passed.read()
        body, self._body = self._body, None
        return body

    def close(self) -> None:
        if self._passed is not None:
            self._passed.close()


@dataclass(slots=True)
class _NativeMockHook:
    """What the native pools hand every request to while the mocker is installed
    (`lothc._native.set_mock_hook`)."""

    _engine: _Engine

    def request(self, intercepted: Intercepted, /) -> asyncio.Future[Response[Any, Any]]:
        return asyncio.ensure_future(_native_answer(self._engine, intercepted))

    def stream(self, intercepted: Intercepted, /) -> _NativeMockStream:
        return _NativeMockStream(self._engine, intercepted)

    def request_sync(self, intercepted: SyncIntercepted, /) -> Response[Any, Any]:
        return _native_answer_sync(self._engine, intercepted)

    def stream_sync(self, intercepted: SyncIntercepted, /) -> _SyncNativeMockStream:
        return _SyncNativeMockStream(self._engine, intercepted)


def _install(engine: _Engine | None) -> None:
    """Hand every request either transport sends to `engine` from now on (`None`: send them all
    for real again)."""
    _mock_hook.middleware = None if engine is None else _MockMiddleware(engine)
    _mock_hook.sync_middleware = None if engine is None else _SyncMockMiddleware(engine)
    set_mock_hook(None if engine is None else _NativeMockHook(engine))
