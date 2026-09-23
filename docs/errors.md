---
icon: lucide/octagon-alert
---

# Error handling

Two separate failure classes, never unified:

- **`HTTPResponseError`** — the server answered with a 4xx/5xx. Carries `.status`, `.headers` (the
  error response's own, e.g. a `Retry-After` once retries are exhausted, or a request id to quote
  to support), `.body` (the whole body) and `.body_start` (its first 100 bytes, as used in the
  message), `.parsed_body`, and `.request` (the request that got this response —
  method/url/path/host; handy for telling which call failed when several are in flight at once).
  It pickles, so it can cross a process boundary (multiprocessing, a task queue).
- **`HTTPTransportError`** — no usable response: either none arrived at all, or the connection
  failed partway through one (a body cut short raises `HTTPConnectionError`). Its subclasses are
  `HTTPTimeoutError` and `HTTPConnectionError`. pyreqwest's own exception types never leak
  through; they're translated to these at every call site.

Using a client after its `with`/`async with` block has exited raises a plain `RuntimeError`: that's
a bug in the calling code, not a network failure, so it deliberately isn't an `HTTPTransportError`
that a retry handler would swallow. And a `response_data_type=dict` body that isn't valid JSON
raises the stdlib's own `json.JSONDecodeError`, like any other decode failure.

```python
from lothc import HTTPClient, HTTPConnectionError, HTTPResponseError, HTTPTimeoutError

async with HTTPClient(base_url="https://pokeapi.co/api/v2/") as client:
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


async with HTTPClient(base_url="https://api.example.com/") as client:
    try:
        await client.get("items/7", error_type=APIError)
    except HTTPResponseError as e:
        print(e.parsed_body)  # APIError(type=..., title=..., status=...)
```

Decoding an error body is best-effort. If the body doesn't match `error_type` (the classic case:
a proxy in front of a JSON API answers 502 with an HTML page), you still get the
`HTTPResponseError`, with its real status, headers and body; `.parsed_body` is `None` and
`.parse_error` holds why it didn't parse (the decode library's own exception):

```python
try:
    await client.get("items/7", error_type=APIError)
except HTTPResponseError as e:
    if e.parsed_body is None:
        print(e.status, e.parse_error)  # 502, ValidationError(...)
```

On the success path it's the opposite: a `response_data_type` body that doesn't match raises the
decode library's own error (`pydantic.ValidationError`, `msgspec.ValidationError`), unwrapped,
since choosing that library is opting into its exception too. An error body is where a server is
least likely to honour its own contract, so failing to decode one must never hide the error
itself.

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

async with HTTPClient(base_url=..., bearer_auth=OAuthProvider(...)) as client:
    try:
        await client.get("items/7")
    except OAuthTokenError as e:
        print(e.token_url)  # the token endpoint, not "items/7"
        if isinstance(e.__cause__, HTTPResponseError):
            print(e.__cause__.status)  # what the token endpoint answered
    except HTTPResponseError as e:
        print(e.status)  # an error from "items/7" itself
```

A `response_data_type` decode error is never wrapped because it already belongs to the call that
raised it; a token decode error is wrapped because it doesn't.
