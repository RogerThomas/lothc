"""How each hop of a followed redirect goes out, beyond `test_redirects.py`'s method and limit
rules: the credentials it drops leaving an origin, the `Referer` it sends, the timeout and
connection it shares with the request it came from, the streaming verbs, and the redirects that
end a request.

Another origin on the same test server is its `localhost` name (`base_url` is `127.0.0.1`).
"""

from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import pytest
from _helpers import echoed_headers

from lothc import (
    HTTPClient,
    HTTPResponseError,
    HTTPTimeoutError,
    HTTPTransportError,
    SyncHTTPClient,
)


@pytest.fixture(name="other_origin")
def _other_origin(base_url: str) -> str:
    return base_url.replace("127.0.0.1", "localhost")


@pytest.fixture(name="https_client")
async def _https_client(https_base_url: str, tls_ca_pem: bytes) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], bearer_token="token"
    ) as client:
        yield client


@pytest.fixture(name="sync_https_client")
def _sync_https_client(https_base_url: str, tls_ca_pem: bytes) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], bearer_token="token"
    ) as client:
        yield client


def _credentials() -> dict[str, str]:
    # Every header reqwest drops on a hop to another origin, and one it keeps.
    return {
        "cookie": "cookie",
        "cookie2": "cookie2",
        "proxy-authorization": "proxy-authorization",
        "www-authenticate": "www-authenticate",
        "x-custom": "x-custom",
    }


def _dropped() -> set[str]:
    return {"authorization", "cookie", "cookie2", "proxy-authorization", "www-authenticate"}


async def test_a_hop_to_another_origin_drops_every_credential_header(
    base_url: str, other_origin: str
) -> None:
    async with HTTPClient(base_url=base_url, bearer_token="token") as client:
        result = await client.get(
            "redirect-to",
            params={"to": f"{other_origin}echo-headers"},
            headers=_credentials(),
            into=dict,
        )

    headers = echoed_headers(result.data)
    assert headers.keys() & _dropped() == set()
    assert headers["x-custom"] == "x-custom"
    assert headers["host"].startswith("localhost:")


def test_sync_a_hop_to_another_origin_drops_every_credential_header(
    base_url: str, other_origin: str
) -> None:
    with SyncHTTPClient(base_url=base_url, bearer_token="token") as client:
        result = client.get(
            "redirect-to",
            params={"to": f"{other_origin}echo-headers"},
            headers=_credentials(),
            into=dict,
        )

    headers = echoed_headers(result.data)
    assert headers.keys() & _dropped() == set()
    assert headers["x-custom"] == "x-custom"
    assert headers["host"].startswith("localhost:")


async def test_a_further_hop_within_the_other_origin_sends_the_credentials_again(
    base_url: str, other_origin: str
) -> None:
    # reqwest drops them from the copy of the request that leaves the first origin, not from the
    # one every hop starts from, and compares each hop with the one before it alone.
    async with HTTPClient(base_url=base_url, bearer_token="token") as client:
        result = await client.get(
            "redirect-to",
            params={"to": f"{other_origin}redirect-to?to=/echo-headers"},
            into=dict,
        )

    headers = echoed_headers(result.data)
    assert headers["host"].startswith("localhost:")
    assert headers["authorization"] == "Bearer token"


def test_sync_a_further_hop_within_the_other_origin_sends_the_credentials_again(
    base_url: str, other_origin: str
) -> None:
    with SyncHTTPClient(base_url=base_url, bearer_token="token") as client:
        result = client.get(
            "redirect-to",
            params={"to": f"{other_origin}redirect-to?to=/echo-headers"},
            into=dict,
        )

    headers = echoed_headers(result.data)
    assert headers["host"].startswith("localhost:")
    assert headers["authorization"] == "Bearer token"


async def test_a_hop_sends_the_url_it_came_from_as_referer(
    base_url: str, client: HTTPClient
) -> None:
    # In place of the caller's own `Referer`, which the first request still sends.
    result = await client.get(
        "redirect-to", params={"to": "/echo-headers"}, headers={"referer": "referer"}, into=dict
    )

    assert echoed_headers(result.data)["referer"] == f"{base_url}redirect-to?to=%2Fecho-headers"


