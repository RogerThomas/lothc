---
icon: lucide/refresh-cw
---

# Retries

```python
async with HTTPClient(base_url="https://api.example.com/", max_retries=3) as client:
    await client.get("items/7")
```

Retries a transport failure (including a body cut off partway) or a `429`/`500`/`502`/`503`/`504`,
with exponential backoff, honouring `Retry-After`. `retry_statuses` replaces those statuses (a set,
tuple or `range`, each 400 to 599); an empty one retries no status, only transport failures:

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    max_retries=3,
    retry_statuses={408, 429, 503, 529},
) as client:
    ...
```

By default, only the idempotent verbs retry: `get`, `put`, `delete`, `head`, `options`. `post`/`patch` need
an explicit opt-in, since retrying a non-idempotent request can duplicate side effects:

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    max_retries=3,
    retry_methods={"GET", "POST"},
) as client:
    ...
```

Every verb but `sse()` also takes `max_retries=`, which replaces the client's for that call, either
way, and retries even a verb outside `retry_methods`:

```python
await client.get("items/7", max_retries=0)  # sent once
await client.post("jobs", json={"id": 7}, max_retries=3)  # retried, though POST isn't opted in
```

## Backoff

The wait between attempts is `backoff_base * 2 ** attempt`, so the default `backoff_base=0.1`
waits 0.1s, then 0.2s, then 0.4s. A `Retry-After` header on the response wins over the computed
delay, up to `max_retry_after` (default 60s). A server asking for a longer wait ends retrying:
its 429/503 is raised as `HTTPResponseError` (or returned, with `raise_for_status=False`), so you can schedule a retry from
`e.headers["Retry-After"]` rather than a call silently blocking for as long as the server likes.
Pass `max_retry_after=None` to honour any wait:

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    max_retries=3,
    backoff_base=0.5,  # 0.5s, then 1.0s, then 2.0s
) as client:
    ...
```

## Deciding each retry

`retry=` takes a plain function (not an `async` one) that lothc asks about each failure it would
retry. It returns the seconds to wait before the next attempt, or `None` to stop, and the call then
raises that error (or returns it, with `raise_for_status=False`):

```python
from lothc import HTTPClient, HTTPResponseError, RetryAttempt


def retry_rule(retry: RetryAttempt) -> float | None:
    error = retry.error  # the error the call would raise
    if isinstance(error, QuotaExceededError):  # one of your raises= classes
        return None  # stop: retrying won't help
    if isinstance(error, HTTPResponseError):
        if error.headers.get("x-should-retry") == "false":
            return None
        if "retry-after-ms" in error.headers:
            return int(error.headers["retry-after-ms"]) / 1000
    return retry.delay  # lothc's own wait


async with HTTPClient(
    base_url="https://api.example.com/",
    max_retries=3,
    retry_statuses={408, 429, 500, 502, 503, 504, 529},
    raises=[QuotaExceededError],
    retry=retry_rule,
) as client:
    await client.get("jobs/7")
    await client.get("jobs/7", max_retries=0)  # this call doesn't retry
```

`QuotaExceededError` is a class like those in [Error classes](errors.md#error-classes).

| `RetryAttempt` field | |
| --- | --- |
| `attempt` | which retry this would be, `0` for the first |
| `error` | the error the call would raise: an `HTTPResponseError` (the matching `raises=` class, its `.problem` decoded) or an `HTTPTransportError` |
| `delay` | the wait lothc would take: the response's `Retry-After`, else the backoff |
| `method` | the upper-case HTTP method |
| `elapsed` | seconds since the call started |

The rule only decides failures lothc would already retry: it isn't asked once `max_retries` runs
out, nor about a verb outside `retry_methods`, a status outside `retry_statuses` or a `form=`
upload, and `sse()` asks it only when opening a connection, never for a reconnect. Its wait, not
the server's `Retry-After`, is the one checked against `max_retry_after`: a longer one stops
retrying, and a negative one retries at once. Anything but a finite number or `None` raises from
the call, as does an exception from the rule.

## What is never retried

- A request that can't be built (a non-`https` URL under `https_only=True`, a malformed URL); it
  raises `HTTPTransportError` at once, and `sse()` doesn't reconnect it either.
- A redirect loop past `max_redirects` (though `sse()` reconnects on one).
- A body that can never be decoded, such as a corrupt gzip body (`HTTPConnectionError`).

## Multipart `form=` requests are sent once

A multipart `form=` body is streamed as it's sent, so it can't be replayed. A `form=` request is
therefore sent exactly once, even on a verb in `retry_methods` or with its own `max_retries=`: a
`429`/`5xx` or a transport failure is raised straight away, the same as for a verb that isn't
retried. Every other body (`json=`, `data=`, `content=`) is held in memory and is replayed byte for
byte.

The same goes for re-authenticating on a `401` (see [Authentication](auth.md)): the rejected
token is still discarded, so the next call gets a fresh one, but the `form=` request itself is
raised rather than sent again. To retry an upload, catch the error and call the verb again; the
form is rebuilt from your `dict` (a `Path` is reopened) on every call.

`retry_methods` is case-insensitive (`{"get"}` works), and an empty collection means "retry
nothing": with `max_retries` set, pass `retry_methods=set()` to switch retries off per client.
