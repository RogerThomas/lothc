---
icon: lucide/octagon-alert
---

# Error handling

Two separate failure classes, never unified:

- **`HTTPResponseError`** — the server answered with a 4xx/5xx. Carries `.status` and `.body_start`
  (the first 100 bytes of the body, for a quick look without decoding), `.parsed_body`, and
  `.request` (the request that got this response — method/url/path/host; handy for telling which
  call failed when several are in flight at once).
- **`HTTPTransportError`** — never got a response at all: a `HTTPTimeoutError` or `HTTPConnectionError`
  subclass. pyreqwest's own exception types never leak through; they're translated to these at
  every call site.

```python
from lothc import HTTPClient, HTTPConnectionError, HTTPResponseError, HTTPTimeoutError

async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
    try:
        await client.get("pokemon/does-not-exist")
    except HTTPResponseError as e:
        print(e.status, e.body_start)
        print(e.request.method, e.request.path)  # "GET", "/api/v2/pokemon/does-not-exist"
    except HTTPTimeoutError:
        print("took too long")
    except HTTPConnectionError:
        print("never reached the server")
```

## Typed error bodies

`.parsed_body` is `None` unless you pass `error_type` — every verb takes it, mirroring
`response_data_type`, and it decodes the 4xx/5xx body onto `HTTPResponseError.parsed_body`
instead of leaving it `None`:

```python
from pydantic import BaseModel


class APIError(BaseModel):
    type: str
    title: str
    status: int


async with HTTPClient.build(base_url="https://api.example.com/") as client:
    try:
        await client.get("items/7", error_type=APIError)
    except HTTPResponseError as e:
        print(e.parsed_body)  # APIError(type=..., title=..., status=...)
```

A validation error from whichever decode library you picked (`pydantic.ValidationError`,
`msgspec.ValidationError`) is never wrapped, on either the success (`response_data_type`) or
error (`error_type`) path — it propagates as-is, since choosing that library is opting into its
own exception too.

## `OAuthTokenError`

The one exception to the rule above. `OAuthProvider`/`SyncOAuthProvider` (see
[Authentication](auth.md#oauth-2-client-credentials)) run *inside* whatever request needed the
token, so a failure at the token endpoint would otherwise surface as a failure of that unrelated
call — an `HTTPResponseError` whose `.status` is really the auth server's, a
`pydantic.ValidationError` from a token response you never asked that call to decode. Every
failure to obtain a token is therefore raised as `OAuthTokenError`, with the original exception as
`__cause__` and the endpoint in `.token_url`:

```python
from lothc import HTTPClient, HTTPResponseError, OAuthProvider, OAuthTokenError

async with HTTPClient.build(base_url=..., bearer_auth=OAuthProvider(...)) as client:
    try:
        await client.get("items/7")
    except OAuthTokenError as e:
        print(e.token_url)  # the token endpoint, not "items/7"
        if isinstance(e.__cause__, HTTPResponseError):
            print(e.__cause__.status)  # what the token endpoint answered
    except HTTPResponseError as e:
        print(e.status)  # an error from "items/7" itself
```

A `response_data_type`/`error_type` decode error is never wrapped because it already belongs to
the call that raised it; a token decode error is wrapped because it doesn't.
