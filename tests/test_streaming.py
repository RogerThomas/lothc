from typing import Any, TypedDict, cast

import pytest
from msgspec import Struct
from msgspec.json import Decoder
from pydantic import BaseModel, TypeAdapter

from lothc import JSON, HTTPClient, HTTPConnectionError, SyncHTTPClient


class Line(Struct):
    i: int


class LineModel(BaseModel):
    i: int


class LineDict(TypedDict):
    i: int


class _Unsupported:
    pass


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


async def test_stream_get_decodes_via_msgspec_struct_class(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get("ndjson", params={"count": 3}, response_data_type=Line)
    ]

    assert lines == [Line(i=0), Line(i=1), Line(i=2)]


async def test_stream_get_decodes_via_pydantic_model_class(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get(
            "ndjson", params={"count": 3}, response_data_type=LineModel
        )
    ]

    assert lines == [LineModel(i=0), LineModel(i=1), LineModel(i=2)]


async def test_stream_get_decodes_via_pydantic_type_adapter(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get(
            "ndjson", params={"count": 3}, response_data_type=TypeAdapter(LineModel)
        )
    ]

    assert lines == [LineModel(i=0), LineModel(i=1), LineModel(i=2)]


async def test_stream_get_decodes_typed_dict(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get(
            "ndjson", params={"count": 3}, response_data_type=LineDict
        )
    ]

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_stream_get_raw_bytes_mode_error_for_status_false_suppresses_raise(
    client: HTTPClient,
) -> None:
    chunks = [chunk async for chunk in client.stream_get("boom", error_for_status=False)]

    assert b"".join(chunks) != b""


async def test_stream_get_skips_blank_lines_between_ndjson_records(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get(
            "ndjson-blank-line", params={"count": 3}, response_data_type=JSON
        )
    ]

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_stream_get_transport_error_mid_stream_raises_connection_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError):
        [chunk async for chunk in client.stream_get("truncated")]


async def test_stream_get_unsupported_response_data_type_raises_type_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(TypeError, match="Unsupported SSE response_data_type"):
        [
            line
            async for line in client.stream_get(
                "ndjson", params={"count": 1}, response_data_type=cast(Any, _Unsupported)
            )
        ]


def test_sync_stream_get_without_response_data_type_reconstructs_exact_bytes(
    sync_client: SyncHTTPClient,
) -> None:
    chunks = list(sync_client.stream_get("ndjson", params={"count": 3}))

    assert b"".join(chunks) == b'{"i": 0}\n{"i": 1}\n{"i": 2}'


def test_sync_stream_get_raw_bytes_mode_error_for_status_false_suppresses_raise(
    sync_client: SyncHTTPClient,
) -> None:
    chunks = list(sync_client.stream_get("boom", error_for_status=False))

    assert b"".join(chunks) != b""


def test_sync_stream_get_skips_blank_lines_between_ndjson_records(
    sync_client: SyncHTTPClient,
) -> None:
    lines = list(
        sync_client.stream_get("ndjson-blank-line", params={"count": 3}, response_data_type=JSON)
    )

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_sync_stream_get_transport_error_mid_stream_raises_connection_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError):
        list(sync_client.stream_get("truncated"))
