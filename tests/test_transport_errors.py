import socket

import pytest

from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPTimeoutError,
    HTTPTransportError,
    SyncHTTPClient,
)


def _closed_port_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/"


async def test_connecting_to_a_closed_port_raises_connection_error() -> None:
    async with HTTPClient.build(base_url=_closed_port_url()) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("anything")


async def test_slow_endpoint_past_timeout_raises_timeout_error(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, timeout=0.1) as client:
        with pytest.raises(HTTPTimeoutError):
            await client.get("slow")


def test_sync_connecting_to_a_closed_port_raises_connection_error() -> None:
    with (
        SyncHTTPClient.build(base_url=_closed_port_url()) as client,
        pytest.raises(HTTPConnectionError),
    ):
        client.get("anything")


def test_sync_slow_endpoint_past_timeout_raises_timeout_error(base_url: str) -> None:
    with (
        SyncHTTPClient.build(base_url=base_url, timeout=0.1) as client,
        pytest.raises(HTTPTimeoutError),
    ):
        client.get("slow")


async def test_per_call_timeout_shorter_than_client_raises_timeout_error(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, timeout=30.0) as client:
        with pytest.raises(HTTPTimeoutError):
            await client.get("slow", timeout=0.1)


def test_sync_per_call_timeout_shorter_than_client_raises_timeout_error(base_url: str) -> None:
    with (
        SyncHTTPClient.build(base_url=base_url, timeout=30.0) as client,
        pytest.raises(HTTPTimeoutError),
    ):
        client.get("slow", timeout=0.1)


async def test_per_call_timeout_longer_than_client_overrides_it(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, timeout=0.1) as client:
        result = await client.get("slow", timeout=10.0, response_data_type=dict)

    assert result == {"finally": True}


def test_sync_per_call_timeout_longer_than_client_overrides_it(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, timeout=0.1) as client:
        result = client.get("slow", timeout=10.0, response_data_type=dict)

    assert result == {"finally": True}


async def test_exceeding_max_redirects_raises_transport_error(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, max_redirects=1) as client:
        with pytest.raises(HTTPTransportError):
            await client.get("redirect-loop")


def test_sync_exceeding_max_redirects_raises_transport_error(base_url: str) -> None:
    with (
        SyncHTTPClient.build(base_url=base_url, max_redirects=1) as client,
        pytest.raises(HTTPTransportError),
    ):
        client.get("redirect-loop")


async def test_https_only_against_plain_http_raises_transport_error(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, https_only=True) as client:
        with pytest.raises(HTTPTransportError):
            await client.get("anything")


def test_sync_https_only_against_plain_http_raises_transport_error(base_url: str) -> None:
    with (
        SyncHTTPClient.build(base_url=base_url, https_only=True) as client,
        pytest.raises(HTTPTransportError),
    ):
        client.get("anything")


async def test_https_only_against_plain_http_raises_transport_error_for_sse(
    base_url: str,
) -> None:
    async with HTTPClient.build(base_url=base_url, https_only=True) as client:
        with pytest.raises(HTTPTransportError):
            async for _ in client.sse("events"):
                pass


def test_sync_https_only_against_plain_http_raises_transport_error_for_sse(
    base_url: str,
) -> None:
    with (
        SyncHTTPClient.build(base_url=base_url, https_only=True) as client,
        pytest.raises(HTTPTransportError),
    ):
        for _ in client.sse("events"):
            pass
