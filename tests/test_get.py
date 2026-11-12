import json
from typing import Any, cast

import pytest
from _helpers import echoed_headers
from msgspec import Struct
from msgspec.json import Decoder
from pydantic import BaseModel, TypeAdapter, ValidationError

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient

# pylint: disable=duplicate-code
# ItemModel/ItemStruct below are the same trivial fixture shape as test_write.py's own — not
# shared via a fixture since each is a two-line, self-contained class, and style-guide.md's
# "almost never use globals" preference for tests already argues against a shared module-level
# constant instead. duplicate-code disabled file-wide (a line-level pragma doesn't suppress this
# particular check — confirmed live) rather than for just this one pair.


class ItemModel(BaseModel):
    id: int
    name: str


class ItemStruct(Struct):
    id: int
    name: str


class SearchParamsModel(BaseModel):
    q: str
    page: int
    limit: int | None = None


class SearchParamsStruct(Struct):
    q: str
    page: int
    cursor: str | None = None


async def test_get_returns_raw_bytes_by_default(client: HTTPClient) -> None:
    body = (await client.get("items/7")).data

    assert isinstance(body, bytes)
    assert json.loads(body) == {"id": 7, "name": "item-7"}


async def test_get_decodes_pydantic_model(client: HTTPClient) -> None:
    item = (await client.get("items/7", into=ItemModel)).data
    assert item == ItemModel(id=7, name="item-7")


async def test_get_decodes_msgspec_struct(client: HTTPClient) -> None:
    item = (await client.get("items/7", into=ItemStruct)).data
    assert item == ItemStruct(id=7, name="item-7")


async def test_get_with_raw_dict_params(client: HTTPClient) -> None:
    result = (await client.get("items", params={"q": "pikachu", "page": 2}, into=dict)).data
    assert result == {"q": "pikachu", "page": 2, "items": [{"id": 2, "name": "pikachu-match"}]}


async def test_get_with_repeated_query_param(client: HTTPClient) -> None:
    """A `list[...]` `params` value sends that key once per element, verbatim — see `Params`'s
    own doc comment in `lothc/_client.py`."""
    result = (
        await client.get("echo-query", params={"tag": ["a", "b"], "limit": 10}, into=dict)
    ).data
    assert result == {"query": {"tag": ["a", "b"], "limit": ["10"]}}


async def test_get_with_repeated_query_param_as_tuple(client: HTTPClient) -> None:
    """A `tuple[...]` value works exactly like `list[...]` — both spell "repeat this key"."""
    result = (await client.get("echo-query", params={"tag": ("a", "b")}, into=dict)).data
    assert result == {"query": {"tag": ["a", "b"]}}


async def test_get_with_scalar_query_params_still_works(client: HTTPClient) -> None:
    result = (await client.get("echo-query", params={"page": "2"}, into=dict)).data
    assert result == {"query": {"page": ["2"]}}


async def test_get_writes_query_values_as_their_wire_strings(client: HTTPClient) -> None:
    # Bools lowercase, numbers as written, and reserved/non-ASCII characters escaped so they
    # round-trip (the server sees the decoded values).
    result = (
        await client.get(
            "echo-query",
            params={"on": True, "off": False, "count": 3, "ratio": 1.5, "text": "a b&c=ü"},
            into=dict,
        )
    ).data

    assert result == {
        "query": {
            "on": ["true"],
            "off": ["false"],
            "count": ["3"],
            "ratio": ["1.5"],
            "text": ["a b&c=ü"],
        }
    }


async def test_get_writes_an_int_outside_64_bits_as_its_exact_digits(client: HTTPClient) -> None:
    # pyreqwest turns an int past the signed 64-bit range into a float (`9.223372036854776e18`),
    # silently changing it; it goes out as its own digits, a repeat's elements included.
    result = (
        await client.get(
            "echo-query",
            params={"over": 2**63, "under": -(2**63) - 1, "max": 2**63 - 1, "tag": [2**64, 1]},
            into=dict,
        )
    ).data

    assert result == {
        "query": {
            "over": ["9223372036854775808"],
            "under": ["-9223372036854775809"],
            "max": ["9223372036854775807"],
            "tag": ["18446744073709551616", "1"],
        }
    }


async def test_get_with_pydantic_params_omits_none_fields(client: HTTPClient) -> None:
    result = (
        await client.get("echo-query", params=SearchParamsModel(q="pikachu", page=1), into=dict)
    ).data
    assert result == {"query": {"q": ["pikachu"], "page": ["1"]}}


async def test_get_with_msgspec_params_omits_none_fields(client: HTTPClient) -> None:
    result = (
        await client.get("echo-query", params=SearchParamsStruct(q="pikachu", page=1), into=dict)
    ).data
    assert result == {"query": {"q": ["pikachu"], "page": ["1"]}}


async def test_get_with_raw_headers(client: HTTPClient) -> None:
    result = (
        await client.get("echo-headers", headers={"x-custom": "header-value"}, into=dict)
    ).data
    headers = echoed_headers(result)
    assert headers["x-custom"] == "header-value"


class TypedHeadersModel(BaseModel):
    x_custom: str
    x_omitted: str | None = None


class TypedHeadersStruct(Struct):
    x_custom: str
    x_omitted: str | None = None


async def test_get_with_pydantic_headers_omits_none_fields(client: HTTPClient) -> None:
    result = (
        await client.get(
            "echo-headers",
            headers=TypedHeadersModel(x_custom="header-value"),
            into=dict,
        )
    ).data
    headers = echoed_headers(result)
    assert headers["x-custom"] == "header-value"
    assert "x-omitted" not in headers


