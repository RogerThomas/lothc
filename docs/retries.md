---
icon: lucide/refresh-cw
---

# Retries

```python
async with HTTPClient(base_url="https://api.example.com/", max_retries=3) as client:
    await client.get("items/7")
```

Retries a transport failure (including a body cut off partway) or a `429`/`500`/`502`/`503`/`504`,
with exponential backoff, honouring `Retry-After`.

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

## What is never retried

- A request that can't be built (a non-`https` URL under `https_only=True`, a malformed URL); it
  raises `HTTPTransportError` at once, and `sse()` doesn't reconnect it either.
- A redirect loop past `max_redirects` (though `sse()` reconnects on one).
- A body that can never be decoded, such as a corrupt gzip body (`HTTPConnectionError`).

## Multipart `form=` requests are sent once

A multipart `form=` body is streamed as it's sent, so it can't be replayed. A `form=` request is
therefore sent exactly once, even on a verb in `retry_methods`: a `429`/`5xx` or a transport
failure is raised straight away, the same as for a verb that isn't retried. Every other body
(`json=`, `data=`, `content=`) is held in memory and is replayed byte for byte.

The same goes for re-authenticating on a `401` (see [Authentication](auth.md)): the rejected
token is still discarded, so the next call gets a fresh one, but the `form=` request itself is
raised rather than sent again. To retry an upload, catch the error and call the verb again; the
form is rebuilt from your `dict` (a `Path` is reopened) on every call.

`retry_methods` is case-insensitive (`{"get"}` works), and an empty collection means "retry
nothing": with `max_retries` set, pass `retry_methods=set()` to switch retries off per client.
