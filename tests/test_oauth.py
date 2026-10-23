import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from urllib.parse import quote

import msgspec
import pytest
from _helpers import echoed_headers
from _oauth_server import TokenEndpoint, TokenServer
from pydantic import BaseModel, ConfigDict, Field

from lothc import (
    HTTPClient,
    HTTPResponseError,
    OAuthProvider,
    OAuthTokenError,
    SyncHTTPClient,
    SyncOAuthProvider,
)


# `validate_by_name` lets lothc construct these by field name. `serialize_by_alias` is no longer
# needed (lothc encodes pydantic models by alias unless a model opts out), but kept here to show
# an explicit `True` is honoured too.
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


# No `refresh_token` field at all — an API that never issues one shouldn't force one onto the
# model just to satisfy `token_response=`.
class _NoRefreshFieldTokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    expires_in: int = Field(alias="expiresInSeconds")


async def _bearer(client: HTTPClient) -> str:
    return echoed_headers((await client.get("echo-headers", into=dict)).data)["authorization"]


def _sync_bearer(client: SyncHTTPClient) -> str:
    return echoed_headers(client.get("echo-headers", into=dict).data)["authorization"]


def _cache_file(cache_dir: Path) -> Path:
    """The one token cache file a single provider writes into `cache_dir`."""
    (cache_file,) = cache_dir.glob("*.json")
    return cache_file


