import json
import time
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import msgspec
import pytest
from msgspec import Struct
from msgspec.json import Decoder
from pydantic import BaseModel, TypeAdapter
from typing_extensions import TypeForm

from lothc import HTTPClient, HTTPConnectionError, SyncHTTPClient


class Line(Struct):
    i: int


class LineModel(BaseModel):
    i: int


@pytest.fixture(name="timeout_client")
async def _timeout_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, timeout=0.5) as client:
        yield client


@pytest.fixture(name="sync_timeout_client")
def _sync_timeout_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, timeout=0.5) as client:
        yield client


async def test_stream_get_without_into_reconstructs_exact_bytes(
    client: HTTPClient,
) -> None:
    chunks = [chunk async for chunk in client.stream_get("ndjson", params={"count": 3})]

    assert b"".join(chunks) == b'{"i": 0}\n{"i": 1}\n{"i": 2}'


async def test_stream_get_raw_mode_does_not_split_embedded_newlines(client: HTTPClient) -> None:
    # Binary content with embedded `\n` bytes must round-trip exactly — raw mode
    # must not treat those bytes as line separators the way into does.
    chunks = [chunk async for chunk in client.stream_get("binary")]

    assert b"".join(chunks) == b"AAA\nBBB\x00\nCCC"


async def test_stream_get_decodes_into(client: HTTPClient) -> None:
    lines = [line async for line in client.stream_get("ndjson", params={"count": 3}, into=dict)]

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_stream_post_decodes_typed_lines(client: HTTPClient) -> None:
    lines = [
        line async for line in client.stream_post("ndjson-echo", json={"n": 3}, into=Decoder(Line))
    ]

    assert lines == [Line(i=0), Line(i=1), Line(i=2)]


def test_sync_stream_get_decodes_into(sync_client: SyncHTTPClient) -> None:
    lines = list(sync_client.stream_get("ndjson", params={"count": 3}, into=dict))

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_sync_stream_post_decodes_typed_lines(sync_client: SyncHTTPClient) -> None:
    lines = list(sync_client.stream_post("ndjson-echo", json={"n": 3}, into=Decoder(Line)))

    assert lines == [Line(i=0), Line(i=1), Line(i=2)]


async def test_stream_get_decodes_via_msgspec_struct_class(client: HTTPClient) -> None:
    lines = [line async for line in client.stream_get("ndjson", params={"count": 3}, into=Line)]

    assert lines == [Line(i=0), Line(i=1), Line(i=2)]


async def test_stream_get_decodes_via_pydantic_model_class(client: HTTPClient) -> None:
    lines = [
        line async for line in client.stream_get("ndjson", params={"count": 3}, into=LineModel)
    ]

    assert lines == [LineModel(i=0), LineModel(i=1), LineModel(i=2)]


async def test_stream_get_decodes_via_pydantic_type_adapter(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get(
            "ndjson", params={"count": 3}, into=TypeAdapter(LineModel)
        )
    ]

    assert lines == [LineModel(i=0), LineModel(i=1), LineModel(i=2)]


async def test_stream_get_raw_bytes_mode_error_for_status_false_suppresses_raise(
    client: HTTPClient,
) -> None:
    chunks = [chunk async for chunk in client.stream_get("boom", error_for_status=False)]

    assert json.loads(b"".join(chunks)) == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


async def test_stream_get_skips_blank_lines_between_ndjson_records(client: HTTPClient) -> None:
    lines = [
        line
        async for line in client.stream_get("ndjson-blank-line", params={"count": 3}, into=dict)
    ]

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_stream_get_transport_error_mid_stream_raises_connection_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError):
        [chunk async for chunk in client.stream_get("truncated")]


async def test_stream_get_into_not_a_type_raises_type_error(client: HTTPClient) -> None:
    with pytest.raises(TypeError, match="Unsupported into: 1"):
        [line async for line in client.stream_get("ndjson", into=cast(TypeForm[Any], 1))]


@pytest.mark.parametrize("interruptible", [False, True], ids=["blocking", "interruptible"])
def test_sync_stream_get_without_into_reconstructs_exact_bytes(
    sync_client: SyncHTTPClient, *, interruptible: bool
) -> None:
    chunks = list(
        sync_client.stream_get("ndjson", params={"count": 3}, interruptible=interruptible)
    )

    assert b"".join(chunks) == b'{"i": 0}\n{"i": 1}\n{"i": 2}'


def test_sync_stream_post_interruptible_decodes_typed_lines(sync_client: SyncHTTPClient) -> None:
    lines = list(
        sync_client.stream_post(
            "ndjson-echo", json={"n": 3}, into=Decoder(Line), interruptible=True
        )
    )

    assert lines == [Line(i=0), Line(i=1), Line(i=2)]


