"""OAuth 2 client-credentials token providers, for `HTTPClient.build(bearer_auth=...)`.

A utility built ON the client, not a split of it: `OAuthProvider`/`SyncOAuthProvider` are just
objects whose `__call__` returns a valid access token, which is exactly what the clients'
`bearer_auth` slot already accepts (`AuthProvider`/`SyncAuthProvider`). Minting/refreshing goes
through a fresh, short-lived `HTTPClient`/`SyncHTTPClient` per token request.
"""

import asyncio
import base64
import os
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from json import dumps as _json_dumps
from json import loads as _json_loads
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from urllib.parse import quote, urlencode

from ._client import HTTPClient, HTTPResponseError, JSONPayload, SyncHTTPClient
from ._compat import BaseModelTyping, StructTyping

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


class _TokenFieldsTyping(Protocol):
    """The three attributes every `token_response=` class must expose. `expires_in` may be
    `None` (the model declares it optional) — `default_expires_in` then fills it in."""

    @property
    def access_token(self) -> str: ...
    @property
    def expires_in(self) -> int | None: ...
    @property
    def refresh_token(self) -> str | None: ...


class _StructTokenResponseTyping(StructTyping, _TokenFieldsTyping, Protocol): ...


class _BaseModelTokenResponseTyping(BaseModelTyping, _TokenFieldsTyping, Protocol): ...


# A union of two Protocols that each *also* inherit `_compat.py`'s structural `StructTyping`/
# `BaseModelTyping` — rather than one flat Protocol with the three fields — so that a
# `type[TokenResponseTyping]` is itself a valid `response_data_type=` (every member is a `Data`
# member) and `_post_model` can hand it straight to `client.post(...)` with no local decoder and
# no cast. Confirmed live on basedpyright, mypy, ty and zuban with the test suite's real pydantic
# and msgspec models: `client.post(..., response_data_type=token_response)` reveals as this
# union, a model declaring plain `expires_in: int` still satisfies the `int | None` property
# covariantly, and a model with no `expires_in` at all is rejected at the call site.
type TokenResponseTyping = _StructTokenResponseTyping | _BaseModelTokenResponseTyping


class OAuthTokenError(Exception):
    """Raised by `OAuthProvider`/`SyncOAuthProvider` when a token could not be obtained, wrapping
    whatever the token endpoint call raised as `__cause__` (an `HTTPResponseError`, an
    `HTTPTransportError`, the decode library's own validation error, a `ValueError`/`KeyError`
    from a malformed payload). `.token_url` is the endpoint that was asked.

    This is the one place lothc wraps a decode library's error: the provider runs *inside* the
    user's unrelated verb call (`await client.get("items/7")`), so an unwrapped error there reads
    as a failure of that call — e.g. an `HTTPResponseError` whose `.status` is really the token
    endpoint's, not the API's. A `response_data_type` decode error, by contrast, already belongs
    to the call that raised it.
    """

    def __init__(self, token_url: str, error: BaseException) -> None:
        self.token_url = token_url
        super().__init__(f"Failed to obtain an access token from {token_url}: {error}")


@dataclass(slots=True)
class _CachedToken:
    access_token: str
    refresh_token: str | None
    # Wall-clock epoch seconds (`time.time()`), never `monotonic()`: this is persisted to the
    # token cache file and read back by a later process, where a monotonic reading is meaningless.
    expires_at: float
    # The lifetime the token was issued with, in seconds — kept so `refresh_leeway` can be
    # clamped against it (a 300s leeway on a 60s token would otherwise renew on every call).
    expires_in: float


def _fresh_token(
    token: _CachedToken | None, refresh_leeway: float, now: float
) -> _CachedToken | None:
    """The cached token if it still has more than the effective leeway left, else `None`. The
    effective leeway is `min(refresh_leeway, expires_in / 2)`: a leeway longer than half the
    token's lifetime is clamped so a short-lived token still gets used for at least half of it.

    >>> token = _CachedToken("access-token", None, expires_at=1000.0, expires_in=3600.0)
    >>> _fresh_token(token, refresh_leeway=300.0, now=699.0) is token
    True
    >>> _fresh_token(token, refresh_leeway=300.0, now=700.0) is None
    True
    >>> short = _CachedToken("access-token", None, expires_at=1000.0, expires_in=60.0)
    >>> _fresh_token(short, refresh_leeway=300.0, now=969.0) is short  # clamped to 30s
    True
    >>> _fresh_token(short, refresh_leeway=300.0, now=970.0) is None
    True
    >>> zero = _CachedToken("access-token", None, expires_at=1000.0, expires_in=0.0)
    >>> _fresh_token(zero, refresh_leeway=300.0, now=1000.0) is None  # stale at issue time
    True
    >>> _fresh_token(None, refresh_leeway=300.0, now=0.0) is None
    True
    """
    if token is None:
        return None
    effective_leeway = min(refresh_leeway, token.expires_in / 2)
    if now >= token.expires_at - effective_leeway:
        return None
    return token


