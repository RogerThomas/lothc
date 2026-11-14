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

## Returning errors instead of raising

When an error status is an outcome you'd rather branch on than catch, pass
`raise_for_status=False` to the client. A 4xx/5xx then comes back as a `Response`, with the error
response's `.status` and `.headers`, and reading `.data` raises the `HTTPResponseError` the call
would have raised:

```python
async with HTTPClient(base_url="https://api.example.com/", raise_for_status=False) as client:
    response = await client.get("jobs/7", into=Job)
    if response.status == 503:
        raise TryAgainLaterError()
    job = response.data  # Job, or raises HTTPResponseError for any other 4xx/5xx
```

Nothing is decoded into `into=` on an error. Instead `.raw_error` is the body's bytes and `.error`
is that body decoded as [`HTTPResponseError.error`](#typed-error-bodies) would be, on first read:

```python
response = await client.get("items/7", raises=MyAPIError)
match response.error:
    case NotFound() as missing:
        print("no such item:", missing.title)
    case NotAuthorized():
        print("log in first")
    case _ if response.status >= 400:
        print("unexpected error:", response.status, response.raw_error)
```

Both are `None` on any other response, but `.error is None` isn't a success test, since a non-JSON
error body decodes to `None` too: check `.status`.

Each verb that returns a `Response` takes `raise_for_status=` too, overriding the client's either
way:

```python
await client.get("items/7", raise_for_status=False)  # returns a 4xx/5xx, on any client
await client.delete("items/7", raise_for_status=True)  # raises, even on an opt-out client
```

The second line matters: on an opt-out client, a call whose result you ignore (a bare `delete` or
`post`) fails silently unless it passes `raise_for_status=True`. With raising off, `download` writes
nothing to `dest` for an error status, and exhausted retries return the last error response. The
streaming verbs (`stream_get`, `stream_post`, `sse`) always raise.

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
