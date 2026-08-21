from pathlib import Path

import pytest

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


async def test_download_returns_raw_bytes_by_default(client: HTTPClient) -> None:
    body = await client.download("binary")

    assert body == b"AAA\nBBB\x00\nCCC"


async def test_download_writes_to_file_when_dest_given(client: HTTPClient, tmp_path: Path) -> None:
    dest = tmp_path / "out.bin"

    result = await client.download("binary", dest)

    assert result is None
    assert dest.read_bytes() == b"AAA\nBBB\x00\nCCC"


async def test_download_raises_response_error_for_status(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.download("boom")

    assert exc_info.value.status == 500


async def test_download_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    body = await client.download("boom", error_for_status=False)

    assert b"internal-server-error" in body


def test_sync_download_returns_raw_bytes_by_default(sync_client: SyncHTTPClient) -> None:
    body = sync_client.download("binary")

    assert body == b"AAA\nBBB\x00\nCCC"


def test_sync_download_writes_to_file_when_dest_given(
    sync_client: SyncHTTPClient, tmp_path: Path
) -> None:
    dest = tmp_path / "out.bin"

    result = sync_client.download("binary", dest)

    assert result is None
    assert dest.read_bytes() == b"AAA\nBBB\x00\nCCC"


def test_sync_download_raises_response_error_for_status(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.download("boom")

    assert exc_info.value.status == 500
