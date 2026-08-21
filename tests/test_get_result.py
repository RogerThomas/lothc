from msgspec import Struct
from pydantic import BaseModel

from lothc import HTTPClient, SyncHTTPClient


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

    assert "content-type" in {name.lower() for name in result.headers}


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

    assert "content-type" in {name.lower() for name in result.headers}


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
