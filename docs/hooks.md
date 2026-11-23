---
icon: lucide/activity
---

# Request hooks

`request_hook=` runs your code around every attempt at a request, on `HTTPClient` and
`SyncHTTPClient` alike: say, a tracing span per attempt, with its trace headers sent on that
attempt.

```python
from collections.abc import Generator
from contextlib import contextmanager

from lothc import HTTPClient, RequestHook, RequestInfo


@contextmanager
def trace(request: RequestInfo) -> Generator[RequestHook]:
    with tracer.trace("http.request") as span:
        hook = RequestHook()
        inject(span, hook.headers)
        yield hook
        span.set_tag("http.status_code", hook.status)


async with HTTPClient(base_url="https://api.example.com/", request_hook=trace) as client:
    await client.get("items/7")
```

The hook is a plain function (not an `async` one, for either client) that takes the request's
`RequestInfo` and returns a context manager yielding a `RequestHook`. `RequestInfo` has the
method and the full URL, `base_url` joined and query included.

| `RequestHook` field | |
| --- | --- |
| `headers` | headers to send on this attempt, empty at first; each replaces a header of the same name the request already has (`headers=`, `default_headers`, the auth) |
| `status` | the response's status, set by lothc once a response arrives; `None` until then, and if none does |

## When it runs

- **Once per attempt.** Each retry gets a context of its own, entered afresh with fresh headers,
  and the one before it has exited before the backoff wait. A redirect chain is one attempt, and
  so is re-authenticating a 401.
- **Streaming verbs too.** `stream_get`, `stream_post`, `download` and each `sse()` reconnect
  enter it like any other attempt.
- **Until the response is in.** The context exits once the body has been read, or for a
  streaming verb once the response's headers have arrived.

An exception from the attempt, a transport error, an `HTTPResponseError` (its `status` already
set) or an `HTTPDecodeError`, is raised into the context, as a `with` block raising it would. The
call raises it whatever the hook does: a hook can't swallow it. That includes a streaming
verb's `HTTPTimeoutError` when no headers arrive in time; on `SyncHTTPClient`, whose stream waits
on a worker thread, the hook exits with it once that wait ends. A request that can't be built (a
bad header value, a malformed URL) raises before the hook is entered: it never was an attempt.

With `lothc.testing`'s mocker, a mocked request carries the hook's headers like any other.
