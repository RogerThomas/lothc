"""A token rejected with a 401 before it expires (revoked server-side) is discarded and, for an
idempotent verb, retried once with a fresh one: `OAuthProvider` used to keep sending the revoked
token, failing every call until it naturally expired.
"""

from typing import Any, Literal

import pytest
from _oauth_server import TokenServer

from lothc import (
    HTTPClient,
    HTTPResponseError,
    SyncHTTPClient,
)


async def _no_invalidate_bearer() -> str:
    return "token-1"


class ReauthError(HTTPResponseError[dict[str, Any]]):
    pass


async def test_a_revoked_token_is_replaced_and_the_request_retried(
    base_url: str, token_server: TokenServer
) -> None:
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        result = (await client.get("rejects-first-token", into=dict)).data

    assert result == {"authorization": "Bearer token-2"}
    assert len(token_server.requests()) == 2


def test_sync_a_revoked_token_is_replaced_and_the_request_retried(
    base_url: str, token_server: TokenServer
) -> None:
    with SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client:
        result = client.get("rejects-first-token", into=dict).data

    assert result == {"authorization": "Bearer token-2"}


async def test_a_rejected_post_is_not_replayed_but_the_next_call_gets_a_fresh_token(
    base_url: str, token_server: TokenServer
) -> None:
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.post("rejects-first-token", json={"a": 1})
        follow_up = (await client.post("rejects-first-token", json={"a": 1}, into=dict)).data

    assert exc_info.value.status == 401
    assert follow_up == {"authorization": "Bearer token-2"}


async def test_a_second_401_is_raised_rather_than_retried_again(
    base_url: str, token_server: TokenServer
) -> None:
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("always-unauthorized", params={"key": f"{token_server.key}-api"})

    assert exc_info.value.status == 401
    assert exc_info.value.error == {"hits": 2}  # the original request plus exactly one retry


async def test_a_401_after_reauthenticating_is_raised_as_the_clients_raises_class(
    base_url: str, token_server: TokenServer
) -> None:
    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(), raises=ReauthError
    ) as client:
        with pytest.raises(ReauthError) as exc_info:
            await client.get("always-unauthorized", params={"key": f"{token_server.key}-api"})

    assert exc_info.value.error == {"hits": 2}


async def test_a_bearer_auth_without_invalidate_is_not_retried(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, bearer_auth=_no_invalidate_bearer) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("always-unauthorized", params={"key": server_key})

    assert exc_info.value.error == {"hits": 1}


async def test_a_skip_auth_request_is_not_retried(base_url: str, token_server: TokenServer) -> None:
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get(
                "always-unauthorized", params={"key": f"{token_server.key}-api"}, skip_auth=True
            )

    assert exc_info.value.error == {"hits": 1}


async def test_invalidate_with_a_stale_token_keeps_a_newer_one(token_server: TokenServer) -> None:
    # Two requests that both got a 401 must not each throw away the token the other just fetched.
    provider = token_server.provider()
    assert await provider() == "token-1"

    provider.invalidate("token-0")

    assert await provider() == "token-1"
    assert len(token_server.requests()) == 1


async def test_invalidate_makes_the_next_call_fetch_a_new_token(token_server: TokenServer) -> None:
    provider = token_server.provider()
    assert await provider() == "token-1"

    provider.invalidate()

    assert await provider() == "token-2"


def test_sync_a_rejected_post_is_not_replayed_but_the_next_call_gets_a_fresh_token(
    base_url: str, token_server: TokenServer
) -> None:
    with SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.post("rejects-first-token", json={"a": 1})
        follow_up = client.post("rejects-first-token", json={"a": 1}, into=dict).data

    assert exc_info.value.status == 401
    assert follow_up == {"authorization": "Bearer token-2"}


def test_sync_a_skip_auth_request_is_not_retried(base_url: str, token_server: TokenServer) -> None:
    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get("always-unauthorized", params={"key": f"{token_server.key}-api"}, skip_auth=True)

    assert exc_info.value.error == {"hits": 1}


def test_sync_a_401_after_reauthenticating_is_raised_as_the_clients_raises_class(
    base_url: str, token_server: TokenServer
) -> None:
    with (
        SyncHTTPClient(
            base_url=base_url, bearer_auth=token_server.sync_provider(), raises=ReauthError
        ) as client,
        pytest.raises(ReauthError) as exc_info,
    ):
        client.get("always-unauthorized", params={"key": f"{token_server.key}-api"})

    assert exc_info.value.error == {"hits": 2}


