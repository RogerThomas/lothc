---
icon: lucide/key-round
---

# Authentication

Two ways to send a `Bearer` `Authorization` header — provide at most one:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/", bearer_token="my-static-token"
) as client:
    await client.get("items/7")
```

`bearer_token` is a static string, sent as-is on every request. For a token that expires or
rotates, pass `bearer_auth` instead — an async callable (sync callable on `SyncHTTPClient`)
resolved fresh on *every* request, not just once at `build()` time:

```python
async def get_current_token() -> str:
    return await token_store.get_access_token()  # e.g. refreshed from a cache or auth server


async with HTTPClient.build(
    base_url="https://api.example.com/", bearer_auth=get_current_token
) as client:
    await client.get("items/7")  # calls get_current_token() for this request
    await client.get("items/8")  # calls it again — always the latest token
```

For the most common reason to need `bearer_auth` — an OAuth 2 client-credentials token that
expires — lothc ships a ready-made provider, so you don't write that callable yourself; see
[OAuth 2 client credentials](#oauth-2-client-credentials) below.

`bearer_token`/`bearer_auth` both only ever produce a `Bearer` `Authorization` header. For a
Basic-auth `username`/`password` pair instead, pass `basic_auth` — provide at most one of the
three:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/", basic_auth=("my-username", "my-password")
) as client:
    await client.get("items/7")
```

`password` may be `None` for a username with no password. There's no other custom-auth-scheme
option today.

Every verb also takes `skip_auth`, which omits the `Authorization` header for that one call —
useful when a client configured with `bearer_token`/`bearer_auth`/`basic_auth` also needs to hit a
differently-authenticated target through the same instance, e.g. a presigned S3 URL that must
never see your API's own token:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/", bearer_token="my-static-token"
) as client:
    await client.get("items/7")  # gets the Authorization header
    await client.get("https://presigned-bucket.example.com/file", skip_auth=True)  # doesn't
```

`skip_auth=True` also skips *calling* `bearer_auth` for that request — if refreshing the token is
expensive (a network round-trip to an auth server, say), that cost isn't paid on a skipped call.

## OAuth 2 client credentials

`OAuthProvider` is a `bearer_auth` provider for the OAuth 2 client-credentials grant
([RFC 6749 §4.4](https://www.rfc-editor.org/rfc/rfc6749#section-4.4)). Pass an instance where
you'd pass the callable, and every request gets a valid access token, minted or refreshed behind
the scenes only when needed:

```python
from lothc import HTTPClient, OAuthProvider

async with HTTPClient.build(
    base_url="https://api.example.com/",
    bearer_auth=OAuthProvider(
        token_url="https://auth.example.com/oauth/token",
        client_id="my-client-id",
        client_secret="my-client-secret",
    ),
) as client:
    await client.get("items/7")
```

The defaults do exactly what the RFC says: `POST token_url` as
`application/x-www-form-urlencoded` with `grant_type=client_credentials`, the client credentials
as an HTTP Basic `Authorization` header (the RFC's preferred placement), and the standard
`access_token`/`expires_in`/`refresh_token` JSON fields decoded from the response. Two optional
knobs on that request: `scope="read:things write:things"` adds a `scope` field, and
`client_auth="body"` puts `client_id`/`client_secret` in the form body instead of the header,
for servers that only accept them there.

### Token lifecycle

- The first request mints a token and remembers `access_token`, `refresh_token` (if the server
  sent one) and an absolute expiry, `now + expires_in`.
- Every later request is a timestamp compare, no lock, no HTTP — until fewer than
  `refresh_leeway` seconds (default `300.0`, i.e. five minutes) remain, at which point the token
  is treated as already expired and renewed.
- Renewal prefers a refresh: if the last response carried a `refresh_token`, it sends
  `grant_type=refresh_token`. If it didn't, or the refresh is rejected with a 4xx (a `400
  invalid_grant` because the refresh token itself expired or was revoked), it falls back to
  minting a fresh token with the client-credentials grant. Either way the new response replaces
  the whole cached triple. A 5xx from the token endpoint, a transport error, or a decode error
  is not a fallback case — it propagates from the request that triggered the renewal, as an
  `HTTPResponseError`/`HTTPTransportError`/the decode library's own exception respectively.
- Concurrent requests that all land inside the leeway window share one renewal (a lock); the
  rest wait for it and reuse its result rather than each hitting the token endpoint.

`timeout` (default `30.0`) bounds each token request; it's separate from the API client's own
`timeout`.

### Persisting the token across restarts

`token_cache_path` adds persistence, and only persistence — it never changes *when* a token is
renewed:

```python
from pathlib import Path

