"""`root_certificates`/`identity_pem`/`min_tls_version`/`max_tls_version`/
`danger_accept_invalid_certs` only matter once an actual TLS handshake happens — this project has
no TLS-enabled test server, so there's no way to get a real pass/fail signal out of them here.
Those five are covered by a "the client still builds (with real, parseable PEM material — a
throwaway self-signed cert/key pair, since pyreqwest does parse `identity_pem` eagerly) and still
makes a normal plain-HTTP request afterward" check instead — real behavior for
`connect_timeout`/`max_connections`/`pool_idle_timeout`/`pool_max_idle_per_host`/`pool_timeout` is
covered on top of that, against the real local server. `https_only` gets a real behavioral test
too, but lives in `tests/test_transport_errors.py` next to the other `BuilderError`-translation
cases, not here.
"""

import pytest

from lothc import HTTPClient, SyncHTTPClient

# A throwaway self-signed cert/key pair (CN=lothc-test, generated once via `openssl req -x509`),
# never used for a real handshake here — just real enough for pyreqwest's eager PEM parsing to
# accept it.
_TEST_CERT_PEM = b"""-----BEGIN CERTIFICATE-----
MIIDCzCCAfOgAwIBAgIUR/aaw+3FICbMLBrLlysJkOdt5PwwDQYJKoZIhvcNAQEL
BQAwFTETMBEGA1UEAwwKbG90aGMtdGVzdDAeFw0yNjA4MjQxOTM1MDZaFw0zNjA4
MjExOTM1MDZaMBUxEzARBgNVBAMMCmxvdGhjLXRlc3QwggEiMA0GCSqGSIb3DQEB
AQUAA4IBDwAwggEKAoIBAQC9wutuf2kz21Fw8wzsNye2GZ1P3WIO4UVL8353DI4e
ltYVl/pwM86PXlgvBHXG2rHPMvdEJ1MXlzDv7hG4+/EJGEGjcr+wqlmxPPZPeqvE
dD1U8hnVYqcSzHPLPuuwaH9TQtbN+2I64vqohWqpen3IoiAmx5DQpgyFvrbzUh/f
UjuNK9ECB4t889ijKRM5LK+U3ZKv6N8rw4v9D3Q9mHO7qXnYu0+GzDe+IDoKAiY9
QoyyvcUszRurky35athMJNMANw1G1gUcrUj4Z7DRikLlRvHsxV9pbHYLdTKB2+aa
XzLarg9pMDBBTtZRVw4MGYJxWqGTK9YTvc738Xi63avJAgMBAAGjUzBRMB0GA1Ud
DgQWBBRhlJGAJKWJyT7ivsG88TQk9D1DGjAfBgNVHSMEGDAWgBRhlJGAJKWJyT7i
vsG88TQk9D1DGjAPBgNVHRMBAf8EBTADAQH/MA0GCSqGSIb3DQEBCwUAA4IBAQBq
h2JGEWpUurtzPf+KASRTbVY3O3fUh/0/nCi6Jv7Hgs4e2MpfVcwKZ87yB+SKNtoN
HI37h3qh3x7SeDypCx+a8CwYMu1pZDyKq3mL8RB2VRlWx79yvQxEbzvPg/WTlET0
SxVwiua4Mdg2Qyb+83gU02gMLGTpOEPBtylCaJL5LGC9c/LWJILEPVlRhN/MquUe
UziTHLXQ1cROJgnXe8wLXdIYO0JO1F9LRRYtROw9ZZ/pMPt/q5RHAkTpLVnoEgES
e0VqFc2kdVXUfmdXcuWB5VE3Wt3HpC1x2kRqTuhfiaIMRv00fIaLuKskInIi0T1V
cglG4/8eqq8HMviXPPpd
-----END CERTIFICATE-----
"""

