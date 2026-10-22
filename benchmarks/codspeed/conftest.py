import asyncio
from collections.abc import AsyncGenerator, Generator

import pytest

from lothc import HTTPClient, SyncHTTPClient

BASE_URL = "http://bench.lothc.test/"


@pytest.fixture(name="sync_client")
def _sync_client() -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=BASE_URL) as client:
        yield client


@pytest.fixture(name="event_loop_runner")
def _event_loop_runner() -> Generator[asyncio.Runner]:
    with asyncio.Runner() as runner:
        yield runner


@pytest.fixture(name="async_client")
def _async_client(event_loop_runner: asyncio.Runner) -> Generator[HTTPClient]:
    async def _open() -> AsyncGenerator[HTTPClient]:
        async with HTTPClient(base_url=BASE_URL) as client:
            yield client

    agen = _open()
    client = event_loop_runner.run(anext(agen))
    yield client
    event_loop_runner.run(agen.aclose())
