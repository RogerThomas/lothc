import ssl
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from uuid import uuid4

import pytest
import trustme
from _oauth_server import TokenServer
from _server import make_server, serve_in_background
from _tls_server import serve_tls, tls_server_context

from lothc import HTTPClient, SyncHTTPClient


@pytest.fixture(scope="session", name="base_url")
def _base_url() -> Generator[str]:
    with serve_in_background(make_server()) as server:
        yield f"http://127.0.0.1:{server.server_port}/"


@pytest.fixture(name="server_key")
def _server_key() -> str:
    """A fresh key for the test server's per-key state (hit counters, OAuth request logs).

    The server lives for the whole session, so a hand-picked key collides silently when a test
    is copy-pasted, and its state leaks into a re-run of the same test within the session.
    """
    return str(uuid4())


@pytest.fixture(name="client")
async def _client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url) as client:
        yield client


@pytest.fixture(name="sync_client")
def _sync_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url) as client:
        yield client


@pytest.fixture(name="token_server")
def _token_server(
    base_url: str, server_key: str, sync_client: SyncHTTPClient, tmp_path: Path
) -> TokenServer:
    cache_dir = tmp_path / "token-cache"
    cache_dir.mkdir()
    return TokenServer(base_url, server_key, sync_client, cache_dir)


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
