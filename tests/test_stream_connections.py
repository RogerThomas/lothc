"""How the streaming verbs (`stream_get`/`stream_post`, `download`, `sse`) use the network, on
whichever transport runs the suite: over TLS and through a proxy as any request does, a compressed
body decoded as it arrives, the connection kept for the next request once a body is read to its
end, the socket read no faster than the caller reads (and nothing spinning while it waits), a
stream given up mid-read leaving the client usable, and a revoked token replaced as on any verb.
"""

import asyncio
import json
import time
from collections.abc import Generator

import pytest
from _helpers import clear_proxy_variables
from _oauth_server import TokenServer
from _proxy_server import HTTPProxy, serve_http_proxy
from _server import compressed_payload

from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    SyncHTTPClient,
)

_codecs = ["gzip", "deflate", "br", "zstd"]


@pytest.fixture(name="http_proxy")
def _http_proxy(monkeypatch: pytest.MonkeyPatch) -> Generator[HTTPProxy]:
    clear_proxy_variables(monkeypatch)
    with serve_http_proxy() as proxy:
        yield proxy


async def _first_event_ids(client: HTTPClient) -> list[str]:
    # Two events of three, then closed: the TLS test server ends a stream without TLS's
    # close_notify, which reads as a dropped connection that `sse()` would reconnect after.
    events = client.sse("events", params={"count": 3})
    ids = [(await anext(events)).id, (await anext(events)).id]
    await events.aclose()
    return ids


def _sync_first_event_ids(client: SyncHTTPClient) -> list[str]:
    events = client.sse("events", params={"count": 3})
    ids = [next(events).id, next(events).id]
    events.close()
    return ids


async def _written_while_paused(client: HTTPClient, key: str, timeout: float | None) -> int:
    """How much of a 128MB body the server got written while its reader paused for 0.2s, having
    taken one chunk."""
    stream = client.stream_get("large", params={"key": key, "size": 128 * 2**20}, timeout=timeout)
    await anext(stream)
    await asyncio.sleep(0.2)
    written = (await client.get("hits", params={"key": f"{key}-written"}, into=dict)).data
    await stream.aclose()
    return written["hits"]


def _sync_written_while_paused(client: SyncHTTPClient, key: str, timeout: float | None) -> int:
    stream = client.stream_get("large", params={"key": key, "size": 128 * 2**20}, timeout=timeout)
    next(stream)
    time.sleep(0.2)
    written = client.get("hits", params={"key": f"{key}-written"}, into=dict).data
    stream.close()
    return written["hits"]


async def test_the_streaming_verbs_work_over_tls(https_base_url: str, tls_ca_pem: bytes) -> None:
    async with HTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem]) as client:
        lines = [line async for line in client.stream_get("ndjson", params={"count": 2}, into=dict)]
        downloaded = (await client.download("binary")).data
        events = await _first_event_ids(client)

    assert (lines, downloaded, events) == ([{"i": 0}, {"i": 1}], b"AAA\nBBB\x00\nCCC", ["0", "1"])


def test_sync_the_streaming_verbs_work_over_tls(https_base_url: str, tls_ca_pem: bytes) -> None:
    with SyncHTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem]) as client:
        lines = list(client.stream_get("ndjson", params={"count": 2}, into=dict))
        downloaded = client.download("binary").data
        events = _sync_first_event_ids(client)

    assert (lines, downloaded, events) == ([{"i": 0}, {"i": 1}], b"AAA\nBBB\x00\nCCC", ["0", "1"])


async def test_the_streaming_verbs_go_through_a_proxy(base_url: str, http_proxy: HTTPProxy) -> None:
    async with HTTPClient(base_url=base_url, proxy=http_proxy.url) as client:
        lines = [line async for line in client.stream_get("ndjson", params={"count": 2}, into=dict)]
        downloaded = (await client.download("binary")).data
        events = await _first_event_ids(client)

    assert (lines, downloaded, events) == ([{"i": 0}, {"i": 1}], b"AAA\nBBB\x00\nCCC", ["0", "1"])
    assert [request.line for request in http_proxy.requests] == [
        f"GET {base_url}ndjson?count=2 HTTP/1.1",
        f"GET {base_url}binary HTTP/1.1",
        f"GET {base_url}events?count=3 HTTP/1.1",
    ]


