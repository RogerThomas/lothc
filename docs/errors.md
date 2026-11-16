---
icon: lucide/octagon-alert
---

# Error handling

Three failure classes, never unified:

- **`HTTPResponseError`** — the server answered with a 4xx/5xx. Carries `.status`, `.headers` (the
  error response's own, e.g. a `Retry-After` once retries are exhausted, or a request id to quote
  to support), `.body` (the whole body) and `.body_start` (its first 100 bytes, as used in the
  message), `.problem` (the body decoded, see [Error classes](#error-classes)), and
  `.request` (the request that got this response —
  method/url/path/host; handy for telling which call failed when several are in flight at once).
  It pickles, so it can cross a process boundary (multiprocessing, a task queue).
- **`HTTPDecodeError`** — a successful response that didn't decode into `into=`. See
  [below](#httpdecodeerror).
- **`HTTPTransportError`** — no usable response: either none arrived at all, or the connection
  failed partway through one (a body cut short raises `HTTPConnectionError`). Its subclasses are
  `HTTPTimeoutError` and `HTTPConnectionError`. pyreqwest's own exception types never leak
  through; they're translated to these at every call site.

Using a client after its `with`/`async with` block has exited raises a plain `RuntimeError`: that's
a bug in the calling code, not a network failure, so it deliberately isn't an `HTTPTransportError`
that a retry handler would swallow.

```python
from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPDecodeError,
    HTTPResponseError,
    HTTPTimeoutError,
)

async with HTTPClient(base_url="https://pokeapi.co/api/v2/") as client:
    try:
        await client.get("pokemon/does-not-exist", into=dict)
    except HTTPResponseError as e:
        print(e.status, e.body_start)
        print(e.request.method, e.request.path)  # "GET", "/api/v2/pokemon/does-not-exist"
    except HTTPDecodeError as e:
        print("not a JSON object:", e.parse_error)
    except HTTPTimeoutError:
        print("took too long")
    except HTTPConnectionError:
        print("never reached the server")
```

## Error classes

Give each error body a struct (`...Problem`) and each error an `HTTPResponseError` subclass
(`...Error`) that pins that struct and says which statuses it's for. A struct several errors share
gets a general name, like `Problem`. Pass the classes as `raises=`, once on the client, since an
API's errors are usually the same on every endpoint:

```python
from typing import Literal

from msgspec import Struct

from lothc import HTTPClient, HTTPResponseError


class QuotaExceededProblem(Struct):  # 429: {"limit": 100, "used": 100}
    limit: int
    used: int


class Problem(Struct):  # 404: {"detail": "No such job"}
    detail: str


class DocumentLockedProblem(Struct):  # any status: {"code": "document_locked", "locked_by": "sam"}
    code: Literal["document_locked"]
    locked_by: str


class QuotaExceededError(HTTPResponseError[QuotaExceededProblem], status=429):
    pass


class JobNotFoundError(HTTPResponseError[Problem], status=404):
    pass


class DocumentLockedError(HTTPResponseError[DocumentLockedProblem]):  # no status=: any 4xx/5xx
    pass


async with HTTPClient(
    base_url="https://api.example.com/",
    raises=[QuotaExceededError, JobNotFoundError, DocumentLockedError],
) as client:
    try:
        job = (await client.post("jobs", content=document, into=Job)).data
    except QuotaExceededError as e:
        print(e.problem.used, e.problem.limit)
    except DocumentLockedError as e:
        print(e.problem.locked_by)
    except HTTPResponseError as e:  # nothing matched
        print(e.status, e.problem)  # a dict for a JSON object, else None; e.body has the bytes
```

A 4xx/5xx is raised as the first class in the list whose `status=` covers it and whose struct
decodes the body; if none does, it's plain `HTTPResponseError`. So `DocumentLockedError`, with no
`status=`, is chosen by the body's `code` alone, and a proxy's HTML 502 is still a plain
`HTTPResponseError`. `.problem` is then:

| Raised as | `.problem` |
| --- | --- |
| a class that pins a type (`QuotaExceededError`) | that type, exactly |
| a class that pins none, or plain `HTTPResponseError` | the body as a `dict` if it's a JSON object, else `None` |

On a plain `HTTPResponseError`, `.parse_error` says why the first class for its status didn't
decode the body (`None` if no class was for it), and `.body` always has the raw bytes.

- `status=` takes an int or several (a tuple, `range(500, 600)`), each 400 to 599. A subclass
  inherits its parent's.
- List specific classes before general ones. A class that can never be raised, because one listed
  before it already takes every body for its statuses, is a `TypeError` when `raises=` is resolved:
  at construction, or before sending for a call's own.
- A verb's `raises=` (one class or a list) replaces the client's for that call:
  `await client.get("jobs/7", raises=JobNotFoundError)`.

To catch all of one API's errors in one `except`, give its classes a generic base and list the base
last, where, pinning nothing, it takes every 4xx/5xx the others didn't:

```python
class APIError[P = object](HTTPResponseError[P]):
    pass


class QuotaExceededError(APIError[QuotaExceededProblem], status=429):
    pass


raises = [QuotaExceededError, APIError]
```

They're subclasses because Python can't `except` a parametrized generic. lothc reads the pinned
type back off the class; subclasses inherit it and pickle as themselves (define them at module
level).

## Returning errors instead of raising

When an error status is an outcome you'd rather branch on than catch, pass
`raise_for_status=False` to the client. A 4xx/5xx then comes back as a `Response`, with the error
response's `.status` and `.headers`. Its `.error` is the exception the call would have raised
(`None` on every other response), and reading `.data` raises it. Nothing is decoded into `into=`
on an error:

```python
response = await client.post("jobs", content=document, into=Job)
match response.error:
    case QuotaExceededError() as error:
        print(error.problem.used, error.problem.limit)
    case HTTPResponseError(status=503):
        raise TryAgainLaterError()
    case HTTPResponseError() as error:  # any other error; not `case _`, which matches None too
        print("unknown status", error.status)
        raise error
job = response.data
```

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

## `HTTPDecodeError`

A response below 400 whose body doesn't decode into `into=` raises `HTTPDecodeError`, a
`ValueError`; so do headers that don't parse into `response_headers_type`, and a streamed line or
SSE event's data that doesn't decode. It carries the response (`.status`, `.headers`, `.body`,
`.body_start`, `.request`), and the decode library's own error as `.parse_error` (and
`__cause__`):

```python
try:
    job = (await client.get("jobs/7", into=Job)).data
except HTTPDecodeError as e:
    print(e.status, e.body_start, e.parse_error)  # e.g. a msgspec.ValidationError
```

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
