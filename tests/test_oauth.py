import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import msgspec
import pytest
from pydantic import BaseModel, ConfigDict, Field

from lothc import (
    HTTPClient,
    HTTPResponseError,
    OAuthProvider,
    OAuthTokenError,
    SyncHTTPClient,
    SyncOAuthProvider,
)


# `validate_by_name` lets lothc construct these by field name; `serialize_by_alias` makes lothc's
# `json=` encoding (`model_dump(mode="json")`, no `by_alias=True`) emit the wire names.
class _TokenRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    client_id: str = Field(alias="clientID")
    client_secret: str = Field(alias="clientSecret")
    grant_type: str = Field(default="client_credentials", alias="grantType")


class _TokenRefreshRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    refresh_token: str = Field(alias="refreshToken")
    grant_type: str = Field(default="refresh_token", alias="grantType")


class _TokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    expires_in: int = Field(alias="expiresInSeconds")
    refresh_token: str | None = Field(default=None, alias="refreshToken")


class _MsgspecTokenRequest(msgspec.Struct):
    client_id: str = msgspec.field(name="clientID")
    client_secret: str = msgspec.field(name="clientSecret")
    grant_type: str = msgspec.field(default="client_credentials", name="grantType")


class _MsgspecTokenRefreshRequest(msgspec.Struct):
    refresh_token: str = msgspec.field(name="refreshToken")
    grant_type: str = msgspec.field(default="refresh_token", name="grantType")


class _MsgspecTokenResponse(msgspec.Struct):
    access_token: str = msgspec.field(name="accessToken")
    expires_in: int = msgspec.field(name="expiresInSeconds")
    refresh_token: str | None = msgspec.field(default=None, name="refreshToken")


class _OptionalExpiresTokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    expires_in: int | None = Field(default=None, alias="expiresInSeconds")
    refresh_token: str | None = Field(default=None, alias="refreshToken")


class _MsgspecOptionalExpiresTokenResponse(msgspec.Struct):
    access_token: str = msgspec.field(name="accessToken")
    expires_in: int | None = msgspec.field(default=None, name="expiresInSeconds")
    refresh_token: str | None = msgspec.field(default=None, name="refreshToken")


def _headers(result: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in result["headers"]}


async def _bearer(client: HTTPClient) -> str:
    return _headers(await client.get("echo-headers", response_data_type=dict))["authorization"]


def _sync_bearer(client: SyncHTTPClient) -> str:
    return _headers(client.get("echo-headers", response_data_type=dict))["authorization"]


async def _token_requests(client: HTTPClient, key: str) -> list[dict[str, Any]]:
    result = await client.get(f"oauth/token-requests?key={key}", response_data_type=dict)
    return result["requests"]


def _sync_token_requests(client: SyncHTTPClient, key: str) -> list[dict[str, Any]]:
    result = client.get(f"oauth/token-requests?key={key}", response_data_type=dict)
    return result["requests"]


