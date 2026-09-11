from typing import Any, cast

import pytest
from msgspec import Struct
from pydantic import BaseModel
from pyreqwest.pytest_plugin.mock import ClientMocker

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient
from lothc.testing import LOTHCMocker, MockRequest, MockResponse

# Every method `_MockTyping`/`_ClientMockerTyping` (lothc/testing.py) declare against pyreqwest's
# real `Mock`/`ClientMocker` — kept in sync manually so a future pyreqwest rename/removal fails
# this test loudly instead of surfacing as a runtime AttributeError the first time a consumer
# hits the changed method (the `cast` at the fixture boundary hides this from type checkers).
_MOCK_METHODS = (
    "with_status",
    "with_headers",
    "with_body_bytes",
    "with_body_json",
    "match_query",
    "match_query_param",
    "match_header",
    "match_body_json",
    "match_request",
    "match_request_with_response",
    "assert_called",
    "get_requests",
    "get_call_count",
    "reset_requests",
)
_CLIENT_MOCKER_METHODS = (
    "mock",
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "strict",
    "get_requests",
    "get_call_count",
    "clear",
    "reset_requests",
)


class ItemModel(BaseModel):
    id: int
    name: str


class ItemStruct(Struct):
    id: int
    name: str


class ItemHeaders(BaseModel):
    x_total_count: int


async def test_add_get_response_returns_raw_bytes_by_default(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b'{"id": 7, "name": "item-7"}')

    body = await client.get("items/7")

    assert body == b'{"id": 7, "name": "item-7"}'


async def test_add_get_response_encodes_pydantic_model(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=ItemModel(id=7, name="item-7"))

    item = await client.get("items/7", response_data_type=ItemModel)

    assert item == ItemModel(id=7, name="item-7")


async def test_add_get_response_encodes_msgspec_struct(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=ItemStruct(id=7, name="item-7"))

    item = await client.get("items/7", response_data_type=ItemStruct)

    assert item == ItemStruct(id=7, name="item-7")