def test_sync_the_streaming_verbs_go_through_a_proxy(base_url: str, http_proxy: HTTPProxy) -> None:
    with SyncHTTPClient(base_url=base_url, proxy=http_proxy.url) as client:
        lines = list(client.stream_get("ndjson", params={"count": 2}, into=dict))
        downloaded = client.download("binary").data
        events = _sync_first_event_ids(client)

    assert (lines, downloaded, events) == ([{"i": 0}, {"i": 1}], b"AAA\nBBB\x00\nCCC", ["0", "1"])
    assert [request.line for request in http_proxy.requests] == [
        f"GET {base_url}ndjson?count=2 HTTP/1.1",
        f"GET {base_url}binary HTTP/1.1",
        f"GET {base_url}events?count=3 HTTP/1.1",
    ]


@pytest.mark.parametrize("codec", _codecs)
async def test_a_compressed_stream_is_decoded_as_it_arrives(client: HTTPClient, codec: str) -> None:
    params = {"codec": codec, "shape": "byte-chunks"}
    chunks = [chunk async for chunk in client.stream_get("compressed", params=params)]

    assert b"".join(chunks) == compressed_payload(codec)


@pytest.mark.parametrize("codec", _codecs)
def test_sync_a_compressed_stream_is_decoded_as_it_arrives(
    sync_client: SyncHTTPClient, codec: str
) -> None:
    params = {"codec": codec, "shape": "byte-chunks"}
    chunks = list(sync_client.stream_get("compressed", params=params))

    assert b"".join(chunks) == compressed_payload(codec)


async def test_a_compressed_stream_comes_in_pieces_of_at_most_4kb(client: HTTPClient) -> None:
    # tower-http's, as reqwest decodes a body: a megabyte from a kilobyte of gzip never sits in
    # memory whole.
    chunks = [
        chunk async for chunk in client.stream_get("compressed-zeros", params={"size": 2**20})
    ]

    assert (b"".join(chunks), max(map(len, chunks))) == (bytes(2**20), 4096)


def test_sync_a_compressed_stream_comes_in_pieces_of_at_most_4kb(
    sync_client: SyncHTTPClient,
) -> None:
    chunks = list(sync_client.stream_get("compressed-zeros", params={"size": 2**20}))

    assert (b"".join(chunks), max(map(len, chunks))) == (bytes(2**20), 4096)


async def test_a_compressed_download_is_decoded(client: HTTPClient) -> None:
    response = await client.download("compressed", params={"codec": "zstd-frames"})

    assert (response.data, "content-encoding" in response.headers) == (b"frame-1frame-2", False)


def test_sync_a_compressed_download_is_decoded(sync_client: SyncHTTPClient) -> None:
    response = sync_client.download("compressed", params={"codec": "zstd-frames"})

    assert (response.data, "content-encoding" in response.headers) == (b"frame-1frame-2", False)


async def test_a_corrupt_compressed_stream_is_a_connection_error(client: HTTPClient) -> None:
    with pytest.raises(HTTPConnectionError, match="deflate decompression error"):
        [chunk async for chunk in client.stream_get("compressed", params={"codec": "deflate-raw"})]


def test_sync_a_corrupt_compressed_stream_is_a_connection_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError, match="deflate decompression error"):
        list(sync_client.stream_get("compressed", params={"codec": "deflate-raw"}))


async def test_a_stream_read_to_its_end_leaves_its_connection_for_the_next_request(
    client: HTTPClient,
) -> None:
    streamed = json.loads(b"".join([chunk async for chunk in client.stream_get("peer")]))
    downloaded = json.loads((await client.download("peer")).data)
    after = (await client.get("peer", into=dict)).data

    assert streamed == downloaded == after


@pytest.mark.parametrize("timeout", [None, 5.0], ids=["idle-limit", "total-cap"])
def test_sync_a_stream_read_to_its_end_leaves_its_connection_for_the_next_request(
    sync_client: SyncHTTPClient, timeout: float | None
) -> None:
    streamed = json.loads(b"".join(sync_client.stream_get("peer", timeout=timeout)))
    downloaded = json.loads(sync_client.download("peer", timeout=timeout).data)
    after = sync_client.get("peer", into=dict).data

    assert streamed == downloaded == after


async def test_a_slow_consumer_holds_the_server_back(client: HTTPClient, server_key: str) -> None:
    # Reading ahead of the caller is bounded: what's written beyond the first chunk is the
    # socket buffers' and a few chunks' worth, never the body.
    assert await _written_while_paused(client, server_key, None) < 64 * 2**20


@pytest.mark.parametrize("timeout", [None, 5.0], ids=["idle-limit", "total-cap"])
def test_sync_a_slow_consumer_holds_the_server_back(
    sync_client: SyncHTTPClient, server_key: str, timeout: float | None
) -> None:
    assert _sync_written_while_paused(sync_client, server_key, timeout) < 64 * 2**20