def test_sync_stream_get_raw_bytes_mode_error_for_status_false_suppresses_raise(
    sync_client: SyncHTTPClient,
) -> None:
    chunks = list(sync_client.stream_get("boom", error_for_status=False))

    assert json.loads(b"".join(chunks)) == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }


def test_sync_stream_get_skips_blank_lines_between_ndjson_records(
    sync_client: SyncHTTPClient,
) -> None:
    lines = list(sync_client.stream_get("ndjson-blank-line", params={"count": 3}, into=dict))

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


@pytest.mark.parametrize("interruptible", [False, True], ids=["blocking", "interruptible"])
def test_sync_stream_get_transport_error_mid_stream_raises_connection_error(
    sync_client: SyncHTTPClient, *, interruptible: bool
) -> None:
    with pytest.raises(HTTPConnectionError):
        list(sync_client.stream_get("truncated", interruptible=interruptible))


def _consume_one_chunk_then_break(sync_client: SyncHTTPClient) -> None:
    for _ in sync_client.stream_get("ndjson", params={"count": 3}, interruptible=True):
        break


def test_sync_stream_get_interruptible_early_break_does_not_hang(
    sync_client: SyncHTTPClient,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_consume_one_chunk_then_break, sync_client).result(timeout=5)


async def test_stream_get_skips_blank_lines_in_a_crlf_ndjson_stream(client: HTTPClient) -> None:
    # A CRLF stream's blank line arrives as a bare `\r`, which is truthy, so a plain `if line:`
    # handed it to the JSON decoder and raised.
    lines = [
        line
        async for line in client.stream_get(
            "ndjson-blank-line", params={"count": 3, "newline": "crlf"}, into=dict
        )
    ]

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


def test_sync_stream_get_skips_blank_lines_in_a_crlf_ndjson_stream(
    sync_client: SyncHTTPClient,
) -> None:
    lines = list(
        sync_client.stream_get(
            "ndjson-blank-line", params={"count": 3, "newline": "crlf"}, into=dict
        )
    )

    assert lines == [{"i": 0}, {"i": 1}, {"i": 2}]


async def test_stream_get_decodes_many_ndjson_lines_arriving_in_one_chunk(
    client: HTTPClient,
) -> None:
    lines = [
        line
        async for line in client.stream_get("ndjson-blank-line", params={"count": 5000}, into=dict)
    ]

    assert lines == [{"i": i} for i in range(5000)]


async def test_stream_get_hands_over_each_chunk_as_it_arrives(
    server_key: str, timeout_client: HTTPClient
) -> None:
    # `/endless` writes 5 bytes every 5ms and never finishes. Held back until pyreqwest's 64KB
    # default read buffer filled, the first chunk would take over a minute; the 0.5s idle limit
    # turns that into a failure here rather than a hang.
    stream = timeout_client.stream_get("endless", params={"key": server_key})
    started = time.monotonic()
    first = await anext(stream)
    elapsed = time.monotonic() - started
    await stream.aclose()

    assert first.startswith(b"chunk")
    assert elapsed < 0.4


async def test_stream_post_hands_over_each_chunk_as_it_arrives(
    server_key: str, timeout_client: HTTPClient
) -> None:
    stream = timeout_client.stream_post("endless", params={"key": server_key}, json={"a": 1})
    started = time.monotonic()
    first = await anext(stream)
    elapsed = time.monotonic() - started
    await stream.aclose()

    assert first.startswith(b"chunk")
    assert elapsed < 0.4


def test_sync_stream_get_hands_over_each_chunk_as_it_arrives(
    server_key: str, sync_timeout_client: SyncHTTPClient
) -> None:
    stream = sync_timeout_client.stream_get("endless", params={"key": server_key})
    started = time.monotonic()
    first = next(stream)
    elapsed = time.monotonic() - started
    stream.close()

    assert first.startswith(b"chunk")
    assert elapsed < 0.4


def test_sync_stream_post_hands_over_each_chunk_as_it_arrives(
    server_key: str, sync_timeout_client: SyncHTTPClient
) -> None:
    stream = sync_timeout_client.stream_post("endless", params={"key": server_key}, json={"a": 1})
    started = time.monotonic()
    first = next(stream)
    elapsed = time.monotonic() - started
    stream.close()

    assert first.startswith(b"chunk")
    assert elapsed < 0.4


class _WrongLine(Struct):
    i: str


async def test_stream_get_raises_the_decoders_error_for_a_line_that_does_not_fit(
    client: HTTPClient,
) -> None:
    with pytest.raises(msgspec.ValidationError):
        [line async for line in client.stream_get("ndjson", into=_WrongLine)]


def test_sync_stream_get_raises_the_decoders_error_for_a_line_that_does_not_fit(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(msgspec.ValidationError):
        list(sync_client.stream_get("ndjson", into=_WrongLine))
