from typing import Annotated, Any

import msgspec
import pytest
from msgspec import Struct
from pydantic import BaseModel

from lothc import HTTPClient, SyncHTTPClient

# pylint: disable=duplicate-code
# ItemModel/ItemStruct are the same trivial two-field shapes as test_get.py's own, see there.


class ItemModel(BaseModel):
    id: int
    name: str


class ItemStruct(Struct):
    id: int
    name: str


class Line(Struct):
    i: int


class LineModel(BaseModel):
    i: int


class Tick(Struct):
    msg: str
    now: int


type ItemStructs = list[ItemStruct]
type Lines = Line | list[int]


async def test_get_decodes_a_top_level_array_into_a_list_of_structs(client: HTTPClient) -> None:
    items = (await client.get("json-array", into=list[ItemStruct])).data

    assert items == [ItemStruct(id=1, name="one"), ItemStruct(id=2, name="two")]


async def test_get_decodes_a_top_level_array_into_a_list_of_models(client: HTTPClient) -> None:
    items = (await client.get("json-array", into=list[ItemModel])).data

    assert items == [ItemModel(id=1, name="one"), ItemModel(id=2, name="two")]


def test_sync_get_decodes_a_top_level_array_into_a_list_of_structs(
    sync_client: SyncHTTPClient,
) -> None:
    items = sync_client.get("json-array", into=list[ItemStruct]).data

    assert items == [ItemStruct(id=1, name="one"), ItemStruct(id=2, name="two")]


def test_sync_get_decodes_a_top_level_array_into_a_list_of_models(
    sync_client: SyncHTTPClient,
) -> None:
    items = sync_client.get("json-array", into=list[ItemModel]).data

    assert items == [ItemModel(id=1, name="one"), ItemModel(id=2, name="two")]


async def test_get_decodes_into_a_union(client: HTTPClient) -> None:
    item = (await client.get("items/7", into=ItemStruct | list[int])).data

    assert item == ItemStruct(id=7, name="item-7")


def test_sync_get_decodes_into_a_union(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("json-literal", params={"value": "[1]"}, into=ItemModel | list[int]).data

    assert item == [1]


async def test_get_decodes_a_null_body_into_an_optional(client: HTTPClient) -> None:
    item = (await client.get("json-literal", params={"value": "null"}, into=ItemStruct | None)).data

    assert item is None


def test_sync_get_decodes_a_null_body_into_an_optional(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("json-literal", params={"value": "null"}, into=ItemModel | None).data

    assert item is None


async def test_get_decodes_into_a_type_alias(client: HTTPClient) -> None:
    items = (await client.get("json-array", into=ItemStructs)).data

    assert [item.id for item in items] == [1, 2]


def test_sync_get_decodes_into_a_type_alias(sync_client: SyncHTTPClient) -> None:
    items = sync_client.get("json-array", into=ItemStructs).data

    assert [item.id for item in items] == [1, 2]


async def test_get_decodes_a_list_of_ints(client: HTTPClient) -> None:
    numbers = (await client.get("json-literal", params={"value": "[1, 2]"}, into=list[int])).data

    assert numbers == [1, 2]


def test_sync_get_decodes_a_list_of_ints(sync_client: SyncHTTPClient) -> None:
    numbers = sync_client.get("json-literal", params={"value": "[1, 2]"}, into=list[int]).data

    assert numbers == [1, 2]


async def test_get_validates_a_list_of_ints(client: HTTPClient) -> None:
    # Same rule as a model `into`: the chosen library's own error, unwrapped.
    with pytest.raises(msgspec.ValidationError):
        await client.get("json-array", into=list[int])


async def test_get_decodes_into_an_unhashable_annotated_form(client: HTTPClient) -> None:
    numbers = (
        await client.get("json-literal", params={"value": "[1]"}, into=Annotated[list[int], {}])
    ).data

    assert numbers == [1]


async def test_get_decodes_into_explicit_dict_str_any(client: HTTPClient) -> None:
    item = (await client.get("items/7", into=dict[str, Any])).data

    assert item == {"id": 7, "name": "item-7"}


async def test_get_into_mixing_struct_and_model_raises_type_error(client: HTTPClient) -> None:
    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic BaseModels"):
        await client.get("json-array", into=list[ItemStruct | ItemModel])


def test_sync_get_into_mixing_struct_and_model_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic BaseModels"):
        sync_client.get("json-array", into=dict[str, ItemModel] | ItemStruct)


async def test_stream_get_decodes_ndjson_lines_into_a_list_form(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get(
            "ndjson", params={"count": 3, "shape": "array"}, into=list[int]
        )
    ]

    assert lines == [[0], [1], [2]]


async def test_stream_get_decodes_ndjson_lines_into_a_union_alias(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get(
            "ndjson", params={"count": 3, "shape": "mixed"}, into=Lines
        )
    ]

    assert lines == [Line(i=0), [1], Line(i=2)]


def test_sync_stream_get_decodes_ndjson_lines_into_a_list_form(
    sync_client: SyncHTTPClient,
) -> None:
    lines = list(
        sync_client.stream_get("ndjson", params={"count": 2, "shape": "array"}, into=list[int])
    )

    assert lines == [[0], [1]]


async def test_stream_post_decodes_ndjson_lines_into_a_union(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_post(
            "ndjson-echo", json={"n": 3, "shape": "mixed"}, into=LineModel | list[int]
        )
    ]

    assert lines == [LineModel(i=0), [1], LineModel(i=2)]


def test_sync_stream_post_decodes_ndjson_lines_into_a_union(sync_client: SyncHTTPClient) -> None:
    lines = list(
        sync_client.stream_post("ndjson-echo", json={"n": 3, "shape": "mixed"}, into=Lines)
    )

    assert lines == [Line(i=0), [1], Line(i=2)]


async def test_sse_decodes_data_into_a_form(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", params={"count": 2}, into=Tick | None)]

    assert [event.data for event in events] == [
        Tick(msg="hello 0", now=0),
        Tick(msg="hello 1", now=100),
    ]


def test_sync_sse_decodes_data_into_a_form(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("events", params={"count": 2}, into=dict[str, str | int]))

    assert [event.data for event in events] == [
        {"msg": "hello 0", "now": 0},
        {"msg": "hello 1", "now": 100},
    ]
