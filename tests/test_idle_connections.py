"""Idle keep-alive connections: `pool_idle_timeout` (reqwest's 90s when unset) and
`pool_max_idle_per_host`, told apart by `/peer`, which answers with the client port of the
connection that served it. Two concurrent requests finish in a known order by holding both
answers back by different amounts (`seconds=`), which shows which idle connection a pool keeps and
which it reuses first. Both are held back so neither can finish before the other's connection is
open: a request that finds its sibling's connection already free takes that one instead.

The async client also closes an expired connection when no request comes to find it (a background
reaper). The sync client only promises to close it the next time it is used, so that test is
async only.

Whether a request waiting for a new connection takes one freed in the meantime is shown through an
HTTPS proxy that holds every `CONNECT` but the first (`hold_later_tunnels`): the second request's
own connection stays mid-handshake until the first request has finished, for as long as the test
says. Through a tunnel, `/peer` names the proxy's connection to the server, one per tunnel.
"""

import asyncio
import threading
import time
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor, wait

import pytest
from _proxy_server import HTTPProxy, serve_http_proxy

from lothc import HTTPClient, SyncHTTPClient


async def _port(client: HTTPClient, seconds: float = 0) -> int:
    response = await client.get("peer", params={"seconds": seconds}, into=dict)
    return int(response.data["port"])


def _sync_port(client: SyncHTTPClient, seconds: float = 0) -> int:
    response = client.get("peer", params={"seconds": seconds}, into=dict)
    return int(response.data["port"])


async def _closed_ports(observer: HTTPClient, key: str) -> list[int]:
    """The `/peer?key=` connections the server has seen closed, waiting up to 2s for one."""
    deadline = time.monotonic() + 2.0
    while True:
        response = await observer.get("peer-closed", params={"key": key}, into=dict)
        ports = [int(port) for port in response.data["ports"]]
        if ports or time.monotonic() > deadline:
            return ports
        await asyncio.sleep(0.01)


async def _tunnels_opened(proxy: HTTPProxy, count: int) -> None:
    """Wait up to 2s for `proxy` to have received `count` `CONNECT`s."""
    deadline = time.monotonic() + 2.0
    while len(proxy.requests) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.005)


def _sync_tunnels_opened(proxy: HTTPProxy, count: int) -> None:
    deadline = time.monotonic() + 2.0
    while len(proxy.requests) < count and time.monotonic() < deadline:
        time.sleep(0.005)


def _sync_closed_ports(observer: SyncHTTPClient, key: str) -> list[int]:
    deadline = time.monotonic() + 2.0
    while True:
        response = observer.get("peer-closed", params={"key": key}, into=dict)
        ports = [int(port) for port in response.data["ports"]]
        if ports or time.monotonic() > deadline:
            return ports
        time.sleep(0.01)


@pytest.fixture(name="idle_timeout_client")
async def _idle_timeout_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, pool_idle_timeout=0.05) as client:
        yield client


@pytest.fixture(name="sync_idle_timeout_client")
def _sync_idle_timeout_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, pool_idle_timeout=0.05) as client:
        yield client


async def test_a_connection_is_reused_within_pool_idle_timeout_and_replaced_after_it(
    idle_timeout_client: HTTPClient,
) -> None:
    first = await _port(idle_timeout_client)
    reused = await _port(idle_timeout_client)
    await asyncio.sleep(0.07)
    replaced = await _port(idle_timeout_client)

    assert reused == first
    assert replaced != first


def test_sync_a_connection_is_reused_within_pool_idle_timeout_and_replaced_after_it(
    sync_idle_timeout_client: SyncHTTPClient,
) -> None:
    first = _sync_port(sync_idle_timeout_client)
    reused = _sync_port(sync_idle_timeout_client)
    time.sleep(0.07)
    replaced = _sync_port(sync_idle_timeout_client)

    assert reused == first
    assert replaced != first


async def test_an_idle_connection_is_closed_after_pool_idle_timeout_with_no_request_to_find_it(
    base_url: str, server_key: str, idle_timeout_client: HTTPClient
) -> None:
    async with HTTPClient(base_url=base_url) as observer:
        response = await idle_timeout_client.get("peer", params={"key": server_key}, into=dict)
        closed = await _closed_ports(observer, server_key)

    assert closed == [int(response.data["port"])]


async def test_an_expired_idle_connection_is_closed_once_the_client_is_used_again(
    base_url: str, server_key: str
) -> None:
    # Used again for another origin, so no checkout comes across the expired connection.
    other_origin = base_url.replace("127.0.0.1", "localhost")
    async with (
        HTTPClient(pool_idle_timeout=0.05) as client,
        HTTPClient(base_url=base_url) as observer,
    ):
        response = await client.get(f"{base_url}peer", params={"key": server_key}, into=dict)
        await asyncio.sleep(0.07)
        await client.get(f"{other_origin}items/7")
        closed = await _closed_ports(observer, server_key)

    assert closed == [int(response.data["port"])]


