"""Compressed responses: every request sends `Accept-Encoding: zstd,gzip,deflate,br` unless it
sets its own, and a body decoded only
when its `Content-Encoding` is exactly one of those four, which then drops `Content-Encoding` and
`Content-Length` from the response's headers.
"""

import gzip
import json

import pytest
from _helpers import echoed_headers
from _server import compressed_payload

from lothc import HTTPClient, HTTPConnectionError, HTTPResponseError, SyncHTTPClient

_codecs = ["gzip", "deflate", "br", "zstd"]


@pytest.mark.parametrize("codec", _codecs)
async def test_a_compressed_body_is_decoded(client: HTTPClient, codec: str) -> None:
    result = await client.get("compressed", params={"codec": codec}, into=dict)

    assert result.data == json.loads(compressed_payload(codec))
    assert ("content-encoding" in result.headers, "content-length" in result.headers) == (
        False,
        False,
    )


@pytest.mark.parametrize("codec", _codecs)
def test_sync_a_compressed_body_is_decoded(sync_client: SyncHTTPClient, codec: str) -> None:
    result = sync_client.get("compressed", params={"codec": codec}, into=dict)

    assert result.data == json.loads(compressed_payload(codec))
    assert ("content-encoding" in result.headers, "content-length" in result.headers) == (
        False,
        False,
    )


async def test_every_supported_coding_is_accepted(client: HTTPClient) -> None:
    result = await client.get("echo-headers", into=dict)

    assert echoed_headers(result.data)["accept-encoding"] == "zstd,gzip,deflate,br"


