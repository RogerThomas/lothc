"""A caller cancelling a request (its own timeout, or cancelling the task) gets control back
promptly, and the client stays usable afterwards."""

import asyncio
import time

import pytest

from lothc import HTTPClient


async def test_request_cancelled_by_the_callers_timeout_returns_promptly(
    client: HTTPClient,
) -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.get("slow", params={"seconds": 0.3}), 0.05)

    assert time.monotonic() - started < 0.25


async def test_cancelling_the_task_raises_cancelled_error(client: HTTPClient) -> None:
    request = asyncio.create_task(client.get("slow", params={"seconds": 0.3}))
    await asyncio.sleep(0.05)
    request.cancel()

    with pytest.raises(asyncio.CancelledError):
        await request


async def test_client_still_works_after_a_cancelled_request(client: HTTPClient) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.get("slow", params={"seconds": 0.3}), 0.05)

    assert (await client.get("items/7", into=dict)).data == {"id": 7, "name": "item-7"}
