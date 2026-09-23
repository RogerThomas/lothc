"""The streaming verbs return generators, so a caller can close one it stops reading early and
release the connection right away: typed as plain iterators, they had no `close()`/`aclose()` to
call, leaving an abandoned stream open until garbage collection got to it.
"""

import asyncio
import time
from uuid import uuid4

from lothc import HTTPClient, SyncHTTPClient


async def _hangups(client: HTTPClient, key: str) -> int:
    # The server only notices on its next write, a few milliseconds after the close.
    deadline = time.monotonic() + 2.0
    while True:
        hangups = (
            await client.get("hits", params={"key": f"{key}-hangups"}, response_data_type=dict)
        )["hits"]
        if hangups or time.monotonic() > deadline:
            return hangups
        await asyncio.sleep(0.01)


def _sync_hangups(sync_client: SyncHTTPClient, key: str) -> int:
    deadline = time.monotonic() + 2.0
    while True:
        hangups = sync_client.get(
            "hits", params={"key": f"{key}-hangups"}, response_data_type=dict
        )["hits"]
        if hangups or time.monotonic() > deadline:
            return hangups
        time.sleep(0.01)


async def test_closing_a_stream_get_early_releases_the_connection(client: HTTPClient) -> None:
    key = str(uuid4())
    chunks = client.stream_get("endless", params={"key": key})
    await anext(chunks)

    await chunks.aclose()

    assert await _hangups(client, key) == 1


async def test_closing_a_stream_post_early_releases_the_connection(client: HTTPClient) -> None:
    key = str(uuid4())
    chunks = client.stream_post("endless", params={"key": key}, content=b"body")
    await anext(chunks)

    await chunks.aclose()

    assert await _hangups(client, key) == 1


def test_sync_closing_a_stream_get_early_releases_the_connection(
    sync_client: SyncHTTPClient,
) -> None:
    key = str(uuid4())
    chunks = sync_client.stream_get("endless", params={"key": key})
    next(chunks)

    chunks.close()

    assert _sync_hangups(sync_client, key) == 1


def test_sync_closing_a_stream_post_early_releases_the_connection(
    sync_client: SyncHTTPClient,
) -> None:
    key = str(uuid4())
    chunks = sync_client.stream_post("endless", params={"key": key}, content=b"body")
    next(chunks)

    chunks.close()

    assert _sync_hangups(sync_client, key) == 1


async def test_an_sse_stream_can_be_closed_early(client: HTTPClient) -> None:
    events = client.sse("events", params={"count": 100}, max_reconnects=0)
    await anext(events)

    await events.aclose()

    assert [event async for event in events] == []


def test_sync_an_sse_stream_can_be_closed_early(sync_client: SyncHTTPClient) -> None:
    events = sync_client.sse("events", params={"count": 100}, max_reconnects=0)
    next(events)

    events.close()

    assert list(events) == []
