---
icon: lucide/octagon-alert
---

# Error handling

Two separate failure classes, never unified:

- **`HTTPResponseError`** — the server answered with a 4xx/5xx. Carries `.status`, `.headers` (the
  error response's own, e.g. a `Retry-After` once retries are exhausted, or a request id to quote
  to support), `.body` (the whole body) and `.body_start` (its first 100 bytes, as used in the
  message), `.error` (the body decoded, see [Typed error bodies](#typed-error-bodies)), and
  `.request` (the request that got this response —
  method/url/path/host; handy for telling which call failed when several are in flight at once).
  It pickles, so it can cross a process boundary (multiprocessing, a task queue).
- **`HTTPTransportError`** — no usable response: either none arrived at all, or the connection
  failed partway through one (a body cut short raises `HTTPConnectionError`). Its subclasses are
  `HTTPTimeoutError` and `HTTPConnectionError`. pyreqwest's own exception types never leak
  through; they're translated to these at every call site.

Using a client after its `with`/`async with` block has exited raises a plain `RuntimeError`: that's
a bug in the calling code, not a network failure, so it deliberately isn't an `HTTPTransportError`
that a retry handler would swallow. And an `into=dict` body that isn't valid JSON
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

Every `HTTPResponseError` decodes its body onto `.error`, best-effort, with one of three outcomes:

| Body | `.error` | `.parse_error` |
| --- | --- | --- |
| JSON matching the error type you pinned (below) | that type | `None` |
| any other JSON object | `dict[str, Any]` | why it didn't match, if you pinned a type |
| not a JSON object (HTML, text, an array, empty) | `None` | as above; the raw bytes stay on `.body` |

With nothing pinned, the first row never applies: a JSON object is a `dict`, anything else `None`.

To get your own type, pin it on a one-line subclass and pass that as `raises=`. Set it once on the
client, since an API's error schema is usually the same on every endpoint, and override it on a
call that answers with a different shape:

```python
from msgspec import Struct

from lothc import HTTPClient, HTTPResponseError


class NotFound(Struct, tag_field="type", tag="not-found"):
    title: str


class NotAuthorized(Struct, tag_field="type", tag="not-authorized"):
    title: str


class LoginFailed(Struct):
    reason: str


type APIError = NotFound | NotAuthorized


class MyAPIError(HTTPResponseError[APIError]):
    pass


class LoginError(HTTPResponseError[LoginFailed]):
    pass


async with HTTPClient(base_url="https://api.example.com/", raises=MyAPIError) as client:
    try:
        await client.get("items/7")
    except MyAPIError as e:
        match e.error:  # NotFound | NotAuthorized | dict[str, Any] | None
            case NotFound():
                print("no such item:", e.error.title)
            case NotAuthorized():
                print("log in first")
            case dict():
                print("unexpected JSON:", e.error, "because", e.parse_error)
            case None:
                print("not JSON:", e.status, e.body)

    try:
        await client.post("login", json={"user": "user"}, raises=LoginError)
    except LoginError as e:
        print(e.error)  # LoginFailed | dict[str, Any] | None
```

It's a subclass because Python can't `except` a parametrized generic. lothc reads the pinned type
back off the class; subclasses inherit it and pickle as themselves (define them at module level).
An unusable `raises=` raises `TypeError` at construction, or before sending when passed to a verb.

Decoding an error body is best-effort, since that's where a server is least likely to honour its
contract (a proxy's HTML 502): a mismatch only leaves `.error` a `dict` or `None`, with the reason
on `.parse_error` (always a `ValueError`). On the success path an `into` mismatch raises the decode
library's own error (`pydantic.ValidationError`, `msgspec.ValidationError`), unwrapped.

## `OAuthTokenError`

`OAuthProvider`/`SyncOAuthProvider` (see [Authentication](auth.md#oauth-2-client-credentials)) run
*inside* whatever request needed the token, so every failure to obtain one is wrapped in
`OAuthTokenError`, with the original exception as `__cause__` and the endpoint in `.token_url`,
rather than surfacing as a failure of that unrelated call:

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
