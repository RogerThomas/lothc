import copy
import pickle
from collections.abc import KeysView
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest
from msgspec import Struct
from pydantic import BaseModel

from lothc import CaseInsensitiveDict, HTTPClient, Response, SyncHTTPClient


@dataclass
class _KeysAndGetItem:
    """A `SupportsKeysAndGetItem` that is deliberately *not* a `Mapping` — the shape
    `MutableMapping.update` accepts beyond a real mapping, and the one branch a plain `dict`
    argument never reaches."""

    _data: dict[str, str]

    def keys(self) -> KeysView[str]:
        return self._data.keys()

    def __getitem__(self, key: str) -> str:
        return self._data[key]


class ItemModel(BaseModel):
    id: int
    name: str


class EchoedHeaders(BaseModel):
    x_custom: str | None = None


class EchoedHeadersStruct(Struct):
    x_custom: str | None = None


async def test_response_get_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.get("items/7", into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="item-7")


async def test_response_get_includes_raw_headers(client: HTTPClient) -> None:
    result = await client.get("items/7", into=ItemModel)

    assert "content-type" in result.headers


async def test_response_get_with_typed_headers(client: HTTPClient) -> None:
    result = await client.get("items/7", into=ItemModel, response_headers_type=EchoedHeaders)

    assert result.typed_headers is not None


async def test_response_get_with_msgspec_typed_headers(client: HTTPClient) -> None:
    result = await client.get("items/7", into=ItemModel, response_headers_type=EchoedHeadersStruct)

    assert result.typed_headers is not None


async def test_response_get_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    result = await client.get("boom", error_for_status=False)

    assert result.status == 500
    assert b"internal-server-error" in result.data


def test_sync_get_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("items/7", into=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="item-7")


