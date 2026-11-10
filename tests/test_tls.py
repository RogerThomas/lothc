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
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

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


def _unusable_identity_pem(case: str, tls_ca: trustme.CA) -> bytes:
    """An `identity_pem` that can't be used, by `case`: no PEM at all, a cert without a key, a key
    without a cert, a cert with someone else's key, or a usable pair plus a PEM section that is
    neither (a public key)."""
    issued = tls_ca.issue_cert("client@lothc.test")
    cert = issued.cert_chain_pems[0].bytes()
    key = issued.private_key_pem.bytes()
    match case:
        case "no-pem":
            return b"identity-pem"
        case "cert-only":
            return cert
        case "key-only":
            return key
        case "other-key":
            return cert + trustme.CA().issue_cert("client@lothc.test").private_key_pem.bytes()
        case "public-key-section":
            public_key = load_pem_private_key(key, password=None).public_key()
            return (
                cert
                + key
                + public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            )
        case _:
            raise ValueError(case)


@pytest.fixture(scope="module", name="other_name_base_url")
def _other_name_base_url(tls_ca: trustme.CA) -> Generator[str]:
    """A trusted CA's cert, but for another host name than the `localhost` the URL connects to."""
    with serve_tls(tls_server_context(tls_ca, ("other-name.test",))) as url:
        yield url


async def test_root_certificates_trusts_a_custom_ca(https_base_url: str, tls_ca_pem: bytes) -> None:
    async with HTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem]) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_root_certificates_trusts_a_custom_ca(https_base_url: str, tls_ca_pem: bytes) -> None:
    with SyncHTTPClient(base_url=https_base_url, root_certificates=[tls_ca_pem]) as client:
        result = client.get("items/7", into=dict).data

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


async def test_cert_for_another_host_name_raises_connection_error(
    other_name_base_url: str, tls_ca_pem: bytes
) -> None:
    # rustls names this `NotValidForName` where the OS verifier reports it bare (macOS) and
    # `certificate not valid for name ...` where it reports the names involved (Linux).
    async with HTTPClient(base_url=other_name_base_url, root_certificates=[tls_ca_pem]) as client:
        with pytest.raises(HTTPConnectionError, match=r"NotValidForName|not valid for name"):
            await client.get("items/7")


def test_sync_cert_for_another_host_name_raises_connection_error(
    other_name_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        SyncHTTPClient(base_url=other_name_base_url, root_certificates=[tls_ca_pem]) as client,
        pytest.raises(HTTPConnectionError, match=r"NotValidForName|not valid for name"),
    ):
        client.get("items/7")