def test_sync_every_supported_coding_is_accepted(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("echo-headers", into=dict)

    assert echoed_headers(result.data)["accept-encoding"] == "zstd,gzip,deflate,br"


async def test_a_caller_set_accept_encoding_wins(client: HTTPClient) -> None:
    result = await client.get("echo-headers", headers={"Accept-Encoding": "identity"}, into=dict)

    assert echoed_headers(result.data)["accept-encoding"] == "identity"


def test_sync_a_caller_set_accept_encoding_wins(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("echo-headers", headers={"Accept-Encoding": "identity"}, into=dict)

    assert echoed_headers(result.data)["accept-encoding"] == "identity"


async def test_a_compressed_body_is_decoded_despite_a_caller_set_accept_encoding(
    client: HTTPClient,
) -> None:
    result = await client.get(
        "compressed", params={"codec": "gzip"}, headers={"Accept-Encoding": "identity"}
    )

    assert result.data == compressed_payload("gzip")


def test_sync_a_compressed_body_is_decoded_despite_a_caller_set_accept_encoding(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get(
        "compressed", params={"codec": "gzip"}, headers={"Accept-Encoding": "identity"}
    )

    assert result.data == compressed_payload("gzip")


@pytest.mark.parametrize("codec", ["gzip-large", "deflate-large", "br-large", "zstd-large"])
async def test_a_body_decompressing_to_many_reads_is_decoded_whole(
    client: HTTPClient, codec: str
) -> None:
    result = await client.get("compressed", params={"codec": codec})

    assert result.data == compressed_payload(codec)


@pytest.mark.parametrize("codec", ["gzip-large", "deflate-large", "br-large", "zstd-large"])
def test_sync_a_body_decompressing_to_many_reads_is_decoded_whole(
    sync_client: SyncHTTPClient, codec: str
) -> None:
    result = sync_client.get("compressed", params={"codec": codec})

    assert result.data == compressed_payload(codec)


async def test_only_the_first_content_encoding_header_decides(client: HTTPClient) -> None:
    result = await client.get("compressed", params={"codec": "gzip", "coding": ["gzip", "br"]})

    assert result.data == compressed_payload("gzip")
    assert "content-encoding" not in result.headers


def test_sync_only_the_first_content_encoding_header_decides(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("compressed", params={"codec": "gzip", "coding": ["gzip", "br"]})

    assert result.data == compressed_payload("gzip")
    assert "content-encoding" not in result.headers


@pytest.mark.parametrize("coding", ["GZIP", "gzip, br"])
async def test_a_coding_that_is_not_exactly_a_supported_one_is_left_alone(
    client: HTTPClient, coding: str
) -> None:
    result = await client.get("compressed", params={"codec": "gzip", "coding": coding})

    assert gzip.decompress(result.data) == compressed_payload("gzip")
    assert (result.headers["content-encoding"], "content-length" in result.headers) == (
        coding,
        True,
    )


@pytest.mark.parametrize("coding", ["GZIP", "gzip, br"])
def test_sync_a_coding_that_is_not_exactly_a_supported_one_is_left_alone(
    sync_client: SyncHTTPClient, coding: str
) -> None:
    result = sync_client.get("compressed", params={"codec": "gzip", "coding": coding})

    assert gzip.decompress(result.data) == compressed_payload("gzip")
    assert (result.headers["content-encoding"], "content-length" in result.headers) == (
        coding,
        True,
    )


@pytest.mark.parametrize("status", [200, 204])
async def test_an_empty_compressed_body_decodes_to_nothing(client: HTTPClient, status: int) -> None:
    result = await client.get("compressed", params={"shape": "empty", "status": status})

    assert (result.status, result.data, "content-encoding" in result.headers) == (
        status,
        b"",
        False,
    )


@pytest.mark.parametrize("status", [200, 204])
def test_sync_an_empty_compressed_body_decodes_to_nothing(
    sync_client: SyncHTTPClient, status: int
) -> None:
    result = sync_client.get("compressed", params={"shape": "empty", "status": status})

    assert (result.status, result.data, "content-encoding" in result.headers) == (
        status,
        b"",
        False,
    )


async def test_a_head_response_drops_the_coding_headers_too(client: HTTPClient) -> None:
    result = await client.head("compressed")

    assert ("content-encoding" in result.headers, "content-length" in result.headers) == (
        False,
        False,
    )


def test_sync_a_head_response_drops_the_coding_headers_too(sync_client: SyncHTTPClient) -> None:
    result = sync_client.head("compressed")

    assert ("content-encoding" in result.headers, "content-length" in result.headers) == (
        False,
        False,
    )


@pytest.mark.parametrize("codec", ["gzip", "deflate", "br"])
async def test_bytes_after_a_complete_stream_in_the_same_read_are_ignored(
    client: HTTPClient, codec: str
) -> None:
    result = await client.get("compressed", params={"codec": codec, "shape": "trailing"})

    assert result.data == compressed_payload(codec)


@pytest.mark.parametrize("codec", ["gzip", "deflate", "br"])
def test_sync_bytes_after_a_complete_stream_in_the_same_read_are_ignored(
    sync_client: SyncHTTPClient, codec: str
) -> None:
    result = sync_client.get("compressed", params={"codec": codec, "shape": "trailing"})

    assert result.data == compressed_payload(codec)


async def test_bytes_arriving_after_a_complete_stream_are_a_connection_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError, match="extra bytes after body"):
        await client.get("compressed", params={"shape": "trailing-later"})


def test_sync_bytes_arriving_after_a_complete_stream_are_a_connection_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError, match="extra bytes after body"):
        sync_client.get("compressed", params={"shape": "trailing-later"})


async def test_bytes_after_a_zstd_frame_must_be_another_frame(client: HTTPClient) -> None:
    with pytest.raises(HTTPConnectionError, match="Unknown frame descriptor"):
        await client.get("compressed", params={"codec": "zstd", "shape": "trailing"})


def test_sync_bytes_after_a_zstd_frame_must_be_another_frame(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError, match="Unknown frame descriptor"):
        sync_client.get("compressed", params={"codec": "zstd", "shape": "trailing"})


async def test_zstd_frames_back_to_back_are_all_decoded(client: HTTPClient) -> None:
    result = await client.get("compressed", params={"codec": "zstd-frames"})

    assert result.data == b"frame-1frame-2"


def test_sync_zstd_frames_back_to_back_are_all_decoded(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("compressed", params={"codec": "zstd-frames"})

    assert result.data == b"frame-1frame-2"


@pytest.mark.parametrize("codec", ["gzip", "gzip-named", "deflate", "br", "zstd"])
async def test_a_body_arriving_one_byte_at_a_time_is_decoded(
    client: HTTPClient, codec: str
) -> None:
    result = await client.get("compressed", params={"codec": codec, "shape": "byte-chunks"})

    assert result.data == compressed_payload(codec)


@pytest.mark.parametrize("codec", ["gzip", "gzip-named", "deflate", "br", "zstd"])
def test_sync_a_body_arriving_one_byte_at_a_time_is_decoded(
    sync_client: SyncHTTPClient, codec: str
) -> None:
    result = sync_client.get("compressed", params={"codec": codec, "shape": "byte-chunks"})

    assert result.data == compressed_payload(codec)


async def test_zstd_frames_arriving_one_byte_at_a_time_are_all_decoded(
    client: HTTPClient,
) -> None:
    result = await client.get("compressed", params={"codec": "zstd-frames", "shape": "byte-chunks"})

    assert result.data == b"frame-1frame-2"


def test_sync_zstd_frames_arriving_one_byte_at_a_time_are_all_decoded(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get("compressed", params={"codec": "zstd-frames", "shape": "byte-chunks"})

    assert result.data == b"frame-1frame-2"


async def test_gzip_header_fields_are_skipped(client: HTTPClient) -> None:
    result = await client.get("compressed", params={"codec": "gzip-named"})

    assert result.data == compressed_payload("gzip-named")


def test_sync_gzip_header_fields_are_skipped(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("compressed", params={"codec": "gzip-named"})

    assert result.data == compressed_payload("gzip-named")


async def test_deflate_means_zlib_wrapped(client: HTTPClient) -> None:
    with pytest.raises(HTTPConnectionError, match="deflate decompression error"):
        await client.get("compressed", params={"codec": "deflate-raw"})


def test_sync_deflate_means_zlib_wrapped(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPConnectionError, match="deflate decompression error"):
        sync_client.get("compressed", params={"codec": "deflate-raw"})


@pytest.mark.parametrize(
    ("shape", "message"),
    [("bad-crc", "CRC computed does not match"), ("bad-length", "amount of bytes read")],
)
async def test_a_gzip_footer_that_does_not_match_is_a_connection_error(
    client: HTTPClient, shape: str, message: str
) -> None:
    with pytest.raises(HTTPConnectionError, match=message):
        await client.get("compressed", params={"shape": shape})


@pytest.mark.parametrize(
    ("shape", "message"),
    [("bad-crc", "CRC computed does not match"), ("bad-length", "amount of bytes read")],
)
def test_sync_a_gzip_footer_that_does_not_match_is_a_connection_error(
    sync_client: SyncHTTPClient, shape: str, message: str
) -> None:
    with pytest.raises(HTTPConnectionError, match=message):
        sync_client.get("compressed", params={"shape": shape})


async def test_removing_the_coding_headers_keeps_the_rest(client: HTTPClient) -> None:
    result = await client.get("compressed", params={"extra": 1})

    assert sorted(result.headers) == ["date", "server", "x-1", "x-2"]


def test_sync_removing_the_coding_headers_keeps_the_rest(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("compressed", params={"extra": 1})

    assert sorted(result.headers) == ["date", "server", "x-1", "x-2"]


async def test_a_corrupt_compressed_body_is_a_connection_error(
    client: HTTPClient, server_key: str
) -> None:
    with pytest.raises(HTTPConnectionError, match="Invalid gzip header") as excinfo:
        await client.get("corrupt-gzip", params={"key": server_key})

    # Not a cut-off connection, so lothc's retries leave it alone.
    assert "error reading a body from connection" not in str(excinfo.value)


def test_sync_a_corrupt_compressed_body_is_a_connection_error(
    sync_client: SyncHTTPClient, server_key: str
) -> None:
    with pytest.raises(HTTPConnectionError, match="Invalid gzip header") as excinfo:
        sync_client.get("corrupt-gzip", params={"key": server_key})

    assert "error reading a body from connection" not in str(excinfo.value)


@pytest.mark.parametrize("codec", _codecs)
async def test_a_compressed_stream_cut_short_is_a_connection_error(
    client: HTTPClient, codec: str
) -> None:
    with pytest.raises(HTTPConnectionError) as excinfo:
        await client.get("compressed", params={"codec": codec, "shape": "truncated"})

    # Not a cut-off connection, so lothc's retries leave it alone.
    assert "error reading a body from connection" not in str(excinfo.value)


@pytest.mark.parametrize("codec", _codecs)
def test_sync_a_compressed_stream_cut_short_is_a_connection_error(
    sync_client: SyncHTTPClient, codec: str
) -> None:
    with pytest.raises(HTTPConnectionError) as excinfo:
        sync_client.get("compressed", params={"codec": codec, "shape": "truncated"})

    assert "error reading a body from connection" not in str(excinfo.value)


@pytest.mark.parametrize("codec", _codecs)
async def test_a_compressed_body_whose_connection_is_cut_off_is_a_connection_error(
    client: HTTPClient, codec: str
) -> None:
    # The connection's own failure, which lothc's retries recognise.
    with pytest.raises(HTTPConnectionError, match="error reading a body from connection"):
        await client.get("compressed", params={"codec": codec, "shape": "cut-off"})


@pytest.mark.parametrize("codec", _codecs)
def test_sync_a_compressed_body_whose_connection_is_cut_off_is_a_connection_error(
    sync_client: SyncHTTPClient, codec: str
) -> None:
    with pytest.raises(HTTPConnectionError, match="error reading a body from connection"):
        sync_client.get("compressed", params={"codec": codec, "shape": "cut-off"})


async def test_a_compressed_error_body_is_decoded(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as excinfo:
        await client.get("compressed", params={"status": 404})

    assert excinfo.value.body == compressed_payload("gzip")


def test_sync_a_compressed_error_body_is_decoded(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as excinfo:
        sync_client.get("compressed", params={"status": 404})

    assert excinfo.value.body == compressed_payload("gzip")
