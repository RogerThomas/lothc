import socket

import pytest

from lothc import HTTPClient, HTTPConnectionError, HTTPTimeoutError


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
