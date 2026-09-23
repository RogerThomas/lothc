"""Connection-pool settings against the real local server.

`max_connections`/`pool_timeout` get a real behavioural test: with one connection slot and a
short `pool_timeout`, a second concurrent request times out waiting for the slot (without
`max_connections` it never waits; without `pool_timeout` it waits and succeeds, so the test
fails if either setting stops reaching pyreqwest). `connect_timeout`/`pool_idle_timeout`/
`pool_max_idle_per_host` only get a "still builds and still makes a normal request" check: none
has an observable effect against a local server that accepts connections instantly. The TLS
settings are tested against a real HTTPS server in `tests/test_tls.py`.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

from lothc import HTTPClient, HTTPTimeoutError, SyncHTTPClient


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
        result = await client.get("items/7", response_data_type=dict)

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
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_request_past_pool_timeout_waiting_for_a_connection_slot_raises_timeout_error(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.01) as client:
        outcomes = await asyncio.gather(
            client.get("slow", params={"seconds": 0.05}),
            client.get("slow", params={"seconds": 0.05}),
            return_exceptions=True,
        )

    assert [isinstance(outcome, HTTPTimeoutError) for outcome in outcomes].count(True) == 1
    assert b'{"finally": true}' in outcomes


def test_sync_request_past_pool_timeout_waiting_for_a_connection_slot_raises_timeout_error(
    base_url: str,
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.01) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        futures = [pool.submit(client.get, "slow", params={"seconds": 0.05}) for _ in range(2)]
        outcomes = [future.exception() or future.result() for future in futures]

    assert [isinstance(outcome, HTTPTimeoutError) for outcome in outcomes].count(True) == 1
    assert b'{"finally": true}' in outcomes