def _resolve_expires_in(expires_in: float | None, default_expires_in: float | None) -> float:
    """The lifetime to use: the server's `expires_in` when it sent one, else the configured
    default, else a `ValueError` — a token with no known lifetime can't be cached safely.

    >>> _resolve_expires_in(60, default_expires_in=3600.0)
    60
    >>> _resolve_expires_in(None, default_expires_in=3600.0)
    3600.0
    >>> _resolve_expires_in(None, default_expires_in=None)
    Traceback (most recent call last):
        ...
    ValueError: Token response has no 'expires_in' and no 'default_expires_in' was configured
    """
    if expires_in is not None:
        return expires_in
    if default_expires_in is not None:
        return default_expires_in
    raise ValueError(
        "Token response has no 'expires_in' and no 'default_expires_in' was configured"
    )


def _token_from_expires_in(
    access_token: str, expires_in: float, refresh_token: str | None, now: float
) -> _CachedToken:
    """`expires_in` is relative to when the server answered, so it's pinned to an absolute
    instant once, at receipt.

    >>> _token_from_expires_in("access-token", 60, None, now=100.0)
    _CachedToken(access_token='access-token', refresh_token=None, expires_at=160.0, expires_in=60)
    """
    return _CachedToken(access_token, refresh_token, now + expires_in, expires_in)


def _token_from_rfc_payload(
    payload: dict[str, Any], default_expires_in: float | None, now: float
) -> _CachedToken:
    """Standard RFC 6749 §5.1 token response. `access_token` is required (a `KeyError` says so
    if the server left it out); `expires_in` falls back to `default_expires_in` (§5.1 only
    RECOMMENDS it); `refresh_token` is genuinely optional.

    >>> _token_from_rfc_payload({"access_token": "a", "expires_in": "60"}, None, now=100.0)
    _CachedToken(access_token='a', refresh_token=None, expires_at=160.0, expires_in=60)
    >>> payload = {"access_token": "a", "expires_in": 60, "refresh_token": "r"}
    >>> _token_from_rfc_payload(payload, None, now=0.0)
    _CachedToken(access_token='a', refresh_token='r', expires_at=60.0, expires_in=60)
    >>> _token_from_rfc_payload({"access_token": "a"}, 3600.0, now=0.0)
    _CachedToken(access_token='a', refresh_token=None, expires_at=3600.0, expires_in=3600.0)
    """
    # `int(...)`: some real servers return `expires_in` as a numeric string, against the RFC.
    raw_expires_in = payload.get("expires_in")
    expires_in = _resolve_expires_in(
        None if raw_expires_in is None else int(raw_expires_in), default_expires_in
    )
    return _token_from_expires_in(
        payload["access_token"], expires_in, payload.get("refresh_token"), now
    )


def _token_from_model(
    model: TokenResponseTyping, default_expires_in: float | None, now: float
) -> _CachedToken:
    expires_in = _resolve_expires_in(model.expires_in, default_expires_in)
    return _token_from_expires_in(model.access_token, expires_in, model.refresh_token, now)


def _carry_refresh_token(refreshed: _CachedToken, previous: _CachedToken) -> _CachedToken:
    """RFC 6749 §6: a refresh response MAY omit `refresh_token`, in which case the one that was
    used stays valid — so it's carried forward rather than dropped.

    >>> previous = _CachedToken("old", "refresh-1", expires_at=0.0, expires_in=0.0)
    >>> _carry_refresh_token(_CachedToken("new", None, 60.0, 60.0), previous).refresh_token
    'refresh-1'
    >>> _carry_refresh_token(_CachedToken("new", "refresh-2", 60.0, 60.0), previous).refresh_token
    'refresh-2'
    """
    if refreshed.refresh_token is not None:
        return refreshed
    return replace(refreshed, refresh_token=previous.refresh_token)


