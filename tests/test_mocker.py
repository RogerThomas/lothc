import re
from typing import Any, cast

import pytest
from msgspec import Struct
from pydantic import BaseModel, Field

from lothc import CaseInsensitiveDict, HTTPClient, HTTPResponseError, SSEEvent, SyncHTTPClient
from lothc.testing import LOTHCMocker, MockRequest, MockResponse


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

    body = (await client.get("items/7")).data

    assert body == b'{"id": 7, "name": "item-7"}'


async def test_add_get_response_encodes_pydantic_model(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=ItemModel(id=7, name="item-7"))

    item = (await client.get("items/7", into=ItemModel)).data

    assert item == ItemModel(id=7, name="item-7")


async def test_add_get_response_encodes_msgspec_struct(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=ItemStruct(id=7, name="item-7"))

    item = (await client.get("items/7", into=ItemStruct)).data

    assert item == ItemStruct(id=7, name="item-7")


async def test_add_post_response_encodes_dict(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/items", data={"id": 7, "name": "item-7"})

    item = (await client.post("items", into=dict)).data

    assert item == {"id": 7, "name": "item-7"}


async def test_add_get_response_with_status_raises_http_response_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", status=404, data=b"not found")

    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("items/7")

    assert exc_info.value.status == 404
    assert exc_info.value.body_start == b"not found"


async def test_a_mocked_error_status_is_returned_with_raise_for_status_false(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", status=404, data={"detail": "not-found"})

    response = await client.get("items/7", into=ItemModel, raise_for_status=False)

    assert response.status == 404
    assert response.error == {"detail": "not-found"}
    assert response.raw_error == b'{"detail":"not-found"}'
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = response.data
    assert exc_info.value.status == 404


def test_sync_a_mocked_error_status_is_returned_with_raise_for_status_false(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", status=404, data={"detail": "not-found"})

    response = sync_client.get("items/7", into=ItemModel, raise_for_status=False)

    assert response.status == 404
    assert response.error == {"detail": "not-found"}
    with pytest.raises(HTTPResponseError):
        _ = response.data


async def test_add_get_response_with_headers(client: HTTPClient, lothc_mocker: LOTHCMocker) -> None:
    lothc_mocker.add_get_response(path="/items/7", headers={"x-custom": "header-value"})

    result = await client.get("items/7")

    assert result.headers["x-custom"] == "header-value"


async def test_add_get_response_with_data_and_headers_together(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """A caller's own `headers={"content-type": ...}` replaces `data=`'s default Content-Type
    rather than leaving two Content-Type values on the wire: the same invariant
    `test_with_headers_then_with_data_content_type_not_duplicated` checks for the chainable API."""
    lothc_mocker.add_get_response(
        path="/items/7", data={"id": 7}, headers={"content-type": "text/plain"}
    )

    result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7}
    assert result.headers.get_all("content-type") == ["text/plain"]


async def test_add_get_response_matches_on_params(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    matching = lothc_mocker.add_get_response(path="/items", params={"page": "2"}, data={"page": 2})
    other_page = lothc_mocker.add_get_response(
        path="/items", params={"page": "1"}, data={"page": 1}
    )

    item = (await client.get("items", params={"page": "2"}, into=dict)).data

    assert item == {"page": 2}
    matching.assert_called(count=1)
    other_page.assert_called(count=0)


async def test_add_get_response_matches_on_bool_param(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """pyreqwest encodes a bool query param as lowercase `true`/`false`, not Python's
    `str(True)` == `"True"` — confirmed live against a real `RequestBuilder.query()` call."""
    lothc_mocker.add_get_response(path="/items", params={"flag": True}, data={"ok": True})

    item = (await client.get("items", params={"flag": True}, into=dict)).data

    assert item == {"ok": True}


async def test_add_get_response_matches_on_a_repeated_query_param(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """A `list[...]` `params` value (a genuinely repeated query key) narrows on the full,
    order-sensitive list of values for that key, not just its first value."""
    matching = lothc_mocker.add_get_response(
        path="/items", params={"tag": ["a", "b"]}, data={"ok": True}
    )

    item = (await client.get("items", params={"tag": ["a", "b"]}, into=dict)).data

    assert item == {"ok": True}
    matching.assert_called(count=1)


async def test_add_get_response_matches_on_a_repeated_query_param_as_tuple(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """A `tuple[...]` `params` value narrows the same way `list[...]` does — both mean "repeat
    this key"."""
    lothc_mocker.add_get_response(path="/items", params={"tag": ("a", "b")}, data={"ok": True})

    item = (await client.get("items", params={"tag": ("a", "b")}, into=dict)).data

    assert item == {"ok": True}


async def test_add_get_response_matches_an_int_outside_64_bits_exactly(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """An int past the signed 64-bit range is matched by its exact digits, as a real request
    sends it, not as the float pyreqwest's encoder would turn it into."""
    mock = lothc_mocker.add_get_response(path="/items", params={"id": 2**64}, data={"ok": True})

    item = (await client.get("items", params={"id": 2**64}, into=dict)).data

    assert item == {"ok": True}
    [seen] = mock.get_requests()
    assert seen.query == {"id": "18446744073709551616"}


def test_sync_add_get_response_matches_an_int_outside_64_bits_exactly(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", params={"id": 2**64}, data={"ok": True})

    item = sync_client.get("items", params={"id": 2**64}, into=dict).data

    assert item == {"ok": True}
    [seen] = mock.get_requests()
    assert seen.query == {"id": "18446744073709551616"}


async def test_add_get_response_does_not_match_a_neighbouring_int_outside_64_bits(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    # As floats, 2**64 and 2**64 + 1 are the same number, so a lossy encoding matched both.
    mock = lothc_mocker.add_get_response(path="/items", params={"id": 2**64}, data={})

    with pytest.raises(AssertionError, match="No mock answered"):
        await client.get("items", params={"id": 2**64 + 1})

    mock.assert_called(count=0)


async def test_add_get_response_does_not_match_a_different_repeated_query_param_order(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", params={"tag": ["a", "b"]}, data={})

    with pytest.raises(AssertionError, match="No mock answered"):
        await client.get("items", params={"tag": ["b", "a"]})

    mock.assert_called(count=0)


def test_add_get_response_with_none_param_raises_like_a_real_request(
    lothc_mocker: LOTHCMocker,
) -> None:
    """`None` isn't a valid `Params` value (`Params` is `Mapping[str, str | int | float | bool] |
    BaseModel | Struct`), but nothing stops it arriving at runtime — a real request rejects it
    (`ValueError` from pyreqwest's own query encoder), so mock registration must too, rather than
    silently registering a mock a real call could never actually produce."""
    with pytest.raises(ValueError, match="Invalid query value"):
        lothc_mocker.add_get_response(path="/items", params=cast(Any, {"flag": None}))


def test_add_get_response_with_empty_list_param_raises_instead_of_matching_anything(
    lothc_mocker: LOTHCMocker,
) -> None:
    """An empty `list`/`tuple` `params` value has no query-string representation to narrow a mock
    against — `Url.parse_with_params` silently drops the key entirely, which would otherwise
    register a mock that matches ANY query string instead of none (confirmed live before this
    check was added)."""
    with pytest.raises(ValueError, match="Invalid query value"):
        lothc_mocker.add_get_response(path="/items", params={"tag": []})


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

    with pytest.raises(AssertionError, match="No mock answered"):
        await client.get("items")


def test_mock_request_is_not_frozen_and_not_hashable() -> None:
    """`MockRequest` deliberately isn't `frozen=True` — see its docstring for why (a mutable
    mapping field can't be made genuinely immutable/hashable by `frozen=True` alone)."""
    headers = CaseInsensitiveDict()
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

    result = await client.get("items", response_headers_type=ItemHeaders)

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

    item = (await client.get("items/7", into=dict)).data

    assert item == {"id": 7, "name": "item-7"}


@pytest.mark.lothc_mocker(strict=False)
async def test_lothc_mocker_marker_disables_strict(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    item = (await client.get("items/7", into=dict)).data

    assert item == {"id": 7, "name": "item-7"}
    assert lothc_mocker.get_call_count() == 0


@pytest.mark.lothc_mocker(False)  # noqa: FBT003 -- testing the positional-arg form on purpose
async def test_lothc_mocker_marker_disables_strict_positional(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    item = (await client.get("items/7", into=dict)).data

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

    body = sync_client.get("items/7").data

    assert body == b'{"id": 7, "name": "item-7"}'


def test_add_post_response_encodes_dict_sync(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/items", data={"id": 7, "name": "item-7"})

    item = sync_client.post("items", into=dict).data

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

    item = (await client.get("items/42", into=dict)).data

    assert item == {"id": 42}


def _sync_handler_computes_item_from_path(request: MockRequest) -> MockResponse:
    return MockResponse(status=201, data={"id": _item_id_from_request(request)})


def test_match_request_with_response_works_for_sync_client(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_sync_handler_computes_item_from_path)

    item = sync_client.get("items/42", into=dict).data

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

    item = (await client.get("items/42", into=dict)).data

    assert item == {"id": 42}


async def _handler_declines(_request: MockRequest) -> MockResponse | None:
    return None


async def test_match_request_with_response_returning_none_falls_through(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET", path="/items/7").match_request_with_response(_handler_declines)
    lothc_mocker.add_get_response(path="/items/7", data={"id": 7})

    item = (await client.get("items/7", into=dict)).data

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
    `.headers()` only *replaces* a same-key value if called afterward). `get_all` sees every
    value, so a duplicate fails here even when the first one is the right one."""
    lothc_mocker.mock("GET", path="/items").with_headers({"content-type": "text/plain"}).with_data({
        "a": 1
    })

    result = await client.get("items")

    assert result.headers.get_all("content-type") == ["text/plain"]


async def test_with_headers_snapshots_the_dict_instead_of_aliasing_it(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """`with_headers` must encode a defensive snapshot, not alias the caller's own `dict` — a
    caller mutating their `headers` dict after `.with_headers(...)` (here between it and
    `.with_data(...)`, the natural place to do it) must not silently change the mocked response.
    Confirmed live this genuinely leaked through when `_encode_headers`'s fallback branch returned
    the caller's `Mapping` unchanged instead of copying it."""
    headers = {"x-custom": "initial"}
    mock = lothc_mocker.mock("GET", path="/items").with_headers(headers)
    headers["x-custom"] = "mutated-after-the-fact"
    mock.with_data({"a": 1})

    result = await client.get("items")

    assert result.headers["x-custom"] == "initial"


async def test_match_query_and_match_body_json_narrow_a_mock(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    narrow = lothc_mocker.add_get_response(path="/items", data={"matched": True})
    narrow.match_query({"page": "2"}).match_body_json({"any": "thing"})
    wide = lothc_mocker.add_get_response(path="/items", data={"matched": False})

    item = (await client.get("items", params={"page": "2"}, into=dict)).data

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

    item = (await client.get("items/7", headers={"x-flag": "1"}, into=dict)).data

    assert item == {"ok": True}
    mock.assert_called(count=1)


class _MatchesFlagHeader:
    async def __call__(self, request: MockRequest) -> bool:
        return request.headers.get("x-flag") == "1"


async def test_match_request_accepts_an_async_callable_object(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """An async-callable-object predicate works correctly end-to-end for `match_request` — but,
    confirmed live, this does NOT actually exercise `_wrap_custom_matcher`'s `_is_async_callable`
    fix the way `test_match_request_with_response_accepts_an_async_callable_object` exercises
    `_wrap_custom_handler`'s: pyreqwest's own async `Mock._matches_custom` unconditionally awaits
    whatever its matcher call returns if it's an `Awaitable`, regardless of whether *this
    module's* dispatch classified the matcher as sync or async — so reverting `_is_async_callable`
    to plain `inspect.iscoroutinefunction` here still calls the async `__call__`, gets back an
    un-awaited (but not-yet-run) coroutine, and pyreqwest awaits it anyway, running the predicate's
    real logic and getting the correct answer either way. This test is still worth keeping as
    end-to-end coverage for the *feature* (an async-callable-object predicate is a legitimate
    thing to pass), just not as a regression guard for the dispatch fix itself — that guard is the
    handler-side test."""
    mock = lothc_mocker.add_get_response(path="/items/7", data={"ok": True})
    mock.match_request(_MatchesFlagHeader())

    item = (await client.get("items/7", headers={"x-flag": "1"}, into=dict)).data

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


async def test_get_requests_collapses_a_repeated_query_key_to_its_first_value(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    """`MockRequest.query`'s whole reason to exist (per its own docstring) is collapsing a
    genuinely repeated query key to its first value, same as `.headers` already does — exercises
    `_first_value_per_key`'s `list[str]` branch, which nothing else here reaches (`params=` only
    ever sends one scalar per key)."""
    mock = lothc_mocker.add_get_response(path="/items", data={"ok": True})

    await client.get("items?tag=a&tag=b")

    [seen] = mock.get_requests()
    assert seen.query_string == "tag=a&tag=b"
    assert seen.query == {"tag": "a"}


async def test_clear_removes_all_registered_mocks(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7")
    lothc_mocker.clear()
    lothc_mocker.strict()

    with pytest.raises(AssertionError):
        await client.get("items/7")


async def test_get_requests_headers_are_case_insensitive(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7", data={"ok": True})

    await client.get("items/7", headers={"X-Flag": "1"})

    [seen] = mock.get_requests()
    assert seen.headers["x-flag"] == "1"
    assert seen.headers["X-FLAG"] == "1"
    assert "X-Flag" in seen.headers


class _AliasedMockBody(BaseModel):
    user_id: int = Field(alias="userId")


async def test_mock_response_data_encodes_aliases_like_a_real_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=_AliasedMockBody(userId=7))

    result = (await client.get("items/7", into=dict)).data

    assert result == {"userId": 7}


async def test_add_put_response_returns_its_data(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_put_response(path="/items/7", data={"id": 7, "name": "put"})

    response = await client.put("items/7", json={"name": "put"}, into=dict)

    assert response.data == {"id": 7, "name": "put"}


async def test_add_patch_response_returns_its_data(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_patch_response(path="/items/7", data={"id": 7, "name": "patch"})

    response = await client.patch("items/7", json={"name": "patch"}, into=dict)

    assert response.data == {"id": 7, "name": "patch"}


async def test_add_delete_response_returns_its_status(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_delete_response(path="/items/7", status=404)

    with pytest.raises(HTTPResponseError) as exc_info:
        await client.delete("items/7")

    assert exc_info.value.status == 404


def test_sync_add_put_patch_and_delete_responses(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_put_response(path="/items/7", data={"verb": "put"})
    lothc_mocker.add_patch_response(path="/items/7", data={"verb": "patch"})
    lothc_mocker.add_delete_response(path="/items/7", data={"verb": "delete"})

    assert sync_client.put("items/7", into=dict).data == {"verb": "put"}
    assert sync_client.patch("items/7", into=dict).data == {"verb": "patch"}
    assert sync_client.delete("items/7", into=dict).data == {"verb": "delete"}


async def test_url_matches_the_whole_url_exactly(
    base_url: str, client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(url=f"{base_url}items/7", data={"matched": True})

    response = await client.get("items/7", into=dict)
    with pytest.raises(AssertionError):  # strict, and another path doesn't match
        await client.get("items/8")

    assert response.data == {"matched": True}


async def test_url_matches_a_regular_expression(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(url=re.compile(r"/items/\d+$"), data={"matched": True})

    response = await client.get("items/8", into=dict)

    assert response.data == {"matched": True}


async def test_mock_request_body_is_the_bytes_that_were_sent(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_post_response(path="/items")

    await client.post("items", json={"a": 1})

    [seen] = mock.get_requests()
    assert seen.body == b'{"a":1}'


async def test_get_requests_shows_no_body_for_a_bodyless_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7")

    await client.get("items/7")

    [seen] = mock.get_requests()
    assert seen.body is None


async def test_a_mocked_response_can_be_streamed(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/ndjson", data=b'{"i": 0}\n{"i": 1}\n')

    lines = [line async for line in client.stream_get("ndjson", into=dict)]

    assert lines == [{"i": 0}, {"i": 1}]


async def test_a_mocked_response_can_be_downloaded(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/binary", data=b"content")

    response = await client.download("binary")

    assert response.data == b"content"


async def test_a_mocked_event_stream_can_be_read_with_sse(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(
        path="/events",
        data=b"id: 1\ndata: first\n\n",
        headers={"content-type": "text/event-stream"},
    )

    events = [event async for event in client.sse("events", max_reconnects=0)]

    assert events == [SSEEvent(id="1", event="message", data="first")]


def test_sync_a_mocked_response_can_be_streamed(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/ndjson", data=b'{"i": 0}\n{"i": 1}\n')

    lines = list(sync_client.stream_get("ndjson", into=dict))

    assert lines == [{"i": 0}, {"i": 1}]


# --- How a request finds its mock, and what the failure messages say -----------------------------


def _sync_matches_flag_header(request: MockRequest) -> bool:
    return request.headers.get("x-flag") == "1"


async def test_a_plain_function_matcher_and_handler_answer_an_async_client(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request(_sync_matches_flag_header).match_request_with_response(
        _sync_handler_computes_item_from_path
    )

    item = (await client.get("items/42", headers={"x-flag": "1"}, into=dict)).data

    assert item == {"id": 42}


def test_sync_a_plain_function_matcher_and_handler_answer_a_sync_client(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request(_sync_matches_flag_header).match_request_with_response(
        _sync_handler_computes_item_from_path
    )

    item = sync_client.get("items/42", headers={"x-flag": "1"}, into=dict).data

    assert item == {"id": 42}


@pytest.mark.parametrize("kind", ["matcher", "handler"])
def test_sync_an_async_matcher_or_handler_is_a_type_error(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker, kind: str
) -> None:
    mock = lothc_mocker.mock("GET")
    if kind == "matcher":
        mock.match_request(_matches_flag_header)
    else:
        mock.match_request_with_response(_handler_computes_item_from_path)

    with pytest.raises(TypeError, match="can't answer a SyncHTTPClient's request"):
        sync_client.get("items/42")


async def test_an_unmatched_request_names_the_closest_mock(
    base_url: str, client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/other")
    lothc_mocker.add_get_response(path="/items").match_header("x-flag", "1")

    with pytest.raises(AssertionError) as exc_info:
        await client.get("items", headers={"x-flag": "2"})

    assert str(exc_info.value) == (
        f"No mock answered GET {base_url}items\n"
        "The closest mock, GET path='/items':\n"
        "  header 'x-flag': expected '1', got '2'\n"
        "lothc_mocker is strict: mock the request, or let unmocked requests through with "
        "@pytest.mark.lothc_mocker(strict=False)."
    )


def test_sync_an_unmatched_request_names_the_closest_mock(
    base_url: str, sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/other")
    lothc_mocker.add_get_response(path="/items").match_header("x-flag", "1")

    with pytest.raises(AssertionError) as exc_info:
        sync_client.get("items", headers={"x-flag": "2"})

    assert str(exc_info.value) == (
        f"No mock answered GET {base_url}items\n"
        "The closest mock, GET path='/items':\n"
        "  header 'x-flag': expected '1', got '2'\n"
        "lothc_mocker is strict: mock the request, or let unmocked requests through with "
        "@pytest.mark.lothc_mocker(strict=False)."
    )


async def test_assert_called_lists_the_requests_past_the_expected_count(
    base_url: str, client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(url=re.compile("items"))

    await client.get("items/1")
    await client.get("items/2")

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value) == (
        "The mock GET url=re.compile('items') answered 2 requests, expected exactly 1.\n"
        "It answered:\n"
        f"  GET {base_url}items/1\n"
        f"  GET {base_url}items/2"
    )


def test_sync_assert_called_lists_the_requests_past_the_expected_count(
    base_url: str, sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(url=re.compile("items"))

    sync_client.get("items/1")
    sync_client.get("items/2")

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value) == (
        "The mock GET url=re.compile('items') answered 2 requests, expected exactly 1.\n"
        "It answered:\n"
        f"  GET {base_url}items/1\n"
        f"  GET {base_url}items/2"
    )


async def test_assert_called_shows_the_request_with_the_fewest_mismatches(
    base_url: str, client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_post_response(path="/items").match_body_json({"a": 1})
    lothc_mocker.mock()

    await client.post("items", json={"a": 2})
    await client.get("other")

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value) == (
        "The mock POST path='/items' answered 0 requests, expected exactly 1.\n"
        f"The closest request it didn't answer, POST {base_url}items:\n"
        "  JSON body: expected {'a': 1}, got {'a': 2}"
    )


def test_sync_assert_called_shows_the_request_with_the_fewest_mismatches(
    base_url: str, sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_post_response(path="/items").match_body_json({"a": 1})
    lothc_mocker.mock()

    sync_client.post("items", json={"a": 2})
    sync_client.get("other")

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value) == (
        "The mock POST path='/items' answered 0 requests, expected exactly 1.\n"
        f"The closest request it didn't answer, POST {base_url}items:\n"
        "  JSON body: expected {'a': 1}, got {'a': 2}"
    )


def _sync_handler_declines(_request: MockRequest) -> MockResponse | None:
    return None


async def test_assert_called_says_why_a_matching_request_went_elsewhere(
    base_url: str, client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    declining = lothc_mocker.mock("GET").match_request_with_response(_handler_declines)
    answering = lothc_mocker.add_get_response(path="/items")
    shadowed = lothc_mocker.add_get_response(path="/items")

    await client.get("items")

    answering.assert_called()
    with pytest.raises(AssertionError) as declined:
        declining.assert_called()
    with pytest.raises(AssertionError) as went_first:
        shadowed.assert_called()
    assert str(declined.value).endswith(
        f"GET {base_url}items:\n  match_request_with_response(_handler_declines) returned None"
    )
    assert str(went_first.value).endswith(f"GET {base_url}items:\n  another mock answered it first")


def test_sync_assert_called_says_why_a_matching_request_went_elsewhere(
    base_url: str, sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    declining = lothc_mocker.mock("GET").match_request_with_response(_sync_handler_declines)
    answering = lothc_mocker.add_get_response(path="/items")
    shadowed = lothc_mocker.add_get_response(path="/items")

    sync_client.get("items")

    answering.assert_called()
    with pytest.raises(AssertionError) as declined:
        declining.assert_called()
    with pytest.raises(AssertionError) as went_first:
        shadowed.assert_called()
    assert str(declined.value).endswith(
        f"GET {base_url}items:\n  match_request_with_response(_sync_handler_declines) returned None"
    )
    assert str(went_first.value).endswith(f"GET {base_url}items:\n  another mock answered it first")


@pytest.mark.lothc_mocker(strict=False)
async def test_assert_called_says_a_request_came_before_the_mock(
    base_url: str, client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    await client.get("items/7")
    mock = lothc_mocker.add_get_response(path="/items/7")

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value).endswith(
        f"GET {base_url}items/7:\n  it was sent before this mock was registered"
    )


@pytest.mark.lothc_mocker(strict=False)
def test_sync_assert_called_says_a_request_came_before_the_mock(
    base_url: str, sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    sync_client.get("items/7")
    mock = lothc_mocker.add_get_response(path="/items/7")

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value).endswith(
        f"GET {base_url}items/7:\n  it was sent before this mock was registered"
    )


def test_assert_called_says_when_no_request_was_sent(lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.mock()

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called(min_count=1)
    assert str(exc_info.value) == (
        "The mock for any request answered 0 requests, expected at least 1.\n"
        "No other request was sent."
    )


async def test_assert_called_checks_every_bound_it_is_given(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7")

    await client.get("items/7")
    await client.get("items/7")

    mock.assert_called(count=2, min_count=1, max_count=2)
    with pytest.raises(AssertionError, match=r"expected at least 1 and at most 1\.\n"):
        mock.assert_called(min_count=1, max_count=1)


def test_sync_assert_called_checks_every_bound_it_is_given(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7")

    sync_client.get("items/7")
    sync_client.get("items/7")

    mock.assert_called(count=2, min_count=1, max_count=2)
    with pytest.raises(AssertionError, match=r"expected at least 1 and at most 1\.\n"):
        mock.assert_called(min_count=1, max_count=1)


async def test_get_requests_lists_every_mock_s_requests_in_the_order_sent(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/a")
    lothc_mocker.add_get_response(path="/b")

    await client.get("a")
    await client.get("b")
    await client.get("a")

    assert [seen.path for seen in lothc_mocker.get_requests()] == ["/a", "/b", "/a"]


def test_sync_get_requests_lists_every_mock_s_requests_in_the_order_sent(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/a")
    lothc_mocker.add_get_response(path="/b")

    sync_client.get("a")
    sync_client.get("b")
    sync_client.get("a")

    assert [seen.path for seen in lothc_mocker.get_requests()] == ["/a", "/b", "/a"]


async def test_clear_leaves_a_removed_mock_its_requests(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7")
    await client.get("items/7")

    lothc_mocker.clear()

    assert (lothc_mocker.get_call_count(), mock.get_call_count()) == (0, 1)


def test_sync_clear_leaves_a_removed_mock_its_requests(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items/7")
    sync_client.get("items/7")

    lothc_mocker.clear()

    assert (lothc_mocker.get_call_count(), mock.get_call_count()) == (0, 1)


async def test_match_query_narrows_a_params_mock_further(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    narrow = lothc_mocker.add_get_response(path="/items", params={"page": "2"})
    narrow.match_query({"tag": "a"})
    wide = lothc_mocker.add_get_response(path="/items")

    await client.get("items", params={"page": 2, "tag": "a"})
    await client.get("items", params={"tag": "a"})

    narrow.assert_called(count=1)
    wide.assert_called(count=1)


def test_sync_match_query_narrows_a_params_mock_further(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    narrow = lothc_mocker.add_get_response(path="/items", params={"page": "2"})
    narrow.match_query({"tag": "a"})
    wide = lothc_mocker.add_get_response(path="/items")

    sync_client.get("items", params={"page": 2, "tag": "a"})
    sync_client.get("items", params={"tag": "a"})

    narrow.assert_called(count=1)
    wide.assert_called(count=1)


async def test_match_body_json_twice_needs_both(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    both = lothc_mocker.add_post_response(path="/items")
    both.match_body_json({"a": 1}).match_body_json({"a": 2})
    rest = lothc_mocker.add_post_response(path="/items")

    await client.post("items", json={"a": 2})

    both.assert_called(count=0)
    rest.assert_called(count=1)


def test_sync_match_body_json_twice_needs_both(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    both = lothc_mocker.add_post_response(path="/items")
    both.match_body_json({"a": 1}).match_body_json({"a": 2})
    rest = lothc_mocker.add_post_response(path="/items")

    sync_client.post("items", json={"a": 2})

    both.assert_called(count=0)
    rest.assert_called(count=1)


async def test_a_query_pattern_matches_a_key_sent_once_and_a_list_every_value(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    pattern = lothc_mocker.add_get_response(path="/items").match_query_param("a", re.compile("2"))
    listed = lothc_mocker.add_get_response(path="/items").match_query({"a": ["1"]})
    rest = lothc_mocker.add_get_response(path="/items")

    await client.get("items?a=1&a=2")
    await client.get("items?a=1")

    assert (pattern.get_call_count(), listed.get_call_count(), rest.get_call_count()) == (0, 1, 1)


def test_sync_a_query_pattern_matches_a_key_sent_once_and_a_list_every_value(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    pattern = lothc_mocker.add_get_response(path="/items").match_query_param("a", re.compile("2"))
    listed = lothc_mocker.add_get_response(path="/items").match_query({"a": ["1"]})
    rest = lothc_mocker.add_get_response(path="/items")

    sync_client.get("items?a=1&a=2")
    sync_client.get("items?a=1")

    assert (pattern.get_call_count(), listed.get_call_count(), rest.get_call_count()) == (0, 1, 1)


async def test_with_data_keeps_the_data_as_it_was_given(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    data = {"a": 1}
    lothc_mocker.add_get_response(path="/items", data=data)
    lothc_mocker.mock("GET", path="/other").with_data(data)
    data["a"] = 2

    assert (await client.get("items", into=dict)).data == {"a": 1}
    assert (await client.get("other", into=dict)).data == {"a": 1}


def test_sync_with_data_keeps_the_data_as_it_was_given(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    data = {"a": 1}
    lothc_mocker.add_get_response(path="/items", data=data)
    lothc_mocker.mock("GET", path="/other").with_data(data)
    data["a"] = 2

    assert sync_client.get("items", into=dict).data == {"a": 1}
    assert sync_client.get("other", into=dict).data == {"a": 1}


async def test_a_response_changed_after_it_answered_answers_the_next_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", data={"a": 1})
    first = await client.get("items", into=dict)
    mock.with_data({"a": 2}).with_headers({"x-a": "b"}).with_status(201)

    second = await client.get("items", into=dict)

    assert (first.status, first.data, first.headers.get("x-a")) == (200, {"a": 1}, None)
    assert (second.status, second.data, second.headers["x-a"]) == (201, {"a": 2}, "b")


def test_sync_a_response_changed_after_it_answered_answers_the_next_request(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", data={"a": 1})
    first = sync_client.get("items", into=dict)
    mock.with_data({"a": 2}).with_headers({"x-a": "b"}).with_status(201)

    second = sync_client.get("items", into=dict)

    assert (first.status, first.data, first.headers.get("x-a")) == (200, {"a": 1}, None)
    assert (second.status, second.data, second.headers["x-a"]) == (201, {"a": 2}, "b")