def _basic(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


async def test_rfc_mint_sends_bearer_token_and_basic_credentials(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=3600",
        client_id="client-id",
        client_secret="client-secret",
        scope="scope",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = await _token_requests(client, key)
    assert request["content_type"] == "application/x-www-form-urlencoded"
    assert request["authorization"] == _basic("client-id", "client-secret")
    assert request["body"] == {"grant_type": "client_credentials", "scope": "scope"}


def test_sync_rfc_mint_sends_bearer_token_and_basic_credentials(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=3600",
        client_id="client-id",
        client_secret="client-secret",
        scope="scope",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = _sync_token_requests(sync_client, key)
    assert request["content_type"] == "application/x-www-form-urlencoded"
    assert request["authorization"] == _basic("client-id", "client-secret")
    assert request["body"] == {"grant_type": "client_credentials", "scope": "scope"}


async def test_rfc_client_auth_body_puts_credentials_in_form(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client-id",
        client_secret="client-secret",
        client_auth="body",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = await _token_requests(client, key)
    assert request["authorization"] is None
    assert request["body"] == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }


def test_sync_rfc_client_auth_body_puts_credentials_in_form(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client-id",
        client_secret="client-secret",
        client_auth="body",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = _sync_token_requests(sync_client, key)
    assert request["authorization"] is None
    assert request["body"] == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }


async def test_valid_token_is_reused_across_requests(base_url: str, client: HTTPClient) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=3600",
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert len(await _token_requests(client, key)) == 1


def test_sync_valid_token_is_reused_across_requests(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=3600",
        client_id="client-id",
        client_secret="client-secret",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert len(_sync_token_requests(sync_client, key)) == 1


async def test_token_inside_leeway_window_is_minted_again_without_refresh_token(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=0",
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    grant_types = [r["body"]["grant_type"] for r in await _token_requests(client, key)]
    assert grant_types == ["client_credentials", "client_credentials"]


def test_sync_token_inside_leeway_window_is_minted_again_without_refresh_token(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=0",
        client_id="client-id",
        client_secret="client-secret",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    grant_types = [r["body"]["grant_type"] for r in _sync_token_requests(sync_client, key)]
    assert grant_types == ["client_credentials", "client_credentials"]


async def test_token_inside_leeway_window_is_refreshed_with_refresh_token(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1",
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = await _token_requests(client, key)
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert refresh["authorization"] == _basic("client-id", "client-secret")


def test_sync_token_inside_leeway_window_is_refreshed_with_refresh_token(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1",
        client_id="client-id",
        client_secret="client-secret",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = _sync_token_requests(sync_client, key)
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert refresh["authorization"] == _basic("client-id", "client-secret")


async def test_rejected_refresh_falls_back_to_minting(base_url: str, client: HTTPClient) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=(f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1&refresh_fails=1"),
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-3")
    grant_types = [r["body"]["grant_type"] for r in await _token_requests(client, key)]
    assert grant_types == ["client_credentials", "refresh_token", "client_credentials"]


def test_sync_rejected_refresh_falls_back_to_minting(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=(f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1&refresh_fails=1"),
        client_id="client-id",
        client_secret="client-secret",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-3")
    grant_types = [r["body"]["grant_type"] for r in _sync_token_requests(sync_client, key)]
    assert grant_types == ["client_credentials", "refresh_token", "client_credentials"]


async def test_custom_pydantic_models_send_json_token_request(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={key}",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = await _token_requests(client, key)
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None
    assert request["body"] == {
        "clientID": "client-id",
        "clientSecret": "client-secret",
        "grantType": "client_credentials",
    }


def test_sync_custom_pydantic_models_send_json_token_request(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={key}",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = _sync_token_requests(sync_client, key)
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None
    assert request["body"] == {
        "clientID": "client-id",
        "clientSecret": "client-secret",
        "grantType": "client_credentials",
    }


async def test_custom_msgspec_models_send_json_token_request(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={key}",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_MsgspecTokenRequest,
        token_response=_MsgspecTokenResponse,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = await _token_requests(client, key)
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None
    assert request["body"] == {
        "clientID": "client-id",
        "clientSecret": "client-secret",
        "grantType": "client_credentials",
    }


async def test_custom_models_refresh_with_token_refresh_request(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={key}&expires_in=0&with_refresh=1",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_refresh_request=_TokenRefreshRequest,
        token_response=_TokenResponse,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = await _token_requests(client, key)
    assert refresh["body"] == {"refreshToken": "refresh-1", "grantType": "refresh_token"}


def test_sync_custom_models_refresh_with_token_refresh_request(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={key}&expires_in=0&with_refresh=1",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_MsgspecTokenRequest,
        token_refresh_request=_MsgspecTokenRefreshRequest,
        token_response=_MsgspecTokenResponse,
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = _sync_token_requests(sync_client, key)
    assert refresh["body"] == {"refreshToken": "refresh-1", "grantType": "refresh_token"}


async def test_custom_models_without_token_refresh_request_mint_again(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={key}&expires_in=0&with_refresh=1",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, renewal = await _token_requests(client, key)
    assert renewal["body"]["clientID"] == "client-id"
    assert "refreshToken" not in renewal["body"]


def test_sync_custom_models_without_token_refresh_request_mint_again(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={key}&expires_in=0&with_refresh=1",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, renewal = _sync_token_requests(sync_client, key)
    assert renewal["body"]["clientID"] == "client-id"
    assert "refreshToken" not in renewal["body"]


async def test_token_cache_path_writes_token_file_with_owner_only_permissions(
    base_url: str, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}",
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=cache_path,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        await _bearer(api)

    assert cache_path.stat().st_mode & 0o777 == 0o600
    cached = json.loads(cache_path.read_text())
    assert cached["access_token"] == "token-1"
    assert cached["client_id"] == "client-id"


def test_sync_token_cache_path_writes_token_file_with_owner_only_permissions(
    base_url: str, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}",
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=cache_path,
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        _sync_bearer(api)

    assert cache_path.stat().st_mode & 0o777 == 0o600
    cached = json.loads(cache_path.read_text())
    assert cached["access_token"] == "token-1"
    assert cached["client_id"] == "client-id"


async def test_cached_token_is_reused_by_a_new_provider(
    base_url: str, client: HTTPClient, tmp_path: Path
) -> None:
    key = str(uuid4())
    token_url = f"{base_url}oauth/token?key={key}&expires_in=3600"
    first_provider = OAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = OAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    assert len(await _token_requests(client, key)) == 1


def test_sync_cached_token_is_reused_by_a_new_provider(
    base_url: str, sync_client: SyncHTTPClient, tmp_path: Path
) -> None:
    key = str(uuid4())
    token_url = f"{base_url}oauth/token?key={key}&expires_in=3600"
    first_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    assert len(_sync_token_requests(sync_client, key)) == 1


async def test_cached_token_for_a_different_client_id_is_ignored(
    base_url: str, tmp_path: Path
) -> None:
    token_url = f"{base_url}oauth/token?key={uuid4()}&expires_in=3600"
    first_provider = OAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = OAuthProvider(
        token_url=token_url,
        client_id="other-client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"


def test_sync_cached_token_for_a_different_client_id_is_ignored(
    base_url: str, tmp_path: Path
) -> None:
    token_url = f"{base_url}oauth/token?key={uuid4()}&expires_in=3600"
    first_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="other-client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"


async def test_corrupt_cache_file_is_ignored_and_overwritten(base_url: str, tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_bytes(b"not json")
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}",
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=cache_path,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(cache_path.read_text())["access_token"] == "token-1"


def test_sync_corrupt_cache_file_is_ignored_and_overwritten(base_url: str, tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache_path.write_bytes(b"not json")
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}",
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=cache_path,
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(cache_path.read_text())["access_token"] == "token-1"


async def test_cached_refresh_token_is_used_by_a_new_provider_inside_leeway_window(
    base_url: str, client: HTTPClient, tmp_path: Path
) -> None:
    key = str(uuid4())
    token_url = f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1"
    first_provider = OAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = OAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"
    _, refresh = await _token_requests(client, key)
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


def test_sync_cached_refresh_token_is_used_by_a_new_provider_inside_leeway_window(
    base_url: str, sync_client: SyncHTTPClient, tmp_path: Path
) -> None:
    key = str(uuid4())
    token_url = f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1"
    first_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"
    _, refresh = _sync_token_requests(sync_client, key)
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


async def test_concurrent_requests_share_one_mint(base_url: str, client: HTTPClient) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=3600",
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearers = await asyncio.gather(*(_bearer(api) for _ in range(10)))

    assert bearers == ["Bearer token-1"] * 10
    assert len(await _token_requests(client, key)) == 1


def test_sync_concurrent_requests_share_one_mint(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=3600",
        client_id="client-id",
        client_secret="client-secret",
    )

    with (
        SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api,
        ThreadPoolExecutor(max_workers=10) as pool,
    ):
        bearers = list(pool.map(_sync_bearer, [api] * 10))

    assert bearers == ["Bearer token-1"] * 10
    assert len(_sync_token_requests(sync_client, key)) == 1


async def test_token_endpoint_server_error_is_an_oauth_token_error(base_url: str) -> None:
    token_url = f"{base_url}oauth/token-boom?key={uuid4()}"
    provider = OAuthProvider(
        token_url=token_url, client_id="client-id", client_secret="client-secret"
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        with pytest.raises(OAuthTokenError) as error_info:
            await _bearer(api)

    assert error_info.value.token_url == token_url
    assert isinstance(error_info.value.__cause__, HTTPResponseError)
    assert error_info.value.__cause__.status == 500


def test_sync_token_endpoint_server_error_is_an_oauth_token_error(base_url: str) -> None:
    token_url = f"{base_url}oauth/token-boom?key={uuid4()}"
    provider = SyncOAuthProvider(
        token_url=token_url, client_id="client-id", client_secret="client-secret"
    )

    with (
        SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api,
        pytest.raises(OAuthTokenError) as error_info,
    ):
        _sync_bearer(api)

    assert error_info.value.token_url == token_url
    assert isinstance(error_info.value.__cause__, HTTPResponseError)
    assert error_info.value.__cause__.status == 500


def test_token_refresh_request_without_token_request_is_rejected() -> None:
    with pytest.raises(ValueError, match="'token_refresh_request' requires"):
        OAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_refresh_request=_TokenRefreshRequest,
        )


def test_token_request_without_token_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="both 'token_request' and 'token_response'"):
        OAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_request=_TokenRequest,
        )


def test_token_response_without_token_request_is_rejected() -> None:
    with pytest.raises(ValueError, match="both 'token_request' and 'token_response'"):
        OAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_response=_TokenResponse,
        )


def test_negative_refresh_leeway_is_rejected() -> None:
    with pytest.raises(ValueError, match="'refresh_leeway' must be >= 0"):
        OAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            refresh_leeway=-1.0,
        )


def test_sync_token_request_without_token_response_is_rejected() -> None:
    with pytest.raises(ValueError, match="both 'token_request' and 'token_response'"):
        SyncOAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_request=_TokenRequest,
        )


def test_sync_negative_refresh_leeway_is_rejected() -> None:
    with pytest.raises(ValueError, match="'refresh_leeway' must be >= 0"):
        SyncOAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            refresh_leeway=-1.0,
        )


def test_missing_cache_parent_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="parent directory does not exist"):
        OAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_cache_path=tmp_path / "missing" / "cache.json",
        )


def test_sync_missing_cache_parent_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="parent directory does not exist"):
        SyncOAuthProvider(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_cache_path=tmp_path / "missing" / "cache.json",
        )


async def test_refresh_leeway_is_clamped_to_half_the_token_lifetime(
    base_url: str, client: HTTPClient
) -> None:
    # A 60s token with the default 300s leeway would otherwise be "expired" at birth and minted
    # again on every single call.
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=60",
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)
        concurrent = await asyncio.gather(*(_bearer(api) for _ in range(5)))

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert concurrent == ["Bearer token-1"] * 5
    assert len(await _token_requests(client, key)) == 1


def test_sync_refresh_leeway_is_clamped_to_half_the_token_lifetime(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=60",
        client_id="client-id",
        client_secret="client-secret",
    )

    with (
        SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api,
        ThreadPoolExecutor(max_workers=5) as pool,
    ):
        first = _sync_bearer(api)
        second = _sync_bearer(api)
        concurrent = list(pool.map(_sync_bearer, [api] * 5))

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert concurrent == ["Bearer token-1"] * 5
    assert len(_sync_token_requests(sync_client, key)) == 1


async def test_refresh_response_without_refresh_token_keeps_the_previous_one(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=(
            f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1&refresh_omits_token=1"
        ),
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearers = [await _bearer(api) for _ in range(3)]

    assert bearers == ["Bearer token-1", "Bearer token-2", "Bearer token-3"]
    requests = await _token_requests(client, key)
    assert [r["body"]["grant_type"] for r in requests] == [
        "client_credentials",
        "refresh_token",
        "refresh_token",
    ]
    assert [r["body"]["refresh_token"] for r in requests[1:]] == ["refresh-1", "refresh-1"]


def test_sync_refresh_response_without_refresh_token_keeps_the_previous_one(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=(
            f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1&refresh_omits_token=1"
        ),
        client_id="client-id",
        client_secret="client-secret",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearers = [_sync_bearer(api) for _ in range(3)]

    assert bearers == ["Bearer token-1", "Bearer token-2", "Bearer token-3"]
    requests = _sync_token_requests(sync_client, key)
    assert [r["body"]["grant_type"] for r in requests] == [
        "client_credentials",
        "refresh_token",
        "refresh_token",
    ]
    assert [r["body"]["refresh_token"] for r in requests[1:]] == ["refresh-1", "refresh-1"]


async def test_cached_token_for_a_different_scope_is_ignored(base_url: str, tmp_path: Path) -> None:
    token_url = f"{base_url}oauth/token?key={uuid4()}&expires_in=3600"
    first_provider = OAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        scope="read",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = OAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        scope="read write",
        token_cache_path=tmp_path / "cache.json",
    )
    async with HTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"
    assert json.loads((tmp_path / "cache.json").read_text())["scope"] == "read write"


def test_sync_cached_token_for_a_different_scope_is_ignored(base_url: str, tmp_path: Path) -> None:
    token_url = f"{base_url}oauth/token?key={uuid4()}&expires_in=3600"
    first_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        scope="read",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = SyncOAuthProvider(
        token_url=token_url,
        client_id="client-id",
        client_secret="client-secret",
        scope="read write",
        token_cache_path=tmp_path / "cache.json",
    )
    with SyncHTTPClient.build(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"
    assert json.loads((tmp_path / "cache.json").read_text())["scope"] == "read write"


async def _gather_bearers(base_url: str, provider: OAuthProvider) -> list[str]:
    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        return await asyncio.gather(*(_bearer(api) for _ in range(3)))


def test_provider_can_be_reused_across_event_loops(base_url: str) -> None:
    # A plain `def` on purpose: each `asyncio.run` is its own event loop, and the provider's
    # renewal lock must not stay bound to the first one (`RuntimeError: ... is bound to a
    # different event loop`, reproduced on 3.14 before the lock was made per-loop).
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}&expires_in=0",
        client_id="client-id",
        client_secret="client-secret",
    )

    first = asyncio.run(_gather_bearers(base_url, provider))
    second = asyncio.run(_gather_bearers(base_url, provider))

    # A lifetime-0 token is stale on every call, so each of the six mints (serialized by the
    # lock) — the point is that the second loop's three complete at all.
    assert first + second == [f"Bearer token-{n}" for n in range(1, 7)]


async def test_basic_credentials_are_percent_encoded(base_url: str, client: HTTPClient) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client id",
        client_secret="s3cr3t+/%= :",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        await _bearer(api)

    (request,) = await _token_requests(client, key)
    decoded = base64.b64decode(request["authorization"].removeprefix("Basic ")).decode()
    assert decoded == quote("client id", safe="") + ":" + quote("s3cr3t+/%= :", safe="")


def test_sync_basic_credentials_are_percent_encoded(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client id",
        client_secret="s3cr3t+/%= :",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        _sync_bearer(api)

    (request,) = _sync_token_requests(sync_client, key)
    decoded = base64.b64decode(request["authorization"].removeprefix("Basic ")).decode()
    assert decoded == quote("client id", safe="") + ":" + quote("s3cr3t+/%= :", safe="")


async def test_missing_expires_in_uses_default_expires_in(base_url: str, tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}&omit_expires_in=1",
        client_id="client-id",
        client_secret="client-secret",
        default_expires_in=900.0,
        token_cache_path=cache_path,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(cache_path.read_text())["expires_in"] == 900


def test_sync_missing_expires_in_uses_default_expires_in(base_url: str, tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}&omit_expires_in=1",
        client_id="client-id",
        client_secret="client-secret",
        default_expires_in=900.0,
        token_cache_path=cache_path,
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(cache_path.read_text())["expires_in"] == 900


async def test_missing_expires_in_without_default_is_an_oauth_token_error(base_url: str) -> None:
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}&omit_expires_in=1",
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        with pytest.raises(OAuthTokenError, match="no 'expires_in'") as error_info:
            await _bearer(api)

    assert isinstance(error_info.value.__cause__, ValueError)


def test_sync_missing_expires_in_without_default_is_an_oauth_token_error(base_url: str) -> None:
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={uuid4()}&omit_expires_in=1",
        client_id="client-id",
        client_secret="client-secret",
    )

    with (
        SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api,
        pytest.raises(OAuthTokenError, match="no 'expires_in'") as error_info,
    ):
        _sync_bearer(api)

    assert isinstance(error_info.value.__cause__, ValueError)


async def test_custom_models_missing_expires_in_uses_default_expires_in(
    base_url: str, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={uuid4()}&omit_expires_in=1",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_OptionalExpiresTokenResponse,
        default_expires_in=900.0,
        token_cache_path=cache_path,
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(cache_path.read_text())["expires_in"] == 900


def test_sync_custom_models_missing_expires_in_uses_default_expires_in(
    base_url: str, tmp_path: Path
) -> None:
    cache_path = tmp_path / "cache.json"
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token-custom?key={uuid4()}&omit_expires_in=1",
        client_id="client-id",
        client_secret="client-secret",
        token_request=_MsgspecTokenRequest,
        token_response=_MsgspecOptionalExpiresTokenResponse,
        default_expires_in=900.0,
        token_cache_path=cache_path,
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(cache_path.read_text())["expires_in"] == 900


async def test_refresh_rejected_with_a_non_400_is_not_retried_as_a_mint(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1&refresh_status=429",
        client_id="client-id",
        client_secret="client-secret",
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        with pytest.raises(OAuthTokenError) as error_info:
            await _bearer(api)

    assert first == "Bearer token-1"
    assert isinstance(error_info.value.__cause__, HTTPResponseError)
    assert error_info.value.__cause__.status == 429
    grant_types = [r["body"]["grant_type"] for r in await _token_requests(client, key)]
    assert grant_types == ["client_credentials", "refresh_token"]


def test_sync_refresh_rejected_with_a_non_400_is_not_retried_as_a_mint(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}&expires_in=0&with_refresh=1&refresh_status=429",
        client_id="client-id",
        client_secret="client-secret",
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        with pytest.raises(OAuthTokenError) as error_info:
            _sync_bearer(api)

    assert first == "Bearer token-1"
    assert isinstance(error_info.value.__cause__, HTTPResponseError)
    assert error_info.value.__cause__.status == 429
    grant_types = [r["body"]["grant_type"] for r in _sync_token_requests(sync_client, key)]
    assert grant_types == ["client_credentials", "refresh_token"]


async def test_client_factory_builds_the_token_endpoint_client(
    base_url: str, client: HTTPClient
) -> None:
    key = str(uuid4())
    provider = OAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client-id",
        client_secret="client-secret",
        client_factory=partial(HTTPClient.build, default_headers={"x-factory": "yes"}),
    )

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = await _token_requests(client, key)
    assert request["headers"]["x-factory"] == "yes"


def test_sync_client_factory_builds_the_token_endpoint_client(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    key = str(uuid4())
    provider = SyncOAuthProvider(
        token_url=f"{base_url}oauth/token?key={key}",
        client_id="client-id",
        client_secret="client-secret",
        client_factory=partial(SyncHTTPClient.build, default_headers={"x-factory": "yes"}),
    )

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = _sync_token_requests(sync_client, key)
    assert request["headers"]["x-factory"] == "yes"