def _basic_authorization_header(client_id: str, client_secret: str) -> str:
    """HTTP Basic client authentication per RFC 6749 §2.3.1: both halves are
    `application/x-www-form-urlencoded`-encoded *before* being joined and base64'd, so a secret
    containing `:`, `%`, `+` or a space survives the round trip. `quote(..., safe="")`, not
    `quote_plus` — a space becomes `%20`, matching what authlib's `encode_client_secret_basic`
    sends and what servers following the RFC decode.

    >>> _basic_authorization_header("client-id", "client-secret")
    'Basic Y2xpZW50LWlkOmNsaWVudC1zZWNyZXQ='
    >>> import base64
    >>> header = _basic_authorization_header("id", "s3cr3t+/%= :")
    >>> base64.b64decode(header.removeprefix("Basic ")).decode()
    'id:s3cr3t%2B%2F%25%3D%20%3A'
    """
    credentials = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"
    return "Basic " + base64.b64encode(credentials.encode()).decode()


def _rfc_headers(
    client_id: str, client_secret: str, client_auth: Literal["basic", "body"]
) -> dict[str, str]:
    """Per-request headers for an RFC 6749 token request: the form content type, plus the Basic
    `Authorization` header when the credentials travel there rather than in the body.

    >>> _rfc_headers("client-id", "client-secret", "body")
    {'content-type': 'application/x-www-form-urlencoded'}
    >>> sorted(_rfc_headers("client-id", "client-secret", "basic"))
    ['authorization', 'content-type']
    """
    headers = {"content-type": "application/x-www-form-urlencoded"}
    if client_auth == "basic":
        headers["authorization"] = _basic_authorization_header(client_id, client_secret)
    return headers


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
        expires_in=float(payload["expires_in"]),
    )


