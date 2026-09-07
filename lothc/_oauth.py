"""OAuth 2 client-credentials token providers, for `HTTPClient.build(bearer_auth=...)`.

A utility built ON the client, not a split of it: `OAuthProvider`/`SyncOAuthProvider` are just
objects whose `__call__` returns a valid access token, which is exactly what the clients'
`bearer_auth` slot already accepts (`AuthProvider`/`SyncAuthProvider`). Minting/refreshing goes
through a fresh, short-lived `HTTPClient`/`SyncHTTPClient` per token request.
"""

import asyncio
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from json import dumps as _json_dumps
from json import loads as _json_loads
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlencode

from ._client import HTTPClient, HTTPResponseError, JSONPayload, SyncHTTPClient
from ._compat import BaseModel, Struct, msgspec

# `token_request=`/`token_refresh_request=` accept a *class* — a pydantic `BaseModel` or msgspec
# `Struct` subclass — that lothc constructs as `Cls(client_id=..., client_secret=...)` /
# `Cls(refresh_token=...)` and sends as `json=`; any other field it declares needs a default. Wire
# names are the model's business (`Field(alias=...)`/`msgspec.field(name=...)`); lothc relies on
# the attribute names only.
#
# These are `Callable[..., JSONPayload]`, not `Protocol`s spelling out those exact keyword
# parameters, deliberately: a `__call__(self, *, client_id: str, client_secret: str)` Protocol was
# tried first and confirmed live to accept a msgspec `Struct` on all four checkers but to reject an
# *aliased* pydantic model on three of them (basedpyright, mypy, zuban — ty accepted it), e.g.
# basedpyright: `Type "type[_TokenRequest]" is not assignable to type "(*, client_id: str,
# client_secret: str) -> JSONPayload" / Extra parameter "clientID" / Missing keyword parameter
# "client_id"`. pydantic's `Field(alias=...)` is a `dataclass_transform` field specifier, so the
# checkers synthesize the constructor with the *alias* as the parameter name, even though
# `validate_by_name=True` makes `Cls(client_id=...)` work at runtime. Since an aliased request
# model is the whole point of the model path, the precise Protocol would have been wrong for the
# main use case; `...` keeps the return-type check (the instance must be a valid `json=` payload)
# and leaves the constructor kwargs to the runtime, where pydantic/msgspec raise on a mismatch.
type TokenRequestTyping = Callable[..., JSONPayload]
type TokenRefreshRequestTyping = Callable[..., JSONPayload]


class TokenResponseTyping(Protocol):
    """What `token_response=` accepts (as a class): a decode target exposing `.access_token`,
    `.expires_in` (seconds) and `.refresh_token` (`None` when the server issued none)."""

    @property
    def access_token(self) -> str: ...
    @property
    def expires_in(self) -> int: ...
    @property
    def refresh_token(self) -> str | None: ...


@dataclass(slots=True)
class _CachedToken:
    access_token: str
    refresh_token: str | None
    # Wall-clock epoch seconds (`time.time()`), never `monotonic()`: this is persisted to the
    # token cache file and read back by a later process, where a monotonic reading is meaningless.
    expires_at: float


def _fresh_token(
    token: _CachedToken | None, refresh_leeway: float, now: float
) -> _CachedToken | None:
    """The cached token if it still has more than `refresh_leeway` seconds left, else `None`.

    >>> token = _CachedToken("access-token", None, expires_at=1000.0)
    >>> _fresh_token(token, refresh_leeway=300.0, now=699.0) is token
    True
    >>> _fresh_token(token, refresh_leeway=300.0, now=700.0) is None
    True
    >>> _fresh_token(None, refresh_leeway=300.0, now=0.0) is None
    True
    """
    if token is None or now >= token.expires_at - refresh_leeway:
        return None
    return token


def _token_from_expires_in(
    access_token: str, expires_in: int, refresh_token: str | None, now: float
) -> _CachedToken:
    """`expires_in` is relative to when the server answered, so it's pinned to an absolute
    instant once, at receipt.

    >>> _token_from_expires_in("access-token", 3600, None, now=1000.0)
    _CachedToken(access_token='access-token', refresh_token=None, expires_at=4600.0)
    """
    return _CachedToken(access_token, refresh_token, now + expires_in)