OAuthProvider(
    token_url="https://auth.example.com/oauth/token",
    client_id="my-client-id",
    client_secret="my-client-secret",
    token_cache_path=Path("~/.cache/my-app/token.json").expanduser(),
)
```

After every mint/refresh the token triple is written there as JSON, atomically (a temp file in
the same directory, then replaced into place) with `0600` permissions, since it holds live
credentials. It's meant to be readable by a person checking why a token did or didn't renew, so
the expiry is an ISO 8601 UTC timestamp at whole-second precision, not an epoch float:

```json
{
  "token_url": "https://auth.example.com/oauth/token",
  "client_id": "my-client-id",
  "access_token": "eyJ...",
  "refresh_token": null,
  "expires_at": "2026-09-08T10:56:40Z"
}
```

On construction the file is read back, so a restarted process picks up where the
last one left off: a still-valid token is used as-is, one inside the leeway window is renewed via
the cached refresh token. The file also records `token_url` and `client_id`; a mismatch on either
reads as "no cache", so one client is never handed another client's token. A corrupt or
unreadable file is never fatal — it's ignored and overwritten on the next mint. The parent
directory must already exist.

### Non-RFC token endpoints

Plenty of real APIs hand out bearer tokens without speaking RFC 6749: a JSON body instead of
form-encoding, every field renamed. Describe such an endpoint with your own pydantic/msgspec
models, and lothc reuses its existing `json=`/`response_data_type` machinery to send and decode
them. The contract is on the *attribute* names, never the *wire* names:

- `token_request` must be constructible as `Cls(client_id=..., client_secret=...)`; any other
  field it declares (`grant_type`, `audience`, ...) needs a default.
- `token_response` must expose `.access_token: str`, `.expires_in: int` and
  `.refresh_token: str | None`.
- `token_refresh_request`, optional, must be constructible as `Cls(refresh_token=...)`. Without
  it, renewal always means minting a fresh token, even if the response carried a
  `refresh_token` — lothc has no way to know how this API spells a refresh request.

Provide `token_request` and `token_response` together, or neither. `client_auth` is ignored on
this path: the request model carries the credentials, so nothing is sent as HTTP Basic.

```python
from pydantic import BaseModel, ConfigDict, Field


class TokenRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    client_id: str = Field(alias="clientID")
    client_secret: str = Field(alias="clientSecret")
    grant_type: str = Field(default="client_credentials", alias="grantType")


class TokenResponse(BaseModel):
    access_token: str = Field(alias="accessToken")
    expires_in: int = Field(alias="expiresIn")
    # This API never returns a refresh token. The attribute is still required by the contract,
    # so declare it with a `None` default; leave `token_refresh_request` out and renewal simply
    # mints a fresh token every time.
    refresh_token: str | None = Field(default=None, alias="refreshToken")


