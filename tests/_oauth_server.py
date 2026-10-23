"""Test-side helpers for `_server.py`'s `/oauth/token*` endpoints.

`TokenEndpoint` spells a token endpoint's behaviour as typed fields instead of a hand-written
query string, and `TokenServer` (the `token_server` fixture) binds it to the test's own
`server_key`, builds providers against it, and reads back what the endpoint received.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, Unpack
from urllib.parse import urlencode

from lothc import OAuthProvider, SyncHTTPClient, SyncOAuthProvider


@dataclass(slots=True, kw_only=True)
class TokenEndpoint:
    """One `/oauth/token*` route plus the query options its handler in `_server.py` reads.

    Every option left at its default is omitted from the URL, so the handler's own default
    applies (e.g. a 3600s `expires_in`, no refresh token).
    """

    path: Literal["oauth/token", "oauth/token-custom", "oauth/token-boom"] = "oauth/token"
    expires_in: int | None = None
    omit_expires_in: bool = False
    with_refresh: bool = False
    # A refresh fails with this status; `refresh_fails` is the RFC's own `400 invalid_grant`.
    refresh_status: int | None = None
    refresh_fails: bool = False
    # A refresh response without a `refresh_token` key (RFC 6749 §6); a mint still has one.
    refresh_omits_token: bool = False
    # Every mint after the key's first request fails with this status.
    later_mint_status: int | None = None

    def url(self, base_url: str, key: str) -> str:
        params: dict[str, str | int] = {"key": key}
        # `is not None`, never truthiness: `expires_in=0` (stale at birth) must still be sent.
        if self.expires_in is not None:
            params["expires_in"] = self.expires_in
        if self.omit_expires_in:
            params["omit_expires_in"] = 1
        if self.with_refresh:
            params["with_refresh"] = 1
        if self.refresh_status is not None:
            params["refresh_status"] = self.refresh_status
        if self.refresh_fails:
            params["refresh_fails"] = 1
        if self.refresh_omits_token:
            params["refresh_omits_token"] = 1
        if self.later_mint_status is not None:
            params["later_mint_status"] = self.later_mint_status
        return f"{base_url}{self.path}?{urlencode(params)}"


class ProviderOptions(TypedDict, total=False):
    """The provider settings a test may override; anything more unusual (`token_request=`,
    `client_factory=`, ...) builds its own provider against `TokenServer.url()`."""

    scope: str
    client_auth: Literal["basic", "body"]
    refresh_leeway: float
    default_expires_in: float


@dataclass
class TokenServer:
    """The test server's token endpoint, keyed by the test's own `server_key`."""

    base_url: str
    key: str
    _reader: SyncHTTPClient
    # A per-test directory, for providers built with `cached=True`.
    cache_dir: Path

    def _url(self, endpoint: TokenEndpoint | None) -> str:
        return (endpoint or TokenEndpoint()).url(self.base_url, self.key)

    def _requests(self) -> list[dict[str, Any]]:
        result = self._reader.get("oauth/token-requests", params={"key": self.key}, into=dict)
        return result.data["requests"]

    def _cache_dir_if(self, *, cached: bool) -> Path | None:
        return self.cache_dir if cached else None

    def url(self, endpoint: TokenEndpoint | None = None) -> str:
        return self._url(endpoint)

    def provider(
        self,
        endpoint: TokenEndpoint | None = None,
        *,
        cached: bool = False,
        client_id: str = "client-id",
        client_secret: str = "client-secret",
        **overrides: Unpack[ProviderOptions],
    ) -> OAuthProvider:
        return OAuthProvider(
            token_url=self._url(endpoint),
            client_id=client_id,
            client_secret=client_secret,
            token_cache_dir=self._cache_dir_if(cached=cached),
            **overrides,
        )

    def sync_provider(
        self,
        endpoint: TokenEndpoint | None = None,
        *,
        cached: bool = False,
        client_id: str = "client-id",
        client_secret: str = "client-secret",
        **overrides: Unpack[ProviderOptions],
    ) -> SyncOAuthProvider:
        return SyncOAuthProvider(
            token_url=self._url(endpoint),
            client_id=client_id,
            client_secret=client_secret,
            token_cache_dir=self._cache_dir_if(cached=cached),
            **overrides,
        )

    def requests(self) -> list[dict[str, Any]]:
        """Every request the token endpoint received for this key, oldest first."""
        return self._requests()

    def grant_types(self) -> list[str]:
        return [request["body"]["grant_type"] for request in self._requests()]
