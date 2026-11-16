"""`HTTPDecodeError`: a successful response lothc received but couldn't decode, carrying the raw
response. Each test runs on both transports (`task test-native`, `task test-pyreqwest`)."""

import pickle

import msgspec
import pydantic
import pytest
from msgspec import Struct
from msgspec.json import Decoder

from lothc import (
    HTTPClient,
    HTTPDecodeError,
    HTTPResponseError,
    HTTPTransportError,
    SyncHTTPClient,
)
from lothc.testing import LOTHCMocker


class Item(Struct):
    id: int


class ItemModel(pydantic.BaseModel):
    id: int


class RequiredHeaders(pydantic.BaseModel):
    x_required: str


class RequiredHeadersStruct(Struct):
    x_required: str


async def test_a_body_that_does_not_fit_a_struct_is_a_decode_error(client: HTTPClient) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("json-literal", params={"value": '{"id": "x"}'}, into=Item)

    error = exc_info.value
    assert isinstance(error, ValueError)
    assert not isinstance(error, HTTPResponseError | HTTPTransportError)
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert error.body_start == b'{"id": "x"}'
    assert error.headers["content-type"] == "application/json"
    assert error.request.method == "GET"
    assert error.request.path == "/json-literal"
    assert error.__cause__ is error.parse_error
    # msgspec's stubs don't declare its errors `ValueError`s, though at runtime they are, so a
    # checker reads anything after this as unreachable.
    assert type(error.parse_error) is msgspec.ValidationError


def test_sync_a_body_that_does_not_fit_a_struct_is_a_decode_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        sync_client.get("json-literal", params={"value": '{"id": "x"}'}, into=Item)

    error = exc_info.value
    assert not isinstance(error, HTTPResponseError | HTTPTransportError)
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert error.headers["content-type"] == "application/json"
    assert error.request.method == "GET"
    assert error.request.path == "/json-literal"
    assert error.__cause__ is error.parse_error
    # msgspec's stubs don't declare its errors `ValueError`s, though at runtime they are, so a
    # checker reads anything after this as unreachable.
    assert type(error.parse_error) is msgspec.ValidationError


async def test_a_body_that_does_not_fit_a_model_carries_pydantics_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("json-literal", params={"value": '{"id": "x"}'}, into=ItemModel)

    assert isinstance(exc_info.value.parse_error, pydantic.ValidationError)
    assert exc_info.value.__cause__ is exc_info.value.parse_error


def test_sync_a_body_that_does_not_fit_a_model_carries_pydantics_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        sync_client.get("json-literal", params={"value": '{"id": "x"}'}, into=ItemModel)

    assert isinstance(exc_info.value.parse_error, pydantic.ValidationError)
    assert exc_info.value.__cause__ is exc_info.value.parse_error


async def test_a_prebuilt_msgspec_decoder_failure_is_a_decode_error(client: HTTPClient) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("json-literal", params={"value": "[1]"}, into=Decoder(Item))

    assert type(exc_info.value.parse_error) is msgspec.ValidationError


async def test_a_post_response_that_does_not_decode_is_a_decode_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_post_response(path="/items", data=b"[1]")

    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.post("items", json={"id": 1}, into=Item)

    assert exc_info.value.request.method == "POST"
    assert exc_info.value.body == b"[1]"


async def test_a_mocked_response_that_does_not_decode_is_a_decode_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    body = b'{"id": "' + b"x" * 200 + b'"}'
    lothc_mocker.add_get_response(path="/items/7", data=body, headers={"x-name": "value"})

    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("items/7", into=Item)

    error = exc_info.value
    assert error.status == 200
    assert error.body == body
    assert error.body_start == body[:100]
    assert error.headers["x-name"] == "value"
    assert error.request.path == "/items/7"
    assert type(error.parse_error) is msgspec.ValidationError


def test_sync_a_mocked_response_that_does_not_decode_is_a_decode_error(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b"[1]", headers={"x-name": "value"})

    with pytest.raises(HTTPDecodeError) as exc_info:
        sync_client.get("items/7", into=Item)

    error = exc_info.value
    assert error.status == 200
    assert error.body == b"[1]"
    assert error.headers["x-name"] == "value"
    assert error.request.path == "/items/7"


