import pytest

from lothc import JSON, HTTPClient, SyncHTTPClient


async def _async_bearer_auth() -> str:
    return "token-value"


def _sync_bearer_auth() -> str:
    return "token-value"


async def test_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with pytest.raises(ValueError, match="at most one"):
        async with HTTPClient.build(
            base_url=base_url, bearer_token="token-value", bearer_auth=_async_bearer_auth
        ):
            pass


async def test_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    async with HTTPClient.build(timeout=None) as client:
        result = await client.get(f"{base_url}items/7", response_data_type=JSON)

    assert result == {"id": 7, "name": "item-7"}


async def test_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, max_redirects=5) as client:
        result = await client.get_result("redirect")

    assert result.status == 200


async def test_build_with_proxy_configured_does_not_error(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, proxy="http://127.0.0.1:1"):
        pass


def test_sync_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with (
        pytest.raises(ValueError, match="at most one"),
        SyncHTTPClient.build(
            base_url=base_url, bearer_token="token-value", bearer_auth=_sync_bearer_auth
        ),
    ):
        pass


def test_sync_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    with SyncHTTPClient.build(timeout=None) as client:
        result = client.get(f"{base_url}items/7", response_data_type=JSON)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_build_with_default_headers_sent_on_every_request(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, default_headers={"x-api-key": "secret"}) as client:
        result = client.get("echo-headers", response_data_type=JSON)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-api-key"] == "secret"


def test_sync_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, max_redirects=5) as client:
        result = client.get_result("redirect")

    assert result.status == 200


def test_sync_build_with_proxy_configured_does_not_error(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, proxy="http://127.0.0.1:1"):
        pass
