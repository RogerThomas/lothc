"""Requests keep succeeding when the server closes connections between them, whether it says so
(`Connection: close`) or not (an idle keep-alive connection dropped). Several requests on one
client, since reusing a dead connection would fail one of them."""

import asyncio
import time

import pytest

from lothc import HTTPClient, SyncHTTPClient


@pytest.mark.parametrize(
    ("path", "body"),
    [("connection-close", {"closed": "announced"}), ("silent-close", {"closed": "silently"})],
)
async def test_requests_succeed_after_the_server_closes_the_connection(
    client: HTTPClient, path: str, body: dict[str, str]
) -> None:
    results: list[dict[str, str]] = []
    for _ in range(3):
        results.append((await client.get(path, into=dict)).data)
        # Lets the server's close land first, as it would between real requests: a request
        # racing the close of the very connection it reuses is lost by every client.
        await asyncio.sleep(0.01)

    assert results == [body, body, body]


@pytest.mark.parametrize(
    ("path", "body"),
    [("connection-close", {"closed": "announced"}), ("silent-close", {"closed": "silently"})],
)
def test_sync_requests_succeed_after_the_server_closes_the_connection(
    sync_client: SyncHTTPClient, path: str, body: dict[str, str]
) -> None:
    results: list[dict[str, str]] = []
    for _ in range(3):
        results.append(sync_client.get(path, into=dict).data)
        time.sleep(0.01)

    assert results == [body, body, body]
