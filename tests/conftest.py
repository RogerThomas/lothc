from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
from _server import make_server

from lothc import HTTPClient, SyncHTTPClient


@pytest.fixture(scope="session", name="base_url")
def _base_url() -> Generator[str]:
    with make_server() as server, ThreadPoolExecutor(max_workers=1) as pool:
        # `poll_interval=0.01`, not the 0.5s default: `shutdown()` blocks until
        # `serve_forever`'s loop next wakes up, so the default makes every teardown pay up
        # to half a second of pure waiting (measured).
        pool.submit(server.serve_forever, 0.01)
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