async def test_danger_accept_invalid_certs_skips_verification(https_base_url: str) -> None:
    async with HTTPClient(base_url=https_base_url, danger_accept_invalid_certs=True) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_danger_accept_invalid_certs_skips_verification(https_base_url: str) -> None:
    with SyncHTTPClient(base_url=https_base_url, danger_accept_invalid_certs=True) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_identity_pem_satisfies_a_server_requiring_a_client_cert(
    mtls_base_url: str, tls_ca_pem: bytes, tls_client_identity_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=mtls_base_url,
        root_certificates=[tls_ca_pem],
        identity_pem=tls_client_identity_pem,
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_identity_pem_satisfies_a_server_requiring_a_client_cert(
    mtls_base_url: str, tls_ca_pem: bytes, tls_client_identity_pem: bytes
) -> None:
    with SyncHTTPClient(
        base_url=mtls_base_url,
        root_certificates=[tls_ca_pem],
        identity_pem=tls_client_identity_pem,
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


def _identity_pem(case: str, tls_ca: trustme.CA) -> bytes:
    """A usable `identity_pem` in another shape than `tls_client_identity_pem` (a cert, then
    trustme's SEC1 `EC PRIVATE KEY`), by `case`: a PKCS#8 key, a PKCS#1 (RSA) key, the key before
    the cert, two keys of which the last is the cert's, or a cert from an intermediate CA followed
    by that CA's cert, which the server, trusting only `tls_ca`, needs to verify it."""
    issued = tls_ca.issue_cert("client@lothc.test")
    cert = issued.cert_chain_pems[0].bytes()
    key = issued.private_key_pem.bytes()
    match case:
        case "pkcs8-key":
            pkcs8 = load_pem_private_key(key, password=None).private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
            )
            return cert + pkcs8
        case "pkcs1-key":
            rsa = tls_ca.issue_cert("client@lothc.test", key_type=trustme.KeyType.RSA)
            return rsa.cert_chain_pems[0].bytes() + rsa.private_key_pem.bytes()
        case "key-first":
            return key + cert
        case "last-key-wins":
            other_key = tls_ca.issue_cert("client@lothc.test").private_key_pem.bytes()
            return cert + other_key + key
        case "intermediate-ca":
            chained = tls_ca.create_child_ca().issue_cert("client@lothc.test")
            leaf, intermediate = (pem.bytes() for pem in chained.cert_chain_pems)
            return leaf + intermediate + chained.private_key_pem.bytes()
        case _:
            raise ValueError(case)


def _identity_cases() -> list[str]:
    return ["pkcs8-key", "pkcs1-key", "key-first", "last-key-wins", "intermediate-ca"]


@pytest.mark.parametrize("case", _identity_cases())
async def test_identity_pem_in_any_shape_reqwest_reads_satisfies_the_server(
    mtls_base_url: str, tls_ca: trustme.CA, tls_ca_pem: bytes, case: str
) -> None:
    async with HTTPClient(
        base_url=mtls_base_url,
        root_certificates=[tls_ca_pem],
        identity_pem=_identity_pem(case, tls_ca),
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("case", _identity_cases())
def test_sync_identity_pem_in_any_shape_reqwest_reads_satisfies_the_server(
    mtls_base_url: str, tls_ca: trustme.CA, tls_ca_pem: bytes, case: str
) -> None:
    with SyncHTTPClient(
        base_url=mtls_base_url,
        root_certificates=[tls_ca_pem],
        identity_pem=_identity_pem(case, tls_ca),
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_a_client_cert_without_its_intermediate_ca_is_refused(
    mtls_base_url: str, tls_ca: trustme.CA, tls_ca_pem: bytes
) -> None:
    # The control for "intermediate-ca" above: the same cert, sent alone.
    chained = tls_ca.create_child_ca().issue_cert("client@lothc.test")
    identity_pem = chained.cert_chain_pems[0].bytes() + chained.private_key_pem.bytes()
    async with HTTPClient(
        base_url=mtls_base_url, root_certificates=[tls_ca_pem], identity_pem=identity_pem
    ) as client:
        with pytest.raises(HTTPConnectionError, match="UnknownCA"):
            await client.get("items/7")


def test_sync_a_client_cert_without_its_intermediate_ca_is_refused(
    mtls_base_url: str, tls_ca: trustme.CA, tls_ca_pem: bytes
) -> None:
    chained = tls_ca.create_child_ca().issue_cert("client@lothc.test")
    identity_pem = chained.cert_chain_pems[0].bytes() + chained.private_key_pem.bytes()
    with (
        SyncHTTPClient(
            base_url=mtls_base_url, root_certificates=[tls_ca_pem], identity_pem=identity_pem
        ) as client,
        pytest.raises(HTTPConnectionError, match="UnknownCA"),
    ):
        client.get("items/7")


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


def test_sync_client_cert_from_an_untrusted_ca_raises_connection_error(
    mtls_base_url: str, tls_ca_pem: bytes
) -> None:
    issued = trustme.CA().issue_cert("client@lothc.test")
    identity_pem = issued.cert_chain_pems[0].bytes() + issued.private_key_pem.bytes()
    with (
        SyncHTTPClient(
            base_url=mtls_base_url, root_certificates=[tls_ca_pem], identity_pem=identity_pem
        ) as client,
        pytest.raises(HTTPConnectionError, match="UnknownCA"),
    ):
        client.get("items/7")


async def test_identity_pem_works_with_danger_accept_invalid_certs(
    mtls_base_url: str, tls_client_identity_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=mtls_base_url,
        identity_pem=tls_client_identity_pem,
        danger_accept_invalid_certs=True,
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_identity_pem_works_with_danger_accept_invalid_certs(
    mtls_base_url: str, tls_client_identity_pem: bytes
) -> None:
    with SyncHTTPClient(
        base_url=mtls_base_url,
        identity_pem=tls_client_identity_pem,
        danger_accept_invalid_certs=True,
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


# Checked when the client is opened, not on the first HTTPS request, so a plain-HTTP client fails
# too. Any message will do (it is pyreqwest's), but the type is a plain `ValueError`, never
# pyreqwest's own `BuilderError`.
@pytest.mark.parametrize(
    "case", ["no-pem", "cert-only", "key-only", "other-key", "public-key-section"]
)
async def test_unusable_identity_pem_raises_value_error_on_entry(
    base_url: str, tls_ca: trustme.CA, case: str
) -> None:
    client = HTTPClient(base_url=base_url, identity_pem=_unusable_identity_pem(case, tls_ca))

    with pytest.raises(ValueError) as exc_info:  # noqa: PT011
        async with client:
            pass

    assert type(exc_info.value) is ValueError


@pytest.mark.parametrize(
    "case", ["no-pem", "cert-only", "key-only", "other-key", "public-key-section"]
)
def test_sync_unusable_identity_pem_raises_value_error_on_entry(
    base_url: str, tls_ca: trustme.CA, case: str
) -> None:
    client = SyncHTTPClient(base_url=base_url, identity_pem=_unusable_identity_pem(case, tls_ca))

    with pytest.raises(ValueError) as exc_info, client:  # noqa: PT011
        pass

    assert type(exc_info.value) is ValueError


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
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize(
    ("fixture_name", "min_tls_version", "max_tls_version"),
    [
        ("tls12_only_base_url", "TLSv1.2", "TLSv1.2"),
        ("tls12_only_base_url", "TLSv1.2", "TLSv1.3"),
        ("tls13_only_base_url", "TLSv1.3", "TLSv1.3"),
        ("tls13_only_base_url", "TLSv1.2", "TLSv1.3"),
    ],
)
def test_sync_tls_version_range_overlapping_the_server_succeeds(
    request: pytest.FixtureRequest,
    tls_ca_pem: bytes,
    fixture_name: str,
    min_tls_version: TlsVersion,
    max_tls_version: TlsVersion,
) -> None:
    base_url: str = request.getfixturevalue(fixture_name)
    with SyncHTTPClient(
        base_url=base_url,
        root_certificates=[tls_ca_pem],
        min_tls_version=min_tls_version,
        max_tls_version=max_tls_version,
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


# rustls has no TLS 1.0 or 1.1, so as a minimum they allow everything it has (1.2 and 1.3).
@pytest.mark.parametrize("fixture_name", ["tls12_only_base_url", "tls13_only_base_url"])
@pytest.mark.parametrize("min_tls_version", ["TLSv1.0", "TLSv1.1"])
async def test_min_tls_version_below_what_rustls_supports_allows_every_version(
    request: pytest.FixtureRequest,
    tls_ca_pem: bytes,
    fixture_name: str,
    min_tls_version: TlsVersion,
) -> None:
    base_url: str = request.getfixturevalue(fixture_name)
    async with HTTPClient(
        base_url=base_url, root_certificates=[tls_ca_pem], min_tls_version=min_tls_version
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("fixture_name", ["tls12_only_base_url", "tls13_only_base_url"])
@pytest.mark.parametrize("min_tls_version", ["TLSv1.0", "TLSv1.1"])
def test_sync_min_tls_version_below_what_rustls_supports_allows_every_version(
    request: pytest.FixtureRequest,
    tls_ca_pem: bytes,
    fixture_name: str,
    min_tls_version: TlsVersion,
) -> None:
    base_url: str = request.getfixturevalue(fixture_name)
    with SyncHTTPClient(
        base_url=base_url, root_certificates=[tls_ca_pem], min_tls_version=min_tls_version
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


# A range leaving rustls no version (a TLS 1.0/1.1 maximum, or a minimum above the maximum) fails
# before any request, even on a plain-HTTP client, when it is opened: a plain `ValueError` with
# pyreqwest's message, never pyreqwest's own `BuilderError`.
@pytest.mark.parametrize(
    ("min_tls_version", "max_tls_version"),
    [(None, "TLSv1.0"), (None, "TLSv1.1"), ("TLSv1.0", "TLSv1.1"), ("TLSv1.3", "TLSv1.2")],
)
async def test_tls_versions_rustls_cannot_satisfy_raise_value_error(
    base_url: str, min_tls_version: TlsVersion | None, max_tls_version: TlsVersion
) -> None:
    with pytest.raises(ValueError, match=r"^builder error: empty supported tls versions$") as exc:
        async with HTTPClient(
            base_url=base_url, min_tls_version=min_tls_version, max_tls_version=max_tls_version
        ):
            pass

    assert type(exc.value) is ValueError


@pytest.mark.parametrize(
    ("min_tls_version", "max_tls_version"),
    [(None, "TLSv1.0"), (None, "TLSv1.1"), ("TLSv1.0", "TLSv1.1"), ("TLSv1.3", "TLSv1.2")],
)
def test_sync_tls_versions_rustls_cannot_satisfy_raise_value_error(
    base_url: str, min_tls_version: TlsVersion | None, max_tls_version: TlsVersion
) -> None:
    with (
        pytest.raises(ValueError, match=r"^builder error: empty supported tls versions$") as exc,
        SyncHTTPClient(
            base_url=base_url, min_tls_version=min_tls_version, max_tls_version=max_tls_version
        ),
    ):
        pass

    assert type(exc.value) is ValueError


async def test_https_only_allows_an_https_request(https_base_url: str, tls_ca_pem: bytes) -> None:
    async with HTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], https_only=True
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_https_only_allows_an_https_request(https_base_url: str, tls_ca_pem: bytes) -> None:
    with SyncHTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], https_only=True
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_http2_over_tls_falls_back_to_http1_via_alpn(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    # The stdlib server only speaks HTTP/1.1, so this proves `http2=True` negotiates rather than
    # forcing h2 (prior-knowledge h2 would fail here), not that h2 was used anywhere.
    async with HTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], http2=True
    ) as client:
        result = (await client.get("items/7", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_http2_over_tls_falls_back_to_http1_via_alpn(
    https_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], http2=True
    ) as client:
        result = client.get("items/7", into=dict).data

    assert result == {"id": 7, "name": "item-7"}
