---
icon: lucide/refresh-cw
---

# Retries

```python
async with HTTPClient(base_url="https://api.example.com/", max_retries=3) as client:
    await client.get("items/7")
```

Implemented as a real pyreqwest `with_middleware` hook — retries on a transport failure or a
`429`/`500`/`502`/`503`/`504` response, with exponential backoff, honoring a `Retry-After`
header when the server sends one.

By default, only the idempotent verbs retry: `get`, `put`, `delete`, `head`. `post`/`patch` need
an explicit opt-in, since retrying a non-idempotent request can duplicate side effects:

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    max_retries=3,
    retry_methods={"GET", "POST"},
) as client:
    ...
```

## Backoff

The wait between attempts is `backoff_base * 2 ** attempt`, so the default `backoff_base=0.1`
waits 0.1s, then 0.2s, then 0.4s. A `Retry-After` header on the response wins over the computed
delay, up to `max_retry_after` (default 60s). A server asking for a longer wait ends retrying:
its 429/503 is raised as `HTTPResponseError`, so you can schedule a retry from
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

## What is never retried

A request that could never be *built* — a non-`https` URL under `https_only=True`, a malformed
URL — fails immediately rather than being retried, since no amount of waiting changes the
outcome. It still raises the same `HTTPTransportError` as any other transport failure.

This applies to `sse()`'s reconnect loop too: a permanently unbuildable request raises on the
first attempt instead of working through `max_reconnects` × `reconnect_delay` first.

Two more things the retry middleware never retries, because pyreqwest reports neither as a
transport error, which is all the middleware retries:

- **A redirect loop** (past `max_redirects`). `sse()`'s reconnect loop does reconnect on one,
  since a redirect loop can be transient, but a plain verb raises it straight away.
- **A body cut short mid-read.** pyreqwest reads a non-streamed body in full before handing the
  response back, and reports a truncated one as a decode error rather than a transport error, so
  it raises `HTTPConnectionError` without a retry.

`retry_methods` is case-insensitive (`{"get"}` works), and an empty collection means "retry
nothing": with `max_retries` set, pass `retry_methods=set()` to switch retries off per client.
