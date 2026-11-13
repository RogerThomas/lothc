"""Connection-pool settings against the real local server.

`max_connections`/`pool_timeout` get a real behavioural test: with one connection slot and a
short `pool_timeout`, a second concurrent request times out waiting for the slot (without
`max_connections` it never waits; without `pool_timeout` it waits and succeeds, so the test
fails if either setting stops reaching pyreqwest). `connect_timeout` only gets a "still builds and
still makes a normal request" check: it has no observable effect against a local server that
accepts connections instantly. `pool_idle_timeout`/`pool_max_idle_per_host` are tested by which
connection serves each request, in `tests/test_idle_connections.py`. The TLS settings are tested
against a real HTTPS server in `tests/test_tls.py`.
"""

import asyncio
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from _helpers import release, sync_release

from lothc import HTTPClient, HTTPTimeoutError, Response, SyncHTTPClient


async def test_connect_timeout_and_pool_settings_still_allow_a_normal_request(
    base_url: str,
) -> None:
    async with HTTPClient(
        base_url=base_url,
        connect_timeout=5.0,
        max_connections=10,
        pool_idle_timeout=30.0,
        pool_max_idle_per_host=5,
        pool_timeout=2.0,
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_connect_timeout_and_pool_settings_still_allow_a_normal_request(
    base_url: str,
) -> None:
    with SyncHTTPClient(
        base_url=base_url,
        connect_timeout=5.0,
        max_connections=10,
        pool_idle_timeout=30.0,
        pool_max_idle_per_host=5,
        pool_timeout=2.0,
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_request_past_pool_timeout_waiting_for_a_connection_slot_raises_timeout_error(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.01) as client:
        # Whichever takes the slot holds it until the other has given up waiting for it.
        requests = [
            asyncio.create_task(client.get("slow", params={"seconds": 5.0, "key": server_key}))
            for _ in range(2)
        ]
        await asyncio.wait(requests, return_when=asyncio.FIRST_COMPLETED)
        await release(base_url, server_key)
        outcomes = await asyncio.gather(*requests, return_exceptions=True)

    assert [isinstance(outcome, HTTPTimeoutError) for outcome in outcomes].count(True) == 1
    bodies = [outcome.data for outcome in outcomes if not isinstance(outcome, BaseException)]
    assert bodies == [b'{"finally": true}']


def _sync_hold_or_wait(client: SyncHTTPClient, key: str) -> Response[bytes]:
    return client.get("slow", params={"seconds": 5.0, "key": key})


def test_sync_request_past_pool_timeout_waiting_for_a_connection_slot_raises_timeout_error(
    base_url: str, server_key: str
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.01) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(_sync_hold_or_wait, client, server_key) for _ in range(2)]
        wait(futures, return_when=FIRST_COMPLETED)
        sync_release(base_url, server_key)
        outcomes = [future.exception() or future.result() for future in futures]

    assert [isinstance(outcome, HTTPTimeoutError) for outcome in outcomes].count(True) == 1
    bodies = [outcome.data for outcome in outcomes if not isinstance(outcome, BaseException)]
    assert bodies == [b'{"finally": true}']
