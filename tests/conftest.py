from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from _server import make_server

from lothc import HTTPClient, SyncHTTPClient


@pytest.fixture(scope="session", name="base_url")
def _base_url() -> Generator[str]:
    with make_server() as server, ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(server.serve_forever)
        try:
            yield f"http://127.0.0.1:{server.server_port}/"
        finally:
            server.shutdown()


@pytest.fixture(name="client")
async def _client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient.build(base_url=base_url) as client:
        yield client


@pytest.fixture(name="sync_client")
def _sync_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient.build(base_url=base_url) as client:
        yield client
