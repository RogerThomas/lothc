import asyncio

import pytest
from pydantic import BaseModel

from lothc import HTTPClient, SyncHTTPClient


class _DefaultHeaders(BaseModel):
    x_api_key: str
    x_unset: str | None = None


async def _async_bearer_auth() -> str:
    return "token-value"


def _sync_bearer_auth() -> str:
    return "token-value"


async def test_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with pytest.raises(ValueError, match="at most one"):
        async with HTTPClient(
            base_url=base_url, bearer_token="token-value", bearer_auth=_async_bearer_auth
        ):
            pass


async def test_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    async with HTTPClient(timeout=None) as client:
        result = await client.get(f"{base_url}items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


async def test_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_redirects=5) as client:
        result = await client.with_result.get("redirect")

    assert result.status == 200


async def test_build_with_proxy_configured_does_not_error(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, proxy="http://127.0.0.1:1"):
        pass


def test_sync_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with (
        pytest.raises(ValueError, match="at most one"),
        SyncHTTPClient(
            base_url=base_url, bearer_token="token-value", bearer_auth=_sync_bearer_auth
        ),
    ):
        pass


def test_sync_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    with SyncHTTPClient(timeout=None) as client:
        result = client.get(f"{base_url}items/7", response_data_type=dict)

    assert result == {"id": 7, "name": "item-7"}


def test_sync_build_with_default_headers_sent_on_every_request(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, default_headers={"x-api-key": "secret"}) as client:
        result = client.get("echo-headers", response_data_type=dict)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-api-key"] == "secret"


def test_sync_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_redirects=5) as client:
        result = client.with_result.get("redirect")

    assert result.status == 200


def test_sync_build_with_proxy_configured_does_not_error(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, proxy="http://127.0.0.1:1"):
        pass


async def test_build_accepts_a_model_as_default_headers(base_url: str) -> None:
    # Encoded like per-request `headers=`: `_` becomes `-`, and a `None` field is omitted.
    async with HTTPClient(
        base_url=base_url, default_headers=_DefaultHeaders(x_api_key="secret")
    ) as client:
        result = await client.get("echo-headers", response_data_type=dict)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-api-key"] == "secret"
    assert "x-unset" not in headers


async def _fetch_once(client: HTTPClient) -> dict[str, object]:
    async with client:
        return await client.get("items/7", response_data_type=dict)


async def test_client_used_before_it_is_opened_raises_a_clear_error(base_url: str) -> None:
    client = HTTPClient(base_url=base_url)

    with pytest.raises(RuntimeError, match="never opened"):
        await client.get("items/7")


def test_sync_client_used_before_it_is_opened_raises_a_clear_error(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)

    with pytest.raises(RuntimeError, match="never opened"):
        client.get("items/7")


async def test_client_can_be_opened_again_after_it_closes(base_url: str) -> None:
    # Each entry builds a fresh pyreqwest client from the stored settings.
    client = HTTPClient(base_url=base_url)
    async with client:
        first = await client.get("items/7", response_data_type=dict)
    async with client:
        second = await client.get("items/7", response_data_type=dict)

    assert first == second == {"id": 7, "name": "item-7"}


def test_sync_client_can_be_opened_again_after_it_closes(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)
    with client:
        first = client.get("items/7", response_data_type=dict)
    with client:
        second = client.get("items/7", response_data_type=dict)

    assert first == second


async def test_entering_an_already_open_client_raises(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        with pytest.raises(RuntimeError, match="already open"):
            await client.__aenter__()


def test_sync_entering_an_already_open_client_raises(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url) as client,
        pytest.raises(RuntimeError, match="already open"),
    ):
        client.__enter__()


def test_conflicting_auth_is_rejected_when_the_client_is_constructed() -> None:
    # At construction, before anything is opened, not on entry.
    with pytest.raises(ValueError, match="at most one"):
        HTTPClient(bearer_token="bearer-token", basic_auth=("user", "pass"))


def test_one_client_works_across_separate_event_loops(base_url: str) -> None:
    # A module-level client reused by separate `asyncio.run()` calls, each opening it afresh.
    client = HTTPClient(base_url=base_url)

    assert asyncio.run(_fetch_once(client)) == asyncio.run(_fetch_once(client))


async def test_a_request_in_flight_when_the_client_closes_raises_runtime_error(
    base_url: str,
) -> None:
    # pyreqwest's own ClientClosedError for the in-flight request is translated, not leaked.
    client = HTTPClient(base_url=base_url)
    await client.__aenter__()
    request = asyncio.create_task(client.get("slow", params={"seconds": 0.5}))
    await asyncio.sleep(0.1)
    await client.__aexit__(None, None, None)

    with pytest.raises(RuntimeError, match="client is closed"):
        await request


async def test_exiting_a_client_that_is_not_open_is_harmless(base_url: str) -> None:
    client = HTTPClient(base_url=base_url)
    await client.__aexit__(None, None, None)
    async with client:
        pass
    await client.__aexit__(None, None, None)


def test_sync_exiting_a_client_that_is_not_open_is_harmless(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__exit__(None, None, None)
    with client:
        pass
    client.__exit__(None, None, None)
