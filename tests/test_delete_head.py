import pytest
from pydantic import BaseModel

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


class EchoedHeaders(BaseModel):
    content_type: str


async def test_delete_returns_raw_bytes_by_default(client: HTTPClient) -> None:
    body = (await client.delete("items/7")).data

    assert body == b'{"id": 7, "deleted": true}'


async def test_response_delete_includes_status_and_data(client: HTTPClient) -> None:
    result = await client.delete("items/7", into=dict)

    assert result.status == 200
    assert result.data == {"id": 7, "deleted": True}


async def test_response_delete_with_typed_headers(client: HTTPClient) -> None:
    result = await client.delete("items/7", into=dict, response_headers_type=EchoedHeaders)

    assert result.typed_headers == EchoedHeaders(content_type="application/json")


def test_sync_delete_result_includes_status_and_data(sync_client: SyncHTTPClient) -> None:
    result = sync_client.delete("items/7", into=dict)

    assert result.status == 200
    assert result.data == {"id": 7, "deleted": True}


async def test_response_delete_raise_for_status_false_returns_the_error_response(
    client: HTTPClient,
) -> None:
    result = await client.delete("missing", raise_for_status=False)

    assert result.status == 404
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = result.data
    assert exc_info.value.status == 404


async def test_head_returns_headers_only_result(client: HTTPClient) -> None:
    result = await client.head("items/7")

    assert result.data is None
    assert result.status == 200
    assert result.headers["x-item-exists"] == "true"


def test_sync_head_returns_headers_only_result(sync_client: SyncHTTPClient) -> None:
    result = sync_client.head("items/7")

    assert result.data is None
    assert result.headers["x-item-exists"] == "true"


async def test_head_raise_for_status_false_returns_the_error_response(
    client: HTTPClient,
) -> None:
    result = await client.head("missing", raise_for_status=False)

    assert result.status == 404
    assert result.raw_error == b""
    assert result.error is None
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = result.data
    assert exc_info.value.status == 404


def test_sync_head_raise_for_status_false_returns_the_error_response(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.head("missing", raise_for_status=False)

    assert result.status == 404
    with pytest.raises(HTTPResponseError):
        _ = result.data


async def test_options_returns_preflight_status_and_headers(client: HTTPClient) -> None:
    result = await client.options(
        "items", headers={"origin": "origin", "access-control-request-method": "GET"}
    )

    assert result.data is None
    assert result.status == 204
    assert result.headers["access-control-allow-origin"] == "origin"
    assert result.headers["access-control-allow-methods"] == "GET, POST"
    assert result.request.method == "OPTIONS"


def test_sync_options_returns_preflight_status_and_headers(sync_client: SyncHTTPClient) -> None:
    result = sync_client.options("items", headers={"origin": "origin"})

    assert result.data is None
    assert result.status == 204
    assert result.headers["access-control-allow-origin"] == "origin"
    assert result.request.method == "OPTIONS"


async def test_options_raise_for_status_false_returns_the_error_response(
    client: HTTPClient,
) -> None:
    result = await client.options("missing", raise_for_status=False)

    assert result.status == 404
    assert result.raw_error == b""
    assert result.error is None
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = result.data
    assert exc_info.value.status == 404


def test_sync_options_raise_for_status_false_returns_the_error_response(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.options("missing", raise_for_status=False)

    assert result.status == 404
    with pytest.raises(HTTPResponseError):
        _ = result.data
