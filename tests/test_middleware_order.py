"""The order retrying, re-authenticating and following redirects happen in, which a request's
attempts show: retries wrap re-authentication, which wraps the whole redirect chain, so a retry or
a re-authenticated attempt starts that chain over from the request as the caller made it.
"""

from _oauth_server import TokenServer

from lothc import HTTPClient, SyncHTTPClient


async def test_a_retry_goes_through_reauthentication_again_with_the_original_token(
    base_url: str, server_key: str, token_server: TokenServer
) -> None:
    # The first attempt's `token-1` is rejected and replaced by `token-2`, which gets a 503. The
    # retry is the request as first built, so `token-1` again, rejected and replaced again. The
    # token endpoint counts its mints on `server_key`, so the API counts on a key of its own.
    async with HTTPClient(
        base_url=base_url,
        bearer_auth=token_server.provider(),
        max_retries=1,
        backoff_base=0.001,
    ) as client:
        result = await client.get(
            "rejects-first-token-then-flaky",
            params={"key": f"{server_key}-api", "fail_times": 1},
            into=dict,
        )

    assert result.data == {
        "authorizations": ["Bearer token-1", "Bearer token-2", "Bearer token-1", "Bearer token-2"]
    }
    assert len(token_server.requests()) == 2


def test_sync_a_retry_goes_through_reauthentication_again_with_the_original_token(
    base_url: str, server_key: str, token_server: TokenServer
) -> None:
    with SyncHTTPClient(
        base_url=base_url,
        bearer_auth=token_server.sync_provider(),
        max_retries=1,
        backoff_base=0.001,
    ) as client:
        result = client.get(
            "rejects-first-token-then-flaky",
            params={"key": f"{server_key}-api", "fail_times": 1},
            into=dict,
        )

    assert result.data == {
        "authorizations": ["Bearer token-1", "Bearer token-2", "Bearer token-1", "Bearer token-2"]
    }
    assert len(token_server.requests()) == 2


async def test_a_retry_starts_the_redirect_chain_over(base_url: str, server_key: str) -> None:
    to = f"/status-then-ok?key={server_key}-target&fail_times=1"
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = await client.get(
            "redirect-counted", params={"key": server_key, "to": to}, into=dict
        )
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert result.data["attempts"] == 2
    assert hits == {"hits": 2}


def test_sync_a_retry_starts_the_redirect_chain_over(base_url: str, server_key: str) -> None:
    to = f"/status-then-ok?key={server_key}-target&fail_times=1"
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = client.get("redirect-counted", params={"key": server_key, "to": to}, into=dict)
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert result.data["attempts"] == 2
    assert hits == {"hits": 2}


async def test_a_401_after_a_redirect_reauthenticates_from_the_first_url(
    base_url: str, server_key: str, token_server: TokenServer
) -> None:
    # The token endpoint counts its mints on `server_key`, so the redirect counts on its own key.
    params = {"key": f"{server_key}-api", "to": "/rejects-first-token"}
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        result = await client.get("redirect-counted", params=params, into=dict)
        hits = (await client.get("hits", params={"key": f"{server_key}-api"}, into=dict)).data

    assert result.data == {"authorization": "Bearer token-2"}
    assert hits == {"hits": 2}


def test_sync_a_401_after_a_redirect_reauthenticates_from_the_first_url(
    base_url: str, server_key: str, token_server: TokenServer
) -> None:
    # The token endpoint counts its mints on `server_key`, so the redirect counts on its own key.
    params = {"key": f"{server_key}-api", "to": "/rejects-first-token"}
    with SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client:
        result = client.get("redirect-counted", params=params, into=dict)
        hits = client.get("hits", params={"key": f"{server_key}-api"}, into=dict).data

    assert result.data == {"authorization": "Bearer token-2"}
    assert hits == {"hits": 2}