OAuthProvider(
    token_url="https://auth.example.com/v2/token",
    client_id="my-client-id",
    client_secret="my-client-secret",
    token_request=TokenRequest,
    token_response=TokenResponse,
)
```

The `model_config` line on the request model is load-bearing, not boilerplate. lothc constructs
it by attribute name (`validate_by_name=True` is what lets an aliased pydantic model accept
`client_id=` at all), and lothc's `json=` encoding calls `model_dump(mode="json")` with no
`by_alias=True` — so without `serialize_by_alias=True` the body goes over the wire with the
Python names. Confirmed against a real endpoint: with that config the token request succeeds,
without it the same model is rejected with `400 "clientID must not be blank"`. This applies to
any aliased pydantic model passed as `json=` anywhere in lothc, not just here. (For a
request-only model, `Field(serialization_alias="clientID")` with just
`ConfigDict(serialize_by_alias=True)` is an equivalent spelling that needs no
`validate_by_name`, since the model is never validated from the wire.)

The msgspec spelling of the same thing needs no config — `name=` covers both directions:

```python
import msgspec


class TokenRequest(msgspec.Struct):
    client_id: str = msgspec.field(name="clientID")
    client_secret: str = msgspec.field(name="clientSecret")
    grant_type: str = msgspec.field(default="client_credentials", name="grantType")


class TokenResponse(msgspec.Struct):
    access_token: str = msgspec.field(name="accessToken")
    expires_in: int = msgspec.field(name="expiresIn")
    refresh_token: str | None = msgspec.field(default=None, name="refreshToken")
```

If the API does issue refresh tokens, add a third model for the refresh call:

```python
class TokenRefreshRequest(BaseModel):
    model_config = ConfigDict(validate_by_name=True, serialize_by_alias=True)

    refresh_token: str = Field(alias="refreshToken")
    grant_type: str = Field(default="refresh_token", alias="grantType")


OAuthProvider(
    token_url="https://auth.example.com/v2/token",
    client_id="my-client-id",
    client_secret="my-client-secret",
    token_request=TokenRequest,
    token_refresh_request=TokenRefreshRequest,
    token_response=TokenResponse,
)
```

### Sync

`SyncOAuthProvider` is the mirror for `SyncHTTPClient` — same constructor, same lifecycle, a
plain `def __call__`:

```python
from lothc import SyncHTTPClient, SyncOAuthProvider

with SyncHTTPClient.build(
    base_url="https://api.example.com/",
    bearer_auth=SyncOAuthProvider(
        token_url="https://auth.example.com/oauth/token",
        client_id="my-client-id",
        client_secret="my-client-secret",
    ),
) as client:
    client.get("items/7")
```

## Default headers

For anything that isn't a `Bearer` token — an API key header, a custom user-agent, whatever your
API needs on every request — pass `default_headers` at `build()` time. Unlike `bearer_auth`,
these are fixed for the client's whole lifetime, resolved once, not per-request:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/",
    default_headers={"x-api-key": "my-api-key"},
) as client:
    await client.get("items/7")  # sent with every request through this client
```

Combine freely with `bearer_token`/`bearer_auth`/`basic_auth` — they set different headers
(`Authorization` vs. whatever you name here).

## Timeouts

`timeout` (seconds, default `30.0`) applies to the whole client, covering every request made
through it:

```python
async with HTTPClient.build(base_url="https://api.example.com/", timeout=5.0) as client:
    await client.get("items/7")  # raises HTTPTimeoutError if this takes longer than 5s
```

Pass `timeout=None` to disable it and fall back to pyreqwest's own default. See
[Error handling](errors.md) for `HTTPTimeoutError`.

Every verb also takes its own `timeout`, overriding the client's for that one call only:

```python
async with HTTPClient.build(base_url="https://api.example.com/", timeout=5.0) as client:
    await client.get("items/7")  # uses the client default, 5s
    await client.get("exports/large-file.csv", timeout=60.0)  # this call gets 60s instead
```

There's no way to make a single call wait forever when the client itself has a finite
`timeout` — pyreqwest's own per-request `.timeout()` only ever accepts a duration, never a
sentinel meaning "no timeout." To remove the limit entirely, build the client with
`timeout=None` instead.