def _basic(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()


type ProviderClass = type[OAuthProvider] | type[SyncOAuthProvider]


async def test_rfc_mint_sends_bearer_token_and_basic_credentials(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(TokenEndpoint(expires_in=3600), scope="scope")

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["content_type"] == "application/x-www-form-urlencoded"
    assert request["authorization"] == _basic("client-id", "client-secret")
    assert request["body"] == {"grant_type": "client_credentials", "scope": "scope"}


def test_sync_rfc_mint_sends_bearer_token_and_basic_credentials(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(TokenEndpoint(expires_in=3600), scope="scope")

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["content_type"] == "application/x-www-form-urlencoded"
    assert request["authorization"] == _basic("client-id", "client-secret")
    assert request["body"] == {"grant_type": "client_credentials", "scope": "scope"}


async def test_rfc_client_auth_body_puts_credentials_in_form(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(client_auth="body")

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["authorization"] is None
    assert request["body"] == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }


def test_sync_rfc_client_auth_body_puts_credentials_in_form(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(client_auth="body")

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["authorization"] is None
    assert request["body"] == {
        "grant_type": "client_credentials",
        "client_id": "client-id",
        "client_secret": "client-secret",
    }


async def test_valid_token_is_reused_across_requests(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(TokenEndpoint(expires_in=3600))

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert len(token_server.requests()) == 1


def test_sync_valid_token_is_reused_across_requests(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(TokenEndpoint(expires_in=3600))

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert len(token_server.requests()) == 1


async def test_token_inside_leeway_window_is_minted_again_without_refresh_token(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(TokenEndpoint(expires_in=0))

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    assert token_server.grant_types() == ["client_credentials", "client_credentials"]


def test_sync_token_inside_leeway_window_is_minted_again_without_refresh_token(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(TokenEndpoint(expires_in=0))

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    assert token_server.grant_types() == ["client_credentials", "client_credentials"]


async def test_token_inside_leeway_window_is_refreshed_with_refresh_token(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(TokenEndpoint(expires_in=0, with_refresh=True))

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = token_server.requests()
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert refresh["authorization"] == _basic("client-id", "client-secret")


def test_sync_token_inside_leeway_window_is_refreshed_with_refresh_token(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(TokenEndpoint(expires_in=0, with_refresh=True))

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = token_server.requests()
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert refresh["authorization"] == _basic("client-id", "client-secret")


async def test_rejected_refresh_falls_back_to_minting(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_fails=True)
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-3")
    assert token_server.grant_types() == [
        "client_credentials",
        "refresh_token",
        "client_credentials",
    ]


def test_sync_rejected_refresh_falls_back_to_minting(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_fails=True)
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-3")
    assert token_server.grant_types() == [
        "client_credentials",
        "refresh_token",
        "client_credentials",
    ]


async def test_custom_pydantic_models_send_json_token_request(
    base_url: str, token_server: TokenServer
) -> None:
    provider = OAuthProvider(
        token_url=token_server.url(TokenEndpoint(path="oauth/token-custom")),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None
    assert request["body"] == {
        "clientID": "client-id",
        "clientSecret": "client-secret",
        "grantType": "client_credentials",
    }


def test_sync_custom_pydantic_models_send_json_token_request(
    base_url: str, token_server: TokenServer
) -> None:
    provider = SyncOAuthProvider(
        token_url=token_server.url(TokenEndpoint(path="oauth/token-custom")),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None
    assert request["body"] == {
        "clientID": "client-id",
        "clientSecret": "client-secret",
        "grantType": "client_credentials",
    }


async def test_custom_msgspec_models_send_json_token_request(
    base_url: str, token_server: TokenServer
) -> None:
    provider = OAuthProvider(
        token_url=token_server.url(TokenEndpoint(path="oauth/token-custom")),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_MsgspecTokenRequest,
        token_response=_MsgspecTokenResponse,
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["content_type"] == "application/json"
    assert request["authorization"] is None
    assert request["body"] == {
        "clientID": "client-id",
        "clientSecret": "client-secret",
        "grantType": "client_credentials",
    }


async def test_custom_pydantic_model_with_no_refresh_token_field_works(
    base_url: str, token_server: TokenServer
) -> None:
    provider = OAuthProvider(
        token_url=token_server.url(TokenEndpoint(path="oauth/token-custom")),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_NoRefreshFieldTokenResponse,
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"


def test_sync_custom_pydantic_model_with_no_refresh_token_field_works(
    base_url: str, token_server: TokenServer
) -> None:
    provider = SyncOAuthProvider(
        token_url=token_server.url(TokenEndpoint(path="oauth/token-custom")),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_NoRefreshFieldTokenResponse,
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"


async def test_custom_models_refresh_with_token_refresh_request(
    base_url: str, token_server: TokenServer
) -> None:
    provider = OAuthProvider(
        token_url=token_server.url(
            TokenEndpoint(path="oauth/token-custom", expires_in=0, with_refresh=True)
        ),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_refresh_request=_TokenRefreshRequest,
        token_response=_TokenResponse,
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = token_server.requests()
    assert refresh["body"] == {"refreshToken": "refresh-1", "grantType": "refresh_token"}


def test_sync_custom_models_refresh_with_token_refresh_request(
    base_url: str, token_server: TokenServer
) -> None:
    provider = SyncOAuthProvider(
        token_url=token_server.url(
            TokenEndpoint(path="oauth/token-custom", expires_in=0, with_refresh=True)
        ),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_MsgspecTokenRequest,
        token_refresh_request=_MsgspecTokenRefreshRequest,
        token_response=_MsgspecTokenResponse,
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, refresh = token_server.requests()
    assert refresh["body"] == {"refreshToken": "refresh-1", "grantType": "refresh_token"}


async def test_custom_models_without_token_refresh_request_mint_again(
    base_url: str, token_server: TokenServer
) -> None:
    provider = OAuthProvider(
        token_url=token_server.url(
            TokenEndpoint(path="oauth/token-custom", expires_in=0, with_refresh=True)
        ),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, renewal = token_server.requests()
    assert renewal["body"]["clientID"] == "client-id"
    assert "refreshToken" not in renewal["body"]


def test_sync_custom_models_without_token_refresh_request_mint_again(
    base_url: str, token_server: TokenServer
) -> None:
    provider = SyncOAuthProvider(
        token_url=token_server.url(
            TokenEndpoint(path="oauth/token-custom", expires_in=0, with_refresh=True)
        ),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_TokenResponse,
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-2")
    _, renewal = token_server.requests()
    assert renewal["body"]["clientID"] == "client-id"
    assert "refreshToken" not in renewal["body"]


async def test_token_cache_dir_writes_token_file_with_owner_only_permissions(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(cached=True)

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        await _bearer(api)

    assert _cache_file(token_server.cache_dir).stat().st_mode & 0o777 == 0o600
    cached = json.loads(_cache_file(token_server.cache_dir).read_text())
    assert cached["access_token"] == "token-1"
    assert cached["client_id"] == "client-id"


def test_sync_token_cache_dir_writes_token_file_with_owner_only_permissions(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(cached=True)

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        _sync_bearer(api)

    assert _cache_file(token_server.cache_dir).stat().st_mode & 0o777 == 0o600
    cached = json.loads(_cache_file(token_server.cache_dir).read_text())
    assert cached["access_token"] == "token-1"
    assert cached["client_id"] == "client-id"


async def test_cached_token_is_reused_by_a_new_provider(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.provider(endpoint, cached=True)
    async with HTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = token_server.provider(endpoint, cached=True)
    async with HTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    assert len(token_server.requests()) == 1


def test_sync_cached_token_is_reused_by_a_new_provider(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.sync_provider(endpoint, cached=True)
    with SyncHTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = token_server.sync_provider(endpoint, cached=True)
    with SyncHTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    assert len(token_server.requests()) == 1


async def test_cached_token_for_a_different_client_id_is_ignored(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.provider(endpoint, cached=True)
    async with HTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = token_server.provider(endpoint, cached=True, client_id="other-client-id")
    async with HTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"


def test_sync_cached_token_for_a_different_client_id_is_ignored(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.sync_provider(endpoint, cached=True)
    with SyncHTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = token_server.sync_provider(endpoint, cached=True, client_id="other-client-id")
    with SyncHTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"


async def test_corrupt_cache_file_is_ignored_and_overwritten(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.provider(endpoint, cached=True)
    async with HTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)
    _cache_file(token_server.cache_dir).write_bytes(b"not json")

    second_provider = token_server.provider(endpoint, cached=True)
    async with HTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["access_token"] == "token-2"


def test_sync_corrupt_cache_file_is_ignored_and_overwritten(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.sync_provider(endpoint, cached=True)
    with SyncHTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)
    _cache_file(token_server.cache_dir).write_bytes(b"not json")

    second_provider = token_server.sync_provider(endpoint, cached=True)
    with SyncHTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["access_token"] == "token-2"


async def test_cached_refresh_token_is_used_by_a_new_provider_inside_leeway_window(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=0, with_refresh=True)
    first_provider = token_server.provider(endpoint, cached=True)
    async with HTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = token_server.provider(endpoint, cached=True)
    async with HTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"
    _, refresh = token_server.requests()
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


def test_sync_cached_refresh_token_is_used_by_a_new_provider_inside_leeway_window(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=0, with_refresh=True)
    first_provider = token_server.sync_provider(endpoint, cached=True)
    with SyncHTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = token_server.sync_provider(endpoint, cached=True)
    with SyncHTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"
    _, refresh = token_server.requests()
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


async def test_concurrent_requests_share_one_mint(base_url: str, token_server: TokenServer) -> None:
    provider = token_server.provider(TokenEndpoint(expires_in=3600))

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearers = await asyncio.gather(*(_bearer(api) for _ in range(10)))

    assert bearers == ["Bearer token-1"] * 10
    assert len(token_server.requests()) == 1


def test_sync_concurrent_requests_share_one_mint(base_url: str, token_server: TokenServer) -> None:
    provider = token_server.sync_provider(TokenEndpoint(expires_in=3600))

    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api,
        ThreadPoolExecutor(max_workers=10) as pool,
    ):
        bearers = list(pool.map(_sync_bearer, [api] * 10))

    assert bearers == ["Bearer token-1"] * 10
    assert len(token_server.requests()) == 1


async def test_token_endpoint_server_error_is_an_oauth_token_error(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(path="oauth/token-boom")
    provider = token_server.provider(endpoint)

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        with pytest.raises(OAuthTokenError) as error_info:
            await _bearer(api)

    assert error_info.value.token_url == token_server.url(endpoint)
    cause = error_info.value.__cause__
    assert isinstance(cause, HTTPResponseError)
    assert cause.status == 500


def test_sync_token_endpoint_server_error_is_an_oauth_token_error(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(path="oauth/token-boom")
    provider = token_server.sync_provider(endpoint)

    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api,
        pytest.raises(OAuthTokenError) as error_info,
    ):
        _sync_bearer(api)

    assert error_info.value.token_url == token_server.url(endpoint)
    cause = error_info.value.__cause__
    assert isinstance(cause, HTTPResponseError)
    assert cause.status == 500


@pytest.mark.parametrize(
    "provider_class", [OAuthProvider, SyncOAuthProvider], ids=["async", "sync"]
)
def test_token_refresh_request_without_token_request_is_rejected(
    provider_class: ProviderClass,
) -> None:
    with pytest.raises(ValueError, match="'token_refresh_request' requires"):
        provider_class(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_refresh_request=_TokenRefreshRequest,
        )


@pytest.mark.parametrize(
    "provider_class", [OAuthProvider, SyncOAuthProvider], ids=["async", "sync"]
)
def test_token_request_without_token_response_is_rejected(provider_class: ProviderClass) -> None:
    with pytest.raises(ValueError, match="both 'token_request' and 'token_response'"):
        provider_class(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_request=_TokenRequest,
        )


@pytest.mark.parametrize(
    "provider_class", [OAuthProvider, SyncOAuthProvider], ids=["async", "sync"]
)
def test_token_response_without_token_request_is_rejected(provider_class: ProviderClass) -> None:
    with pytest.raises(ValueError, match="both 'token_request' and 'token_response'"):
        provider_class(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_response=_TokenResponse,
        )


@pytest.mark.parametrize(
    "provider_class", [OAuthProvider, SyncOAuthProvider], ids=["async", "sync"]
)
def test_negative_refresh_leeway_is_rejected(provider_class: ProviderClass) -> None:
    with pytest.raises(ValueError, match="'refresh_leeway' must be >= 0"):
        provider_class(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            refresh_leeway=-1.0,
        )


@pytest.mark.parametrize(
    "provider_class", [OAuthProvider, SyncOAuthProvider], ids=["async", "sync"]
)
def test_missing_cache_directory_is_rejected(provider_class: ProviderClass, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="token_cache_dir does not exist"):
        provider_class(
            token_url="token-url",
            client_id="client-id",
            client_secret="client-secret",
            token_cache_dir=tmp_path / "missing",
        )


async def test_refresh_leeway_is_clamped_to_half_the_token_lifetime(
    base_url: str, token_server: TokenServer
) -> None:
    # A 60s token with the default 300s leeway would otherwise be "expired" at birth and minted
    # again on every single call.
    provider = token_server.provider(TokenEndpoint(expires_in=60))

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)
        concurrent = await asyncio.gather(*(_bearer(api) for _ in range(5)))

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert concurrent == ["Bearer token-1"] * 5
    assert len(token_server.requests()) == 1


def test_sync_refresh_leeway_is_clamped_to_half_the_token_lifetime(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(TokenEndpoint(expires_in=60))

    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api,
        ThreadPoolExecutor(max_workers=5) as pool,
    ):
        first = _sync_bearer(api)
        second = _sync_bearer(api)
        concurrent = list(pool.map(_sync_bearer, [api] * 5))

    assert (first, second) == ("Bearer token-1", "Bearer token-1")
    assert concurrent == ["Bearer token-1"] * 5
    assert len(token_server.requests()) == 1


async def test_refresh_response_without_refresh_token_keeps_the_previous_one(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_omits_token=True)
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearers = [await _bearer(api) for _ in range(3)]

    assert bearers == ["Bearer token-1", "Bearer token-2", "Bearer token-3"]
    requests = token_server.requests()
    assert [r["body"]["grant_type"] for r in requests] == [
        "client_credentials",
        "refresh_token",
        "refresh_token",
    ]
    assert [r["body"]["refresh_token"] for r in requests[1:]] == ["refresh-1", "refresh-1"]


def test_sync_refresh_response_without_refresh_token_keeps_the_previous_one(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_omits_token=True)
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearers = [_sync_bearer(api) for _ in range(3)]

    assert bearers == ["Bearer token-1", "Bearer token-2", "Bearer token-3"]
    requests = token_server.requests()
    assert [r["body"]["grant_type"] for r in requests] == [
        "client_credentials",
        "refresh_token",
        "refresh_token",
    ]
    assert [r["body"]["refresh_token"] for r in requests[1:]] == ["refresh-1", "refresh-1"]


async def test_cached_token_for_a_different_scope_is_ignored(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.provider(endpoint, cached=True, scope="read")
    async with HTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        await _bearer(api)

    second_provider = token_server.provider(endpoint, cached=True, scope="read write")
    async with HTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"
    scopes = sorted(
        json.loads(path.read_text())["scope"] for path in token_server.cache_dir.glob("*.json")
    )
    assert scopes == ["read", "read write"]


def test_sync_cached_token_for_a_different_scope_is_ignored(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.sync_provider(endpoint, cached=True, scope="read")
    with SyncHTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        _sync_bearer(api)

    second_provider = token_server.sync_provider(endpoint, cached=True, scope="read write")
    with SyncHTTPClient(base_url=base_url, bearer_auth=second_provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"
    scopes = sorted(
        json.loads(path.read_text())["scope"] for path in token_server.cache_dir.glob("*.json")
    )
    assert scopes == ["read", "read write"]


async def _gather_bearers(base_url: str, provider: OAuthProvider) -> list[str]:
    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        return await asyncio.gather(*(_bearer(api) for _ in range(3)))


def test_provider_can_be_reused_across_event_loops(
    base_url: str, token_server: TokenServer
) -> None:
    # A plain `def` on purpose: each `asyncio.run` is its own event loop, and the provider's
    # renewal lock must not stay bound to the first one (`RuntimeError: ... is bound to a
    # different event loop`, reproduced on 3.14 before the lock was made per-loop).
    provider = token_server.provider(TokenEndpoint(expires_in=0))

    first = asyncio.run(_gather_bearers(base_url, provider))
    second = asyncio.run(_gather_bearers(base_url, provider))

    # A lifetime-0 token is stale on every call, so each of the six mints (serialized by the
    # lock) — the point is that the second loop's three complete at all.
    assert first + second == [f"Bearer token-{n}" for n in range(1, 7)]


async def test_basic_credentials_are_percent_encoded(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(client_id="client id", client_secret="s3cr3t+/%= :")

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        await _bearer(api)

    (request,) = token_server.requests()
    decoded = base64.b64decode(request["authorization"].removeprefix("Basic ")).decode()
    assert decoded == quote("client id", safe="") + ":" + quote("s3cr3t+/%= :", safe="")


def test_sync_basic_credentials_are_percent_encoded(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(client_id="client id", client_secret="s3cr3t+/%= :")

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        _sync_bearer(api)

    (request,) = token_server.requests()
    decoded = base64.b64decode(request["authorization"].removeprefix("Basic ")).decode()
    assert decoded == quote("client id", safe="") + ":" + quote("s3cr3t+/%= :", safe="")


async def test_missing_expires_in_uses_default_expires_in(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(
        TokenEndpoint(omit_expires_in=True), cached=True, default_expires_in=900.0
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["expires_in"] == 900


def test_sync_missing_expires_in_uses_default_expires_in(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(
        TokenEndpoint(omit_expires_in=True), cached=True, default_expires_in=900.0
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["expires_in"] == 900


async def test_missing_expires_in_without_default_is_an_oauth_token_error(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(TokenEndpoint(omit_expires_in=True))

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        with pytest.raises(OAuthTokenError, match="no 'expires_in'") as error_info:
            await _bearer(api)

    assert isinstance(error_info.value.__cause__, ValueError)


def test_sync_missing_expires_in_without_default_is_an_oauth_token_error(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(TokenEndpoint(omit_expires_in=True))

    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api,
        pytest.raises(OAuthTokenError, match="no 'expires_in'") as error_info,
    ):
        _sync_bearer(api)

    assert isinstance(error_info.value.__cause__, ValueError)


async def test_custom_models_missing_expires_in_uses_default_expires_in(
    base_url: str, token_server: TokenServer
) -> None:
    provider = OAuthProvider(
        token_url=token_server.url(TokenEndpoint(path="oauth/token-custom", omit_expires_in=True)),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_TokenRequest,
        token_response=_OptionalExpiresTokenResponse,
        default_expires_in=900.0,
        token_cache_dir=token_server.cache_dir,
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["expires_in"] == 900


def test_sync_custom_models_missing_expires_in_uses_default_expires_in(
    base_url: str, token_server: TokenServer
) -> None:
    provider = SyncOAuthProvider(
        token_url=token_server.url(TokenEndpoint(path="oauth/token-custom", omit_expires_in=True)),
        client_id="client-id",
        client_secret="client-secret",
        token_request=_MsgspecTokenRequest,
        token_response=_MsgspecOptionalExpiresTokenResponse,
        default_expires_in=900.0,
        token_cache_dir=token_server.cache_dir,
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["expires_in"] == 900


async def test_refresh_rejected_with_neither_400_nor_401_is_not_retried_as_a_mint(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_status=429)
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        with pytest.raises(OAuthTokenError) as error_info:
            await _bearer(api)

    assert first == "Bearer token-1"
    cause = error_info.value.__cause__
    assert isinstance(cause, HTTPResponseError)
    assert cause.status == 429
    assert token_server.grant_types() == ["client_credentials", "refresh_token"]


def test_sync_refresh_rejected_with_neither_400_nor_401_is_not_retried_as_a_mint(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_status=429)
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        with pytest.raises(OAuthTokenError) as error_info:
            _sync_bearer(api)

    assert first == "Bearer token-1"
    cause = error_info.value.__cause__
    assert isinstance(cause, HTTPResponseError)
    assert cause.status == 429
    assert token_server.grant_types() == ["client_credentials", "refresh_token"]


# RFC 6749 says 400 `invalid_grant`, but some servers answer a revoked refresh token with a 401;
# raising on it would leave every process loading the disk cache stuck on the same refresh token.
async def test_refresh_rejected_with_a_401_falls_back_to_minting(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_status=401)
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = await _bearer(api)
        second = await _bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-3")
    assert token_server.grant_types() == [
        "client_credentials",
        "refresh_token",
        "client_credentials",
    ]


def test_sync_refresh_rejected_with_a_401_falls_back_to_minting(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_status=401)
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        first = _sync_bearer(api)
        second = _sync_bearer(api)

    assert (first, second) == ("Bearer token-1", "Bearer token-3")
    assert token_server.grant_types() == [
        "client_credentials",
        "refresh_token",
        "client_credentials",
    ]


async def test_a_401_from_the_mint_after_a_refresh_401_is_an_oauth_token_error(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_status=401, later_mint_status=401)
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        await _bearer(api)
        with pytest.raises(OAuthTokenError) as error_info:
            await _bearer(api)

    mint_error = error_info.value.__cause__
    assert isinstance(mint_error, HTTPResponseError)
    refresh_error = mint_error.__cause__  # the rejected refresh, chained onto the failed mint
    assert isinstance(refresh_error, HTTPResponseError)
    assert (mint_error.status, refresh_error.status) == (401, 401)
    assert token_server.grant_types() == [
        "client_credentials",
        "refresh_token",
        "client_credentials",
    ]


def test_sync_a_401_from_the_mint_after_a_refresh_401_is_an_oauth_token_error(
    base_url: str, token_server: TokenServer
) -> None:
    provider = token_server.sync_provider(
        TokenEndpoint(expires_in=0, with_refresh=True, refresh_status=401, later_mint_status=401)
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        _sync_bearer(api)
        with pytest.raises(OAuthTokenError) as error_info:
            _sync_bearer(api)

    mint_error = error_info.value.__cause__
    assert isinstance(mint_error, HTTPResponseError)
    refresh_error = mint_error.__cause__  # the rejected refresh, chained onto the failed mint
    assert isinstance(refresh_error, HTTPResponseError)
    assert (mint_error.status, refresh_error.status) == (401, 401)
    assert token_server.grant_types() == [
        "client_credentials",
        "refresh_token",
        "client_credentials",
    ]


async def test_client_factory_builds_the_token_endpoint_client(
    base_url: str, token_server: TokenServer
) -> None:
    provider = OAuthProvider(
        token_url=token_server.url(),
        client_id="client-id",
        client_secret="client-secret",
        client_factory=partial(HTTPClient, default_headers={"x-factory": "yes"}),
    )

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["headers"]["x-factory"] == "yes"


def test_sync_client_factory_builds_the_token_endpoint_client(
    base_url: str, token_server: TokenServer
) -> None:
    provider = SyncOAuthProvider(
        token_url=token_server.url(),
        client_id="client-id",
        client_secret="client-secret",
        client_factory=partial(SyncHTTPClient, default_headers={"x-factory": "yes"}),
    )

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-1"
    (request,) = token_server.requests()
    assert request["headers"]["x-factory"] == "yes"


async def test_failing_to_write_the_token_cache_warns_but_still_uses_the_token(
    base_url: str, token_server: TokenServer
) -> None:
    # The token was obtained; only persisting it failed, and the cache "adds persistence only",
    # so the caller's own request must still go through rather than fail on a full disk.
    provider = token_server.provider(cached=True)
    token_server.cache_dir.chmod(0o500)  # no longer writable, but it still existed at construction
    try:
        async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
            with pytest.warns(RuntimeWarning, match="Could not write the OAuth token cache"):
                authorization = await _bearer(api)
    finally:
        token_server.cache_dir.chmod(0o700)

    assert authorization == "Bearer token-1"
    assert list(token_server.cache_dir.iterdir()) == []


async def test_providers_sharing_a_cache_dir_get_their_own_readable_files(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    for client_id in ("first-client", "second/client"):
        provider = token_server.provider(endpoint, cached=True, client_id=client_id)
        async with HTTPClient(base_url=base_url, bearer_auth=provider) as api:
            await _bearer(api)

    names = sorted(path.name for path in token_server.cache_dir.glob("*.json"))
    # Host and client ID first, so a person can tell the files apart; the client ID is made safe
    # for a filename; a hash suffix keeps them unique.
    assert [name.rsplit("-", 1)[0] for name in names] == [
        "127.0.0.1-first-client",
        "127.0.0.1-second_client",
    ]
    client_ids = sorted(
        json.loads((token_server.cache_dir / name).read_text())["client_id"] for name in names
    )
    assert client_ids == ["first-client", "second/client"]


# A token revoked before it expires used to stay in the cache file: a POST's 401 is never retried,
# so every new process reloaded the dead token and failed until it expired naturally.


def _damage_cache_file(cache_file: Path, damage: str) -> None:
    match damage:
        case "corrupt":
            cache_file.write_bytes(b"not json")
        case "unreadable":
            cache_file.chmod(0o000)
        case _:
            cache_file.unlink()


async def test_a_cached_token_rejected_by_a_post_is_not_reloaded_by_a_new_provider(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(endpoint, cached=True)
    ) as api:
        with pytest.raises(HTTPResponseError) as exc_info:
            await api.post("rejects-first-token", json={"a": 1})

    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(endpoint, cached=True)
    ) as api:
        result = (await api.post("rejects-first-token", json={"a": 1}, into=dict)).data

    assert exc_info.value.status == 401
    assert result == {"authorization": "Bearer token-2"}
    assert len(token_server.requests()) == 2


def test_sync_a_cached_token_rejected_by_a_post_is_not_reloaded_by_a_new_provider(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    with (
        SyncHTTPClient(
            base_url=base_url, bearer_auth=token_server.sync_provider(endpoint, cached=True)
        ) as api,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        api.post("rejects-first-token", json={"a": 1})

    with SyncHTTPClient(
        base_url=base_url, bearer_auth=token_server.sync_provider(endpoint, cached=True)
    ) as api:
        result = api.post("rejects-first-token", json={"a": 1}, into=dict).data

    assert exc_info.value.status == 401
    assert result == {"authorization": "Bearer token-2"}
    assert len(token_server.requests()) == 2


async def test_a_rejected_cached_token_keeps_its_refresh_token_for_a_new_provider(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600, with_refresh=True)
    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(endpoint, cached=True)
    ) as api:
        with pytest.raises(HTTPResponseError):
            await api.post("rejects-first-token", json={"a": 1})

    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(endpoint, cached=True)
    ) as api:
        result = (await api.post("rejects-first-token", json={"a": 1}, into=dict)).data

    assert result == {"authorization": "Bearer token-2"}
    _, refresh = token_server.requests()
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


def test_sync_a_rejected_cached_token_keeps_its_refresh_token_for_a_new_provider(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600, with_refresh=True)
    with (
        SyncHTTPClient(
            base_url=base_url, bearer_auth=token_server.sync_provider(endpoint, cached=True)
        ) as api,
        pytest.raises(HTTPResponseError),
    ):
        api.post("rejects-first-token", json={"a": 1})

    with SyncHTTPClient(
        base_url=base_url, bearer_auth=token_server.sync_provider(endpoint, cached=True)
    ) as api:
        result = api.post("rejects-first-token", json={"a": 1}, into=dict).data

    assert result == {"authorization": "Bearer token-2"}
    _, refresh = token_server.requests()
    assert refresh["body"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}


async def test_a_rejection_leaves_a_cache_file_another_provider_already_renewed(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.provider(endpoint, cached=True)
    assert await first_provider() == "token-1"
    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(endpoint, cached=True)
    ) as api:
        await api.get("rejects-first-token")  # 401, re-mints `token-2` and caches it

    async with HTTPClient(base_url=base_url, bearer_auth=first_provider) as api:
        with pytest.raises(HTTPResponseError):
            await api.post("rejects-first-token", json={"a": 1})  # still sends `token-1`
    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(endpoint, cached=True)
    ) as api:
        bearer = await _bearer(api)

    assert bearer == "Bearer token-2"
    assert len(token_server.requests()) == 2


def test_sync_a_rejection_leaves_a_cache_file_another_provider_already_renewed(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    first_provider = token_server.sync_provider(endpoint, cached=True)
    assert first_provider() == "token-1"
    with SyncHTTPClient(
        base_url=base_url, bearer_auth=token_server.sync_provider(endpoint, cached=True)
    ) as api:
        api.get("rejects-first-token")  # 401, re-mints `token-2` and caches it

    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=first_provider) as api,
        pytest.raises(HTTPResponseError),
    ):
        api.post("rejects-first-token", json={"a": 1})  # still sends `token-1`
    with SyncHTTPClient(
        base_url=base_url, bearer_auth=token_server.sync_provider(endpoint, cached=True)
    ) as api:
        bearer = _sync_bearer(api)

    assert bearer == "Bearer token-2"
    assert len(token_server.requests()) == 2


async def test_a_cached_token_rejected_twice_by_a_get_is_retried_only_once(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    async with HTTPClient(
        base_url=base_url, bearer_auth=token_server.provider(endpoint, cached=True)
    ) as api:
        with pytest.raises(HTTPResponseError) as exc_info:
            await api.get("always-unauthorized", params={"key": f"{token_server.key}-api"})

    assert exc_info.value.error == {"hits": 2}  # the original request plus exactly one retry
    assert len(token_server.requests()) == 2


def test_sync_a_cached_token_rejected_twice_by_a_get_is_retried_only_once(
    base_url: str, token_server: TokenServer
) -> None:
    endpoint = TokenEndpoint(expires_in=3600)
    with (
        SyncHTTPClient(
            base_url=base_url, bearer_auth=token_server.sync_provider(endpoint, cached=True)
        ) as api,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        api.get("always-unauthorized", params={"key": f"{token_server.key}-api"})

    assert exc_info.value.error == {"hits": 2}  # the original request plus exactly one retry
    assert len(token_server.requests()) == 2


@pytest.mark.parametrize("damage", ["corrupt", "unreadable", "missing"])
async def test_invalidate_ignores_an_unusable_cache_file(
    token_server: TokenServer, damage: str
) -> None:
    provider = token_server.provider(TokenEndpoint(expires_in=3600), cached=True)
    assert await provider() == "token-1"
    _damage_cache_file(_cache_file(token_server.cache_dir), damage)

    provider.invalidate()

    assert await provider() == "token-2"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["access_token"] == "token-2"


@pytest.mark.parametrize("damage", ["corrupt", "unreadable", "missing"])
def test_sync_invalidate_ignores_an_unusable_cache_file(
    token_server: TokenServer, damage: str
) -> None:
    provider = token_server.sync_provider(TokenEndpoint(expires_in=3600), cached=True)
    assert provider() == "token-1"
    _damage_cache_file(_cache_file(token_server.cache_dir), damage)

    provider.invalidate()

    assert provider() == "token-2"
    assert json.loads(_cache_file(token_server.cache_dir).read_text())["access_token"] == "token-2"


async def test_failing_to_mark_the_token_cache_expired_warns_rather_than_raises(
    token_server: TokenServer,
) -> None:
    provider = token_server.provider(TokenEndpoint(expires_in=3600), cached=True)
    assert await provider() == "token-1"
    token_server.cache_dir.chmod(0o500)  # the file can't be replaced any more
    try:
        with pytest.warns(RuntimeWarning, match="Could not mark the OAuth token cache"):
            provider.invalidate()
    finally:
        token_server.cache_dir.chmod(0o700)

    assert await provider() == "token-2"
