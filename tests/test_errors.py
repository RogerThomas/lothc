import json
import pickle

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


async def test_get_error_type_mismatch_still_raises_response_error(client: HTTPClient) -> None:
    # An error body that doesn't match `error_type` must not replace the HTTPResponseError: it
    # used to raise pydantic's ValidationError instead, losing the status and skipping any
    # `except HTTPResponseError` handler. The decode failure is kept on `.parse_error`.
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom", error_type=UnmatchedErrorBodyModel)

    assert exc_info.value.status == 500
    assert exc_info.value.parsed_body is None
    assert isinstance(exc_info.value.parse_error, ValidationError)


async def test_get_error_has_request_info(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom")
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"
    assert exc_info.value.request.host == "127.0.0.1"


async def test_response_get_error_has_request_info(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom")
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


def test_sync_get_error_type_mismatch_still_raises_response_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom", error_type=UnmatchedErrorBodyModel)

    assert exc_info.value.status == 500
    assert isinstance(exc_info.value.parse_error, ValidationError)


def test_sync_get_error_has_request_info(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom")
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"
    assert exc_info.value.request.host == "127.0.0.1"


def test_sync_get_result_error_has_request_info(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom")
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"


def test_sync_sse_error_has_request_info(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        for _ in sync_client.sse("boom"):
            pass
    assert exc_info.value.request.method == "GET"
    assert exc_info.value.request.path == "/boom"


async def test_response_error_carries_the_error_responses_headers(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=0) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("retry-after", params={"key": "error-headers"})

    assert exc_info.value.status == 429
    assert exc_info.value.headers["Retry-After"] == "0"


async def test_response_error_keeps_the_whole_body(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom-large")

    error = exc_info.value
    assert json.loads(error.body) == {"detail": "x" * 300}
    assert error.body_start == error.body[:100]
    assert error.headers["x-request-id"] == "request-id"


def test_sync_response_error_carries_headers_and_the_whole_body(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("boom-large")

    assert json.loads(exc_info.value.body) == {"detail": "x" * 300}
    assert exc_info.value.headers["X-Request-Id"] == "request-id"


async def test_response_error_survives_pickling(client: HTTPClient) -> None:
    # Exception's default pickling replayed only the message into `__init__`, so unpickling
    # raised TypeError, which broke multiprocessing and task queues.
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom-large", error_type=dict)

    restored = pickle.loads(pickle.dumps(exc_info.value))  # noqa: S301 — round-tripping our own object

    assert type(restored) is HTTPResponseError
    assert restored.status == 500
    assert restored.body == exc_info.value.body
    assert restored.request == exc_info.value.request
    assert restored.parsed_body == {"detail": "x" * 300}
    assert restored.headers.get_all("x-request-id") == ["request-id"]
    assert str(restored) == str(exc_info.value)


async def test_non_json_error_body_still_raises_response_error(client: HTTPClient) -> None:
    # The motivating case: a proxy's HTML error page against a JSON `error_type`.
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("invalid-json-error", error_type=ErrorBodyModel)

    assert exc_info.value.status == 502
    assert exc_info.value.parsed_body is None
    assert isinstance(exc_info.value.parse_error, ValueError)
    assert exc_info.value.body.startswith(b"<html>")


async def test_matching_error_body_has_no_parse_error(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("boom", error_type=ErrorBodyModel)

    assert exc_info.value.parse_error is None
    assert isinstance(exc_info.value.parsed_body, ErrorBodyModel)


async def test_a_json_array_decoded_as_dict_raises_a_clear_error(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="got a JSON array"):
        await client.get("json-array", response_data_type=dict)


@pytest.mark.parametrize(
    ("value", "kind"),
    [('"text"', "string"), ("true", "boolean"), ("null", "null"), ("7", "number")],
)
async def test_a_non_object_json_body_decoded_as_dict_names_what_it_got(
    client: HTTPClient, value: str, kind: str
) -> None:
    with pytest.raises(ValueError, match=f"got a JSON {kind}"):
        await client.get("json-literal", params={"value": value}, response_data_type=dict)
