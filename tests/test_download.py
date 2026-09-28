from pathlib import Path

import pytest

from lothc import HTTPClient, HTTPConnectionError, HTTPResponseError, SyncHTTPClient


async def test_download_returns_raw_bytes_by_default(client: HTTPClient) -> None:
    response = await client.download("binary")

    assert response.data == b"AAA\nBBB\x00\nCCC"


async def test_download_writes_to_file_when_dest_given(client: HTTPClient, tmp_path: Path) -> None:
    dest = tmp_path / "dest"

    response = await client.download("binary", dest)

    assert response.data == dest
    assert dest.read_bytes() == b"AAA\nBBB\x00\nCCC"


async def test_download_raises_response_error_for_status(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.download("boom")

    assert exc_info.value.status == 500


async def test_download_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    response = await client.download("boom", error_for_status=False)

    assert response.status == 500
    assert b"internal-server-error" in response.data


def test_sync_download_returns_raw_bytes_by_default(sync_client: SyncHTTPClient) -> None:
    response = sync_client.download("binary")

    assert response.data == b"AAA\nBBB\x00\nCCC"


def test_sync_download_writes_to_file_when_dest_given(
    sync_client: SyncHTTPClient, tmp_path: Path
) -> None:
    dest = tmp_path / "dest"

    response = sync_client.download("binary", dest)

    assert response.data == dest
    assert dest.read_bytes() == b"AAA\nBBB\x00\nCCC"


def test_sync_download_raises_response_error_for_status(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.download("boom")

    assert exc_info.value.status == 500


def test_sync_download_error_for_status_false_suppresses_raise(
    sync_client: SyncHTTPClient,
) -> None:
    response = sync_client.download("boom", error_for_status=False)

    assert response.status == 500
    assert b"internal-server-error" in response.data


async def test_download_transport_error_mid_stream_raises_connection_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError):
        await client.download("truncated")


def test_sync_download_transport_error_mid_stream_raises_connection_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError):
        sync_client.download("truncated")


async def test_failed_download_leaves_no_partial_file(client: HTTPClient, tmp_path: Path) -> None:
    dest = tmp_path / "dest"

    with pytest.raises(HTTPConnectionError):
        await client.download("truncated-after-first-chunk", dest=dest)

    # Neither a truncated `dest` nor the hidden temp file it was being written into.
    assert list(tmp_path.iterdir()) == []


async def test_failed_download_keeps_an_existing_file_intact(
    client: HTTPClient, tmp_path: Path
) -> None:
    # The old direct `dest.open("wb")` truncated this before the first byte had even arrived.
    dest = tmp_path / "dest"
    dest.write_bytes(b"previous-content")

    with pytest.raises(HTTPConnectionError):
        await client.download("truncated-after-first-chunk", dest=dest)

    assert dest.read_bytes() == b"previous-content"
    assert list(tmp_path.iterdir()) == [dest]


def test_sync_failed_download_keeps_an_existing_file_intact(
    sync_client: SyncHTTPClient, tmp_path: Path
) -> None:
    dest = tmp_path / "dest"
    dest.write_bytes(b"previous-content")

    with pytest.raises(HTTPConnectionError):
        sync_client.download("truncated-after-first-chunk", dest=dest)

    assert dest.read_bytes() == b"previous-content"
    assert list(tmp_path.iterdir()) == [dest]


async def test_successful_download_replaces_an_existing_file(
    client: HTTPClient, tmp_path: Path
) -> None:
    dest = tmp_path / "dest"
    dest.write_bytes(b"previous-content")

    await client.download("binary", dest=dest)

    assert dest.read_bytes() == b"AAA\nBBB\x00\nCCC"
    assert list(tmp_path.iterdir()) == [dest]


async def test_download_accepts_a_str_dest(client: HTTPClient, tmp_path: Path) -> None:
    dest = tmp_path / "dest"

    response = await client.download("binary", dest=str(dest))

    assert response.data == dest
    assert dest.read_bytes() == b"AAA\nBBB\x00\nCCC"


def test_sync_download_accepts_a_str_dest(sync_client: SyncHTTPClient, tmp_path: Path) -> None:
    dest = tmp_path / "dest"

    response = sync_client.download("binary", dest=str(dest))

    assert response.data == dest
    assert dest.read_bytes() == b"AAA\nBBB\x00\nCCC"


async def test_download_response_carries_status_headers_and_request(
    client: HTTPClient, base_url: str
) -> None:
    response = await client.download("binary")

    assert response.status == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.typed_headers is None
    assert response.request.url == f"{base_url}binary"
    assert response.http_version == "HTTP/1.1"
    assert response.elapsed > 0


async def test_download_to_file_response_carries_status_and_headers(
    client: HTTPClient, tmp_path: Path
) -> None:
    response = await client.download("binary", tmp_path / "dest")

    assert response.status == 200
    assert response.headers["Content-Length"] == "12"


@pytest.mark.parametrize(
    "timeout",
    [
        # No per-call timeout: the idle limit applies, so the body is read on a worker thread.
        None,
        # An explicit one is a total cap instead, read on the calling thread.
        5.0,
    ],
)
def test_sync_download_response_carries_status_headers_and_request(
    sync_client: SyncHTTPClient, base_url: str, timeout: float | None
) -> None:
    response = sync_client.download("binary", timeout=timeout)

    assert response.status == 200
    assert response.headers["Content-Type"] == "application/octet-stream"
    assert response.typed_headers is None
    assert response.request.url == f"{base_url}binary"
    assert response.http_version == "HTTP/1.1"
    assert response.elapsed > 0


def test_sync_download_to_file_response_carries_status_and_headers(
    sync_client: SyncHTTPClient, tmp_path: Path
) -> None:
    response = sync_client.download("binary", tmp_path / "dest")

    assert response.status == 200
    assert response.headers["Content-Length"] == "12"
