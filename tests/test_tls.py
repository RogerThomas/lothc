"""lothc's TLS settings against a real HTTPS server (`_tls_server.py`, certs minted by `trustme`).

Every setting gets a success case *and* a negative control that fails without it, so a setting
lothc silently stopped passing to pyreqwest would fail here rather than pass unnoticed (which a
plain-HTTP request, never doing a handshake, cannot catch). Each negative control matches the
TLS alert/verification failure pyreqwest (rustls) reports, so it can't pass for an unrelated
reason like a refused connection.
"""

import ssl
from collections.abc import Generator

import pytest
import trustme
from _tls_server import serve_tls, tls_server_context

from lothc import HTTPClient, HTTPConnectionError, SyncHTTPClient, TlsVersion


@pytest.fixture(scope="module", name="tls12_only_base_url")
def _tls12_only_base_url(tls_ca: trustme.CA) -> Generator[str]:
    context = tls_server_context(tls_ca)
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    with serve_tls(context) as url:
        yield url


@pytest.fixture(scope="module", name="tls13_only_base_url")
def _tls13_only_base_url(tls_ca: trustme.CA) -> Generator[str]:
    context = tls_server_context(tls_ca)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    with serve_tls(context) as url:
        yield url


async def test_root_certificates_trusts_a_custom_ca(https_base_url: str, tls_ca_pem: bytes) -> None:
    async with HTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem]) as client:
        result = await client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_root_certificates_trusts_a_custom_ca(https_base_url: str, tls_ca_pem: bytes) -> None:
    with SyncHTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem]) as client:
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_untrusted_server_cert_raises_connection_error(https_base_url: str) -> None:
    async with HTTPClient(base_url=https_base_url) as client:
        with pytest.raises(HTTPConnectionError, match="UnknownIssuer"):
            await client.get("items/7")


def test_sync_untrusted_server_cert_raises_connection_error(https_base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=https_base_url) as client,
        pytest.raises(HTTPConnectionError, match="UnknownIssuer"),
    ):
        client.get("items/7")


async def test_root_certificates_from_another_ca_is_still_untrusted(https_base_url: str) -> None:
    # The CA is actually consulted, not just "some root was configured".
    other_ca_pem = trustme.CA().cert_pem.bytes()
    async with HTTPClient(base_url=https_base_url, root_certificates=[other_ca_pem]) as client:
        with pytest.raises(HTTPConnectionError, match="UnknownIssuer"):
            await client.get("items/7")