def test_sync_a_hop_sends_the_url_it_came_from_as_referer(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    result = sync_client.get(
        "redirect-to", params={"to": "/echo-headers"}, headers={"referer": "referer"}, into=dict
    )

    assert echoed_headers(result.data)["referer"] == f"{base_url}redirect-to?to=%2Fecho-headers"


async def test_a_hop_from_https_to_http_sends_neither_referer_nor_credentials(
    base_url: str, https_client: HTTPClient
) -> None:
    result = await https_client.get(
        "redirect-to", params={"to": f"{base_url}echo-headers"}, into=dict
    )

    headers = echoed_headers(result.data)
    assert "referer" not in headers
    assert "authorization" not in headers


def test_sync_a_hop_from_https_to_http_sends_neither_referer_nor_credentials(
    base_url: str, sync_https_client: SyncHTTPClient
) -> None:
    result = sync_https_client.get(
        "redirect-to", params={"to": f"{base_url}echo-headers"}, into=dict
    )

    headers = echoed_headers(result.data)
    assert "referer" not in headers
    assert "authorization" not in headers


async def test_https_only_refuses_a_redirect_to_plain_http(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(
        base_url=https_base_url, root_certificates=[tls_ca_pem], https_only=True
    ) as client:
        with pytest.raises(HTTPTransportError, match="redirect"):
            await client.get("redirect-to", params={"to": f"{base_url}items/7"})


def test_sync_https_only_refuses_a_redirect_to_plain_http(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    with (
        SyncHTTPClient(
            base_url=https_base_url, root_certificates=[tls_ca_pem], https_only=True
        ) as client,
        pytest.raises(HTTPTransportError, match="redirect"),
    ):
        client.get("redirect-to", params={"to": f"{base_url}items/7"})


async def test_a_location_that_is_not_a_url_is_returned_as_the_response(
    client: HTTPClient,
) -> None:
    response = await client.get("redirect-to", params={"to": "http://[::1"})

    assert (response.status, response.headers["location"]) == (302, "http://[::1")


def test_sync_a_location_that_is_not_a_url_is_returned_as_the_response(
    sync_client: SyncHTTPClient,
) -> None:
    response = sync_client.get("redirect-to", params={"to": "http://[::1"})

    assert (response.status, response.headers["location"]) == (302, "http://[::1")


async def test_a_relative_location_resolves_against_the_url_it_came_from(
    client: HTTPClient,
) -> None:
    # A path without a leading slash replaces `/redirect-to`'s last segment.
    result = await client.get("redirect-to", params={"to": "items/7"}, into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_a_relative_location_resolves_against_the_url_it_came_from(
    sync_client: SyncHTTPClient,
) -> None:
    result = sync_client.get("redirect-to", params={"to": "items/7"}, into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


async def test_ten_redirects_are_followed_by_default(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        with pytest.raises(HTTPTransportError, match="too many redirects"):
            await client.get("redirect-loop-counted", params={"key": server_key})
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 11}


def test_sync_ten_redirects_are_followed_by_default(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url) as client:
        with pytest.raises(HTTPTransportError, match="too many redirects"):
            client.get("redirect-loop-counted", params={"key": server_key})
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert hits == {"hits": 11}


async def test_a_negative_max_redirects_fails_on_entry() -> None:
    with pytest.raises(OverflowError):
        async with HTTPClient(max_redirects=-1):
            pass


def test_sync_a_negative_max_redirects_fails_on_entry() -> None:
    with pytest.raises(OverflowError), SyncHTTPClient(max_redirects=-1):
        pass


async def test_an_error_status_after_a_redirect_reports_the_request_made(
    base_url: str, client: HTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("redirect-to", params={"to": "/boom"})

    assert exc_info.value.status == 500
    assert exc_info.value.request.url == f"{base_url}redirect-to?to=%2Fboom"


def test_sync_an_error_status_after_a_redirect_reports_the_request_made(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("redirect-to", params={"to": "/boom"})

    assert exc_info.value.status == 500
    assert exc_info.value.request.url == f"{base_url}redirect-to?to=%2Fboom"


async def test_one_timeout_covers_every_hop(client: HTTPClient) -> None:
    # Each hop takes 0.2s, inside the 0.3s on its own; together they're past it.
    with pytest.raises(HTTPTimeoutError):
        await client.get(
            "slow-redirect", params={"seconds": 0.2, "to": "/slow?seconds=0.2"}, timeout=0.3
        )


def test_sync_one_timeout_covers_every_hop(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPTimeoutError):
        sync_client.get(
            "slow-redirect", params={"seconds": 0.2, "to": "/slow?seconds=0.2"}, timeout=0.3
        )


async def test_a_hop_to_the_same_origin_reuses_the_redirects_connection(
    client: HTTPClient,
) -> None:
    first = (await client.get("peer", into=dict)).data
    hop = (await client.get("redirect-to", params={"to": "/peer"}, into=dict)).data

    assert hop == first


def test_sync_a_hop_to_the_same_origin_reuses_the_redirects_connection(
    sync_client: SyncHTTPClient,
) -> None:
    first = sync_client.get("peer", into=dict).data
    hop = sync_client.get("redirect-to", params={"to": "/peer"}, into=dict).data

    assert hop == first


async def test_a_redirect_that_is_not_followed_keeps_its_connection(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_redirects=0) as client:
        first = (await client.get("peer", into=dict)).data
        with pytest.raises(HTTPTransportError):
            await client.get("redirect-to", params={"to": "/peer"})
        after = (await client.get("peer", into=dict)).data

    assert after == first


def test_sync_a_redirect_that_is_not_followed_keeps_its_connection(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_redirects=0) as client:
        first = client.get("peer", into=dict).data
        with pytest.raises(HTTPTransportError):
            client.get("redirect-to", params={"to": "/peer"})
        after = client.get("peer", into=dict).data

    assert after == first


async def test_the_streaming_verbs_follow_redirects(client: HTTPClient, tmp_path: Path) -> None:
    streamed = b"".join([
        chunk async for chunk in client.stream_get("redirect-to", params={"to": "/items/7"})
    ])
    posted = [
        line
        async for line in client.stream_post(
            "redirect-to",
            params={"to": "/echo-method", "status": 307},
            json={"a": 1},
            into=dict,
        )
    ]
    dest = tmp_path / "dest"
    await client.download("redirect-to", dest, params={"to": "/items/7"})
    events = [event async for event in client.sse("redirect-to", params={"to": "/events?count=2"})]

    assert streamed == b'{"id": 7, "name": "item-7"}'
    assert posted == [{"method": "POST", "body": '{"a":1}', "content_type": "application/json"}]
    assert dest.read_bytes() == streamed
    assert [event.id for event in events] == ["0", "1"]


def test_sync_the_streaming_verbs_follow_redirects(
    sync_client: SyncHTTPClient, tmp_path: Path
) -> None:
    streamed = b"".join(sync_client.stream_get("redirect-to", params={"to": "/items/7"}))
    posted = list(
        sync_client.stream_post(
            "redirect-to",
            params={"to": "/echo-method", "status": 307},
            json={"a": 1},
            into=dict,
        )
    )
    dest = tmp_path / "dest"
    sync_client.download("redirect-to", dest, params={"to": "/items/7"})
    events = list(sync_client.sse("redirect-to", params={"to": "/events?count=2"}))

    assert streamed == b'{"id": 7, "name": "item-7"}'
    assert posted == [{"method": "POST", "body": '{"a":1}', "content_type": "application/json"}]
    assert dest.read_bytes() == streamed
    assert [event.id for event in events] == ["0", "1"]


async def test_sse_does_not_reconnect_after_a_redirect_to_another_scheme(
    client: HTTPClient, server_key: str
) -> None:
    # A request that can never be sent; a redirect loop, below, could clear up, so is reconnected.
    params = {"key": server_key, "to": "ftp://127.0.0.1/file"}
    with pytest.raises(HTTPTransportError, match="scheme"):
        [
            event
            async for event in client.sse(
                "redirect-counted", params=params, max_reconnects=2, reconnect_delay=0.001
            )
        ]
    hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 1}


def test_sync_sse_does_not_reconnect_after_a_redirect_to_another_scheme(
    sync_client: SyncHTTPClient, server_key: str
) -> None:
    params = {"key": server_key, "to": "ftp://127.0.0.1/file"}
    with pytest.raises(HTTPTransportError, match="scheme"):
        list(
            sync_client.sse(
                "redirect-counted", params=params, max_reconnects=2, reconnect_delay=0.001
            )
        )
    hits = sync_client.get("hits", params={"key": server_key}, into=dict).data

    assert hits == {"hits": 1}


async def test_sse_reconnects_after_too_many_redirects(base_url: str, server_key: str) -> None:
    # Each connection is the request plus its one allowed hop.
    async with HTTPClient(base_url=base_url, max_redirects=1) as client:
        with pytest.raises(HTTPTransportError, match="too many redirects"):
            [
                event
                async for event in client.sse(
                    "redirect-loop-counted",
                    params={"key": server_key},
                    max_reconnects=1,
                    reconnect_delay=0.001,
                )
            ]
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 4}


def test_sync_sse_reconnects_after_too_many_redirects(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_redirects=1) as client:
        with pytest.raises(HTTPTransportError, match="too many redirects"):
            list(
                client.sse(
                    "redirect-loop-counted",
                    params={"key": server_key},
                    max_reconnects=1,
                    reconnect_delay=0.001,
                )
            )
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert hits == {"hits": 4}
