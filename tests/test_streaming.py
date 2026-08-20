from msgspec import Struct
from msgspec.json import Decoder

from lothc import JSON, HTTPClient, SyncHTTPClient


class Line(Struct):
    i: int


async def test_stream_get_without_response_data_type_reconstructs_exact_bytes(
    client: HTTPClient,
) -> None:
    chunks = [chunk async for chunk in client.stream_get("ndjson", params={"count": 3})]

    assert b"".join(chunks) == b'{"i": 0}\n{"i": 1}\n{"i": 2}'


async def test_stream_get_raw_mode_does_not_split_embedded_newlines(client: HTTPClient) -> None:
    # Binary content with embedded `\n` bytes must round-trip exactly — raw mode
    # must not treat those bytes as line separators the way response_data_type does.
    chunks = [chunk async for chunk in client.stream_get("binary")]

    assert b"".join(chunks) == b"AAA\nBBB\x00\nCCC"


async def test_stream_get_decodes_response_data_type(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get("ndjson", params={"count": 3}, response_data_type=JSON)
    ]

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_stream_post_decodes_typed_lines(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_post(
            "ndjson-echo", json={"n": 3}, response_data_type=Decoder(Line)
        )
    ]

    assert lines == [Line(i=0), Line(i=1), Line(i=2)]


def test_sync_stream_get_decodes_response_data_type(sync_client: SyncHTTPClient) -> None:
    lines = list(sync_client.stream_get("ndjson", params={"count": 3}, response_data_type=JSON))

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_sync_stream_post_decodes_typed_lines(sync_client: SyncHTTPClient) -> None:
    lines = list(
        sync_client.stream_post("ndjson-echo", json={"n": 3}, response_data_type=Decoder(Line))
    )

    assert lines == [Line(i=0), Line(i=1), Line(i=2)]
