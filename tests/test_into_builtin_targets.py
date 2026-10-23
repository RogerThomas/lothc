"""`into=` targets that need neither decode extra: this module deliberately never names either
library, so `task test-no-extras` runs it with both uninstalled."""

import json
from typing import Any

from lothc import HTTPClient, SyncHTTPClient


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
