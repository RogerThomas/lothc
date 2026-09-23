import ssl
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor

import pytest
import trustme
from _server import make_server
from _tls_server import serve_tls, tls_server_context

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
    async with HTTPClient(base_url=base_url) as client:
        yield client


@pytest.fixture(name="sync_client")
def _sync_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url) as client:
        yield client


@pytest.fixture(scope="session", name="tls_ca")
def _tls_ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture(scope="session", name="tls_ca_pem")
def _tls_ca_pem(tls_ca: trustme.CA) -> bytes:
    return tls_ca.cert_pem.bytes()


@pytest.fixture(scope="session", name="tls_client_identity_pem")
def _tls_client_identity_pem(tls_ca: trustme.CA) -> bytes:
    """A client cert issued by `tls_ca` followed by its private key: one PEM bundle, the shape
    `identity_pem` documents."""
    issued = tls_ca.issue_cert("client@lothc.test")
    return issued.cert_chain_pems[0].bytes() + issued.private_key_pem.bytes()


@pytest.fixture(scope="session", name="https_base_url")
def _https_base_url(tls_ca: trustme.CA) -> Generator[str]:
    with serve_tls(tls_server_context(tls_ca)) as url:
        yield url


@pytest.fixture(scope="session", name="mtls_base_url")
def _mtls_base_url(tls_ca: trustme.CA) -> Generator[str]:
    """An HTTPS server that refuses any client not presenting a cert issued by `tls_ca`."""
    context = tls_server_context(tls_ca)
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=tls_ca.cert_pem.bytes().decode())
    with serve_tls(context) as url:
        yield url
