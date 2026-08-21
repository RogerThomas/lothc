import json
from typing import Any, TypedDict, cast

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


class TypedHeadersModel(BaseModel):
    x_custom: str
    x_omitted: str | None = None


class TypedHeadersStruct(Struct):
    x_custom: str
    x_omitted: str | None = None


async def test_get_with_pydantic_headers_omits_none_fields(client: HTTPClient) -> None:
    result = await client.get(
        "echo-headers",
        headers=TypedHeadersModel(x_custom="header-value"),
        response_data_type=JSON,
    )
    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-custom"] == "header-value"
    assert "x-omitted" not in headers


async def test_get_with_msgspec_headers_omits_none_fields(client: HTTPClient) -> None:
    result = await client.get(
        "echo-headers",
        headers=TypedHeadersStruct(x_custom="header-value"),
        response_data_type=JSON,
    )
    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-custom"] == "header-value"
    assert "x-omitted" not in headers


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


async def test_get_response_data_type_not_a_class_raises_type_error(client: HTTPClient) -> None:
    with pytest.raises(TypeError, match="must be a class"):
        await client.get("items/7", response_data_type=cast(Any, "not-a-class"))


async def test_get_response_data_type_bare_dict_raises_type_error(client: HTTPClient) -> None:
    with pytest.raises(TypeError, match="response_data_type=dict is not supported"):
        await client.get("items/7", response_data_type=cast(Any, dict))


class _Unsupported:
    pass


async def test_get_response_data_type_unsupported_class_raises_type_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(TypeError, match="Unsupported response_data_type"):
        await client.get("items/7", response_data_type=cast(Any, _Unsupported))


def test_sync_get_decodes_pydantic_model(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", response_data_type=ItemModel)
    assert item == ItemModel(id=7, name="item-7")


def test_sync_get_decodes_msgspec_struct(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", response_data_type=ItemStruct)
    assert item == ItemStruct(id=7, name="item-7")


def test_sync_get_decodes_lothc_json(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", response_data_type=JSON)
    assert item == {"id": 7, "name": "item-7"}


def test_sync_get_decodes_typed_dict(sync_client: SyncHTTPClient) -> None:
    item = sync_client.get("items/7", response_data_type=ItemDict)
    assert item == {"id": 7, "name": "item-7"}


def test_sync_get_raises_response_error_for_status(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom")
    assert exc_info.value.status == 500


def test_sync_get_error_for_status_false_suppresses_raise(sync_client: SyncHTTPClient) -> None:
    body = sync_client.get("boom", error_for_status=False)
    assert b"internal-server-error" in body


def test_sync_get_response_data_type_unsupported_class_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="Unsupported response_data_type"):
        sync_client.get("items/7", response_data_type=cast(Any, _Unsupported))