async def test_add_post_response_encodes_dict(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/items", data={"id": 7, "name": "item-7"})

    item = await client.post("items", response_data_type=dict)

    assert item == {"id": 7, "name": "item-7"}


async def test_add_get_response_with_status_raises_http_response_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", status=404, data=b"not found")

    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("items/7")

    assert exc_info.value.status == 404
    assert exc_info.value.body_start == b"not found"


async def test_add_get_response_with_headers(client: HTTPClient, lothc_mocker: LOTHCMocker) -> None:
    lothc_mocker.add_get_response(path="/items/7", headers={"x-custom": "header-value"})

    result = await client.get_result("items/7")

    assert result.headers["x-custom"] == "header-value"


async def test_add_get_response_with_data_and_headers_together(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """`add_*_response` always applies `data=` before `headers=` internally (see `_add_response`'s
    own comment) so a caller's own `headers={"content-type": ...}` wins over `.body_json()`'s
    default Content-Type rather than leaving two Content-Type values on the wire — this is the
    same invariant `test_with_headers_then_with_data_content_type_not_duplicated` exercises for
    the chainable `with_headers`/`with_data` API, covered here for `add_*_response`'s own (data-
    then-headers) call order instead, since neither call order was previously exercised together
    with both `data=` and `headers=` passed in the same call."""
    lothc_mocker.add_get_response(
        path="/items/7", data={"id": 7}, headers={"content-type": "text/plain"}
    )

    result = await client.get_result("items/7", response_data_type=dict)

    assert result.data == {"id": 7}
    assert result.headers["content-type"] == "text/plain"


async def test_add_get_response_matches_on_params(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    matching = lothc_mocker.add_get_response(path="/items", params={"page": "2"}, data={"page": 2})
    other_page = lothc_mocker.add_get_response(
        path="/items", params={"page": "1"}, data={"page": 1}
    )

    item = await client.get("items", params={"page": "2"}, response_data_type=dict)

    assert item == {"page": 2}
    matching.assert_called(count=1)
    other_page.assert_called(count=0)


async def test_add_get_response_matches_on_bool_param(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """pyreqwest encodes a bool query param as lowercase `true`/`false`, not Python's
    `str(True)` == `"True"` — confirmed live against a real `RequestBuilder.query()` call."""
    lothc_mocker.add_get_response(path="/items", params={"flag": True}, data={"ok": True})

    item = await client.get("items", params={"flag": True}, response_data_type=dict)

    assert item == {"ok": True}


def test_add_get_response_with_none_param_raises_like_a_real_request(
    lothc_mocker: LOTHCMocker,
) -> None:
    """`None` isn't a valid `Params` value (`Params` is `Mapping[str, str | int | float | bool] |
    BaseModel | Struct`), but nothing stops it arriving at runtime — a real request rejects it
    (`ValueError` from pyreqwest's own query encoder), so mock registration must too, rather than
    silently registering a mock a real call could never actually produce."""
    with pytest.raises(ValueError, match="Invalid query value"):
        lothc_mocker.add_get_response(path="/items", params=cast(Any, {"flag": None}))


async def test_add_get_response_raising_does_not_register_an_orphaned_mock(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """A `Mock` is registered with pyreqwest the instant the verb method (`.get()`/etc.) is
    called — if `add_get_response` raised *after* that (e.g. from invalid `params=`), the bare,
    unconfigured `Mock` would be left behind, silently matching ANY later request to this
    method/path (its response builder defaults to status 200/empty body). Confirmed live this was
    happening before `_add_response` was reordered to validate everything first."""
    with pytest.raises(ValueError, match="Invalid query value"):
        lothc_mocker.add_get_response(path="/items", params=cast(Any, {"flag": None}))

    with pytest.raises(AssertionError, match="No mock rule matched"):
        await client.get("items")


def test_mock_request_is_not_frozen_and_not_hashable() -> None:
    """`MockRequest` deliberately isn't `frozen=True` — see its docstring for why (a mutable
    `dict` field can't be made genuinely immutable/hashable by `frozen=True` alone)."""
    headers: dict[str, str] = {}
    request = MockRequest(
        method="GET", path="/items/7", query_string="", query={}, headers=headers, body=None
    )

    headers["x"] = "y"  # mutation succeeds, honestly, since nothing claims otherwise
    assert request.headers == {"x": "y"}

    with pytest.raises(TypeError, match="unhashable"):
        hash(request)


async def test_add_get_response_reuses_a_typed_headers_class(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """The same `ItemHeaders` class works as both `add_get_response(headers=...)` (encoding an
    instance into `x-total-count`, mirroring a real request's `headers=`) and
    `response_headers_type=` (decoding the mocked response's headers back into an instance)."""
    lothc_mocker.add_get_response(path="/items", headers=ItemHeaders(x_total_count=1))

    result = await client.get_result("items", response_headers_type=ItemHeaders)

    assert result.typed_headers == ItemHeaders(x_total_count=1)


async def test_mock_assert_called_counts_matched_requests(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7")

    await client.get("items/7")
    await client.get("items/7")

    mock.assert_called(count=2)


async def test_strict_by_default_raises_for_unmatched_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    with pytest.raises(AssertionError):
        await client.get("items/7")

    assert lothc_mocker.get_call_count() == 0


async def test_strict_enabled_false_falls_through_to_the_real_call(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.strict(enabled=False)

    item = await client.get("items/7", response_data_type=dict)

    assert item == {"id": 7, "name": "item-7"}


@pytest.mark.lothc_mocker(strict=False)
async def test_lothc_mocker_marker_disables_strict(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    item = await client.get("items/7", response_data_type=dict)

    assert item == {"id": 7, "name": "item-7"}
    assert lothc_mocker.get_call_count() == 0


@pytest.mark.lothc_mocker(False)  # noqa: FBT003 -- testing the positional-arg form on purpose
async def test_lothc_mocker_marker_disables_strict_positional(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    item = await client.get("items/7", response_data_type=dict)

    assert item == {"id": 7, "name": "item-7"}
    assert lothc_mocker.get_call_count() == 0


@pytest.mark.lothc_mocker(True, strict=False)  # noqa: FBT003 -- testing conflicting args on purpose
def test_lothc_mocker_marker_conflicting_positional_and_keyword_raises(
    request: pytest.FixtureRequest,
) -> None:
    with pytest.raises(TypeError, match="not both"):
        request.getfixturevalue("lothc_mocker")


@pytest.mark.lothc_mocker("not-a-bool")
def test_lothc_mocker_marker_non_bool_value_raises(request: pytest.FixtureRequest) -> None:
    with pytest.raises(TypeError, match="must be a bool"):
        request.getfixturevalue("lothc_mocker")


@pytest.mark.lothc_mocker(True, False)  # noqa: FBT003 -- extra positional arg, on purpose
def test_lothc_mocker_marker_too_many_positional_args_raises(
    request: pytest.FixtureRequest,
) -> None:
    with pytest.raises(TypeError, match="at most one positional argument"):
        request.getfixturevalue("lothc_mocker")


@pytest.mark.lothc_mocker(strikt=False)  # pyright: ignore[reportCallIssue] -- misspelled on purpose
def test_lothc_mocker_marker_unknown_kwarg_raises(request: pytest.FixtureRequest) -> None:
    with pytest.raises(TypeError, match="unexpected keyword"):
        request.getfixturevalue("lothc_mocker")


def test_add_get_response_returns_raw_bytes_by_default_sync(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b'{"id": 7, "name": "item-7"}')

    body = sync_client.get("items/7")

    assert body == b'{"id": 7, "name": "item-7"}'


def test_add_post_response_encodes_dict_sync(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/items", data={"id": 7, "name": "item-7"})

    item = sync_client.post("items", response_data_type=dict)

    assert item == {"id": 7, "name": "item-7"}


def test_add_head_response_with_status(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_head_response(path="/items/7", status=204)

    result = sync_client.head("items/7")

    assert result.status == 204


async def test_match_request_with_response_raises_after_add_response(
    lothc_mocker: LOTHCMocker,
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7")

    with pytest.raises(ValueError, match="mutually exclusive"):
        mock.match_request_with_response(lambda _request: None)


def _item_id_from_request(request: MockRequest) -> int:
    return int(request.path.removeprefix("/items/"))


async def _handler_computes_item_from_path(request: MockRequest) -> MockResponse:
    return MockResponse(status=201, data={"id": _item_id_from_request(request)})


async def test_match_request_with_response_computes_from_the_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_handler_computes_item_from_path)

    item = await client.get("items/42", response_data_type=dict)

    assert item == {"id": 42}


def _sync_handler_computes_item_from_path(request: MockRequest) -> MockResponse:
    return MockResponse(status=201, data={"id": _item_id_from_request(request)})


def test_match_request_with_response_works_for_sync_client(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_sync_handler_computes_item_from_path)

    item = sync_client.get("items/42", response_data_type=dict)

    assert item == {"id": 42}


class _AsyncCallableHandler:
    async def __call__(self, request: MockRequest) -> MockResponse:
        return MockResponse(status=201, data={"id": _item_id_from_request(request)})


async def test_match_request_with_response_accepts_an_async_callable_object(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """`inspect.iscoroutinefunction()` alone misclassifies a callable *object* whose `__call__` is
    `async def` as sync (confirmed live) — `_wrap_custom_handler` checks `__call__` too, so this
    still dispatches through the async path instead of returning an un-awaited coroutine."""
    lothc_mocker.mock("GET").match_request_with_response(_AsyncCallableHandler())

    item = await client.get("items/42", response_data_type=dict)

    assert item == {"id": 42}


async def _handler_declines(_request: MockRequest) -> MockResponse | None:
    return None


async def test_match_request_with_response_returning_none_falls_through(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET", path="/items/7").match_request_with_response(_handler_declines)
    lothc_mocker.add_get_response(path="/items/7", data={"id": 7})

    item = await client.get("items/7", response_data_type=dict)

    assert item == {"id": 7}


def test_with_status_raises_after_match_request_with_response(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.mock("GET", path="/items/7")
    mock.match_request_with_response(lambda _request: None)

    with pytest.raises(ValueError, match="mutually exclusive"):
        mock.with_status(200)


async def test_with_headers_then_with_data_content_type_not_duplicated(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """`.with_headers(...).with_data(...)` — the natural order to chain them in — used to leave
    two `Content-Type` headers on the wire (pyreqwest's `.body_json()` *appends* its own,
    `.headers()` only *replaces* a same-key value if called afterward). Not directly observable
    through `Result.headers` here (it already collapses to the first wire value, which happened
    to be the correct one even before the fix — same reason `_add_response`'s own version of this
    bug in CLAUDE.md's dev notes has no automated duplicate-detection test either); this at least
    exercises the code path and asserts the value a real caller would actually read is correct."""
    lothc_mocker.mock("GET", path="/items").with_headers({"content-type": "text/plain"}).with_data({
        "a": 1
    })

    result = await client.get_result("items")

    assert result.headers["content-type"] == "text/plain"


async def test_match_query_and_match_body_json_narrow_a_mock(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    narrow = lothc_mocker.add_get_response(path="/items", data={"matched": True})
    narrow.match_query({"page": "2"}).match_body_json({"any": "thing"})
    wide = lothc_mocker.add_get_response(path="/items", data={"matched": False})

    item = await client.get("items", params={"page": "2"}, response_data_type=dict)

    assert item == {"matched": False}  # match_body_json requires a body a GET never sends
    narrow.assert_called(count=0)
    wide.assert_called(count=1)


async def _matches_flag_header(request: MockRequest) -> bool:
    return request.headers.get("x-flag") == "1"


async def test_match_request_uses_a_custom_predicate(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7", data={"ok": True})
    mock.match_request(_matches_flag_header)

    item = await client.get("items/7", headers={"x-flag": "1"}, response_data_type=dict)

    assert item == {"ok": True}
    mock.assert_called(count=1)


class _MatchesFlagHeader:
    async def __call__(self, request: MockRequest) -> bool:
        return request.headers.get("x-flag") == "1"


async def test_match_request_accepts_an_async_callable_object(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """Same `iscoroutinefunction` misclassification as
    `test_match_request_with_response_accepts_an_async_callable_object`, for `match_request`'s
    own async/sync split via `_wrap_custom_matcher`."""
    mock = lothc_mocker.add_get_response(path="/items/7", data={"ok": True})
    mock.match_request(_MatchesFlagHeader())

    item = await client.get("items/7", headers={"x-flag": "1"}, response_data_type=dict)

    assert item == {"ok": True}
    mock.assert_called(count=1)


async def test_get_requests_returns_mock_request_snapshots(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7", data={"ok": True})

    await client.get("items/7", params={"page": "2"}, headers={"x-flag": "1"})

    [seen] = mock.get_requests()
    assert isinstance(seen, MockRequest)
    assert seen.method == "GET"
    assert seen.path == "/items/7"
    assert seen.query_string == "page=2"
    assert seen.query == {"page": "2"}
    assert seen.headers["x-flag"] == "1"


async def test_clear_removes_all_registered_mocks(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7")
    lothc_mocker.clear()
    lothc_mocker.strict()

    with pytest.raises(AssertionError):
        await client.get("items/7")


def test_pyreqwest_client_mocker_protocol_surface(client_mocker: ClientMocker) -> None:
    """Existence-only smoke test for `_MOCK_METHODS`/`_CLIENT_MOCKER_METHODS` (lothc/testing.py's
    `_MockTyping`/`_ClientMockerTyping`) — catches a method being renamed or removed on
    pyreqwest's real `Mock`/`ClientMocker`. Deliberately does NOT check parameter signatures (a
    renamed/removed *parameter* on a method that still exists isn't caught here) — the tests
    above call every one of these methods through the real public API with real arguments, which
    is what would actually catch that."""
    for name in _CLIENT_MOCKER_METHODS:
        assert callable(getattr(client_mocker, name))

    # pyreqwest's own dirty_equals-related stub gap (see lothc/testing.py's module docstring)
    # leaks an Unknown into `.get`'s own inferred signature here, since this calls pyreqwest's
    # raw API directly rather than through lothc's re-typed Protocol wrapper.
    mock = client_mocker.get(path="/items/7")  # pyright: ignore[reportUnknownMemberType]
    for name in _MOCK_METHODS:
        assert callable(getattr(mock, name))
