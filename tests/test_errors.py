import pytest
from msgspec import Struct
from pydantic import BaseModel, ValidationError

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


class ErrorBodyModel(BaseModel):
    type: str
    title: str
    status: int


class ErrorBodyStruct(Struct):
    type: str
    title: str
    status: int


class UnmatchedErrorBodyModel(BaseModel):
    nonexistent_field: str


async def test_get_error_type_none_leaves_parsed_body_none(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom")
    assert exc_info.value.parsed_body is None


async def test_get_error_type_dict_populates_parsed_body(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom", error_type=dict)
    assert exc_info.value.parsed_body == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


async def test_get_error_type_pydantic_populates_parsed_body(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom", error_type=ErrorBodyModel)
    assert exc_info.value.parsed_body == ErrorBodyModel(
        type="internal-server-error", title="Internal server error", status=500
    )


async def test_get_error_type_msgspec_populates_parsed_body(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom", error_type=ErrorBodyStruct)
    assert exc_info.value.parsed_body == ErrorBodyStruct(
        type="internal-server-error", title="Internal server error", status=500
    )


async def test_get_error_type_mismatch_raises_validation_error(client: HTTPClient) -> None:
    with pytest.raises(ValidationError):
        await client.get("boom", error_type=UnmatchedErrorBodyModel)


async def test_get_error_has_request_info(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom")
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"
    assert exc_info.value.request.host == "127.0.0.1"


async def test_get_result_error_has_request_info(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get_result("boom")
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"


async def test_sse_error_has_request_info(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        async for _ in client.sse("boom"):
            pass
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"


def test_sync_get_error_type_none_leaves_parsed_body_none(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom")
    assert exc_info.value.parsed_body is None


def test_sync_get_error_type_dict_populates_parsed_body(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom", error_type=dict)
    assert exc_info.value.parsed_body == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


def test_sync_get_error_type_pydantic_populates_parsed_body(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom", error_type=ErrorBodyModel)
    assert exc_info.value.parsed_body == ErrorBodyModel(
        type="internal-server-error", title="Internal server error", status=500
    )


def test_sync_get_error_type_msgspec_populates_parsed_body(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom", error_type=ErrorBodyStruct)
    assert exc_info.value.parsed_body == ErrorBodyStruct(
        type="internal-server-error", title="Internal server error", status=500
    )


def test_sync_get_error_type_mismatch_raises_validation_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(ValidationError):
        sync_client.get("boom", error_type=UnmatchedErrorBodyModel)


def test_sync_get_error_has_request_info(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom")
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"
    assert exc_info.value.request.host == "127.0.0.1"


def test_sync_get_result_error_has_request_info(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get_result("boom")
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"


def test_sync_sse_error_has_request_info(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        for _ in sync_client.sse("boom"):
            pass
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"
