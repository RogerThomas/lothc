"""pytest plugin for mocking `HTTPClient`/`SyncHTTPClient` requests — the `lothc_mocker` fixture,
auto-registered via this module's `pytest11` entry point (see `pyproject.toml`). It loads in any
environment that has pytest, whether or not the `lothc[testing]` extra is installed; the extra only
pins the pytest version it's tested against.

The fixture installs lothc's own mock engine (`_mocking.py`) for the duration of a test, and both
transports hand it every request any client sends meanwhile, however and whenever that client was
made, so a mock matches, answers and counts a request the same way on either transport.
`LOTHCMock`/`LOTHCMocker` fill in the engine's rules directly.

**No pyreqwest type is ever part of this module's public API — not even re-exported.** A custom
matcher/handler receives lothc's own `MockRequest` (a plain snapshot: `method`/`path`/
`query_string`/`query`/`headers`/`body`); `get_requests()` returns `list[MockRequest]`; `url=`
matching takes `str | re.Pattern[str]`; and `match_request_with_response` returns `MockResponse`,
a plain dataclass with no builder.

`params=`/`data=`/`headers=` all encode a value exactly as a real request would, so a mock
always matches/responds the same way: `params=` accepts lothc's `Params` union (via
`_encode_params`, shared with the transports — a `list[...]`/`tuple[...]` value meaning a
genuinely repeated query key, see `Params`'s own doc comment in `_types.py`), each key's match
value derived via `_query_param_match_values` from pyreqwest's own real encoder, not reimplemented
by hand (e.g. `True` becomes `"true"`, not Python's `str(True)` == `"True"`, confirmed live);
`data=` accepts lothc's `Data` union (`bytes | dict | BaseModel | Struct`); and `headers=` accepts
lothc's `Headers` union (`Mapping[str, str] | BaseModel | Struct`, via `_encode_headers`). The same
typed params/headers class used for a real request, or for a `response_headers_type=` decode on the
read side, can be constructed once and passed straight into `add_*_response(...)`. The shared
helpers keep their `_` prefix: it marks them private to the package, and `pyproject.toml` turns
basedpyright's `reportPrivateUsage` off inside `lothc/` only.
"""

from collections.abc import Generator, Mapping
from dataclasses import dataclass
from typing import Literal, Self, cast

import pytest
from pyreqwest.http import Url

from . import _report_as_public
from ._encode import _encode_headers, _encode_params, _query_pairs
from ._mocking import (
    CustomHandler,
    CustomMatcher,
    JsonMatcher,
    Matcher,
    MethodMatcher,
    MockRequest,
    MockResponse,
    PathMatcher,
    QueryMatcher,
    UrlMatcher,
    _Engine,
    _install,
    _mock_request,
    _Rule,
    _snapshot,
)
from ._types import Data, Headers, Params, _QueryValue

__all__ = ["LOTHCMock", "LOTHCMocker", "MockRequest", "MockResponse"]


def _query_param_match_values(params: Mapping[str, _QueryValue]) -> dict[str, str | list[str]]:
    """The exact value(s) each `params` key must have (a `list[str]` for a key whose `Params` value
    was itself a list/tuple — i.e. a genuinely repeated query key, see `Params`'s own doc comment
    in `_types.py` — a plain `str` otherwise), derived from pyreqwest's own real URL/query encoder
    (`Url.parse_with_params` + `.query_dict_multi_value`) rather than a hand-reimplementation of
    it — reimplementing risked silently diverging from pyreqwest for any type it encodes
    differently from Python's own `str()` (confirmed live for `bool`: `str(True)` == `"True"` vs
    pyreqwest's real `"true"`). This also means an invalid value (e.g. `None` — not a valid
    `Params` value, but nothing stops it arriving at runtime) raises the exact same `ValueError` a
    real request would, confirmed live, instead of silently registering a mock that a real call
    could never actually produce. A repeated key keeps every value, in order, so
    `params={"tag": ["a", "b"]}` doesn't match `tag=a` alone. The values go through the real
    request path's own `_query_pairs` first, so its adjustments apply here too: an `int` outside
    the signed 64-bit range is matched by its exact digits, as a request sends it, rather than the
    float `Url.parse_with_params` would make of it (which matched `2**64 + 1` against `2**64`).

    An empty `list`/`tuple` value is rejected explicitly, unlike every other value here — confirmed
    live that `Url.parse_with_params` silently drops a key entirely when its value is `[]`/`()`,
    which would leave the mock with nothing to check for that key: the mock silently matching ANY
    query string at all, the opposite of what a caller registering a `params=` narrowing would
    expect, and far worse to leave silent than a real request's own, more benign handling of an
    empty list (it simply omits the key)."""
    for name, value in params.items():
        if isinstance(value, list | tuple) and not value:
            raise ValueError(
                f"Invalid query value for {name!r}: an empty list/tuple has no query-string "
                "representation to narrow a mock against — it would register a mock that matches "
                "any query string at all instead of none. Omit the key from params= instead."
            )
    return Url.parse_with_params(
        "http://mock.invalid/", _query_pairs(params)
    ).query_dict_multi_value