async def test_danger_accept_invalid_certs_skips_verification(https_base_url: str) -> None:
    async with HTTPClient(base_url=https_base_url, danger_accept_invalid_certs=True) as client:
        result = await client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_danger_accept_invalid_certs_skips_verification(https_base_url: str) -> None:
    with SyncHTTPClient(base_url=https_base_url, danger_accept_invalid_certs=True) as client:
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_identity_pem_satisfies_a_server_requiring_a_client_cert(
    mtls_base_url: str, tls_ca_pem: bytes, tls_client_identity_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=mtls_base_url,
        root_certificates=[tls_ca_pem],
        identity_pem=tls_client_identity_pem,
    ) as client:
        result = await client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_identity_pem_satisfies_a_server_requiring_a_client_cert(
    mtls_base_url: str, tls_ca_pem: bytes, tls_client_identity_pem: bytes
) -> None:
    with SyncHTTPClient(
        base_url=mtls_base_url,
        root_certificates=[tls_ca_pem],
        identity_pem=tls_client_identity_pem,
    ) as client:
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_missing_client_cert_raises_connection_error(
    mtls_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(base_url=mtls_base_url, root_certificates=[tls_ca_pem]) as client:
        with pytest.raises(HTTPConnectionError, match="CertificateRequired"):
            await client.get("items/7")


def test_sync_missing_client_cert_raises_connection_error(
    mtls_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        SyncHTTPClient(base_url=mtls_base_url, root_certificates=[tls_ca_pem]) as client,
        pytest.raises(HTTPConnectionError, match="CertificateRequired"),
    ):
        client.get("items/7")


async def test_client_cert_from_an_untrusted_ca_raises_connection_error(
    mtls_base_url: str, tls_ca_pem: bytes
) -> None:
    issued = trustme.CA().issue_cert("client@lothc.test")
    identity_pem = issued.cert_chain_pems[0].bytes() + issued.private_key_pem.bytes()
    async with HTTPClient(
        base_url=mtls_base_url, root_certificates=[tls_ca_pem], identity_pem=identity_pem
    ) as client:
        with pytest.raises(HTTPConnectionError, match="UnknownCA"):
            await client.get("items/7")


async def test_min_tls_version_above_the_server_max_raises_connection_error(
    tls12_only_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=tls12_only_base_url, root_certificates=[tls_ca_pem], min_tls_version="TLSv1.3"
    ) as client:
        with pytest.raises(HTTPConnectionError, match="ProtocolVersion"):
            await client.get("items/7")


def test_sync_min_tls_version_above_the_server_max_raises_connection_error(
    tls12_only_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        SyncHTTPClient(
            base_url=tls12_only_base_url,
            root_certificates=[tls_ca_pem],
            min_tls_version="TLSv1.3",
        ) as client,
        pytest.raises(HTTPConnectionError, match="ProtocolVersion"),
    ):
        client.get("items/7")


async def test_max_tls_version_below_the_server_min_raises_connection_error(
    tls13_only_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=tls13_only_base_url, root_certificates=[tls_ca_pem], max_tls_version="TLSv1.2"
    ) as client:
        with pytest.raises(HTTPConnectionError, match="ProtocolVersion"):
            await client.get("items/7")


def test_sync_max_tls_version_below_the_server_min_raises_connection_error(
    tls13_only_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        SyncHTTPClient(
            base_url=tls13_only_base_url,
            root_certificates=[tls_ca_pem],
            max_tls_version="TLSv1.2",
        ) as client,
        pytest.raises(HTTPConnectionError, match="ProtocolVersion"),
    ):
        client.get("items/7")


@pytest.mark.parametrize(
    ("fixture_name", "min_tls_version", "max_tls_version"),
    [
        ("tls12_only_base_url", "TLSv1.2", "TLSv1.2"),
        ("tls12_only_base_url", "TLSv1.2", "TLSv1.3"),
        ("tls13_only_base_url", "TLSv1.3", "TLSv1.3"),
        ("tls13_only_base_url", "TLSv1.2", "TLSv1.3"),
    ],
)
async def test_tls_version_range_overlapping_the_server_succeeds(
    request: pytest.FixtureRequest,
    tls_ca_pem: bytes,
    fixture_name: str,
    min_tls_version: TlsVersion,
    max_tls_version: TlsVersion,
) -> None:
    base_url: str = request.getfixturevalue(fixture_name)
    async with HTTPClient(
        base_url=base_url,
        root_certificates=[tls_ca_pem],
        min_tls_version=min_tls_version,
        max_tls_version=max_tls_version,
    ) as client:
        result = await client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_https_only_allows_an_https_request(https_base_url: str, tls_ca_pem: bytes) -> None:
    async with HTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], https_only=True
    ) as client:
        result = await client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_https_only_allows_an_https_request(https_base_url: str, tls_ca_pem: bytes) -> None:
    with SyncHTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], https_only=True
    ) as client:
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_http2_over_tls_falls_back_to_http1_via_alpn(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    # The stdlib server only speaks HTTP/1.1, so this proves `http2=True` negotiates rather than
    # forcing h2 (prior-knowledge h2 would fail here), not that h2 was used anywhere.
    async with HTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], http2=True
    ) as client:
        result = await client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_http2_over_tls_falls_back_to_http1_via_alpn(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], http2=True
    ) as client:
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}
