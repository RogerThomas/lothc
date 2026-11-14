"""`raise_for_status=False`: a 4xx/5xx comes back as a `Response` whose `.data` raises the
`HTTPResponseError` a raising call would have raised, with the error body on `.raw_error` and
decoded on `.error`."""

import copy
import pickle
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from _oauth_server import TokenServer
from msgspec import Struct
from pydantic import BaseModel

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


class NotFound(Struct, tag_field="type", tag="not-found"):
    title: str
    status: int


class NotAuthorized(Struct, tag_field="type", tag="not-authorized"):
    title: str
    status: int


class APIError(HTTPResponseError[NotFound | NotAuthorized]):
    pass


class UnmatchedModel(BaseModel):
    nonexistent_field: str


class UnmatchedError(HTTPResponseError[UnmatchedModel]):
    pass


class Item(BaseModel):
    id: int


class RequiredHeaders(BaseModel):
    x_required: str


@pytest.fixture(name="lenient_client")
async def _lenient_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, raise_for_status=False) as client:
        yield client


@pytest.fixture(name="sync_lenient_client")
def _sync_lenient_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, raise_for_status=False) as client:
        yield client


def _describe(error: HTTPResponseError[Any]) -> tuple[object, ...]:
    return (type(error), error.status, error.body, error.error, error.request)


async def test_a_client_with_raise_for_status_false_returns_an_error_status(
    lenient_client: HTTPClient,
) -> None:
    response = await lenient_client.get("boom")

    assert response.status == 500
    assert response.error == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


def test_sync_a_client_with_raise_for_status_false_returns_an_error_status(
    sync_lenient_client: SyncHTTPClient,
) -> None:
    response = sync_lenient_client.get("boom")

    assert response.status == 500