async def test_the_message_names_the_response_the_body_start_and_the_cause(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    body = b"[" + b"1," * 100 + b"1]"
    lothc_mocker.add_get_response(path="/items/7", data=body, status=201)

    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("items/7", into=Item)

    error = exc_info.value
    assert str(error) == (
        f"Could not decode the 201 response to GET {error.request.url}: {error.parse_error}\n"
        f"Body: {body[:100].decode()}…"
    )


async def test_a_short_body_is_quoted_whole_in_the_message(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b"[1]")

    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("items/7", into=Item)

    assert str(exc_info.value).endswith("\nBody: [1]")


@pytest.mark.parametrize(
    "headers_type", [RequiredHeaders, RequiredHeadersStruct], ids=["pydantic", "msgspec"]
)
async def test_headers_that_do_not_fit_response_headers_type_are_a_decode_error(
    client: HTTPClient,
    lothc_mocker: LOTHCMocker,
    headers_type: type[RequiredHeaders | RequiredHeadersStruct],
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b'{"id": 7}', headers={"x-name": "value"})

    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("items/7", into=Item, response_headers_type=headers_type)

    error = exc_info.value
    assert error.status == 200
    assert error.body == b'{"id": 7}'
    assert error.headers["x-name"] == "value"
    assert isinstance(error.parse_error, ValueError)
    assert error.__cause__ is error.parse_error


def test_sync_headers_that_do_not_fit_response_headers_type_are_a_decode_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        sync_client.get("items/7", response_headers_type=RequiredHeaders)

    error = exc_info.value
    assert error.status == 200
    assert error.body == b'{"id": 7, "name": "item-7"}'
    assert isinstance(error.parse_error, pydantic.ValidationError)


async def test_a_head_whose_headers_do_not_fit_carries_no_body(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_head_response(path="/items/7", headers={"x-name": "value"})

    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.head("items/7", response_headers_type=RequiredHeadersStruct)

    assert exc_info.value.body == b""
    assert exc_info.value.request.method == "HEAD"
    assert type(exc_info.value.parse_error) is msgspec.ValidationError


def test_sync_an_options_whose_headers_do_not_fit_carries_no_body(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("OPTIONS", path="/items/7").with_data(b"ignored")

    with pytest.raises(HTTPDecodeError) as exc_info:
        sync_client.options("items/7", response_headers_type=RequiredHeaders)

    assert exc_info.value.body == b""
    assert exc_info.value.request.method == "OPTIONS"


@pytest.mark.parametrize("ending", [b"\n", b""], ids=["terminated", "final-line"])
async def test_a_streamed_line_that_does_not_decode_is_a_decode_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker, ending: bytes
) -> None:
    lothc_mocker.add_get_response(
        path="/ndjson", data=b'{"id": 1}\n{"id": "x"}' + ending, headers={"x-name": "value"}
    )
    lines = client.stream_get("ndjson", into=Item)
    first = await anext(lines)

    with pytest.raises(HTTPDecodeError) as exc_info:
        await anext(lines)

    error = exc_info.value
    assert first == Item(id=1)
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert error.headers["x-name"] == "value"
    assert error.request.path == "/ndjson"
    assert error.__cause__ is error.parse_error
    # msgspec's stubs don't declare its errors `ValueError`s, though at runtime they are, so a
    # checker reads anything after this as unreachable.
    assert type(error.parse_error) is msgspec.ValidationError


@pytest.mark.parametrize("ending", [b"\n", b""], ids=["terminated", "final-line"])
def test_sync_a_streamed_line_that_does_not_decode_is_a_decode_error(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker, ending: bytes
) -> None:
    lothc_mocker.add_get_response(
        path="/ndjson", data=b'{"id": 1}\n{"id": "x"}' + ending, headers={"x-name": "value"}
    )
    lines = sync_client.stream_get("ndjson", into=Item)
    first = next(lines)

    with pytest.raises(HTTPDecodeError) as exc_info:
        next(lines)

    error = exc_info.value
    assert first == Item(id=1)
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert error.headers["x-name"] == "value"
    assert error.request.path == "/ndjson"
    assert type(error.parse_error) is msgspec.ValidationError


async def test_a_line_streamed_from_the_server_that_does_not_decode_is_a_decode_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        [line async for line in client.stream_post("ndjson-echo", json={"n": 1}, into=list[int])]

    assert exc_info.value.request.method == "POST"
    assert exc_info.value.body == b'{"i": 0}'


def test_sync_a_line_streamed_from_the_server_that_does_not_decode_is_a_decode_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        list(sync_client.stream_get("ndjson", params={"count": 1}, into=list[int]))

    assert exc_info.value.status == 200
    assert exc_info.value.body == b'{"i": 0}'


async def test_an_sse_event_whose_data_does_not_decode_is_a_decode_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(
        path="/events",
        data=b'data: {"id": 1}\n\ndata: {"id": "x"}\n\n',
        headers={"content-type": "text/event-stream"},
    )
    events = client.sse("events", into=Item)
    first = await anext(events)

    with pytest.raises(HTTPDecodeError) as exc_info:
        await anext(events)

    error = exc_info.value
    assert first.data == Item(id=1)
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert error.headers["content-type"] == "text/event-stream"
    assert error.request.path == "/events"
    # Not a transport error, so never reconnected.
    assert lothc_mocker.get_call_count() == 1
    assert type(error.parse_error) is msgspec.ValidationError


def test_sync_an_sse_event_whose_data_does_not_decode_is_a_decode_error(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(
        path="/events",
        data=b'data: {"id": "x"}\n\n',
        headers={"content-type": "text/event-stream"},
    )

    with pytest.raises(HTTPDecodeError) as exc_info:
        list(sync_client.sse("events", into=ItemModel))

    error = exc_info.value
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert error.request.path == "/events"
    assert isinstance(error.parse_error, pydantic.ValidationError)
    assert lothc_mocker.get_call_count() == 1


async def test_a_real_sse_stream_whose_data_does_not_decode_is_a_decode_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        [event async for event in client.sse("events", params={"count": 1}, into=list[int])]

    assert exc_info.value.body == b'{"msg": "hello 0", "now": 0}'
    assert exc_info.value.headers["content-type"].startswith("text/event-stream")


async def test_an_error_status_whose_body_does_not_decode_is_still_a_response_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b"<html>", status=502)

    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("items/7", into=Item)

    assert exc_info.value.status == 502
    assert not isinstance(exc_info.value, HTTPDecodeError)


def test_sync_an_error_status_whose_body_does_not_decode_is_still_a_response_error(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b"<html>", status=404)

    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("items/7", into=Item)

    assert exc_info.value.status == 404


async def test_an_error_status_with_raise_for_status_off_decodes_nothing(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items/7", data=b"<html>", status=500)

    response = await client.get(
        "items/7", into=Item, response_headers_type=RequiredHeaders, raise_for_status=False
    )

    assert response.status == 500
    assert response.error is not None
    with pytest.raises(HTTPResponseError):
        _ = response.data


async def test_a_stream_with_an_error_status_is_still_a_response_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/ndjson", data=b"<html>", status=500)

    with pytest.raises(HTTPResponseError) as exc_info:
        [line async for line in client.stream_get("ndjson", into=Item)]

    assert exc_info.value.status == 500


async def test_a_decode_error_with_retries_on_carries_the_response(base_url: str) -> None:
    # Retries (and re-authentication) send undecoded and finish in Python on the native transport.
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPDecodeError) as exc_info:
            await client.get("json-literal", params={"value": '{"id": "x"}'}, into=Item)

    error = exc_info.value
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert error.headers["content-type"] == "application/json"
    assert error.request.path == "/json-literal"
    assert error.__cause__ is error.parse_error
    # msgspec's stubs don't declare its errors `ValueError`s, though at runtime they are, so a
    # checker reads anything after this as unreachable.
    assert type(error.parse_error) is msgspec.ValidationError


def test_sync_a_decode_error_with_retries_on_carries_the_response(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client,
        pytest.raises(HTTPDecodeError) as exc_info,
    ):
        client.get("json-literal", params={"value": '{"id": "x"}'}, into=ItemModel)

    error = exc_info.value
    assert error.status == 200
    assert error.body == b'{"id": "x"}'
    assert isinstance(error.parse_error, pydantic.ValidationError)


async def test_typed_headers_that_do_not_fit_with_retries_on_are_a_decode_error(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPDecodeError) as exc_info:
            await client.get("items/7", response_headers_type=RequiredHeadersStruct)

    assert exc_info.value.body == b'{"id": 7, "name": "item-7"}'
    assert type(exc_info.value.parse_error) is msgspec.ValidationError


async def test_a_head_whose_headers_do_not_fit_with_retries_on_carries_no_body(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPDecodeError) as exc_info:
            await client.head("items/7", response_headers_type=RequiredHeaders)

    assert exc_info.value.body == b""


async def test_a_decode_error_survives_pickling_without_pydantics_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        await client.get("json-literal", params={"value": '{"id": "x"}'}, into=ItemModel)
    error = exc_info.value

    restored = pickle.loads(pickle.dumps(error))  # noqa: S301 — round-tripping our own object

    assert type(restored) is HTTPDecodeError
    assert restored.status == error.status
    assert restored.body == error.body
    assert restored.body_start == error.body_start
    assert restored.headers == error.headers
    assert restored.request == error.request
    assert str(restored) == str(error)
    # pydantic's `ValidationError` can't be pickled: its message comes back on a plain one.
    assert type(restored.parse_error) is ValueError
    assert str(restored.parse_error) == str(error.parse_error)
    assert restored.__cause__ is None


def test_sync_a_decode_error_survives_pickling(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPDecodeError) as exc_info:
        sync_client.get("json-literal", params={"value": '{"id": "x"}'}, into=Item)
    error = exc_info.value

    restored = pickle.loads(pickle.dumps(error))  # noqa: S301 — round-tripping our own object

    assert restored.status == 200
    assert restored.body == error.body
    assert restored.headers == error.headers
    assert restored.request == error.request
    assert str(restored) == str(error)