_TEST_KEY_PEM = b"""-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQC9wutuf2kz21Fw
8wzsNye2GZ1P3WIO4UVL8353DI4eltYVl/pwM86PXlgvBHXG2rHPMvdEJ1MXlzDv
7hG4+/EJGEGjcr+wqlmxPPZPeqvEdD1U8hnVYqcSzHPLPuuwaH9TQtbN+2I64vqo
hWqpen3IoiAmx5DQpgyFvrbzUh/fUjuNK9ECB4t889ijKRM5LK+U3ZKv6N8rw4v9
D3Q9mHO7qXnYu0+GzDe+IDoKAiY9QoyyvcUszRurky35athMJNMANw1G1gUcrUj4
Z7DRikLlRvHsxV9pbHYLdTKB2+aaXzLarg9pMDBBTtZRVw4MGYJxWqGTK9YTvc73
8Xi63avJAgMBAAECggEAB97LWTFBXaBbZbv85ZiphdrU3JS93ghfkYV7MaiUheYD
YdCkd04cw3M6JNQSZvGX1ZRDb2ESqAwyIEdfVozje7k08rieBO+hyfExdY0szdh0
2T6zzdb6PzQ52ryEtbO10TAY3ODv22mh1Aa5jGcrS5yWyQj2o4K5ivwElj2qGv7Z
tnARLGRrtx7nJB/0n7uUs4sOuwcj2ESm1DM0/61uSYYHc65CB19KG4RFiNuNbZYL
dZefMeHK0M0JP18QvQ17RLw/WSOOA/rqPyaP10+2ICYaQri+bQsZW/ES00huFpe0
H//j2nRRG1HDm6CtItxnVQSSVYnstCJCIlK0NTN3kQKBgQDgqRbFd7zSCuf+dw0V
RUNbNAhmFIUOIkahF60HLiY0PcD52a9Gy86Onf/i8o4PXDIyVGJJhbx1p583YG6G
nWqLHdiDcx/A8Wz+AMrZFgUxK7h2P+fX2RfXnFK3gI+V5cPi2c+TvH/BqHA68HgQ
Hej2NfQ8OPzCbXg9V2RJLfz+MQKBgQDYO4opk5Vjpy18bzqW9UsJ2DhVoJIDaOs1
Plwt8gJTvtE6OPkbcuA7Ak4twcA7jNr+ihj2jHiwmiojIZFmVSy0tyBPqAtAYvd3
5d2SCZ2iHq/qJ/wvEGGUAubWeEkXAThjd+ENOY76zHJZ1mheH2VyzGErwtHlRVaC
HVvRw+MpGQKBgCX2LHTdkLhlQ2JKN5m6hHEqz6iAGyOSQyEBYSlvcOEu9ibB085A
rfyHUi/FEKAj0g+TFrCZuoie9FZlIwf4HYK4XleH4nu1z9bzx1L7V5FBc//3OHPO
qSqzrX54aMrJcloot9yc43GTxrMO4xrGExFXeJecgYlQ+hpTZAzBiphhAoGBAMhh
BHva5AlhFunFOYpC7bLFyA6xqh220KCalVmOd1Gb9s/5k/83yUtlq4UDk1yb/yT6
XH+9VOpzMrEznkYykCc3vJ2UoDiefa2COn3mo0llHqfjPfNvPr2morwE49aJrvOe
V9OljzYi16Ug576xYZWsiC/BbjkEtFIHWttcp9mhAoGBAMhk7oGL7wLmuMGgff8i
bvSZuKTJEK888/X4vcHKi65EzspQ0EtnDbKBxTP0300IOvVmBxoV7CweAuEMtD6d
zdBfDtSVOQenfIS1gPf726w7abcLyYLyUTbMCMDK1pKKYZn9E3wL8ASBkoXN/Kzd
7umc/wOBEVvJBbkzid+4mpIa
-----END PRIVATE KEY-----
"""


@pytest.fixture(name="identity_pem")
def _identity_pem() -> bytes:
    return _TEST_CERT_PEM + _TEST_KEY_PEM


async def test_connect_timeout_and_pool_settings_still_allow_a_normal_request(
    base_url: str,
) -> None:
    async with HTTPClient.build(
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
    with SyncHTTPClient.build(
        base_url=base_url,
        connect_timeout=5.0,
        max_connections=10,
        pool_idle_timeout=30.0,
        pool_max_idle_per_host=5,
        pool_timeout=2.0,
    ) as client:
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_tls_config_builds_and_still_allows_a_normal_request(
    base_url: str, identity_pem: bytes
) -> None:
    async with HTTPClient.build(
        base_url=base_url,
        root_certificates=[_TEST_CERT_PEM],
        identity_pem=identity_pem,
        min_tls_version="TLSv1.2",
        max_tls_version="TLSv1.3",
        danger_accept_invalid_certs=True,
    ) as client:
        result = await client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_tls_config_builds_and_still_allows_a_normal_request(
    base_url: str, identity_pem: bytes
) -> None:
    with SyncHTTPClient.build(
        base_url=base_url,
        root_certificates=[_TEST_CERT_PEM],
        identity_pem=identity_pem,
        min_tls_version="TLSv1.2",
        max_tls_version="TLSv1.3",
        danger_accept_invalid_certs=True,
    ) as client:
        result = client.get("items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}