def _token_from_rfc_payload(payload: dict[str, Any], now: float) -> _CachedToken:
    """Standard RFC 6749 §5.1 token response. `access_token`/`expires_in` are required (a
    `KeyError` says which one the server left out); `refresh_token` is genuinely optional.

    >>> _token_from_rfc_payload({"access_token": "a", "expires_in": "60"}, now=100.0)
    _CachedToken(access_token='a', refresh_token=None, expires_at=160.0)
    >>> payload = {"access_token": "a", "expires_in": 60, "refresh_token": "r"}
    >>> _token_from_rfc_payload(payload, now=0.0)
    _CachedToken(access_token='a', refresh_token='r', expires_at=60.0)
    """
    # `int(...)`: some real servers return `expires_in` as a numeric string, against the RFC.
    return _token_from_expires_in(
        payload["access_token"], int(payload["expires_in"]), payload.get("refresh_token"), now
    )


def _token_from_model(model: TokenResponseTyping, now: float) -> _CachedToken:
    return _token_from_expires_in(model.access_token, model.expires_in, model.refresh_token, now)


def _decode_token_response(
    body: bytes, token_response: type[TokenResponseTyping]
) -> TokenResponseTyping:
    """Decode a token endpoint's body onto the user's own response model. Mirrors
    `_decode_error_body`'s branch order; the runtime dispatch uses the real nominal
    `Struct`/`BaseModel`, never the structural `TokenResponseTyping`."""
    if issubclass(token_response, Struct):
        return msgspec.json.decode(body, type=token_response)
    if issubclass(token_response, BaseModel):
        return token_response.model_validate_json(body)
    raise TypeError(f"Unsupported token_response: {token_response!r}")


def _rfc_mint_body(
    client_id: str, client_secret: str, scope: str | None, client_auth: Literal["basic", "body"]
) -> str:
    """Form body for the client-credentials grant (RFC 6749 §4.4.2); credentials go in the body
    only with `client_auth="body"` (§2.3.1), otherwise they travel as HTTP Basic.

    >>> _rfc_mint_body("client-id", "client-secret", None, "basic")
    'grant_type=client_credentials'
    >>> _rfc_mint_body("client-id", "client-secret", "read write", "body")
    'grant_type=client_credentials&scope=read+write&client_id=client-id&client_secret=client-secret'
    """
    fields = {"grant_type": "client_credentials"}
    if scope is not None:
        fields["scope"] = scope
    if client_auth == "body":
        fields["client_id"] = client_id
        fields["client_secret"] = client_secret
    return urlencode(fields)


def _rfc_refresh_body(
    refresh_token: str, client_id: str, client_secret: str, client_auth: Literal["basic", "body"]
) -> str:
    """Form body for the refresh-token grant (RFC 6749 §6).

    >>> _rfc_refresh_body("refresh-token", "client-id", "client-secret", "basic")
    'grant_type=refresh_token&refresh_token=refresh-token'
    >>> _rfc_refresh_body("refresh-token", "id", "secret", "body")
    'grant_type=refresh_token&refresh_token=refresh-token&client_id=id&client_secret=secret'
    """
    fields = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if client_auth == "body":
        fields["client_id"] = client_id
        fields["client_secret"] = client_secret
    return urlencode(fields)


def _format_expires_at(expires_at: float) -> str:
    """Render an epoch instant as a human-readable ISO 8601 UTC string, whole seconds only, for
    the cache file — sub-second precision on a token that lives for hours is noise, and the one
    audience the file has is a person checking why a token did or didn't renew. Floors rather
    than rounds so the stored expiry is never later than the real one.

    >>> _format_expires_at(1788865000.440887)
    '2026-09-08T10:56:40Z'
    """
    return datetime.fromtimestamp(int(expires_at), tz=UTC).isoformat().replace("+00:00", "Z")