@dataclass
class LOTHCMock:
    """A single registered mock, returned by every `LOTHCMocker.add_*_response`/`mock` call so it
    can be further narrowed with matchers, given a response, or asserted on afterwards. Every
    matcher narrows it: a request must satisfy all of them."""

    _engine: _Engine
    _rule: _Rule
    _committed: Literal["response", "handler"] | None = None
    """A canned response (`with_status`/`with_headers`/`with_data`, or the `add_*_response`
    methods that set one) and `match_request_with_response`'s custom handler are mutually
    exclusive on one mock, in either order. Every setter for either side goes through `_commit`,
    so the guard can't be bypassed by a future setter that forgets to check it."""

    def _commit(self, kind: Literal["response", "handler"]) -> None:
        if self._committed is not None and self._committed != kind:
            raise ValueError(
                "Cannot add a canned response (with_status/with_headers/with_data, or an "
                "add_*_response call) and a match_request_with_response custom handler to the "
                "same mock — they're mutually exclusive on one mock. Register a separate "
                "lothc_mocker.mock() for whichever side is missing."
            )
        self._committed = kind

    def with_status(self, status: int) -> Self:
        self._commit("response")
        self._rule.response.status = status
        self._rule.prepared = None
        return self

    def with_headers(self, headers: Headers) -> Self:
        self._commit("response")
        # Encoded now, into a copy, so changing the caller's own dict later changes nothing.
        self._rule.response.headers = _encode_headers(headers)
        self._rule.prepared = None
        return self

    def with_data(self, data: Data) -> Self:
        self._commit("response")
        self._rule.response.data = _snapshot(data)
        self._rule.prepared = None
        return self

    def match_query(self, query: QueryMatcher) -> Self:
        """A `dict` narrows on those keys only (any others may be there too); a `str` or
        pattern matches the whole query string."""
        if isinstance(query, dict):
            self._rule.query_params.update(query)
        else:
            self._rule.query = query
        return self

    def match_query_param(self, name: str, value: Matcher) -> Self:
        self._rule.query_params[name] = value
        return self

    def match_header(self, name: str, value: Matcher) -> Self:
        self._rule.headers[name.lower()] = value
        return self

    def match_body_json(self, matcher: JsonMatcher) -> Self:
        self._rule.json_bodies.append(matcher)
        return self

    def match_request(self, matcher: CustomMatcher) -> Self:
        self._rule.matchers.append(matcher)
        return self

    def match_request_with_response(self, handler: CustomHandler) -> Self:
        self._commit("handler")
        self._rule.handler = handler
        return self

    def assert_called(
        self,
        *,
        count: int | None = None,
        min_count: int | None = None,
        max_count: int | None = None,
    ) -> None:
        """That this mock answered exactly `count` requests, at least `min_count` and at most
        `max_count`, each bound given; exactly one with none."""
        self._engine.assert_called(self._rule, count, min_count, max_count)

    def get_requests(self) -> list[MockRequest]:
        return [_mock_request(incoming) for incoming in self._engine.answered(self._rule)]

    def get_call_count(self) -> int:
        return len(self._engine.answered(self._rule))

    def reset_requests(self) -> None:
        self._engine.forget(self._rule)


