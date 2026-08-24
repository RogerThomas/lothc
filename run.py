#!yeet
from msgspec import Struct

from lothc import HTTPClient, HTTPResponseError


class GetResponse(Struct):
    args: dict[str, str]
    headers: dict[str, str]
    origin: str
    url: str


class PostResponse(Struct):
    json: dict[str, object] | None
    headers: dict[str, str]
    origin: str
    url: str


class HeadersResponse(Struct):
    headers: dict[str, str]


async def main() -> None:
    async with HTTPClient.build(base_url="https://httpbin.org/") as client:
        get_result = await client.get("get", params={"foo": "bar"}, response_data_type=GetResponse)
        print("GET  /get        ->", get_result.args, get_result.url)

        post_result = await client.post("post", json={"a": 1}, response_data_type=PostResponse)
        print("POST /post       ->", post_result.json)

        headers_result = await client.get(
            "headers", headers={"X-Lothc-Demo": "1"}, response_data_type=HeadersResponse
        )
        print("GET  /headers    ->", headers_result.headers.get("X-Lothc-Demo"))

        redirected = await client.get("redirect/2", response_data_type=GetResponse)
        print("GET  /redirect/2 -> followed to", redirected.url)

        try:
            await client.get("status/404")
        except HTTPResponseError as error:
            print("GET  /status/404 -> raised HTTPResponseError, status", error.status)
