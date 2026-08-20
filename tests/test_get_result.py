from pydantic import BaseModel

from lothc import HTTPClient


class ItemModel(BaseModel):
    id: int
    name: str


class EchoedHeaders(BaseModel):
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
        "items/7", response_data_type=ItemModel, headers_type=EchoedHeaders
    )

    assert result.typed_headers is not None
