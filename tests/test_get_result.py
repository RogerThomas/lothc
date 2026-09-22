from collections.abc import KeysView
from dataclasses import dataclass

from msgspec import Struct
from pydantic import BaseModel

from lothc import CaseInsensitiveDict, HTTPClient, SyncHTTPClient


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


async def test_get_result_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.get_result("items/7", response_data_type=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="item-7")


async def test_get_result_includes_raw_headers(client: HTTPClient) -> None:
    result = await client.get_result("items/7", response_data_type=ItemModel)

    assert "content-type" in result.headers


async def test_get_result_with_typed_headers(client: HTTPClient) -> None:
    result = await client.get_result(
        "items/7", response_data_type=ItemModel, response_headers_type=EchoedHeaders
    )

    assert result.typed_headers is not None


async def test_get_result_with_msgspec_typed_headers(client: HTTPClient) -> None:
    result = await client.get_result(
        "items/7", response_data_type=ItemModel, response_headers_type=EchoedHeadersStruct
    )

    assert result.typed_headers is not None


async def test_get_result_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    result = await client.get_result("boom", error_for_status=False)

    assert result.status == 500
    assert b"internal-server-error" in result.data


def test_sync_get_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get_result("items/7", response_data_type=ItemModel)

    assert result.status == 200
    assert result.data == ItemModel(id=7, name="item-7")


def test_sync_get_result_includes_raw_headers(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get_result("items/7", response_data_type=ItemModel)

    assert "content-type" in result.headers


def test_sync_get_result_with_typed_headers(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get_result(
        "items/7", response_data_type=ItemModel, response_headers_type=EchoedHeaders
    )

    assert result.typed_headers is not None


def test_sync_get_result_error_for_status_false_suppresses_raise(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get_result("boom", error_for_status=False)

    assert result.status == 500
    assert b"internal-server-error" in result.data


async def test_get_result_headers_are_case_insensitive(client: HTTPClient) -> None:
    result = await client.get_result("items/7", response_data_type=ItemModel)

    assert result.headers["Content-Type"] == result.headers["content-type"]
    assert result.headers["CONTENT-TYPE"] == result.headers["content-type"]
    assert "Content-Type" in result.headers
    assert result.headers.get("CoNtEnT-tYpE") is not None
    assert result.headers == {name.upper(): value for name, value in result.headers.items()}


def test_sync_get_result_headers_are_case_insensitive(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get_result("items/7", response_data_type=ItemModel)

    assert result.headers["Content-Type"] == result.headers["content-type"]
    assert "Content-Type" in result.headers
    assert result.headers.get("CoNtEnT-tYpE") is not None


async def test_get_result_headers_keep_every_value_of_a_repeated_header(
    client: HTTPClient,
) -> None:
    result = await client.get_result("multi-set-cookie", response_data_type=dict)

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
    result = sync_client.get_result("multi-set-cookie", response_data_type=dict)

    assert result.headers.get_all("Set-Cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers["set-cookie"] == "session=abc; Path=/"


async def test_get_result_headers_get_all_returns_a_copy(client: HTTPClient) -> None:
    result = await client.get_result("multi-set-cookie", response_data_type=dict)

    result.headers.get_all("Set-Cookie").clear()

    assert len(result.headers.get_all("Set-Cookie")) == 3


async def test_copying_headers_keeps_every_repeated_value(client: HTTPClient) -> None:
    # Mapping.items() is single-valued, so a copy taken through it would silently drop the extra
    # values — the one thing CaseInsensitiveDict exists to keep.
    result = await client.get_result("multi-set-cookie", response_data_type=dict)

    assert CaseInsensitiveDict(result.headers).get_all("set-cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers.copy().get_all("set-cookie") == result.headers.get_all("set-cookie")


async def test_updating_headers_keeps_every_repeated_value(client: HTTPClient) -> None:
    result = await client.get_result("multi-set-cookie", response_data_type=dict)
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
