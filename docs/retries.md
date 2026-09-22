---
icon: lucide/refresh-cw
---

# Retries

```python
async with HTTPClient.build(base_url="https://api.example.com/", max_retries=3) as client:
    await client.get("items/7")
```

Implemented as a real pyreqwest `with_middleware` hook — retries on a transport failure or a
`429`/`500`/`502`/`503`/`504` response, with exponential backoff, honoring a `Retry-After`
header when the server sends one.

By default, only the idempotent verbs retry: `get`, `put`, `delete`, `head`. `post`/`patch` need
an explicit opt-in, since retrying a non-idempotent request can duplicate side effects:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/",
    max_retries=3,
    retry_methods=frozenset({"GET", "POST"}),
) as client:
    ...
```

## Backoff

The wait between attempts is `backoff_base * 2 ** attempt`, so the default `backoff_base=0.1`
waits 0.1s, then 0.2s, then 0.4s. A `Retry-After` header on the response always wins over the
computed delay:

```python
async with HTTPClient.build(
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
first attempt instead of working through `max_reconnects` × `reconnect_delay` first. A redirect
loop (past `max_redirects`) is *not* treated this way — that can be transient, so it stays
retryable.