async def test_a_slow_consumer_costs_no_cpu_while_it_pauses(
    client: HTTPClient, server_key: str
) -> None:
    # Data waiting unread on the socket must not keep the client polling it.
    started = time.process_time()
    await _written_while_paused(client, server_key, None)

    assert time.process_time() - started < 0.1


@pytest.mark.parametrize("timeout", [None, 5.0], ids=["idle-limit", "total-cap"])
def test_sync_a_slow_consumer_costs_no_cpu_while_it_pauses(
    sync_client: SyncHTTPClient, server_key: str, timeout: float | None
) -> None:
    started = time.process_time()
    _sync_written_while_paused(sync_client, server_key, timeout)

    assert time.process_time() - started < 0.1


async def test_a_stream_given_up_waiting_for_its_head_leaves_the_client_working(
    client: HTTPClient,
) -> None:
    stream = client.stream_get("slow", params={"seconds": 5})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(stream), 0.05)
    await stream.aclose()

    assert (await client.get("items/7", into=dict)).data == {"id": 7, "name": "item-7"}


def test_sync_a_stream_given_up_waiting_for_its_head_leaves_the_client_working(
    base_url: str,
) -> None:
    with SyncHTTPClient(base_url=base_url, timeout=0.05) as client:
        stream = client.stream_get("slow", params={"seconds": 0.3})
        with pytest.raises(HTTPTimeoutError):
            next(stream)
        stream.close()

        assert client.get("items/7", into=dict).data == {"id": 7, "name": "item-7"}


async def test_a_stream_given_up_mid_read_leaves_the_client_working(client: HTTPClient) -> None:
    stream = client.stream_get("stall-after-first-chunk", params={"seconds": 5})
    await anext(stream)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(stream), 0.05)
    await stream.aclose()

    assert (await client.get("items/7", into=dict)).data == {"id": 7, "name": "item-7"}


def test_sync_a_stream_given_up_mid_read_leaves_the_client_working(base_url: str) -> None:
    # A short stall: the read given up on stays parked until it ends (see `_drain_stream_chunks`).
    with SyncHTTPClient(base_url=base_url, timeout=0.05) as client:
        stream = client.stream_get("stall-after-first-chunk", params={"seconds": 0.3})
        next(stream)
        with pytest.raises(HTTPTimeoutError):
            next(stream)
        stream.close()

        assert client.get("items/7", into=dict).data == {"id": 7, "name": "item-7"}


async def test_a_stream_with_a_revoked_token_is_retried_with_a_fresh_one(
    base_url: str, token_server: TokenServer
) -> None:
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        lines = [line async for line in client.stream_get("rejects-first-token", into=dict)]

    assert lines == [{"authorization": "Bearer token-2"}]


def test_sync_a_stream_with_a_revoked_token_is_retried_with_a_fresh_one(
    base_url: str, token_server: TokenServer
) -> None:
    with SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client:
        lines = list(client.stream_get("rejects-first-token", into=dict))

    assert lines == [{"authorization": "Bearer token-2"}]


async def test_a_streamed_post_with_a_revoked_token_raises_and_the_next_call_gets_a_fresh_one(
    base_url: str, token_server: TokenServer
) -> None:
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            [line async for line in client.stream_post("rejects-first-token", into=dict)]
        follow_up = [line async for line in client.stream_post("rejects-first-token", into=dict)]

    assert exc_info.value.status == 401
    assert follow_up == [{"authorization": "Bearer token-2"}]


def test_sync_a_streamed_post_with_a_revoked_token_raises_and_the_next_call_gets_a_fresh_one(
    base_url: str, token_server: TokenServer
) -> None:
    with SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            list(client.stream_post("rejects-first-token", into=dict))
        follow_up = list(client.stream_post("rejects-first-token", into=dict))

    assert exc_info.value.status == 401
    assert follow_up == [{"authorization": "Bearer token-2"}]


async def test_a_stream_outlives_the_pool_idle_timeout(base_url: str) -> None:
    # A connection carrying a stream is busy, not idle, however long the stream runs.
    async with HTTPClient(base_url=base_url, pool_idle_timeout=0.01) as client:
        events = [
            event.id async for event in client.sse("events", params={"interval": 0.05, "count": 3})
        ]

    assert events == ["0", "1", "2"]


def test_sync_a_stream_outlives_the_pool_idle_timeout(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, pool_idle_timeout=0.01) as client:
        events = [event.id for event in client.sse("events", params={"interval": 0.05, "count": 3})]

    assert events == ["0", "1", "2"]
