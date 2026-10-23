"""`into=` targets that need neither decode extra: this module deliberately never names either
library, so `task test-no-extras` runs it with both uninstalled."""

import json
from collections import OrderedDict, defaultdict
from typing import Any, assert_type

import pytest

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


class ItemDict(dict[str, Any]):
    pass


class TextError(HTTPResponseError[str]):
    pass


async def test_get_into_bytes_returns_the_raw_body(client: HTTPClient) -> None:
    body = (await client.get("items/7", into=bytes)).data

    assert json.loads(body) == {"id": 7, "name": "item-7"}


def test_sync_get_into_bytes_returns_the_raw_body(sync_client: SyncHTTPClient) -> None:
    body = sync_client.get("items/7", into=bytes).data

    assert json.loads(body) == {"id": 7, "name": "item-7"}


async def test_get_into_dict_decodes_a_json_object(client: HTTPClient) -> None:
    item = (await client.get("items/7", into=dict)).data

    assert item == {"id": 7, "name": "item-7"}


async def test_get_into_dict_str_any_decodes_a_json_object(client: HTTPClient) -> None:
    item = (await client.get("items/7", into=dict[str, Any])).data

    assert item == {"id": 7, "name": "item-7"}


def test_sync_get_into_dict_str_any_decodes_a_json_object(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", into=dict[str, Any]).data

    assert item == {"id": 7, "name": "item-7"}


async def test_stream_get_into_bytes_yields_raw_chunks(client: HTTPClient) -> None:
    chunks = [chunk async for chunk in client.stream_get("binary", into=bytes)]

    assert b"".join(chunks) == b"AAA\nBBB\x00\nCCC"


def test_sync_stream_get_into_dict_str_any_decodes_each_line(sync_client: SyncHTTPClient) -> None:
    lines = list(sync_client.stream_get("ndjson", params={"count": 2}, into=dict[str, Any]))

    assert lines == [{"i": 0}, {"i": 1}]


async def test_sse_into_bytes_keeps_data_raw(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", params={"count": 1}, into=bytes)]

    assert events[0].data == b'{"msg": "hello 0", "now": 0}'


@pytest.mark.parametrize("form", [OrderedDict, defaultdict, ItemDict])
async def test_get_into_a_dict_subclass_builds_that_subclass(
    client: HTTPClient,
    form: type[dict[Any, Any]],
) -> None:
    item = (await client.get("items/7", into=form)).data

    assert type(item) is form
    assert item == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("form", [OrderedDict, defaultdict, ItemDict])
def test_sync_get_into_a_dict_subclass_builds_that_subclass(
    sync_client: SyncHTTPClient,
    form: type[dict[Any, Any]],
) -> None:
    item = sync_client.get("items/7", into=form).data

    assert type(item) is form
    assert item == {"id": 7, "name": "item-7"}


async def test_get_into_a_defaultdict_has_no_default_factory(client: HTTPClient) -> None:
    item = (await client.get("items/7", into=defaultdict)).data

    assert isinstance(item, defaultdict)
    assert item.default_factory is None


async def test_stream_get_into_a_dict_subclass_builds_it_per_line(client: HTTPClient) -> None:
    lines = [line async for line in client.stream_get("ndjson", params={"count": 2}, into=ItemDict)]

    assert [type(line) for line in lines] == [ItemDict, ItemDict]
    assert lines == [{"i": 0}, {"i": 1}]


def test_sync_stream_get_into_a_dict_subclass_builds_it_per_line(
    sync_client: SyncHTTPClient,
) -> None:
    lines = list(sync_client.stream_get("ndjson", params={"count": 2}, into=OrderedDict))

    assert [type(line) for line in lines] == [OrderedDict, OrderedDict]


async def test_get_into_str_returns_the_body_text_unparsed(client: HTTPClient) -> None:
    text = (await client.get("json-literal", params={"value": "hello"}, into=str)).data

    assert_type(text, str)
    assert text == "hello"


def test_sync_get_into_str_returns_the_body_text_unparsed(sync_client: SyncHTTPClient) -> None:
    text = sync_client.get("json-literal", params={"value": '"a"'}, into=str).data

    assert_type(text, str)
    assert text == '"a"'


async def test_get_into_str_rejects_invalid_utf8(client: HTTPClient) -> None:
    with pytest.raises(UnicodeDecodeError):
        await client.get("invalid-utf8", into=str)


async def test_sse_into_str_matches_the_default(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events-weird", into=str)]

    assert_type(events[0].data, str)
    assert [event.data for event in events] == ["hello"]


def test_sync_sse_into_str_matches_the_default(sync_client: SyncHTTPClient) -> None:
    default = [event.data for event in sync_client.sse("events", params={"count": 2})]
    as_str = [event.data for event in sync_client.sse("events", params={"count": 2}, into=str)]

    assert as_str == default


async def test_stream_get_into_str_yields_each_raw_line(client: HTTPClient) -> None:
    lines = [line async for line in client.stream_get("binary", into=str)]

    assert_type(lines, list[str])
    assert lines == ["AAA", "BBB\x00", "CCC"]


def test_sync_stream_post_into_str_yields_each_raw_line(sync_client: SyncHTTPClient) -> None:
    lines = list(sync_client.stream_post("ndjson-echo", json={"n": 2}, into=str))

    assert_type(lines, list[str])
    assert lines == ['{"i": 0}', '{"i": 1}']


async def test_a_str_pinned_error_is_the_body_text(client: HTTPClient) -> None:
    with pytest.raises(TextError) as exc_info:
        await client.get("invalid-json-error", raises=TextError)

    assert exc_info.value.error == "<html><body>502 Bad Gateway</body></html>"
    assert exc_info.value.parse_error is None


def test_sync_a_str_pinned_error_is_the_body_text(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(TextError) as exc_info:
        sync_client.get("invalid-json-error", raises=TextError)

    assert exc_info.value.error == "<html><body>502 Bad Gateway</body></html>"
