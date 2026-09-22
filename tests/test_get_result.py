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

    assert "content-type" in result.headers


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

    assert "content-type" in result.headers


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


async def test_get_result_headers_are_case_insensitive(client: HTTPClient) -> None:
    result = await client.get_result("items/7", response_data_type=ItemModel)

    assert result.headers["Content-Type"] == result.headers["content-type"]
    assert result.headers["CONTENT-TYPE"] == result.headers["content-type"]
    assert "Content-Type" in result.headers
    assert result.headers.get("CoNtEnT-tYpE") is not None
    assert result.headers == {name.upper(): value for name, value in result.headers.items()}


def test_sync_get_result_headers_are_case_insensitive(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get_result("items/7", response_data_type=ItemModel)

    assert result.headers["Content-Type"] == result.headers["content-type"]
    assert "Content-Type" in result.headers
    assert result.headers.get("CoNtEnT-tYpE") is not None


async def test_get_result_headers_keep_every_value_of_a_repeated_header(
    client: HTTPClient,
) -> None:
    result = await client.get_result("multi-set-cookie", response_data_type=dict)

    assert result.headers.get_all("Set-Cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers["set-cookie"] == "session=abc; Path=/"
    assert result.headers.get_all("content-type") == ["application/json"]
    assert result.headers.get_all("never-sent") == []


def test_sync_get_result_headers_keep_every_value_of_a_repeated_header(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get_result("multi-set-cookie", response_data_type=dict)

    assert result.headers.get_all("Set-Cookie") == [
        "session=abc; Path=/",
        "csrf=xyz; Path=/",
        "theme=dark; Path=/",
    ]
    assert result.headers["set-cookie"] == "session=abc; Path=/"


async def test_get_result_headers_get_all_returns_a_copy(client: HTTPClient) -> None:
    result = await client.get_result("multi-set-cookie", response_data_type=dict)

    result.headers.get_all("Set-Cookie").clear()

    assert len(result.headers.get_all("Set-Cookie")) == 3
