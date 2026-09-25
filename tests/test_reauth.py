"""A token rejected with a 401 before it expires (revoked server-side) is discarded and, for an
idempotent verb, retried once with a fresh one: `OAuthProvider` used to keep sending the revoked
token, failing every call until it naturally expired.
"""

from typing import Any
from uuid import uuid4

import pytest

from lothc import (
    HTTPClient,
    HTTPResponseError,
    OAuthProvider,
    SyncHTTPClient,
    SyncOAuthProvider,
)


def _provider(base_url: str, key: str) -> OAuthProvider:
    return OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client-id",
        client_secret="client-secret",
    )


def _sync_provider(base_url: str, key: str) -> SyncOAuthProvider:
    return SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client-id",
        client_secret="client-secret",
    )


async def _token_requests(base_url: str, key: str) -> list[dict[str, Any]]:
    async with HTTPClient(base_url=base_url) as plain:
        result = (await plain.get(f"oauth/token-requests?key={key}", response_data_type=dict)).data
    return result["requests"]


async def _no_invalidate_bearer() -> str:
    return "token-1"


async def test_a_revoked_token_is_replaced_and_the_request_retried(base_url: str) -> None:
    key = str(uuid4())
    async with HTTPClient(base_url=base_url, bearer_auth=_provider(base_url, key)) as client:
        result = (await client.get("rejects-first-token", response_data_type=dict)).data

    assert result == {"authorization": "Bearer token-2"}
    assert len(await _token_requests(base_url, key)) == 2


def test_sync_a_revoked_token_is_replaced_and_the_request_retried(base_url: str) -> None:
    key = str(uuid4())
    with SyncHTTPClient(base_url=base_url, bearer_auth=_sync_provider(base_url, key)) as client:
        result = client.get("rejects-first-token", response_data_type=dict).data

    assert result == {"authorization": "Bearer token-2"}


async def test_a_rejected_post_is_not_replayed_but_the_next_call_gets_a_fresh_token(
    base_url: str,
) -> None:
    key = str(uuid4())
    async with HTTPClient(base_url=base_url, bearer_auth=_provider(base_url, key)) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.post("rejects-first-token", json={"a": 1})
        follow_up = (
            await client.post("rejects-first-token", json={"a": 1}, response_data_type=dict)
        ).data

    assert exc_info.value.status == 401
    assert follow_up == {"authorization": "Bearer token-2"}


async def test_a_second_401_is_raised_rather_than_retried_again(base_url: str) -> None:
    key = str(uuid4())
    async with HTTPClient(base_url=base_url, bearer_auth=_provider(base_url, key)) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("always-unauthorized", params={"key": f"{key}-api"}, error_type=dict)

    assert exc_info.value.status == 401
    assert exc_info.value.parsed_body == {"hits": 2}  # the original request plus exactly one retry


async def test_a_bearer_auth_without_invalidate_is_not_retried(base_url: str) -> None:
    key = str(uuid4())
    async with HTTPClient(base_url=base_url, bearer_auth=_no_invalidate_bearer) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("always-unauthorized", params={"key": f"{key}-api"}, error_type=dict)

    assert exc_info.value.parsed_body == {"hits": 1}


async def test_a_skip_auth_request_is_not_retried(base_url: str) -> None:
    key = str(uuid4())
    async with HTTPClient(base_url=base_url, bearer_auth=_provider(base_url, key)) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get(
                "always-unauthorized", params={"key": f"{key}-api"}, skip_auth=True, error_type=dict
            )

    assert exc_info.value.parsed_body == {"hits": 1}


async def test_invalidate_with_a_stale_token_keeps_a_newer_one(base_url: str) -> None:
    # Two requests that both got a 401 must not each throw away the token the other just fetched.
    key = str(uuid4())
    provider = _provider(base_url, key)
    assert await provider() == "token-1"

    provider.invalidate("token-0")

    assert await provider() == "token-1"
    assert len(await _token_requests(base_url, key)) == 1


async def test_invalidate_makes_the_next_call_fetch_a_new_token(base_url: str) -> None:
    key = str(uuid4())
    provider = _provider(base_url, key)
    assert await provider() == "token-1"

    provider.invalidate()

    assert await provider() == "token-2"


def test_sync_a_rejected_post_is_not_replayed_but_the_next_call_gets_a_fresh_token(
    base_url: str,
) -> None:
    key = str(uuid4())
    with SyncHTTPClient(base_url=base_url, bearer_auth=_sync_provider(base_url, key)) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.post("rejects-first-token", json={"a": 1})
        follow_up = client.post("rejects-first-token", json={"a": 1}, response_data_type=dict).data

    assert exc_info.value.status == 401
    assert follow_up == {"authorization": "Bearer token-2"}


def test_sync_a_skip_auth_request_is_not_retried(base_url: str) -> None:
    key = str(uuid4())
    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=_sync_provider(base_url, key)) as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get(
            "always-unauthorized", params={"key": f"{key}-api"}, skip_auth=True, error_type=dict
        )

    assert exc_info.value.parsed_body == {"hits": 1}
