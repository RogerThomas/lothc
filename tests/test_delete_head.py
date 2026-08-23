from pydantic import BaseModel

from lothc import JSON, HTTPClient, SyncHTTPClient


class EchoedHeaders(BaseModel):
    x_custom: str | None = None


async def test_delete_returns_raw_bytes_by_default(client: HTTPClient) -> None:
    body = await client.delete("items/7")

    assert body == b'{"id": 7, "deleted": true}'


async def test_delete_decodes_response_data_type(client: HTTPClient) -> None:
    result = await client.delete("items/7", response_data_type=JSON)

    assert result == {"id": 7, "deleted": True}


async def test_delete_result_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.delete_result("items/7", response_data_type=JSON)

    assert result.status == 200
    assert result.data == {"id": 7, "deleted": True}


async def test_delete_result_with_typed_headers(client: HTTPClient) -> None:
    result = await client.delete_result(
        "items/7", response_data_type=JSON, response_headers_type=EchoedHeaders
    )

    assert result.typed_headers is not None


def test_sync_delete_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.delete_result("items/7", response_data_type=JSON)

    assert result.status == 200
    assert result.data == {"id": 7, "deleted": True}


async def test_delete_result_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    result = await client.delete_result("missing", error_for_status=False)

    assert result.status == 404


async def test_head_returns_headers_only_result(client: HTTPClient) -> None:
    result = await client.head("items/7")

    assert result.data is None
    assert result.status == 200
    assert result.headers["x-item-exists"] == "true"


def test_sync_delete_decodes_response_data_type(sync_client: SyncHTTPClient) -> None:
    result = sync_client.delete("items/7", response_data_type=JSON)

    assert result == {"id": 7, "deleted": True}


def test_sync_head_returns_headers_only_result(sync_client: SyncHTTPClient) -> None:
    result = sync_client.head("items/7")

    assert result.data is None
    assert result.headers["x-item-exists"] == "true"


async def test_head_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    result = await client.head("missing", error_for_status=False)

    assert result.status == 404


def test_sync_head_error_for_status_false_suppresses_raise(sync_client: SyncHTTPClient) -> None:
    result = sync_client.head("missing", error_for_status=False)

    assert result.status == 404
