from collections.abc import AsyncGenerator, Generator

import pytest

from lothc import HTTPClient, SyncHTTPClient


@pytest.fixture(name="cookie_client")
async def _cookie_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, cookie_store=True) as client:
        yield client


@pytest.fixture(name="sync_cookie_client")
def _sync_cookie_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, cookie_store=True) as client:
        yield client


async def test_cookie_store_disabled_by_default(client: HTTPClient) -> None:
    await client.get("set-cookie")
    result = (await client.get("read-cookie", into=dict)).data

    assert result["cookie"] is None


async def test_cookie_store_persists_cookies_across_requests(cookie_client: HTTPClient) -> None:
    await cookie_client.get("set-cookie")
    result = (await cookie_client.get("read-cookie", into=dict)).data

    assert result["cookie"] == "session=abc123"


def test_sync_cookie_store_disabled_by_default(sync_client: SyncHTTPClient) -> None:
    sync_client.get("set-cookie")
    result = sync_client.get("read-cookie", into=dict).data

    assert result["cookie"] is None


def test_sync_cookie_store_persists_cookies_across_requests(
    sync_cookie_client: SyncHTTPClient,
) -> None:
    sync_cookie_client.get("set-cookie")
    result = sync_cookie_client.get("read-cookie", into=dict).data

    assert result["cookie"] == "session=abc123"


async def test_a_cookie_set_by_a_redirect_is_sent_to_its_target(cookie_client: HTTPClient) -> None:
    result = (
        await cookie_client.get(
            "redirect-to",
            params={"to": "/read-cookie", "cookie": "session=redirected"},
            into=dict,
        )
    ).data

    assert result["cookie"] == "session=redirected"


def test_sync_a_cookie_set_by_a_redirect_is_sent_to_its_target(
    sync_cookie_client: SyncHTTPClient,
) -> None:
    result = sync_cookie_client.get(
        "redirect-to",
        params={"to": "/read-cookie", "cookie": "session=redirected"},
        into=dict,
    ).data

    assert result["cookie"] == "session=redirected"


async def test_the_cookie_jar_starts_empty_each_time_the_client_is_opened(base_url: str) -> None:
    # Each entry builds a fresh connection pool and jar (CLAUDE.md, "Client lifecycle").
    client = HTTPClient(base_url=base_url, cookie_store=True)
    async with client:
        await client.get("set-cookie")
    async with client:
        result = (await client.get("read-cookie", into=dict)).data

    assert result["cookie"] is None


def test_sync_the_cookie_jar_starts_empty_each_time_the_client_is_opened(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url, cookie_store=True)
    with client:
        client.get("set-cookie")
    with client:
        result = client.get("read-cookie", into=dict).data

    assert result["cookie"] is None
