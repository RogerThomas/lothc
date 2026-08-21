import json
from typing import TypedDict

import pytest
from msgspec import Struct
from pydantic import BaseModel

from lothc import JSON, HTTPClient, HTTPResponseError, SyncHTTPClient


class ItemModel(BaseModel):
    id: int
    name: str


class ItemStruct(Struct):
    id: int
    name: str


class ItemDict(TypedDict):
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
    body = await client.get("items/7")

    assert isinstance(body, bytes)
    assert json.loads(body) == {"id": 7, "name": "item-7"}


async def test_get_decodes_pydantic_model(client: HTTPClient) -> None:
    item = await client.get("items/7", response_data_type=ItemModel)
    assert item == ItemModel(id=7, name="item-7")


async def test_get_decodes_msgspec_struct(client: HTTPClient) -> None:
    item = await client.get("items/7", response_data_type=ItemStruct)
    assert item == ItemStruct(id=7, name="item-7")


async def test_get_decodes_lothc_json(client: HTTPClient) -> None:
    item = await client.get("items/7", response_data_type=JSON)
    assert item == {"id": 7, "name": "item-7"}


async def test_get_decodes_typed_dict(client: HTTPClient) -> None:
    item = await client.get("items/7", response_data_type=ItemDict)
    assert item == {"id": 7, "name": "item-7"}


async def test_get_with_raw_dict_params(client: HTTPClient) -> None:
    result = await client.get("items", params={"q": "pikachu", "page": 2}, response_data_type=JSON)
    assert result == {"q": "pikachu", "page": 2, "items": [{"id": 2, "name": "pikachu-match"}]}


async def test_get_with_pydantic_params_omits_none_fields(client: HTTPClient) -> None:
    result = await client.get(
        "items", params=SearchParamsModel(q="pikachu", page=1), response_data_type=JSON
    )
    assert result["q"] == "pikachu"
    assert result["page"] == 1


async def test_get_with_msgspec_params(client: HTTPClient) -> None:
    result = await client.get(
        "items", params=SearchParamsStruct(q="pikachu", page=1), response_data_type=JSON
    )
    assert result["q"] == "pikachu"


async def test_get_with_raw_headers(client: HTTPClient) -> None:
    result = await client.get(
        "echo-headers", headers={"x-custom": "header-value"}, response_data_type=JSON
    )
    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-custom"] == "header-value"


async def test_default_headers_sent_on_every_request(base_url: str) -> None:
    async with HTTPClient.build(
        base_url=base_url, default_headers={"x-api-key": "secret"}
    ) as client:
        result = await client.get("echo-headers", response_data_type=JSON)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-api-key"] == "secret"


async def test_get_raises_response_error_for_status(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom")
    assert exc_info.value.status == 500


async def test_get_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    body = await client.get("boom", error_for_status=False)
    assert b"internal-server-error" in body


def test_sync_get_decodes_pydantic_model(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", response_data_type=ItemModel)
    assert item == ItemModel(id=7, name="item-7")


def test_sync_get_raises_response_error_for_status(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom")
    assert exc_info.value.status == 500
