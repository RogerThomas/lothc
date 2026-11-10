"""`http2=True` against a server that speaks HTTP/2 (`_h2_server.py`): the client must really
negotiate it, not just accept the setting. The fallback to HTTP/1.1 against a server that doesn't
is tested in `test_client_build.py` and `test_tls.py`.
"""

from lothc import HTTPClient, SyncHTTPClient


async def test_http2_true_negotiates_http2_with_a_server_that_offers_it(
    http2_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=http2_base_url, http2=True, root_certificates=[tls_ca_pem]
    ) as client:
        response = await client.get("items/7", into=dict)

    assert response.http_version == "HTTP/2.0"
    assert response.data == {"method": "GET", "path": "/items/7"}


def test_sync_http2_true_negotiates_http2_with_a_server_that_offers_it(
    http2_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(
        base_url=http2_base_url, http2=True, root_certificates=[tls_ca_pem]
    ) as client:
        response = client.get("items/7", into=dict)

    assert response.http_version == "HTTP/2.0"
    assert response.data == {"method": "GET", "path": "/items/7"}


async def test_without_http2_the_same_server_is_spoken_to_over_http1(
    http2_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(base_url=http2_base_url, root_certificates=[tls_ca_pem]) as client:
        response = await client.get("items/7", into=dict)

    assert response.http_version == "HTTP/1.1"
    assert response.data == {"id": 7, "name": "item-7"}


def test_sync_without_http2_the_same_server_is_spoken_to_over_http1(
    http2_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(base_url=http2_base_url, root_certificates=[tls_ca_pem]) as client:
        response = client.get("items/7", into=dict)

    assert response.http_version == "HTTP/1.1"
    assert response.data == {"id": 7, "name": "item-7"}


async def test_http2_carries_several_requests_on_one_client(
    http2_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=http2_base_url, http2=True, root_certificates=[tls_ca_pem]
    ) as client:
        paths = [(await client.get(f"items/{n}", into=dict)).data["path"] for n in range(3)]

    assert paths == ["/items/0", "/items/1", "/items/2"]