def _parse_expires_at(text: str) -> float:
    """Inverse of `_format_expires_at`. A naive timestamp (no offset) is rejected rather than
    guessed at — the file is only ever written with an explicit `Z`.

    >>> _parse_expires_at("2026-09-08T10:56:40Z")
    1788865000.0
    >>> _parse_expires_at("2026-09-08T10:56:40+00:00")
    1788865000.0
    >>> _parse_expires_at("2026-09-08T10:56:40")
    Traceback (most recent call last):
        ...
    ValueError: 'expires_at' must carry a UTC offset: '2026-09-08T10:56:40'
    """
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"'expires_at' must carry a UTC offset: {text!r}")
    return parsed.timestamp()


def _token_from_cache_payload(payload: dict[str, Any]) -> _CachedToken:
    refresh_token = payload.get("refresh_token")
    return _CachedToken(
        access_token=str(payload["access_token"]),
        refresh_token=None if refresh_token is None else str(refresh_token),
        expires_at=_parse_expires_at(payload["expires_at"]),
    )


def _read_token_cache(path: Path, token_url: str, client_id: str) -> _CachedToken | None:
    """Load a previously written cache file. Anything unusable — missing, unreadable, not JSON,
    not an object, missing keys — reads as "no cache", never as an error: the next mint just
    overwrites it. A `token_url`/`client_id` mismatch also reads as "no cache", so one client
    is never silently handed another client's token."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        decoded = _json_loads(raw)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    payload = cast(dict[str, Any], decoded)
    if payload.get("token_url") != token_url or payload.get("client_id") != client_id:
        return None
    try:
        return _token_from_cache_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _write_token_cache(path: Path, token_url: str, client_id: str, token: _CachedToken) -> None:
    """Atomically replace `path` with the current token, `0600` since it holds live credentials.
    `mkstemp` creates the temp file `O_EXCL` with mode `0600` in the same directory, so the
    `os.replace` onto `path` is atomic and the final file keeps that mode. The parent directory
    must already exist."""
    payload = {
        "token_url": token_url,
        "client_id": client_id,
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": _format_expires_at(token.expires_at),
    }
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(_json_dumps(payload, indent=2))
        temp_path.replace(path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _validate_provider_config(
    token_request: TokenRequestTyping | None,
    token_refresh_request: TokenRefreshRequestTyping | None,
    token_response: type[TokenResponseTyping] | None,
    refresh_leeway: float,
) -> None:
    if (token_request is None) != (token_response is None):
        raise ValueError("Provide both 'token_request' and 'token_response', or neither")
    if token_refresh_request is not None and token_request is None:
        raise ValueError("'token_refresh_request' requires 'token_request' and 'token_response'")
    if refresh_leeway < 0:
        raise ValueError("'refresh_leeway' must be >= 0")


def _form_headers() -> dict[str, str]:
    return {"content-type": "application/x-www-form-urlencoded"}


def _is_client_error(error: HTTPResponseError) -> bool:
    return 400 <= error.status < 500


class OAuthProvider:
    """An OAuth 2 client-credentials `bearer_auth` provider for `HTTPClient` — see
    `SyncOAuthProvider` for the sync mirror.

    Pass an instance as `HTTPClient.build(bearer_auth=OAuthProvider(...))`. Every request then
    calls it, and it hands back a cached access token, minting or refreshing one behind the
    scenes only when needed:

    - The default speaks RFC 6749: `POST token_url` as `application/x-www-form-urlencoded` with
      `grant_type=client_credentials` (+ `scope`), the client credentials as an HTTP Basic
      `Authorization` header (`client_auth="basic"`, the RFC's preferred placement) or in the
      form body (`client_auth="body"`), decoding the standard `access_token`/`expires_in`/
      `refresh_token` JSON fields.
    - For a non-conforming API, pass `token_request`/`token_response` (and optionally
      `token_refresh_request`): pydantic/msgspec classes whose *attribute* names are
      `client_id`/`client_secret`, `refresh_token`, and `access_token`/`expires_in`/
      `refresh_token` respectively — wire names are yours via aliases. The request instance is
      sent as `json=`, so `client_auth` is ignored on this path: the model carries the
      credentials.
    - A token is treated as expired once fewer than `refresh_leeway` seconds (default 300)
      remain; until then `__call__` is a timestamp compare, no lock, no HTTP. Inside that
      window it renews: a refresh (`grant_type=refresh_token`, or `token_refresh_request`) if
      the last response carried a `refresh_token`, falling back to a fresh mint if the refresh
      is rejected with a 4xx or if there's no way to refresh; any other failure propagates.
      Concurrent callers inside the window share one renewal via a lock.
    - `token_cache_path` adds persistence only, it never changes *when* a token is renewed:
      the token is written there (JSON, atomic replace, `0600`) after every mint/refresh and read
      back on construction, guarded by `token_url` + `client_id`; a corrupt/unreadable file is
      ignored. The write is plain sync file IO inside the async `__call__` — a few hundred bytes,
      once per token lifetime.

    Deliberately a plain class, not `@dataclass` (style-guide.md #1's usual preference), for the
    same reason as `tests/test_auth.py`'s `_CountingAuthProvider`: confirmed live that zuban
    rejects a `@dataclass`-decorated version of this exact class as `bearer_auth=` —
    `Argument "bearer_auth" to "build" of "HTTPClient" has incompatible type "OAuthProvider";
    expected "Callable[[], Awaitable[str]] | None"  [arg-type]` — while basedpyright/mypy/ty all
    accept it. A plain class with the same `__call__` satisfies all four.
    """

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        client_auth: Literal["basic", "body"] = "basic",
        token_request: TokenRequestTyping | None = None,
        token_refresh_request: TokenRefreshRequestTyping | None = None,
        token_response: type[TokenResponseTyping] | None = None,
        refresh_leeway: float = 300.0,
        token_cache_path: Path | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        _validate_provider_config(
            token_request, token_refresh_request, token_response, refresh_leeway
        )
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._client_auth: Literal["basic", "body"] = client_auth
        self._token_request = token_request
        self._token_refresh_request = token_refresh_request
        self._token_response = token_response
        self._refresh_leeway = refresh_leeway
        self._token_cache_path = token_cache_path
        self._timeout = timeout
        self._token = (
            _read_token_cache(token_cache_path, token_url, client_id)
            if token_cache_path is not None
            else None
        )
        self._lock = asyncio.Lock()

    async def _post_rfc(self, body: str) -> _CachedToken:
        basic_auth = (
            (self._client_id, self._client_secret) if self._client_auth == "basic" else None
        )
        async with HTTPClient.build(timeout=self._timeout, basic_auth=basic_auth) as client:
            payload = await client.post(
                self._token_url, content=body, headers=_form_headers(), response_data_type=dict
            )
        return _token_from_rfc_payload(payload, time.time())

    async def _post_model(
        self, payload: JSONPayload, token_response: type[TokenResponseTyping]
    ) -> _CachedToken:
        async with HTTPClient.build(timeout=self._timeout) as client:
            body = await client.post(self._token_url, json=payload)
        return _token_from_model(_decode_token_response(body, token_response), time.time())

    async def _mint(self) -> _CachedToken:
        if self._token_request is None or self._token_response is None:
            body = _rfc_mint_body(
                self._client_id, self._client_secret, self._scope, self._client_auth
            )
            return await self._post_rfc(body)
        payload = self._token_request(client_id=self._client_id, client_secret=self._client_secret)
        return await self._post_model(payload, self._token_response)

    async def _refresh(self, refresh_token: str) -> _CachedToken | None:
        # `None` means "this provider has no way to refresh": on the model path lothc can't
        # know how the API spells a refresh request unless `token_refresh_request` says so.
        if self._token_request is None or self._token_response is None:
            body = _rfc_refresh_body(
                refresh_token, self._client_id, self._client_secret, self._client_auth
            )
            return await self._post_rfc(body)
        if self._token_refresh_request is None:
            return None
        payload = self._token_refresh_request(refresh_token=refresh_token)
        return await self._post_model(payload, self._token_response)

    async def _renew(self, current: _CachedToken | None) -> _CachedToken:
        if current is not None and current.refresh_token is not None:
            try:
                refreshed = await self._refresh(current.refresh_token)
            except HTTPResponseError as error:
                if not _is_client_error(error):
                    raise
                refreshed = None
            if refreshed is not None:
                return refreshed
        return await self._mint()

    async def __call__(self) -> str:
        fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
        if fresh is not None:
            return fresh.access_token
        async with self._lock:
            # Another waiter may have renewed while this one was queued on the lock.
            fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
            if fresh is not None:
                return fresh.access_token
            token = await self._renew(self._token)
            self._token = token
            if self._token_cache_path is not None:
                _write_token_cache(self._token_cache_path, self._token_url, self._client_id, token)
            return token.access_token


class SyncOAuthProvider:
    """Sync mirror of `OAuthProvider`, for `SyncHTTPClient.build(bearer_auth=...)` — same
    constructor, same token lifecycle, a `threading.Lock` instead of an `asyncio.Lock`, and a
    plain `def __call__` matching `SyncAuthProvider`. See `OAuthProvider` for the details."""

    def __init__(
        self,
        *,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: str | None = None,
        client_auth: Literal["basic", "body"] = "basic",
        token_request: TokenRequestTyping | None = None,
        token_refresh_request: TokenRefreshRequestTyping | None = None,
        token_response: type[TokenResponseTyping] | None = None,
        refresh_leeway: float = 300.0,
        token_cache_path: Path | None = None,
        timeout: float | None = 30.0,
    ) -> None:
        _validate_provider_config(
            token_request, token_refresh_request, token_response, refresh_leeway
        )
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._client_auth: Literal["basic", "body"] = client_auth
        self._token_request = token_request
        self._token_refresh_request = token_refresh_request
        self._token_response = token_response
        self._refresh_leeway = refresh_leeway
        self._token_cache_path = token_cache_path
        self._timeout = timeout
        self._token = (
            _read_token_cache(token_cache_path, token_url, client_id)
            if token_cache_path is not None
            else None
        )
        self._lock = threading.Lock()

    def _post_rfc(self, body: str) -> _CachedToken:
        basic_auth = (
            (self._client_id, self._client_secret) if self._client_auth == "basic" else None
        )
        with SyncHTTPClient.build(timeout=self._timeout, basic_auth=basic_auth) as client:
            payload = client.post(
                self._token_url, content=body, headers=_form_headers(), response_data_type=dict
            )
        return _token_from_rfc_payload(payload, time.time())

    def _post_model(
        self, payload: JSONPayload, token_response: type[TokenResponseTyping]
    ) -> _CachedToken:
        with SyncHTTPClient.build(timeout=self._timeout) as client:
            body = client.post(self._token_url, json=payload)
        return _token_from_model(_decode_token_response(body, token_response), time.time())

    def _mint(self) -> _CachedToken:
        if self._token_request is None or self._token_response is None:
            body = _rfc_mint_body(
                self._client_id, self._client_secret, self._scope, self._client_auth
            )
            return self._post_rfc(body)
        payload = self._token_request(client_id=self._client_id, client_secret=self._client_secret)
        return self._post_model(payload, self._token_response)

    def _refresh(self, refresh_token: str) -> _CachedToken | None:
        # `None` means "this provider has no way to refresh": on the model path lothc can't
        # know how the API spells a refresh request unless `token_refresh_request` says so.
        if self._token_request is None or self._token_response is None:
            body = _rfc_refresh_body(
                refresh_token, self._client_id, self._client_secret, self._client_auth
            )
            return self._post_rfc(body)
        if self._token_refresh_request is None:
            return None
        payload = self._token_refresh_request(refresh_token=refresh_token)
        return self._post_model(payload, self._token_response)

    def _renew(self, current: _CachedToken | None) -> _CachedToken:
        if current is not None and current.refresh_token is not None:
            try:
                refreshed = self._refresh(current.refresh_token)
            except HTTPResponseError as error:
                if not _is_client_error(error):
                    raise
                refreshed = None
            if refreshed is not None:
                return refreshed
        return self._mint()

    def __call__(self) -> str:
        fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
        if fresh is not None:
            return fresh.access_token
        with self._lock:
            # Another waiter may have renewed while this one was queued on the lock.
            fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
            if fresh is not None:
                return fresh.access_token
            token = self._renew(self._token)
            self._token = token
            if self._token_cache_path is not None:
                _write_token_cache(self._token_cache_path, self._token_url, self._client_id, token)
            return token.access_token
