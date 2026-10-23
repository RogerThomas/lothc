from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Any, NamedTuple, TypedDict

import annotated_types
import msgspec
import pydantic
import pytest
from msgspec import Struct
from pydantic import AfterValidator, BaseModel, Field, GetCoreSchemaHandler
from pydantic_core import CoreSchema

from lothc import HTTPClient, SyncHTTPClient
from lothc.testing import LOTHCMocker

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


@pydantic.dataclasses.dataclass
class PositiveItem:
    id: int = Field(gt=0)


class ModelHolder(TypedDict):
    item: ItemModel


class NestedNode(TypedDict):
    id: int
    children: list["NestedNode"]
    item: ItemModel | None


@dataclass
class ModelBox:
    item: ItemModel


class ModelPair(NamedTuple):
    first: ItemModel
    second: ItemModel


class SchemaCounter:
    """`Annotated` metadata pydantic acts on (its schema hook), which counts how many times a
    decoder was built for a form carrying it. Compares by identity, like pydantic's `FieldInfo`."""

    def __init__(self) -> None:
        self.builds = 0

    def __get_pydantic_core_schema__(
        self,
        source: type[Any],
        handler: GetCoreSchemaHandler,
    ) -> CoreSchema:
        self.builds += 1
        return handler(source)


class EqualSchemaCounter(SchemaCounter):
    """A `SchemaCounter` that compares by value (its `name`), like most metadata. Each test needs
    its own `name`: `typing` caches `Annotated[...]` by equality, so an equal counter from another
    test would be the one pydantic sees."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EqualSchemaCounter) and other.name == self.name

    def __hash__(self) -> int:
        return hash(self.name)


def _positive(value: int) -> int:
    if value <= 0:
        raise ValueError("not positive")
    return value


type ItemStructs = list[ItemStruct]
type Lines = Line | list[int]
type PositiveInts = list[Annotated[int, Field(gt=0)]]
# Module level because a `type` statement is (checkers reject one in a function); the test reads
# the counter's builds relative to their value on entry, so other tests can't disturb it.
alias_counter = SchemaCounter()
type CountedInts = list[Annotated[int, alias_counter]]


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
    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic"):
        await client.get("json-array", into=list[ItemStruct | ItemModel])


def test_sync_get_into_mixing_struct_and_model_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic"):
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


@pytest.mark.parametrize(
    "form",
    [
        list[Annotated[int, Field(gt=0)]],
        list[Annotated[int, annotated_types.Gt(0)]],
        list[Annotated[int, AfterValidator(_positive)]],
        PositiveInts,
    ],
)
async def test_get_enforces_pydantic_only_metadata_on_a_form_without_models(
    client: HTTPClient,
    form: Any,  # noqa: ANN401 — the forms differ in type, which isn't what's under test
) -> None:
    with pytest.raises(pydantic.ValidationError):
        await client.get("json-literal", params={"value": "[-5]"}, into=form)


def test_sync_get_enforces_pydantic_field_metadata(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(pydantic.ValidationError):
        sync_client.get(
            "json-literal", params={"value": "[-5]"}, into=list[Annotated[int, Field(gt=0)]]
        )


async def test_get_enforces_a_pydantic_dataclass_field_constraint(client: HTTPClient) -> None:
    with pytest.raises(pydantic.ValidationError):
        await client.get("json-literal", params={"value": '{"id": -1}'}, into=PositiveItem)


def test_sync_get_enforces_a_pydantic_dataclass_field_constraint(
    sync_client: SyncHTTPClient,
) -> None:
    item = sync_client.get("json-literal", params={"value": '{"id": 1}'}, into=PositiveItem).data

    assert item == PositiveItem(id=1)
    with pytest.raises(pydantic.ValidationError):
        sync_client.get("json-literal", params={"value": '{"id": -1}'}, into=PositiveItem)


async def test_get_keeps_plain_and_msgspec_metadata_on_msgspec(client: HTTPClient) -> None:
    # msgspec's own error type shows which library decoded it.
    with pytest.raises(msgspec.ValidationError):
        await client.get(
            "json-literal", params={"value": "[-5]"}, into=list[Annotated[int, msgspec.Meta(gt=0)]]
        )
    with pytest.raises(msgspec.ValidationError):
        await client.get(
            "json-literal", params={"value": '["a"]'}, into=list[Annotated[int, "doc"]]
        )


async def test_get_into_a_struct_with_pydantic_metadata_raises_type_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic"):
        await client.get("json-array", into=list[Annotated[ItemStruct, Field(min_length=1)]])


async def test_get_rebuilds_an_inline_form_whose_metadata_compares_by_identity(
    client: HTTPClient,
) -> None:
    # Such a form is a new cache key each time it's written inline, so caching it would grow
    # the cache on every call: it's rebuilt instead.
    counter = SchemaCounter()

    await client.get("json-literal", params={"value": "[1]"}, into=list[Annotated[int, counter]])
    builds = counter.builds
    await client.get("json-literal", params={"value": "[1]"}, into=list[Annotated[int, counter]])

    assert builds > 0
    assert counter.builds == 2 * builds


def test_sync_get_reuses_the_decoder_of_a_type_alias_with_identity_metadata(
    sync_client: SyncHTTPClient,
) -> None:
    sync_client.get("json-literal", params={"value": "[1]"}, into=CountedInts)
    builds = alias_counter.builds
    numbers = sync_client.get("json-literal", params={"value": "[2]"}, into=CountedInts).data

    assert numbers == [2]
    assert builds > 0
    assert alias_counter.builds == builds


async def test_get_decodes_a_typed_dict_holding_a_model(client: HTTPClient) -> None:
    holder = (
        await client.get(
            "json-literal", params={"value": '{"item": {"id": 1, "name": "one"}}'}, into=ModelHolder
        )
    ).data

    assert holder == {"item": ItemModel(id=1, name="one")}


def test_sync_get_decodes_a_typed_dict_holding_a_model(sync_client: SyncHTTPClient) -> None:
    holder = sync_client.get(
        "json-literal", params={"value": '{"item": {"id": 1, "name": "one"}}'}, into=ModelHolder
    ).data

    assert holder == {"item": ItemModel(id=1, name="one")}


async def test_get_decodes_a_recursive_typed_dict_holding_a_model(client: HTTPClient) -> None:
    child = '{"id": 2, "children": [], "item": {"id": 3, "name": "three"}}'
    body = f'{{"id": 1, "item": null, "children": [{child}]}}'

    node = (await client.get("json-literal", params={"value": body}, into=NestedNode)).data

    assert node["children"][0]["item"] == ItemModel(id=3, name="three")


async def test_get_decodes_a_dataclass_holding_a_model(client: HTTPClient) -> None:
    box = (
        await client.get(
            "json-literal", params={"value": '{"item": {"id": 1, "name": "one"}}'}, into=ModelBox
        )
    ).data

    assert box == ModelBox(item=ItemModel(id=1, name="one"))


def test_sync_get_decodes_a_named_tuple_of_models(sync_client: SyncHTTPClient) -> None:
    pair = sync_client.get(
        "json-literal",
        params={"value": '[{"id": 1, "name": "one"}, {"id": 2, "name": "two"}]'},
        into=ModelPair,
    ).data

    assert pair == ModelPair(ItemModel(id=1, name="one"), ItemModel(id=2, name="two"))


async def test_post_with_an_unusable_into_sends_nothing(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/items", data={"id": 1})

    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic"):
        await client.post("items", json={"id": 1}, into=ItemStruct | ItemModel)

    assert lothc_mocker.get_call_count() == 0


async def test_get_put_patch_delete_with_an_unusable_into_send_nothing(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data={"id": 7})
    lothc_mocker.add_put_response(path="/items/7", data={"id": 7})
    lothc_mocker.add_patch_response(path="/items/7", data={"id": 7})
    lothc_mocker.add_delete_response(path="/items/7", data={"id": 7})
    into = ItemStruct | ItemModel

    with pytest.raises(TypeError):
        await client.get("items/7", into=into)
    with pytest.raises(TypeError):
        await client.put("items/7", json={"id": 7}, into=into)
    with pytest.raises(TypeError):
        await client.patch("items/7", json={"id": 7}, into=into)
    with pytest.raises(TypeError):
        await client.delete("items/7", into=into)

    assert lothc_mocker.get_call_count() == 0


def test_sync_post_with_an_unusable_into_sends_nothing(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/items", data={"id": 1})
    lothc_mocker.add_delete_response(path="/items/7", data={"id": 7})

    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic"):
        sync_client.post("items", json={"id": 1}, into=ItemStruct | ItemModel)
    with pytest.raises(TypeError):
        sync_client.delete("items/7", into=ItemStruct | ItemModel)

    assert lothc_mocker.get_call_count() == 0


async def test_get_reuses_the_decoder_of_an_inline_validator_of_a_module_function(
    client: HTTPClient,
) -> None:
    # `AfterValidator(_positive)` is equal and equal-hashing every time it's written, so the
    # inline form is one cache entry: built once, not per call.
    counter = EqualSchemaCounter("async")

    numbers = (
        await client.get(
            "json-literal",
            params={"value": "[1]"},
            into=list[Annotated[int, AfterValidator(_positive), counter]],
        )
    ).data
    builds = counter.builds
    with pytest.raises(pydantic.ValidationError):
        await client.get(
            "json-literal",
            params={"value": "[-5]"},
            into=list[Annotated[int, AfterValidator(_positive), counter]],
        )

    assert numbers == [1]
    assert builds > 0
    assert counter.builds == builds


def test_sync_get_reuses_the_decoder_of_an_inline_validator_of_a_module_function(
    sync_client: SyncHTTPClient,
) -> None:
    counter = EqualSchemaCounter("sync")

    sync_client.get(
        "json-literal",
        params={"value": '{"a": 1}'},
        into=dict[str, Annotated[int, AfterValidator(_positive), counter]],
    )
    builds = counter.builds
    with pytest.raises(pydantic.ValidationError):
        sync_client.get(
            "json-literal",
            params={"value": '{"a": -5}'},
            into=dict[str, Annotated[int, AfterValidator(_positive), counter]],
        )

    assert builds > 0
    assert counter.builds == builds


async def test_get_decodes_an_inline_lambda_validator(client: HTTPClient) -> None:
    for _ in range(2):
        numbers = (
            await client.get(
                "json-literal",
                params={"value": "[1]"},
                into=list[Annotated[int, AfterValidator(lambda value: value * 2)]],
            )
        ).data

        assert numbers == [2]


def test_sync_get_decodes_an_inline_lambda_validator(sync_client: SyncHTTPClient) -> None:
    for _ in range(2):
        numbers = sync_client.get(
            "json-literal",
            params={"value": "[1]"},
            into=list[Annotated[int, AfterValidator(lambda value: value + 1)]],
        ).data

        assert numbers == [2]


async def test_get_decodes_types_msgspec_cannot_with_pydantic(client: HTTPClient) -> None:
    # msgspec accepts these forms but can't decode any of them from JSON, so pydantic does.
    path = (await client.get("json-literal", params={"value": '"a/b"'}, into=Path)).data
    numbers = (await client.get("json-literal", params={"value": "[1, 2]"}, into=deque[int])).data
    address = (
        await client.get("json-literal", params={"value": '"127.0.0.1"'}, into=IPv4Address)
    ).data

    assert path == Path("a/b")
    assert numbers == deque([1, 2])
    assert type(numbers) is deque
    assert address == IPv4Address("127.0.0.1")


def test_sync_get_decodes_types_msgspec_cannot_with_pydantic(
    sync_client: SyncHTTPClient,
) -> None:
    path = sync_client.get("json-literal", params={"value": '"a/b"'}, into=Path).data
    numbers = sync_client.get("json-literal", params={"value": "[1, 2]"}, into=deque[int]).data
    address = sync_client.get(
        "json-literal", params={"value": '"127.0.0.1"'}, into=IPv4Address
    ).data

    assert path == Path("a/b")
    assert numbers == deque([1, 2])
    assert type(numbers) is deque
    assert address == IPv4Address("127.0.0.1")


async def test_get_decodes_a_list_of_a_type_msgspec_cannot(client: HTTPClient) -> None:
    paths = (await client.get("json-literal", params={"value": '["a"]'}, into=list[Path])).data

    assert paths == [Path("a")]


async def test_get_decodes_parametrized_dict_subclasses(client: HTTPClient) -> None:
    ordered = (
        await client.get("json-literal", params={"value": '{"a": 1}'}, into=OrderedDict[str, int])
    ).data
    counter = (
        await client.get("json-literal", params={"value": '{"a": 1}'}, into=Counter[str])
    ).data

    assert type(ordered) is OrderedDict
    assert ordered == {"a": 1}
    assert type(counter) is Counter
    assert counter == {"a": 1}


def test_sync_get_decodes_parametrized_dict_subclasses(sync_client: SyncHTTPClient) -> None:
    ordered = sync_client.get(
        "json-literal", params={"value": '{"a": 1}'}, into=OrderedDict[str, int]
    ).data
    counter = sync_client.get("json-literal", params={"value": '{"a": 1}'}, into=Counter[str]).data

    assert type(ordered) is OrderedDict
    assert ordered == {"a": 1}
    assert type(counter) is Counter
    assert counter == {"a": 1}


async def test_get_validates_a_parametrized_dict_subclass_values(client: HTTPClient) -> None:
    with pytest.raises(pydantic.ValidationError):
        await client.get("json-literal", params={"value": '{"a": "b"}'}, into=OrderedDict[str, int])


async def test_get_still_decodes_a_type_msgspec_supports_with_msgspec(client: HTTPClient) -> None:
    # Which library's `ValidationError` a bad value raises is what shows who decoded it.
    with pytest.raises(msgspec.ValidationError):
        await client.get("json-literal", params={"value": '"not-a-date"'}, into=datetime)
