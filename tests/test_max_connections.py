"""`max_connections`/`pool_timeout` as pyreqwest implements them (reqwest itself has no such
limit): at most `max_connections` requests in flight at once, every origin's together, each
waiting its turn for a slot, in the order they asked, for at most `pool_timeout` and never past
its own timeout. A request keeps its slot through its redirects and until its body has been read,
a stream until its body ends or it is closed; each attempt of a retried request takes one in turn.
`pool_timeout` alone does nothing. The basic case (a second request times out waiting for the
only slot) is in `tests/test_tls_and_pool_config.py`.

Each test holds the only slot with a stream, or with `/slow` released (`release`) once the wait
it measures is over: a fixed hold could end first on a stalled machine. The sync stream tests use
`timeout=None`, so a stream is read on the caller's thread, only as far as the caller asks (an
idle limit reads it ahead on a worker thread instead).
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import pytest
from _helpers import arrived, closed, native_by_default, release, sync_arrived, sync_release

from lothc import HTTPClient, HTTPTimeoutError, SyncHTTPClient


@pytest.fixture(name="one_slot_client")
async def _one_slot_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.05) as client:
        yield client


@pytest.fixture(name="sync_one_slot_client")
def _sync_one_slot_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.05) as client:
        yield client


@pytest.fixture(name="sync_one_slot_stream_client")
def _sync_one_slot_stream_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(
        base_url=base_url, max_connections=1, pool_timeout=0.05, timeout=None
    ) as client:
        yield client


def _sync_hold_the_slot(client: SyncHTTPClient, key: str) -> bytes:
    return client.get("slow", params={"seconds": 5.0, "key": key}).data


def _sync_release_after(base_url: str, key: str, seconds: float) -> None:
    time.sleep(seconds)
    sync_release(base_url, key)


async def test_max_connections_counts_the_requests_to_every_origin_together(
    base_url: str, server_key: str, one_slot_client: HTTPClient
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")
    holder = asyncio.create_task(
        one_slot_client.get("slow", params={"seconds": 5.0, "key": server_key})
    )
    await arrived(base_url, server_key)

    with pytest.raises(HTTPTimeoutError):
        await one_slot_client.get(f"{other_origin}items/7")
    await release(base_url, server_key)
    assert (await holder).data == b'{"finally": true}'


def test_sync_max_connections_counts_the_requests_to_every_origin_together(
    base_url: str, server_key: str, sync_one_slot_client: SyncHTTPClient
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")
    with ThreadPoolExecutor(max_workers=1) as pool:
        holder = pool.submit(_sync_hold_the_slot, sync_one_slot_client, server_key)
        sync_arrived(base_url, server_key)

        with pytest.raises(HTTPTimeoutError):
            sync_one_slot_client.get(f"{other_origin}items/7")
        sync_release(base_url, server_key)
        assert holder.result() == b'{"finally": true}'


async def test_pool_timeout_without_max_connections_never_times_a_request_out(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, pool_timeout=0.01) as client:
        responses = await asyncio.gather(
            *(client.get("slow", params={"seconds": 0.05}) for _ in range(3))
        )

    assert [response.data for response in responses] == [b'{"finally": true}'] * 3


def test_sync_pool_timeout_without_max_connections_never_times_a_request_out(
    base_url: str,
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, pool_timeout=0.01) as client,
        ThreadPoolExecutor(max_workers=3) as pool,
    ):
        futures = [pool.submit(client.get, "slow", params={"seconds": 0.05}) for _ in range(3)]
        bodies = [future.result().data for future in futures]

    assert bodies == [b'{"finally": true}'] * 3


@pytest.mark.parametrize("pool_timeout", [None, 5.0], ids=["no-pool-timeout", "longer"])
async def test_a_request_waits_for_a_slot_no_longer_than_its_own_timeout(
    base_url: str, server_key: str, pool_timeout: float | None
) -> None:
    async with HTTPClient(
        base_url=base_url, max_connections=1, pool_timeout=pool_timeout
    ) as client:
        holder = asyncio.create_task(client.get("slow", params={"seconds": 5.0, "key": server_key}))
        await arrived(base_url, server_key)
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            await client.get("items/7", timeout=0.03)
        waited = time.monotonic() - started
        await release(base_url, server_key)
        await holder

    # Its own 0.03s, not `pool_timeout`'s 5s nor the hold's: the slot is only given back after.
    assert waited < 1.0


@pytest.mark.parametrize("pool_timeout", [None, 5.0], ids=["no-pool-timeout", "longer"])
def test_sync_a_request_waits_for_a_slot_no_longer_than_its_own_timeout(
    base_url: str, server_key: str, pool_timeout: float | None
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_connections=1, pool_timeout=pool_timeout) as client,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        holder = pool.submit(_sync_hold_the_slot, client, server_key)
        sync_arrived(base_url, server_key)
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get("items/7", timeout=0.03)
        waited = time.monotonic() - started
        sync_release(base_url, server_key)
        holder.result()

    assert waited < 1.0


async def test_the_time_spent_waiting_for_a_slot_counts_against_the_requests_timeout(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=1) as client:
        holder = asyncio.create_task(client.get("slow", params={"seconds": 5.0, "key": server_key}))
        await arrived(base_url, server_key)
        # Long enough for its own exchange, not for that and the wait before it: the slot is
        # given back no sooner than 0.1s into its 0.15s, later if the machine stalls.
        waiting = asyncio.create_task(client.get("slow", params={"seconds": 0.1}, timeout=0.15))
        await asyncio.sleep(0.1)
        await release(base_url, server_key)
        with pytest.raises(HTTPTimeoutError):
            await waiting
        await holder


def test_sync_the_time_spent_waiting_for_a_slot_counts_against_the_requests_timeout(
    base_url: str, server_key: str
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_connections=1) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        holder = pool.submit(_sync_hold_the_slot, client, server_key)
        sync_arrived(base_url, server_key)
        releaser = pool.submit(_sync_release_after, base_url, server_key, 0.1)
        with pytest.raises(HTTPTimeoutError):
            client.get("slow", params={"seconds": 0.1}, timeout=0.15)
        releaser.result()
        holder.result()


async def _get_then_record(client: HTTPClient, index: int, finished: list[int]) -> None:
    await client.get("slow", params={"seconds": 0.03})
    finished.append(index)


def _sync_get_then_record(client: SyncHTTPClient, index: int, finished: list[int]) -> None:
    client.get("slow", params={"seconds": 0.05})
    finished.append(index)


async def test_requests_waiting_for_a_slot_get_one_in_the_order_they_asked(
    base_url: str,
) -> None:
    finished: list[int] = []
    async with HTTPClient(base_url=base_url, max_connections=1) as client:
        requests: list[asyncio.Task[None]] = []
        for index in range(4):
            requests.append(asyncio.create_task(_get_then_record(client, index, finished)))
            await asyncio.sleep(0.01)
        await asyncio.gather(*requests)

    assert finished == [0, 1, 2, 3]


def test_sync_requests_waiting_for_a_slot_get_one_in_the_order_they_asked(base_url: str) -> None:
    finished: list[int] = []
    with (
        SyncHTTPClient(base_url=base_url, max_connections=1) as client,
        ThreadPoolExecutor(max_workers=4) as pool,
    ):
        futures: list[Future[None]] = []
        for index in range(4):
            futures.append(pool.submit(_sync_get_then_record, client, index, finished))
            time.sleep(0.02)
        for future in futures:
            future.result()

    assert finished == [0, 1, 2, 3]


async def test_a_request_keeps_its_slot_for_every_redirect_hop(
    base_url: str, one_slot_client: HTTPClient
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")

    response = await one_slot_client.get(
        "redirect-to", params={"to": f"{other_origin}redirect-chain?hops=2"}, into=dict
    )

    assert response.data == {"hops": "done"}


def test_sync_a_request_keeps_its_slot_for_every_redirect_hop(
    base_url: str, sync_one_slot_client: SyncHTTPClient
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")

    response = sync_one_slot_client.get(
        "redirect-to", params={"to": f"{other_origin}redirect-chain?hops=2"}, into=dict
    )

    assert response.data == {"hops": "done"}


async def test_each_attempt_of_a_retried_request_takes_the_slot_in_turn(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(
        base_url=base_url, max_connections=1, pool_timeout=0.05, max_retries=1, backoff_base=0.001
    ) as client:
        response = await client.get("flaky", params={"key": server_key, "fail_times": 1}, into=dict)

    assert response.data == {"attempts": 2}


def test_sync_each_attempt_of_a_retried_request_takes_the_slot_in_turn(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(
        base_url=base_url, max_connections=1, pool_timeout=0.05, max_retries=1, backoff_base=0.001
    ) as client:
        response = client.get("flaky", params={"key": server_key, "fail_times": 1}, into=dict)

    assert response.data == {"attempts": 2}


async def test_a_stream_holds_its_slot_until_its_body_has_been_read(
    base_url: str, one_slot_client: HTTPClient, server_key: str
) -> None:
    # Not until the stream is closed: the request after it runs first.
    stream = one_slot_client.stream_get("large", params={"key": server_key, "size": 2**20})
    try:
        received = len(await anext(stream))
        with pytest.raises(HTTPTimeoutError):
            await one_slot_client.get("items/7")
        received += sum([len(chunk) async for chunk in stream])
        # `/large` closes its connection behind it without saying so: a request racing that close
        # on the same connection is lost by every client (test_connection_reuse.py), so let it land.
        closed(base_url, server_key)
        after = (await one_slot_client.get("items/7")).data
    finally:
        await stream.aclose()

    assert received == 2**20
    assert after == b'{"id": 7, "name": "item-7"}'


def test_sync_a_stream_holds_its_slot_until_its_body_has_been_read(
    base_url: str, sync_one_slot_stream_client: SyncHTTPClient, server_key: str
) -> None:
    client = sync_one_slot_stream_client
    stream = client.stream_get("large", params={"key": server_key, "size": 2**20})
    try:
        received = len(next(stream))
        with pytest.raises(HTTPTimeoutError):
            client.get("items/7")
        received += sum(len(chunk) for chunk in stream)
        # `/large` closes its connection behind it without saying so: a request racing that close
        # on the same connection is lost by every client (test_connection_reuse.py), so let it land.
        closed(base_url, server_key)
        after = client.get("items/7").data
    finally:
        stream.close()

    assert received == 2**20
    assert after == b'{"id": 7, "name": "item-7"}'


async def test_closing_a_stream_gives_its_slot_back(
    one_slot_client: HTTPClient, server_key: str
) -> None:
    stream = one_slot_client.stream_get("large", params={"key": server_key, "size": 2**22})
    try:
        await anext(stream)
    finally:
        await stream.aclose()

    assert (await one_slot_client.get("items/7")).data == b'{"id": 7, "name": "item-7"}'


def test_sync_closing_a_stream_gives_its_slot_back(
    sync_one_slot_stream_client: SyncHTTPClient, server_key: str
) -> None:
    client = sync_one_slot_stream_client
    stream = client.stream_get("large", params={"key": server_key, "size": 2**22})
    try:
        next(stream)
    finally:
        stream.close()

    assert client.get("items/7").data == b'{"id": 7, "name": "item-7"}'


async def test_cancelling_a_request_waiting_for_a_slot_raises_cancelled_error(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=1) as client:
        holder = asyncio.create_task(client.get("slow", params={"seconds": 5.0, "key": server_key}))
        await arrived(base_url, server_key)
        waiting = asyncio.create_task(client.get("items/7"))
        await asyncio.sleep(0.02)
        waiting.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiting
        await release(base_url, server_key)
        await holder
        assert (await client.get("items/7")).data == b'{"id": 7, "name": "item-7"}'


# pyreqwest keeps running a request after its caller is cancelled: `spawn_handled` drops the task's
# `JoinHandle`, which in tokio detaches the task instead of stopping it.
_pyreqwest_runs_cancelled_requests = pytest.mark.xfail(
    not native_by_default(),
    reason="pyreqwest keeps running a cancelled request in the background",
    strict=True,
)


@_pyreqwest_runs_cancelled_requests
async def test_a_request_cancelled_while_waiting_for_a_slot_is_never_sent(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=1) as client:
        holder = asyncio.create_task(
            client.get("slow", params={"seconds": 5.0, "key": f"{server_key}-holder"})
        )
        await arrived(base_url, f"{server_key}-holder")
        waiting = asyncio.create_task(client.get("flaky", params={"key": server_key}))
        await asyncio.sleep(0.05)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await release(base_url, f"{server_key}-holder")
        await holder
        # Through the same slot, which is handed out in the order it was asked for: a cancelled
        # request still going would be sent, and counted, before this.
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 0}


@_pyreqwest_runs_cancelled_requests
async def test_a_request_cancelled_mid_response_gives_its_slot_back_at_once(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=1) as client:
        slow = asyncio.create_task(client.get("slow", params={"seconds": 1.0, "key": server_key}))
        await arrived(base_url, server_key)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        started = time.monotonic()
        await client.get("items/7")
        waited = time.monotonic() - started

    assert waited < 0.5


async def test_a_request_waiting_for_a_slot_when_the_client_closes_raises_runtime_error(
    base_url: str, server_key: str
) -> None:
    # The slot's holder is a stream whose head has arrived, which reads on after the client
    # closes, so only the close itself can end the wait.
    client = HTTPClient(base_url=base_url, max_connections=1)
    await client.__aenter__()
    stream = client.stream_get("large", params={"key": server_key, "size": 2**22})
    try:
        await anext(stream)
        waiting = asyncio.create_task(client.get("items/7"))
        await asyncio.sleep(0.05)
        await client.__aexit__(None, None, None)
        closed_at = time.monotonic()

        with pytest.raises(RuntimeError, match="client is closed"):
            await waiting
        assert time.monotonic() - closed_at < 0.5
    finally:
        await stream.aclose()


def test_sync_a_request_waiting_for_a_slot_when_the_client_closes_raises_runtime_error(
    base_url: str, server_key: str
) -> None:
    client = SyncHTTPClient(base_url=base_url, max_connections=1, timeout=None)
    client.__enter__()
    stream = client.stream_get("large", params={"key": server_key, "size": 2**22})
    # The stream is closed before the pool waits for its thread, which a wait that the close
    # failed to end would otherwise hold up for good.
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            next(stream)
            waiting = pool.submit(client.get, "items/7")
            time.sleep(0.05)
            client.__exit__(None, None, None)
            closed_at = time.monotonic()

            with pytest.raises(RuntimeError, match="client is closed"):
                waiting.result(timeout=2.0)
            assert time.monotonic() - closed_at < 0.5
        finally:
            stream.close()


def _unusable_limits() -> list[tuple[dict[str, Any], str, str]]:
    # (settings, the exception's class name, its message): pyo3's conversions, and tokio's
    # semaphore panicking past its most permits, which pyo3 raises as `PanicException`.
    return [
        ({"max_connections": -1}, "OverflowError", "can't convert negative int to unsigned"),
        ({"max_connections": 2**70}, "OverflowError", "int too big to convert"),
        (
            {"max_connections": 2**61},
            "PanicException",
            r"MAX_PERMITS permits \(2305843009213693951\)",
        ),
        ({"pool_timeout": -1.0}, "ValueError", "negative timedelta to a Rust Duration"),
    ]


async def _open_and_close(client: HTTPClient) -> None:
    async with client:
        pass


def _open_and_close_sync(client: SyncHTTPClient) -> None:
    with client:
        pass


@pytest.mark.parametrize(("settings", "error", "message"), _unusable_limits())
async def test_an_unusable_max_connections_or_pool_timeout_raises_as_pyreqwest_does(
    settings: dict[str, Any], error: str, message: str
) -> None:
    with pytest.raises(BaseException, match=message) as exc_info:
        await _open_and_close(HTTPClient(**settings))

    assert type(exc_info.value).__name__ == error


@pytest.mark.parametrize(("settings", "error", "message"), _unusable_limits())
def test_sync_an_unusable_max_connections_or_pool_timeout_raises_as_pyreqwest_does(
    settings: dict[str, Any], error: str, message: str
) -> None:
    with pytest.raises(BaseException, match=message) as exc_info:
        _open_and_close_sync(SyncHTTPClient(**settings))

    assert type(exc_info.value).__name__ == error