type _ReplayedVerb = Literal["put", "delete", "head", "options", "patch"]


async def _send_rejected(client: HTTPClient, verb: _ReplayedVerb, key: str) -> None:
    # `/status-then-ok` rejecting every attempt with a 401, bodiless for HEAD.
    params: dict[str, str | int] = {"key": key, "status": 401, "fail_times": 9}
    match verb:
        case "put":
            await client.put("status-then-ok", params=params)
        case "delete":
            await client.delete("status-then-ok", params=params)
        case "head":
            await client.head("status-then-ok", params=params)
        case "options":
            await client.options("status-then-ok", params=params)
        case "patch":
            await client.patch("status-then-ok", params=params)


def _send_rejected_sync(client: SyncHTTPClient, verb: _ReplayedVerb, key: str) -> None:
    params: dict[str, str | int] = {"key": key, "status": 401, "fail_times": 9}
    match verb:
        case "put":
            client.put("status-then-ok", params=params)
        case "delete":
            client.delete("status-then-ok", params=params)
        case "head":
            client.head("status-then-ok", params=params)
        case "options":
            client.options("status-then-ok", params=params)
        case "patch":
            client.patch("status-then-ok", params=params)


@pytest.mark.parametrize(
    ("verb", "attempts"),
    [("put", 2), ("delete", 2), ("head", 2), ("options", 2), ("patch", 1)],
)
async def test_a_401_is_replayed_with_a_fresh_token_only_for_idempotent_verbs(
    base_url: str, token_server: TokenServer, verb: _ReplayedVerb, attempts: int
) -> None:
    # PATCH, like POST, may not be safe to repeat, so its 401 is raised without a second token
    # being fetched. The hit count is read with `skip_auth`, which fetches no token either.
    api_key = f"{token_server.key}-api"
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await _send_rejected(client, verb, api_key)
        hits = (await client.get("hits", params={"key": api_key}, into=dict, skip_auth=True)).data

    assert exc_info.value.status == 401
    assert hits == {"hits": attempts}
    assert len(token_server.requests()) == attempts


@pytest.mark.parametrize(
    ("verb", "attempts"),
    [("put", 2), ("delete", 2), ("head", 2), ("options", 2), ("patch", 1)],
)
def test_sync_a_401_is_replayed_with_a_fresh_token_only_for_idempotent_verbs(
    base_url: str, token_server: TokenServer, verb: _ReplayedVerb, attempts: int
) -> None:
    api_key = f"{token_server.key}-api"
    with SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            _send_rejected_sync(client, verb, api_key)
        hits = client.get("hits", params={"key": api_key}, into=dict, skip_auth=True).data

    assert exc_info.value.status == 401
    assert hits == {"hits": attempts}
    assert len(token_server.requests()) == attempts


# A multipart `form=` body can't be replayed (see tests/test_retries.py), so a 401 on one still
# invalidates the token but is raised, as for a POST, instead of failing before it is sent.
async def test_a_401_on_a_form_put_invalidates_the_token_without_a_replay(
    base_url: str, token_server: TokenServer
) -> None:
    api_key = f"{token_server.key}-api"
    async with HTTPClient(base_url=base_url, bearer_auth=token_server.provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.put(
                "status-then-ok",
                params={"key": api_key, "status": 401, "fail_times": 1},
                form={"a": "b"},
            )
        follow_up = (
            await client.put("status-then-ok", params={"key": api_key}, form={"a": "b"}, into=dict)
        ).data

    assert exc_info.value.status == 401
    assert follow_up["attempts"] == 2
    assert len(token_server.requests()) == 2


def test_sync_a_401_on_a_form_put_invalidates_the_token_without_a_replay(
    base_url: str, token_server: TokenServer
) -> None:
    api_key = f"{token_server.key}-api"
    with SyncHTTPClient(base_url=base_url, bearer_auth=token_server.sync_provider()) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.put(
                "status-then-ok",
                params={"key": api_key, "status": 401, "fail_times": 1},
                form={"a": "b"},
            )
        follow_up = client.put(
            "status-then-ok", params={"key": api_key}, form={"a": "b"}, into=dict
        ).data

    assert exc_info.value.status == 401
    assert follow_up["attempts"] == 2
    assert len(token_server.requests()) == 2