def _read_token_cache(
    path: Path, token_url: str, client_id: str, scope: str | None
) -> _CachedToken | None:
    """Load a previously written cache file. Anything unusable — missing, unreadable, not JSON,
    not an object, missing keys — reads as "no cache", never as an error: the next mint just
    overwrites it. A `token_url`/`client_id`/`scope` mismatch also reads as "no cache", so one
    client is never silently handed another client's token, or a token minted for a different
    set of scopes."""
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
    if (
        payload.get("token_url") != token_url
        or payload.get("client_id") != client_id
        or payload.get("scope") != scope
    ):
        return None
    try:
        return _token_from_cache_payload(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _write_token_cache(
    path: Path, token_url: str, client_id: str, scope: str | None, token: _CachedToken
) -> None:
    """Atomically replace `path` with the current token, `0600` since it holds live credentials.
    `mkstemp` creates the temp file `O_EXCL` with mode `0600` in the same directory, so the
    `os.replace` onto `path` is atomic and the final file keeps that mode. The parent directory
    is checked to exist at construction (`_validate_provider_config`)."""
    payload = {
        "token_url": token_url,
        "client_id": client_id,
        "scope": scope,
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": _format_expires_at(token.expires_at),
        "expires_in": token.expires_in,
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
    token_cache_path: Path | None,
) -> None:
    if (token_request is None) != (token_response is None):
        raise ValueError("Provide both 'token_request' and 'token_response', or neither")
    if token_refresh_request is not None and token_request is None:
        raise ValueError("'token_refresh_request' requires 'token_request' and 'token_response'")
    if refresh_leeway < 0:
        raise ValueError("'refresh_leeway' must be >= 0")
    if token_cache_path is not None and not token_cache_path.parent.is_dir():
        raise FileNotFoundError(
            f"token_cache_path parent directory does not exist: {token_cache_path.parent}"
        )


class OAuthProvider:
    """An OAuth 2 client-credentials `bearer_auth` provider for `HTTPClient` — see
    `SyncOAuthProvider` for the sync mirror.

    Pass an instance as `HTTPClient.build(bearer_auth=OAuthProvider(...))`. Every request then
    calls it, and it hands back a cached access token, minting or refreshing one behind the
    scenes only when needed:

    - The default speaks RFC 6749: `POST token_url` as `application/x-www-form-urlencoded` with
      `grant_type=client_credentials` (+ `scope`), the client credentials as an HTTP Basic
      `Authorization` header (`client_auth="basic"`, the RFC's preferred placement, each half
      percent-encoded per §2.3.1) or in the form body (`client_auth="body"`), decoding the
      standard `access_token`/`expires_in`/`refresh_token` JSON fields. A response without
      `expires_in` uses `default_expires_in`; with neither, the token is rejected.
    - For a non-conforming API, pass `token_request`/`token_response` (and optionally
      `token_refresh_request`): pydantic/msgspec classes whose *attribute* names are
      `client_id`/`client_secret`, `refresh_token`, and `access_token`/`expires_in`/
      `refresh_token` respectively — wire names are yours via aliases. The request instance is
      sent as `json=`, so `client_auth` is ignored on this path: the model carries the
      credentials.
    - A token is treated as expired once fewer than `refresh_leeway` seconds (default 300,
      clamped to half the token's own lifetime) remain; until then `__call__` is a timestamp
      compare, no lock, no HTTP. Inside that window it renews: a refresh
      (`grant_type=refresh_token`, or `token_refresh_request`) if the last response carried a
      `refresh_token` (carried forward if the refresh response omits one), falling back to a
      fresh mint only if the refresh is rejected with a 400 or if there's no way to refresh.
      Any failure to obtain a token — a non-400 rejection, a 5xx, a transport error, a decode
      error, a malformed payload — is raised as `OAuthTokenError` from the API call that
      triggered the renewal, with the original as `__cause__`. Concurrent callers inside the
      window share one renewal via a lock.
    - `token_cache_path` adds persistence only, it never changes *when* a token is renewed:
      the token is written there (JSON, atomic replace, `0600`) after every mint/refresh and read
      back on construction, guarded by `token_url` + `client_id` + `scope`; a corrupt/unreadable
      file is ignored. The parent directory must exist at construction. The write is plain sync
      file IO inside the async `__call__` — a few hundred bytes, once per token lifetime.
    - Every token request goes through a short-lived client from `client_factory` (default
      `HTTPClient.build`), called with no arguments — so `functools.partial(HTTPClient.build,
      timeout=5.0, proxy=...)` is how the token endpoint gets its own timeout/proxy/TLS config.

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
        default_expires_in: float | None = None,
        token_cache_path: Path | None = None,
        client_factory: Callable[[], AbstractAsyncContextManager[HTTPClient]] = HTTPClient.build,
    ) -> None:
        _validate_provider_config(
            token_request, token_refresh_request, token_response, refresh_leeway, token_cache_path
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
        self._default_expires_in = default_expires_in
        self._token_cache_path = token_cache_path
        self._client_factory = client_factory
        self._token = (
            _read_token_cache(token_cache_path, token_url, client_id, scope)
            if token_cache_path is not None
            else None
        )
        # Created lazily, per event loop: an `asyncio.Lock` binds to the loop it's first used
        # on, and one provider instance legitimately outlives an `asyncio.run()` (confirmed
        # live on 3.14: a lock created in `__init__` and reused across two `asyncio.run()`
        # calls raises `RuntimeError: ... is bound to a different event loop`).
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def _post_rfc(self, body: str) -> _CachedToken:
        headers = _rfc_headers(self._client_id, self._client_secret, self._client_auth)
        async with self._client_factory() as client:
            payload = await client.post(
                self._token_url, content=body, headers=headers, response_data_type=dict
            )
        return _token_from_rfc_payload(payload, self._default_expires_in, time.time())

    async def _post_model(
        self, payload: JSONPayload, token_response: type[TokenResponseTyping]
    ) -> _CachedToken:
        async with self._client_factory() as client:
            decoded = await client.post(
                self._token_url, json=payload, response_data_type=token_response
            )
        return _token_from_model(decoded, self._default_expires_in, time.time())

    async def _mint(self) -> _CachedToken:
        if self._token_request is None or self._token_response is None:
            body = _rfc_mint_body(
                self._client_id, self._client_secret, self._scope, self._client_auth
            )
            return await self._post_rfc(body)
        payload = self._token_request(client_id=self._client_id, client_secret=self._client_secret)
        return await self._post_model(payload, self._token_response)

    async def _mint_after_rejected_refresh(self, refresh_error: HTTPResponseError) -> _CachedToken:
        # Chain the rejected refresh onto a failed fallback mint so both show in the traceback.
        try:
            return await self._mint()
        except Exception as mint_error:
            raise mint_error from refresh_error

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
        if current is None or current.refresh_token is None:
            return await self._mint()
        try:
            refreshed = await self._refresh(current.refresh_token)
        except HTTPResponseError as error:
            # Only a 400 (`invalid_grant`: the refresh token expired or was revoked) means "mint
            # instead" — a 401/403/429 says something else is wrong and re-minting would just
            # repeat it.
            if error.status != 400:
                raise
            return await self._mint_after_rejected_refresh(error)
        if refreshed is None:
            return await self._mint()
        return _carry_refresh_token(refreshed, current)

    async def __call__(self) -> str:
        fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
        if fresh is not None:
            return fresh.access_token
        async with self._get_lock():
            # Another waiter may have renewed while this one was queued on the lock.
            fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
            if fresh is not None:
                return fresh.access_token
            try:
                token = await self._renew(self._token)
            except Exception as error:
                raise OAuthTokenError(self._token_url, error) from error
            self._token = token
            if self._token_cache_path is not None:
                _write_token_cache(
                    self._token_cache_path, self._token_url, self._client_id, self._scope, token
                )
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
        default_expires_in: float | None = None,
        token_cache_path: Path | None = None,
        client_factory: Callable[[], AbstractContextManager[SyncHTTPClient]] = SyncHTTPClient.build,
    ) -> None:
        _validate_provider_config(
            token_request, token_refresh_request, token_response, refresh_leeway, token_cache_path
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
        self._default_expires_in = default_expires_in
        self._token_cache_path = token_cache_path
        self._client_factory = client_factory
        self._token = (
            _read_token_cache(token_cache_path, token_url, client_id, scope)
            if token_cache_path is not None
            else None
        )
        self._lock = threading.Lock()

    def _post_rfc(self, body: str) -> _CachedToken:
        headers = _rfc_headers(self._client_id, self._client_secret, self._client_auth)
        with self._client_factory() as client:
            payload = client.post(
                self._token_url, content=body, headers=headers, response_data_type=dict
            )
        return _token_from_rfc_payload(payload, self._default_expires_in, time.time())

    def _post_model(
        self, payload: JSONPayload, token_response: type[TokenResponseTyping]
    ) -> _CachedToken:
        with self._client_factory() as client:
            decoded = client.post(self._token_url, json=payload, response_data_type=token_response)
        return _token_from_model(decoded, self._default_expires_in, time.time())

    def _mint(self) -> _CachedToken:
        if self._token_request is None or self._token_response is None:
            body = _rfc_mint_body(
                self._client_id, self._client_secret, self._scope, self._client_auth
            )
            return self._post_rfc(body)
        payload = self._token_request(client_id=self._client_id, client_secret=self._client_secret)
        return self._post_model(payload, self._token_response)

    def _mint_after_rejected_refresh(self, refresh_error: HTTPResponseError) -> _CachedToken:
        # Chain the rejected refresh onto a failed fallback mint so both show in the traceback.
        try:
            return self._mint()
        except Exception as mint_error:
            raise mint_error from refresh_error

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
        if current is None or current.refresh_token is None:
            return self._mint()
        try:
            refreshed = self._refresh(current.refresh_token)
        except HTTPResponseError as error:
            # Only a 400 (`invalid_grant`: the refresh token expired or was revoked) means "mint
            # instead" — a 401/403/429 says something else is wrong and re-minting would just
            # repeat it.
            if error.status != 400:
                raise
            return self._mint_after_rejected_refresh(error)
        if refreshed is None:
            return self._mint()
        return _carry_refresh_token(refreshed, current)

    def __call__(self) -> str:
        fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
        if fresh is not None:
            return fresh.access_token
        with self._lock:
            # Another waiter may have renewed while this one was queued on the lock.
            fresh = _fresh_token(self._token, self._refresh_leeway, time.time())
            if fresh is not None:
                return fresh.access_token
            try:
                token = self._renew(self._token)
            except Exception as error:
                raise OAuthTokenError(self._token_url, error) from error
            self._token = token
            if self._token_cache_path is not None:
                _write_token_cache(
                    self._token_cache_path, self._token_url, self._client_id, self._scope, token
                )
            return token.access_token