def test_sync_get_result_includes_raw_headers(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("items/7", into=ItemModel)

    assert "content-type" in result.headers


def test_sync_get_result_with_typed_headers(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("items/7", into=ItemModel, response_headers_type=EchoedHeaders)

    assert result.typed_headers is not None


def test_sync_get_result_error_for_status_false_suppresses_raise(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get("boom", error_for_status=False)

    assert result.status == 500
    assert b"internal-server-error" in result.data


async def test_response_get_headers_are_case_insensitive(client: HTTPClient) -> None:
    result = await client.get("items/7", into=ItemModel)

    assert result.headers["Content-Type"] == result.headers["content-type"]
    assert result.headers["CONTENT-TYPE"] == result.headers["content-type"]
    assert "Content-Type" in result.headers
    assert result.headers.get("CoNtEnT-tYpE") is not None
    assert result.headers == {name.upper(): value for name, value in result.headers.items()}


def test_sync_get_result_headers_are_case_insensitive(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("items/7", into=ItemModel)

    assert result.headers["Content-Type"] == result.headers["content-type"]
    assert "Content-Type" in result.headers
    assert result.headers.get("CoNtEnT-tYpE") is not None


async def test_response_get_headers_keep_every_value_of_a_repeated_header(
    client: HTTPClient,
) -> None:
    result = await client.get("multi-set-cookie", into=dict)

    assert result.headers.get_all("Set-Cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers["set-cookie"] == "session=abc; Path=/"
    assert result.headers.get_all("content-type") == ["application/json"]
    assert result.headers.get_all("never-sent") == []


def test_sync_get_result_headers_keep_every_value_of_a_repeated_header(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get("multi-set-cookie", into=dict)

    assert result.headers.get_all("Set-Cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers["set-cookie"] == "session=abc; Path=/"


async def test_response_get_headers_get_all_returns_a_copy(client: HTTPClient) -> None:
    result = await client.get("multi-set-cookie", into=dict)

    result.headers.get_all("Set-Cookie").clear()

    assert len(result.headers.get_all("Set-Cookie")) == 3


async def test_copying_headers_keeps_every_repeated_value(client: HTTPClient) -> None:
    # Mapping.items() is single-valued, so a copy taken through it would silently drop the extra
    # values — the one thing CaseInsensitiveDict exists to keep.
    result = await client.get("multi-set-cookie", into=dict)

    assert CaseInsensitiveDict(result.headers).get_all("set-cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers.copy().get_all("set-cookie") == result.headers.get_all("set-cookie")


async def test_updating_headers_keeps_every_repeated_value(client: HTTPClient) -> None:
    result = await client.get("multi-set-cookie", into=dict)
    target = CaseInsensitiveDict({"X-Keep": "kept", "Set-Cookie": "replaced"})

    target.update(result.headers)

    assert target.get_all("set-cookie") == result.headers.get_all("set-cookie")
    assert target["x-keep"] == "kept"


def test_headers_copy_is_independent_of_the_original() -> None:
    original = CaseInsensitiveDict([("Set-Cookie", "a"), ("Set-Cookie", "b")])

    copied = original.copy()
    copied["Set-Cookie"] = "replaced"

    assert original.get_all("set-cookie") == ["a", "b"]
    assert copied.get_all("set-cookie") == ["replaced"]


def test_headers_accept_any_keys_and_getitem_source() -> None:
    # MutableMapping.update accepts anything with .keys()/[], not just a Mapping — the signature
    # matches that, so this must not fall through to the iterate-pairs branch and raise.
    target = CaseInsensitiveDict()

    target.update({"Content-Type": "application/json"})

    assert target["content-type"] == "application/json"


def test_headers_support_len_and_deletion() -> None:
    headers = CaseInsensitiveDict([("Set-Cookie", "a"), ("Set-Cookie", "b"), ("X-Other", "c")])

    assert len(headers) == 2  # distinct names, not total values

    del headers["x-other"]

    assert len(headers) == 1
    assert "X-Other" not in headers


def test_headers_equality_counts_every_value_against_another_case_insensitive_dict() -> None:
    both = CaseInsensitiveDict([("Set-Cookie", "a"), ("Set-Cookie", "b")])
    same_but_lowercased = CaseInsensitiveDict([("set-cookie", "a"), ("set-cookie", "b")])
    only_first = CaseInsensitiveDict([("Set-Cookie", "a")])

    assert both == same_but_lowercased
    # Two header sets differing only by a dropped repeat must not compare equal...
    assert both != only_first
    # ...but a plain mapping can only hold the single-valued view, so that's what it compares to.
    assert only_first == {"SET-COOKIE": "a"}
    assert both != "not-a-mapping"


def test_headers_repr_reveals_repeated_values() -> None:
    single = CaseInsensitiveDict({"Content-Type": "application/json"})
    repeated = CaseInsensitiveDict([("Set-Cookie", "a"), ("Set-Cookie", "b")])

    assert repr(single) == "CaseInsensitiveDict({'Content-Type': 'application/json'})"
    # The pair-list form, so three cookies can't render as one entry — and it round-trips.
    assert repr(repeated) == "CaseInsensitiveDict([('Set-Cookie', 'a'), ('Set-Cookie', 'b')])"


def test_headers_update_accepts_keyword_arguments() -> None:
    headers = CaseInsensitiveDict({"Accept": "text/plain"})

    headers.update(accept="application/json")

    assert headers["Accept"] == "application/json"
    assert len(headers) == 1


def test_headers_update_accepts_a_non_mapping_keys_and_getitem_source() -> None:
    headers = CaseInsensitiveDict()

    headers.update(_KeysAndGetItem({"Content-Type": "application/json"}))

    assert headers["content-type"] == "application/json"


async def test_result_reports_the_http_version(client: HTTPClient) -> None:
    result = await client.get("items/7")

    assert result.http_version == "HTTP/1.1"


def test_sync_head_result_reports_the_http_version(sync_client: SyncHTTPClient) -> None:
    result = sync_client.head("items/7")

    assert result.http_version == "HTTP/1.1"


async def test_result_elapsed_covers_the_time_the_server_took(client: HTTPClient) -> None:
    result = await client.get("slow", params={"seconds": 0.05})

    assert 0.05 <= result.elapsed < 2.0


def test_sync_result_elapsed_covers_the_time_the_server_took(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("slow", params={"seconds": 0.05})

    assert 0.05 <= result.elapsed < 2.0


async def test_head_result_has_an_elapsed_time(client: HTTPClient) -> None:
    result = await client.head("items/7")

    assert result.elapsed >= 0.0


class _SetCookieHeaders(BaseModel):
    set_cookie: str


def _read_headers_first(headers: CaseInsensitiveDict, operation: str) -> None:
    match operation:
        case "repr":
            repr(headers)
        case "eq":
            _ = CaseInsensitiveDict() == headers
        case "iter":
            list(headers)
        case "len":
            len(headers)
        case "contains":
            _ = "set-cookie" in headers
        case "get_all":
            headers.get_all("set-cookie")
        case "setitem":
            headers["X-Added"] = "added"
        case "delitem":
            del headers["content-length"]
        case "update":
            headers.update({"X-Added": "added"})
        case "copy":
            headers.copy()
        case "pickle":
            pickle.dumps(headers)
        case _:
            raise AssertionError(operation)


# Response headers are converted on first read, so every way in must see the whole set.
@pytest.mark.parametrize(
    "operation",
    [
        "repr",
        "eq",
        "iter",
        "len",
        "contains",
        "get_all",
        "setitem",
        "delitem",
        "update",
        "copy",
        "pickle",
    ],
)
async def test_response_headers_are_complete_whichever_operation_reads_them_first(
    client: HTTPClient, operation: str
) -> None:
    result = await client.get("multi-set-cookie", into=dict)

    _read_headers_first(result.headers, operation)

    assert result.headers.get_all("set-cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers["Content-Type"] == "application/json"
    assert repr(result.headers).startswith("CaseInsensitiveDict([")


async def test_response_headers_keep_the_names_and_order_they_arrived_in(
    client: HTTPClient,
) -> None:
    result = await client.get("multi-set-cookie", into=dict)

    assert list(result.headers) == [
        "server",
        "date",
        "content-type",
        "content-length",
        "set-cookie",
    ]
    assert type(result.headers) is CaseInsensitiveDict


def test_sync_response_headers_keep_the_names_and_order_they_arrived_in(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get("multi-set-cookie", into=dict)

    assert list(result.headers) == [
        "server",
        "date",
        "content-type",
        "content-length",
        "set-cookie",
    ]
    assert type(result.headers) is CaseInsensitiveDict


async def test_response_headers_are_one_object_whose_mutations_persist(client: HTTPClient) -> None:
    result = await client.get("multi-set-cookie", into=dict)

    headers = result.headers
    headers["X-Added"] = "added"
    del headers["content-length"]

    assert result.headers is headers
    assert result.headers["x-added"] == "added"
    assert "content-length" not in result.headers
    assert result.headers.get_all("set-cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]


def test_sync_response_headers_are_one_object_whose_mutations_persist(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get("multi-set-cookie", into=dict)

    headers = result.headers
    headers["X-Added"] = "added"
    del headers["content-length"]

    assert result.headers is headers
    assert result.headers["x-added"] == "added"
    assert "content-length" not in result.headers
    assert result.headers.get_all("set-cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]


async def test_head_and_download_headers_are_one_object_whose_mutations_persist(
    client: HTTPClient,
) -> None:
    head = await client.head("items/7")
    downloaded = await client.download("multi-set-cookie")

    head.headers["X-Added"] = "added"
    downloaded.headers["X-Added"] = "added"

    assert head.headers is head.headers
    assert head.headers["x-added"] == "added"
    assert head.headers["X-Item-Exists"] == "true"
    assert downloaded.headers is downloaded.headers
    assert downloaded.headers["x-added"] == "added"
    assert downloaded.headers.get_all("set-cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]


def test_sync_head_and_download_headers_are_one_object_whose_mutations_persist(
    sync_client: SyncHTTPClient,
) -> None:
    head = sync_client.head("items/7")
    downloaded = sync_client.download("multi-set-cookie")

    head.headers["X-Added"] = "added"
    downloaded.headers["X-Added"] = "added"

    assert head.headers is head.headers
    assert head.headers["x-added"] == "added"
    assert head.headers["X-Item-Exists"] == "true"
    assert downloaded.headers is downloaded.headers
    assert downloaded.headers["x-added"] == "added"
    assert downloaded.headers.get_all("set-cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]


async def test_typed_headers_and_headers_both_come_from_the_response(client: HTTPClient) -> None:
    result = await client.get(
        "multi-set-cookie", into=dict, response_headers_type=_SetCookieHeaders
    )

    assert result.typed_headers.set_cookie == "session=abc; Path=/"
    assert result.headers is result.headers
    assert len(result.headers.get_all("set-cookie")) == 3


def test_sync_typed_headers_and_headers_both_come_from_the_response(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get("multi-set-cookie", into=dict, response_headers_type=_SetCookieHeaders)

    assert result.typed_headers.set_cookie == "session=abc; Path=/"
    assert result.headers is result.headers
    assert len(result.headers.get_all("set-cookie")) == 3


async def test_unread_response_headers_survive_pickling_and_copying(client: HTTPClient) -> None:
    pickled = await client.get("multi-set-cookie", into=dict)
    copied = await client.get("multi-set-cookie", into=dict)

    restored: Response[dict[str, Any]] = pickle.loads(pickle.dumps(pickled))  # noqa: S301 — round-tripping our own object
    deep_copy = copy.deepcopy(copied)

    for response in (restored, deep_copy):
        assert type(response.headers) is CaseInsensitiveDict
        assert response.headers.get_all("set-cookie") == [
            "session=abc; Path=/",
            "csrf=xyz; Path=/",
            "theme=dark; Path=/",
        ]
    assert restored.headers == pickled.headers
    assert deep_copy.headers == copied.headers
    assert deep_copy.headers is not copied.headers


async def test_response_headers_can_be_read_after_the_client_closes(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        result = await client.get("multi-set-cookie", into=dict)

    assert len(result.headers.get_all("set-cookie")) == 3


def test_sync_response_headers_can_be_read_on_another_thread_after_the_client_closes(
    base_url: str,
) -> None:
    with SyncHTTPClient(base_url=base_url) as client:
        result = client.get("multi-set-cookie", into=dict)

    with ThreadPoolExecutor(max_workers=1) as pool:
        set_cookies = pool.submit(result.headers.get_all, "set-cookie").result()

    assert len(set_cookies) == 3
