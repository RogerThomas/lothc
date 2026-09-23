"""How the client's `timeout` applies to the long-lived-body verbs (`stream_get`, `stream_post`,
`download`): as the longest allowed *gap* between chunks, not a cap on the whole transfer. A total
cap used to kill every healthy stream or large download at the 30s mark.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from lothc import HTTPClient, HTTPTimeoutError, SyncHTTPClient


def _stream_workers() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if "_drain_stream_chunks" in t.name]


async def test_stream_outlasting_the_client_timeout_is_not_killed(base_url: str) -> None:
    # A 0.16s stream against a 0.08s `timeout`: every gap is 0.02s, so nothing actually stalls.
    async with HTTPClient(base_url=base_url, timeout=0.08) as client:
        body = b"".join([
            chunk
            async for chunk in client.stream_get("events", params={"interval": 0.02, "count": 8})
        ])

    assert body.count(b"data: ") == 8


def test_sync_stream_outlasting_the_client_timeout_is_not_killed(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, timeout=0.08) as client:
        body = b"".join(client.stream_get("events", params={"interval": 0.02, "count": 8}))

    assert body.count(b"data: ") == 8


async def test_download_outlasting_the_client_timeout_is_not_killed(
    base_url: str, tmp_path: Path
) -> None:
    dest = tmp_path / "dest"
    async with HTTPClient(base_url=base_url, timeout=0.08) as client:
        await client.download("events", dest=dest, params={"interval": 0.02, "count": 8})

    assert dest.read_bytes().count(b"data: ") == 8


def test_sync_download_outlasting_the_client_timeout_is_not_killed(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, timeout=0.08) as client:
        body = client.download("events", params={"interval": 0.02, "count": 8})

    assert body.count(b"data: ") == 8


async def test_a_stalled_stream_raises_within_the_client_timeout(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, timeout=0.1) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError, match="between chunks"):
            async for _ in client.stream_get("stall-after-first-chunk", params={"seconds": 1}):
                pass
        elapsed = time.monotonic() - started

    assert elapsed < 0.6  # gave up during the 1s silence, rather than waiting it out


def test_sync_a_stalled_stream_raises_within_the_client_timeout(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, timeout=0.1) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError, match="between chunks"):
            for _ in client.stream_get("stall-after-first-chunk", params={"seconds": 1}):
                pass
        elapsed = time.monotonic() - started

    assert elapsed < 0.6


async def test_a_stalled_download_raises_and_leaves_no_file(base_url: str, tmp_path: Path) -> None:
    async with HTTPClient(base_url=base_url, timeout=0.1) as client:
        with pytest.raises(HTTPTimeoutError):
            await client.download(
                "stall-after-first-chunk", dest=tmp_path / "dest", params={"seconds": 1}
            )

    assert list(tmp_path.iterdir()) == []


def test_sync_a_stalled_download_raises(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, timeout=0.1) as client,
        pytest.raises(HTTPTimeoutError),
    ):
        client.download("stall-after-first-chunk", params={"seconds": 1})


async def test_waiting_for_response_headers_is_bounded_too(base_url: str) -> None:
    # With the total cap gone, a server that never answers at all must still fail.
    async with HTTPClient(base_url=base_url, timeout=0.1) as client:
        with pytest.raises(HTTPTimeoutError):
            async for _ in client.stream_get("slow", params={"seconds": 1}):
                pass


def test_sync_waiting_for_response_headers_is_bounded_too(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, timeout=0.1) as client,
        pytest.raises(HTTPTimeoutError),
    ):
        list(client.stream_get("slow", params={"seconds": 1}))


async def test_a_slow_consumer_is_not_mistaken_for_a_stalled_server(base_url: str) -> None:
    # The idle clock must only ever run while waiting on the server: here the caller takes longer
    # than `timeout` over every chunk, and the stream must still complete.
    async with HTTPClient(base_url=base_url, timeout=0.1) as client:
        seen = 0
        async for _ in client.stream_get("events", params={"interval": 0.001, "count": 1}):
            seen += 1
            time.sleep(0.25)

    assert seen >= 1


def test_sync_a_slow_consumer_is_not_mistaken_for_a_stalled_server(base_url: str) -> None:
    # The sync worker reads ahead, so the clock has to restart when the caller asks for the next
    # chunk, not when the last one arrived. Timed so the next event lands 0.1s *after* the
    # caller's 0.3s pause: the client itself only ever waits 0.1s (fine against 0.2s), but a
    # clock left running from before the pause would already read 0.35s at its first poll.
    with SyncHTTPClient(base_url=base_url, timeout=0.2) as client:
        seen = 0
        for _ in client.stream_get("events", params={"interval": 0.4, "count": 2}):
            seen += 1
            if seen == 2:
                break
            time.sleep(0.3)

    assert seen == 2


async def test_an_explicit_per_call_timeout_still_caps_the_whole_stream(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        with pytest.raises(HTTPTimeoutError):
            async for _ in client.stream_get(
                "events", params={"interval": 0.05, "count": 6}, timeout=0.1
            ):
                pass


async def test_read_timeout_replaces_the_idle_limit_when_set(base_url: str) -> None:
    # 0.15s gaps: longer than `timeout` (0.05s) but shorter than `read_timeout` (1.0s). Passing
    # proves the client's `timeout` was not also applied as an idle limit on top.
    async with HTTPClient(base_url=base_url, timeout=0.05, read_timeout=1.0) as client:
        body = b"".join([
            chunk
            async for chunk in client.stream_get("events", params={"interval": 0.15, "count": 1})
        ])

    assert body.count(b"data: ") == 1


async def test_sse_keeps_no_idle_limit(base_url: str) -> None:
    # Quiet gaps are normal on an SSE stream, so `sse()` is deliberately left without one.
    async with HTTPClient(base_url=base_url, timeout=0.05) as client:
        events = [
            event
            async for event in client.sse(
                "events", params={"interval": 0.15, "count": 1}, max_reconnects=0
            )
        ]

    assert len(events) == 1


def test_sync_stream_worker_exits_when_the_caller_stops_early(base_url: str) -> None:
    # The worker hands chunks over a bounded queue; once the caller stops, it must notice and
    # release the connection rather than stay parked waiting for room in the queue.
    # Only this test's own worker counts: a stalled stream elsewhere in the suite deliberately
    # leaves its worker parked in `read_chunk()` until that server closes the connection.
    already_running = set(_stream_workers())
    started: set[threading.Thread] = set()
    with SyncHTTPClient(base_url=base_url, timeout=5.0) as client:
        # A `break`, the way a caller really stops early: once the loop exits, the iterator is
        # dropped and the generator's cleanup runs.
        for _ in client.stream_get("events", params={"interval": 0.001, "count": 2000}):
            started = set(_stream_workers()) - already_running
            break

        deadline = time.monotonic() + 3
        while started & set(_stream_workers()) and time.monotonic() < deadline:
            time.sleep(0.02)

    assert started, "the stream should have run on a worker thread"
    assert not started & set(_stream_workers())


def test_sync_stream_loses_nothing_when_the_consumer_falls_behind(base_url: str) -> None:
    # The worker's queue is bounded: while the caller is paused, the worker fills it and then has
    # to wait for room (backpressure) instead of buffering the whole body in memory. Every event
    # must still come through, in order, once the caller catches up.
    with SyncHTTPClient(base_url=base_url, timeout=5.0) as client:
        stream = client.stream_get("events", params={"interval": 0.0005, "count": 400})
        body = bytearray(next(stream))
        time.sleep(0.3)
        for chunk in stream:
            body += chunk

    events = [
        json.loads(line[len(b"data: ") :])
        for line in bytes(body).split(b"\n")
        if line.startswith(b"data: ")
    ]
    assert [event["msg"] for event in events] == [f"hello {i}" for i in range(400)]
