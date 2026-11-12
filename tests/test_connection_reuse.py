"""Requests keep succeeding when the server closes connections between them, whether it says so
(`Connection: close`) or not (an idle keep-alive connection dropped). Several requests on one
client, since reusing a dead connection would fail one of them.

A server can also close a connection right behind a response without saying it will (`/large`,
`/events-silent-close`). When the close has already arrived as the next request to that origin
starts, that request goes out on a new connection, however the response was read (buffered,
`stream_get`, `download` or `sse`), with or without `max_connections`. Each test first waits for
the server to have closed (`closed`), since a request racing the close of the very connection it
reuses is lost by every client: on Linux, loopback buffers can hold all of `/large`, so the close
needn't have arrived by the time its body has been read."""

import pytest
from _helpers import closed

from lothc import HTTPClient, SyncHTTPClient

_item = b'{"id": 7, "name": "item-7"}'


@pytest.mark.parametrize(
    ("path", "body"),
    [("connection-close", {"closed": "announced"}), ("silent-close", {"closed": "silently"})],
)
async def test_requests_succeed_after_the_server_closes_the_connection(
    base_url: str, server_key: str, client: HTTPClient, path: str, body: dict[str, str]
) -> None:
    results: list[dict[str, str]] = []
    for index in range(3):
        key = f"{server_key}-{index}"
        results.append((await client.get(path, params={"key": key}, into=dict)).data)
        # The server's close lands first, as it would between real requests: a request racing
        # the close of the very connection it reuses is lost by every client.
        closed(base_url, key)

    assert results == [body, body, body]


@pytest.mark.parametrize(
    ("path", "body"),
    [("connection-close", {"closed": "announced"}), ("silent-close", {"closed": "silently"})],
)
def test_sync_requests_succeed_after_the_server_closes_the_connection(
    base_url: str, server_key: str, sync_client: SyncHTTPClient, path: str, body: dict[str, str]
) -> None:
    results: list[dict[str, str]] = []
    for index in range(3):
        key = f"{server_key}-{index}"
        results.append(sync_client.get(path, params={"key": key}, into=dict).data)
        closed(base_url, key)

    assert results == [body, body, body]


@pytest.mark.parametrize("max_connections", [None, 1])
async def test_a_request_after_a_body_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=max_connections) as client:
        body = (await client.get("large", params={"key": server_key, "size": 2**20})).data
        closed(base_url, server_key)
        after = (await client.get("items/7")).data

    assert len(body) == 2**20
    assert after == _item


@pytest.mark.parametrize("max_connections", [None, 1])
def test_sync_a_request_after_a_body_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    with SyncHTTPClient(base_url=base_url, max_connections=max_connections) as client:
        body = client.get("large", params={"key": server_key, "size": 2**20}).data
        closed(base_url, server_key)
        after = client.get("items/7").data

    assert len(body) == 2**20
    assert after == _item


@pytest.mark.parametrize("max_connections", [None, 1])
async def test_a_request_after_a_stream_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=max_connections) as client:
        stream = client.stream_get("large", params={"key": server_key, "size": 2**20})
        received = sum([len(chunk) async for chunk in stream])
        closed(base_url, server_key)
        after = (await client.get("items/7")).data

    assert received == 2**20
    assert after == _item


@pytest.mark.parametrize("max_connections", [None, 1])
def test_sync_a_request_after_a_stream_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    with SyncHTTPClient(base_url=base_url, max_connections=max_connections) as client:
        stream = client.stream_get("large", params={"key": server_key, "size": 2**20})
        received = sum(len(chunk) for chunk in stream)
        closed(base_url, server_key)
        after = client.get("items/7").data

    assert received == 2**20
    assert after == _item


@pytest.mark.parametrize("max_connections", [None, 1])
async def test_a_request_after_a_download_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=max_connections) as client:
        body = (await client.download("large", params={"key": server_key, "size": 2**20})).data
        closed(base_url, server_key)
        after = (await client.get("items/7")).data

    assert len(body) == 2**20
    assert after == _item


@pytest.mark.parametrize("max_connections", [None, 1])
def test_sync_a_request_after_a_download_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    with SyncHTTPClient(base_url=base_url, max_connections=max_connections) as client:
        body = client.download("large", params={"key": server_key, "size": 2**20}).data
        closed(base_url, server_key)
        after = client.get("items/7").data

    assert len(body) == 2**20
    assert after == _item


@pytest.mark.parametrize("max_connections", [None, 1])
async def test_a_request_after_an_sse_stream_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=max_connections) as client:
        data = [
            event.data
            async for event in client.sse("events-silent-close", params={"key": server_key})
        ]
        closed(base_url, server_key)
        after = (await client.get("items/7")).data

    assert data == ["only"]
    assert after == _item


@pytest.mark.parametrize("max_connections", [None, 1])
def test_sync_a_request_after_an_sse_stream_the_server_closed_behind_succeeds(
    base_url: str, server_key: str, max_connections: int | None
) -> None:
    with SyncHTTPClient(base_url=base_url, max_connections=max_connections) as client:
        data = [
            event.data for event in client.sse("events-silent-close", params={"key": server_key})
        ]
        closed(base_url, server_key)
        after = client.get("items/7").data

    assert data == ["only"]
    assert after == _item