def test_sync_an_expired_idle_connection_is_closed_once_the_client_is_used_again(
    base_url: str, server_key: str
) -> None:
    other_origin = base_url.replace("127.0.0.1", "localhost")
    with (
        SyncHTTPClient(pool_idle_timeout=0.05) as client,
        SyncHTTPClient(base_url=base_url) as observer,
    ):
        response = client.get(f"{base_url}peer", params={"key": server_key}, into=dict)
        time.sleep(0.07)
        client.get(f"{other_origin}items/7")
        closed = _sync_closed_ports(observer, server_key)

    assert closed == [int(response.data["port"])]


async def test_pool_idle_timeout_zero_never_reuses_a_connection(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, pool_idle_timeout=0) as client:
        ports = [await _port(client) for _ in range(3)]

    assert len(set(ports)) == 3


def test_sync_pool_idle_timeout_zero_never_reuses_a_connection(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, pool_idle_timeout=0) as client:
        ports = [_sync_port(client) for _ in range(3)]

    assert len(set(ports)) == 3


async def test_pool_max_idle_per_host_zero_never_reuses_a_connection(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, pool_max_idle_per_host=0) as client:
        ports = [await _port(client) for _ in range(3)]

    assert len(set(ports)) == 3


def test_sync_pool_max_idle_per_host_zero_never_reuses_a_connection(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, pool_max_idle_per_host=0) as client:
        ports = [_sync_port(client) for _ in range(3)]

    assert len(set(ports)) == 3


async def test_pool_max_idle_per_host_keeps_the_connection_that_went_idle_first(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, pool_max_idle_per_host=1) as client:
        early, late = await asyncio.gather(_port(client, seconds=0.02), _port(client, seconds=0.06))
        reused = await _port(client)

    assert early != late
    assert reused == early


def test_sync_pool_max_idle_per_host_keeps_the_connection_that_went_idle_first(
    base_url: str,
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, pool_max_idle_per_host=1) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        early_future = pool.submit(_sync_port, client, 0.02)
        late_future = pool.submit(_sync_port, client, 0.06)
        early, late = early_future.result(), late_future.result()
        reused = _sync_port(client)

    assert early != late
    assert reused == early


async def test_the_most_recently_idle_connection_is_reused_first(client: HTTPClient) -> None:
    early, late = await asyncio.gather(_port(client, seconds=0.02), _port(client, seconds=0.06))
    reused = await _port(client)

    assert early != late
    assert reused == late


def test_sync_the_most_recently_idle_connection_is_reused_first(
    sync_client: SyncHTTPClient,
) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        early_future = pool.submit(_sync_port, sync_client, 0.02)
        late_future = pool.submit(_sync_port, sync_client, 0.06)
        early, late = early_future.result(), late_future.result()
    reused = _sync_port(sync_client)

    assert early != late
    assert reused == late


async def test_pool_max_idle_per_host_zero_never_hands_a_freed_connection_to_a_waiting_request(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    release = threading.Event()
    with serve_http_proxy(hold_later_tunnels=release) as proxy:
        async with HTTPClient(
            base_url=https_base_url,
            proxy=proxy.url,
            root_certificates=[tls_ca_pem],
            pool_max_idle_per_host=0,
        ) as client:
            first = asyncio.create_task(_port(client, seconds=0.1))
            await _tunnels_opened(proxy, 1)
            # Its connection is held mid-`CONNECT` while the first request finishes.
            second = asyncio.create_task(_port(client))
            await _tunnels_opened(proxy, 2)
            first_port = await first
            await asyncio.wait([second], timeout=0.1)
            release.set()
            second_port = await second

    assert second_port != first_port


def test_sync_pool_max_idle_per_host_zero_never_hands_a_freed_connection_to_a_waiting_request(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    release = threading.Event()
    with (
        serve_http_proxy(hold_later_tunnels=release) as proxy,
        SyncHTTPClient(
            base_url=https_base_url,
            proxy=proxy.url,
            root_certificates=[tls_ca_pem],
            pool_max_idle_per_host=0,
        ) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(_sync_port, client, 0.1)
        _sync_tunnels_opened(proxy, 1)
        second = pool.submit(_sync_port, client)
        _sync_tunnels_opened(proxy, 2)
        first_port = first.result()
        wait([second], timeout=0.1)
        release.set()
        second_port = second.result()

    assert second_port != first_port


async def test_a_request_waiting_for_a_connection_takes_one_freed_meanwhile(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    # The control for the test above: the same steps, pooling on.
    release = threading.Event()
    with serve_http_proxy(hold_later_tunnels=release) as proxy:
        async with HTTPClient(
            base_url=https_base_url, proxy=proxy.url, root_certificates=[tls_ca_pem]
        ) as client:
            first = asyncio.create_task(_port(client, seconds=0.1))
            await _tunnels_opened(proxy, 1)
            second = asyncio.create_task(_port(client))
            await _tunnels_opened(proxy, 2)
            first_port = await first
            await asyncio.wait([second], timeout=0.1)
            release.set()
            second_port = await second

    assert second_port == first_port