@dataclass
class LOTHCMocker:
    """Mocks HTTP requests made by any `HTTPClient`/`SyncHTTPClient` for the duration of a test.
    Get one via the `lothc_mocker` fixture. A request gets the response of the first mock, in the
    order they were registered, that matches it."""

    _engine: _Engine

    def _add_response(
        self,
        method: str,
        *,
        path: PathMatcher | None,
        url: UrlMatcher | None,
        params: Params | None,
        data: Data,
        headers: Headers | None,
        status: int,
    ) -> LOTHCMock:
        # Everything that can raise runs before the mock is registered, so a bad `params=` never
        # leaves a half-built mock behind, answering requests it was never meant to.
        encoded_headers = _encode_headers(headers) if headers is not None else None
        response = MockResponse(_snapshot(data), encoded_headers, status)
        rule = _Rule(method, path, url, response=response)
        if params is not None:
            rule.query_params.update(_query_param_match_values(_encode_params(params)))
        self._engine.rules.append(rule)
        return LOTHCMock(self._engine, rule, "response")

    def mock(
        self,
        method: MethodMatcher | None = None,
        *,
        path: PathMatcher | None = None,
        url: UrlMatcher | None = None,
    ) -> LOTHCMock:
        """Register a bare mock with no canned response yet (it answers a `200` with an empty
        body until given one) — the low-level escape hatch for a `match_request_with_response`
        custom handler, or manual `with_status`/`with_data` building, beyond what the
        `add_*_response` methods below cover. `path=` matches the URL's path and `url=` the whole
        URL, query included: a `str` exactly, a pattern anywhere in it (`re.search`)."""
        rule = _Rule(method, path, url)
        self._engine.rules.append(rule)
        return LOTHCMock(self._engine, rule)

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
            "GET",
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
            "POST",
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
            "PUT",
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
            "PATCH",
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
            "DELETE",
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
        # No `data=` — lothc's `head()` never decodes a body (`Response[None]`, see `_client.py`).
        return self._add_response(
            "HEAD",
            path=path,
            url=url,
            params=params,
            data=b"",
            headers=headers,
            status=status,
        )

    def strict(self, *, enabled: bool = True) -> Self:
        self._engine.strict = enabled
        return self

    def get_requests(self) -> list[MockRequest]:
        return [_mock_request(incoming) for incoming in self._engine.answered()]

    def get_call_count(self) -> int:
        return len(self._engine.answered())

    def clear(self) -> None:
        """Remove every mock (a `LOTHCMock` still held keeps the requests it answered)."""
        self._engine.rules.clear()

    def reset_requests(self) -> None:
        self._engine.forget()


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
    if len(marker.args) > 1:
        raise TypeError(
            "@pytest.mark.lothc_mocker takes at most one positional argument (strict), got "
            f"{len(marker.args)}: {marker.args!r}."
        )
    unknown_kwargs = marker.kwargs.keys() - {"strict"}
    if unknown_kwargs:
        raise TypeError(
            f"@pytest.mark.lothc_mocker got unexpected keyword argument(s): "
            f"{sorted(unknown_kwargs)}. The only recognized keyword is strict=."
        )
    # @pytest.mark.lothc_mocker(False) — positional, not just the documented strict= keyword.
    value = marker.args[0] if marker.args else marker.kwargs.get("strict", True)
    if not isinstance(value, bool):
        raise TypeError(f"@pytest.mark.lothc_mocker's strict value must be a bool, got {value!r}.")
    return value


@pytest.fixture(name="lothc_mocker")
def _lothc_mocker(  # pyright: ignore[reportUnusedFunction]
    request: pytest.FixtureRequest,
) -> Generator[LOTHCMocker]:
    """Only ever resolved by pytest via fixture-name matching, never called directly. Strict by
    default — opt out per-test with `@pytest.mark.lothc_mocker(strict=False)`, or call
    `.strict(enabled=False)` on the returned `LOTHCMocker` mid-test."""
    engine = _Engine(_strict_marker_value(request))
    _install(engine)
    try:
        yield LOTHCMocker(engine)
    finally:
        _install(None)


# Defined with the engine, which hands them to handlers, but imported from here.
_report_as_public(globals(), ["MockRequest", "MockResponse"], __name__)