async def test_a_call_raise_for_status_true_overrides_the_clients_false(
    lenient_client: HTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await lenient_client.get("boom", raise_for_status=True)

    assert exc_info.value.status == 500


def test_sync_a_call_raise_for_status_true_overrides_the_clients_false(
    sync_lenient_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_lenient_client.get("boom", raise_for_status=True)

    assert exc_info.value.status == 500


async def test_a_call_raise_for_status_false_overrides_the_clients_true(client: HTTPClient) -> None:
    response = await client.post("missing", raise_for_status=False)

    assert response.status == 404


def test_sync_a_call_raise_for_status_false_overrides_the_clients_true(
    sync_client: SyncHTTPClient,
) -> None:
    response = sync_client.post("missing", raise_for_status=False)

    assert response.status == 404


async def test_reading_data_raises_what_a_raising_call_raises(client: HTTPClient) -> None:
    with pytest.raises(APIError) as raised:
        await client.get("missing", raises=APIError)
    response = await client.get("missing", raises=APIError, raise_for_status=False)

    with pytest.raises(APIError) as deferred:
        _ = response.data

    assert _describe(deferred.value) == _describe(raised.value)
    assert deferred.value.error == NotFound(title="Not found", status=404)
    assert deferred.value.parse_error is None
    assert deferred.value.headers == response.headers


def test_sync_reading_data_raises_what_a_raising_call_raises(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(APIError) as raised:
        sync_client.get("missing", raises=APIError)
    response = sync_client.get("missing", raises=APIError, raise_for_status=False)

    with pytest.raises(APIError) as deferred:
        _ = response.data

    assert _describe(deferred.value) == _describe(raised.value)
    assert deferred.value.headers == response.headers


async def test_reading_data_raises_the_clients_raises_class(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, raises=APIError, raise_for_status=False) as client:
        response = await client.get("unauthorized")

    with pytest.raises(APIError) as exc_info:
        _ = response.data

    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


def test_sync_reading_data_raises_the_clients_raises_class(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, raises=APIError, raise_for_status=False) as client:
        response = client.get("unauthorized")

    with pytest.raises(APIError) as exc_info:
        _ = response.data

    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


async def test_reading_data_without_raises_raises_plain_http_response_error(
    lenient_client: HTTPClient,
) -> None:
    response = await lenient_client.get("boom")

    with pytest.raises(HTTPResponseError) as exc_info:
        _ = response.data

    assert type(exc_info.value) is HTTPResponseError
    assert exc_info.value.headers["content-type"] == "application/json"


async def test_each_read_of_data_raises_a_new_error(lenient_client: HTTPClient) -> None:
    response = await lenient_client.get("boom")

    with pytest.raises(HTTPResponseError) as first:
        _ = response.data
    with pytest.raises(HTTPResponseError) as second:
        _ = response.data

    assert first.value is not second.value
    assert first.value.error == second.value.error


def test_sync_each_read_of_data_raises_a_new_error(sync_lenient_client: SyncHTTPClient) -> None:
    response = sync_lenient_client.get("boom")

    with pytest.raises(HTTPResponseError) as first:
        _ = response.data
    with pytest.raises(HTTPResponseError) as second:
        _ = response.data

    assert first.value is not second.value


async def test_a_pinned_type_that_does_not_match_keeps_its_parse_error(
    lenient_client: HTTPClient,
) -> None:
    response = await lenient_client.get("boom", raises=UnmatchedError)

    with pytest.raises(UnmatchedError) as exc_info:
        _ = response.data

    assert isinstance(exc_info.value.parse_error, ValueError)
    assert response.error == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


async def test_nothing_is_decoded_into_into_or_parsed_into_typed_headers(
    lenient_client: HTTPClient,
) -> None:
    response = await lenient_client.get("boom", into=Item, response_headers_type=RequiredHeaders)

    assert response.status == 500
    with pytest.raises(HTTPResponseError):
        _ = response.data
    with pytest.raises(HTTPResponseError):
        _ = response.typed_headers


def test_sync_nothing_is_decoded_into_into_or_parsed_into_typed_headers(
    sync_lenient_client: SyncHTTPClient,
) -> None:
    response = sync_lenient_client.get("boom", into=Item, response_headers_type=RequiredHeaders)

    with pytest.raises(HTTPResponseError):
        _ = response.data
    with pytest.raises(HTTPResponseError):
        _ = response.typed_headers


async def test_into_bytes_data_raises_and_raw_error_is_the_body(
    lenient_client: HTTPClient,
) -> None:
    response = await lenient_client.get("invalid-json-error", into=bytes)

    with pytest.raises(HTTPResponseError) as exc_info:
        _ = response.data

    assert response.raw_error == b"<html><body>502 Bad Gateway</body></html>"
    assert exc_info.value.body == response.raw_error


async def test_error_is_the_pinned_type_a_json_object_or_none(lenient_client: HTTPClient) -> None:
    pinned = await lenient_client.get("missing", raises=APIError)
    json_object = await lenient_client.get("boom")
    not_json = await lenient_client.get("invalid-json-error")

    assert pinned.error == NotFound(title="Not found", status=404)
    assert json_object.error == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }
    assert not_json.error is None


def test_sync_error_is_the_pinned_type_a_json_object_or_none(
    sync_lenient_client: SyncHTTPClient,
) -> None:
    pinned = sync_lenient_client.get("missing", raises=APIError)
    not_json = sync_lenient_client.get("invalid-json-error")

    assert pinned.error == NotFound(title="Not found", status=404)
    assert not_json.error is None


async def test_error_is_decoded_once_and_kept(lenient_client: HTTPClient) -> None:
    response = await lenient_client.get("boom")

    assert response.error is response.error


async def test_a_caller_can_match_on_error(lenient_client: HTTPClient) -> None:
    response = await lenient_client.get("missing", raises=APIError)

    match response.error:
        case NotFound(title=title):
            outcome = title
        case NotAuthorized():
            outcome = "not-authorized"
        case {"title": str(title)}:
            outcome = title
        case _:
            outcome = "other"

    assert outcome == "Not found"


async def test_a_success_has_no_error_or_raw_error(lenient_client: HTTPClient) -> None:
    response = await lenient_client.get("items/7", into=dict)

    assert response.error is None
    assert response.raw_error is None
    assert response.data == {"id": 7, "name": "item-7"}


def test_sync_a_success_has_no_error_or_raw_error(sync_client: SyncHTTPClient) -> None:
    response = sync_client.get("items/7")

    assert response.error is None
    assert response.raw_error is None


async def test_exhausted_retries_return_the_last_error_response(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(
        base_url=base_url, max_retries=1, backoff_base=0.001, raise_for_status=False
    ) as client:
        response = await client.get("flaky", params={"key": server_key, "fail_times": 5})
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert response.status == 503
    assert hits == {"hits": 2}


def test_sync_exhausted_retries_return_the_last_error_response(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(
        base_url=base_url, max_retries=1, backoff_base=0.001, raise_for_status=False
    ) as client:
        response = client.get("flaky", params={"key": server_key, "fail_times": 5})
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert response.status == 503
    assert hits == {"hits": 2}


async def test_a_retry_after_longer_than_max_retry_after_returns_the_response(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(
        base_url=base_url, max_retries=2, max_retry_after=60, raise_for_status=False
    ) as client:
        response = await client.get(
            "retry-after-custom", params={"key": server_key, "value": "120"}
        )

    assert response.status == 429
    assert response.headers["Retry-After"] == "120"


def test_sync_a_retry_after_longer_than_max_retry_after_returns_the_response(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(
        base_url=base_url, max_retries=2, max_retry_after=60, raise_for_status=False
    ) as client:
        response = client.get("retry-after-custom", params={"key": server_key, "value": "120"})

    assert response.status == 429
    assert response.headers["Retry-After"] == "120"


async def test_a_401_is_re_authenticated_once_then_returned(
    base_url: str, token_server: TokenServer
) -> None:
    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(), raise_for_status=False
    ) as client:
        response = await client.get(
            "always-unauthorized", params={"key": f"{token_server.key}-api"}
        )

    assert response.status == 401
    assert response.error == {"hits": 2}
    assert len(token_server.requests()) == 2


def test_sync_a_401_is_re_authenticated_once_then_returned(
    base_url: str, token_server: TokenServer
) -> None:
    with SyncHTTPClient(
        base_url=base_url, bearer_auth=token_server.sync_provider(), raise_for_status=False
    ) as client:
        response = client.get("always-unauthorized", params={"key": f"{token_server.key}-api"})

    assert response.status == 401
    assert response.error == {"hits": 2}


async def test_stream_post_raises_even_with_raise_for_status_false(
    lenient_client: HTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = [chunk async for chunk in lenient_client.stream_post("missing", json={"a": 1})]

    assert exc_info.value.status == 404


def test_sync_stream_post_raises_even_with_raise_for_status_false(
    sync_lenient_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = list(sync_lenient_client.stream_post("missing", json={"a": 1}))

    assert exc_info.value.status == 404


async def test_an_error_response_repr_and_equality_do_not_read_data(
    lenient_client: HTTPClient,
) -> None:
    response = await lenient_client.get("missing")
    success = await lenient_client.get("items/7")

    assert "status=404" in repr(response)
    assert response == copy.copy(response)
    assert response != success


def test_sync_an_error_response_pickles_after_a_failed_decode(
    sync_lenient_client: SyncHTTPClient,
) -> None:
    response = sync_lenient_client.get("boom", raises=UnmatchedError)
    with pytest.raises(UnmatchedError):
        _ = response.data

    restored = pickle.loads(pickle.dumps(response))  # noqa: S301 — round-tripping our own object

    assert restored == response
    assert restored.status == 500
    assert restored.error == response.error
    with pytest.raises(UnmatchedError) as exc_info:
        _ = restored.data
    assert isinstance(exc_info.value.parse_error, ValueError)