async def test_get_with_msgspec_headers_omits_none_fields(client: HTTPClient) -> None:
    result = (
        await client.get(
            "echo-headers",
            headers=TypedHeadersStruct(x_custom="header-value"),
            into=dict,
        )
    ).data
    headers = echoed_headers(result)
    assert headers["x-custom"] == "header-value"
    assert "x-omitted" not in headers


async def test_default_headers_sent_on_every_request(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, default_headers={"x-api-key": "secret"}) as client:
        result = (await client.get("echo-headers", into=dict)).data

    headers = echoed_headers(result)
    assert headers["x-api-key"] == "secret"


async def test_get_raises_response_error_for_status(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom")
    assert exc_info.value.status == 500


async def test_get_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    body = (await client.get("boom", error_for_status=False)).data
    assert b"internal-server-error" in body


async def test_get_into_not_a_type_raises_type_error(client: HTTPClient) -> None:
    with pytest.raises(TypeError, match="Unsupported into: 1"):
        await client.get("items/7", into=cast(Any, 1))


def test_sync_get_decodes_pydantic_model(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", into=ItemModel).data
    assert item == ItemModel(id=7, name="item-7")


def test_sync_get_decodes_msgspec_struct(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", into=ItemStruct).data
    assert item == ItemStruct(id=7, name="item-7")


def test_sync_get_decodes_plain_dict(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", into=dict).data
    assert item == {"id": 7, "name": "item-7"}


def test_sync_get_writes_query_values_as_their_wire_strings(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get(
        "echo-query",
        params={"on": True, "off": False, "count": 3, "ratio": 1.5, "text": "a b&c=ü"},
        into=dict,
    ).data

    assert result == {
        "query": {
            "on": ["true"],
            "off": ["false"],
            "count": ["3"],
            "ratio": ["1.5"],
            "text": ["a b&c=ü"],
        }
    }


def test_sync_get_writes_an_int_outside_64_bits_as_its_exact_digits(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get(
        "echo-query",
        params={"over": 2**63, "under": -(2**63) - 1, "max": 2**63 - 1, "tag": [2**64, 1]},
        into=dict,
    ).data

    assert result == {
        "query": {
            "over": ["9223372036854775808"],
            "under": ["-9223372036854775809"],
            "max": ["9223372036854775807"],
            "tag": ["18446744073709551616", "1"],
        }
    }


def test_sync_get_with_repeated_query_param(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("echo-query", params={"tag": ["a", "b"], "limit": 10}, into=dict).data
    assert result == {"query": {"tag": ["a", "b"], "limit": ["10"]}}


def test_sync_get_raises_response_error_for_status(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom")
    assert exc_info.value.status == 500


def test_sync_get_error_for_status_false_suppresses_raise(sync_client: SyncHTTPClient) -> None:
    body = sync_client.get("boom", error_for_status=False).data
    assert b"internal-server-error" in body


def test_sync_get_into_not_a_type_raises_type_error(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(TypeError, match="Unsupported into: 1"):
        sync_client.get("items/7", into=cast(Any, 1))


async def test_get_decodes_a_top_level_array_with_a_pydantic_type_adapter(
    client: HTTPClient,
) -> None:
    items = (await client.get("json-array", into=TypeAdapter(list[ItemModel]))).data

    assert items == [ItemModel(id=1, name="one"), ItemModel(id=2, name="two")]


async def test_get_decodes_a_top_level_array_with_a_msgspec_decoder(client: HTTPClient) -> None:
    items = (await client.get("json-array", into=Decoder(list[ItemStruct]))).data

    assert items == [ItemStruct(id=1, name="one"), ItemStruct(id=2, name="two")]


async def test_response_decodes_a_top_level_array(client: HTTPClient) -> None:
    result = await client.get("json-array", into=TypeAdapter(list[ItemModel]))

    assert result.status == 200
    assert [item.id for item in result.data] == [1, 2]


def test_sync_get_decodes_a_top_level_array(sync_client: SyncHTTPClient) -> None:
    items = sync_client.get("json-array", into=Decoder(list[ItemStruct])).data

    assert [item.id for item in items] == [1, 2]


async def test_a_type_adapter_decode_failure_propagates_natively(client: HTTPClient) -> None:
    # Same rule as a model `into`: the chosen library's own error, unwrapped.
    with pytest.raises(ValidationError):
        await client.get("json-array", into=TypeAdapter(list[int]))


def test_sync_get_decodes_a_top_level_array_with_a_type_adapter(
    sync_client: SyncHTTPClient,
) -> None:
    items = sync_client.get("json-array", into=TypeAdapter(list[ItemModel])).data

    assert [item.id for item in items] == [1, 2]


@pytest.mark.parametrize(
    "headers", [{"bad name": "value"}, {"x-name": "bad\nvalue"}], ids=["name", "value"]
)
async def test_an_invalid_header_raises_value_error_before_anything_is_sent(
    client: HTTPClient, server_key: str, headers: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="header") as exc_info:
        await client.get("status-then-ok", params={"key": server_key}, headers=headers)
    hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert not type(exc_info.value).__module__.startswith("pyreqwest")
    assert hits == {"hits": 0}


@pytest.mark.parametrize(
    "headers", [{"bad name": "value"}, {"x-name": "bad\nvalue"}], ids=["name", "value"]
)
def test_sync_an_invalid_header_raises_value_error_before_anything_is_sent(
    sync_client: SyncHTTPClient, server_key: str, headers: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="header") as exc_info:
        sync_client.get("status-then-ok", params={"key": server_key}, headers=headers)
    hits = sync_client.get("hits", params={"key": server_key}, into=dict).data

    assert not type(exc_info.value).__module__.startswith("pyreqwest")
    assert hits == {"hits": 0}
