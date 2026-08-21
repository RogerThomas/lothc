---
icon: lucide/key-round
---

# Authentication

Two ways to send a `Bearer` `Authorization` header — provide at most one:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/", bearer_token="my-static-token"
) as client:
    await client.get("items/7")
```

`bearer_token` is a static string, sent as-is on every request. For a token that expires or
rotates, pass `bearer_auth` instead — an async callable (sync callable on `SyncHTTPClient`)
resolved fresh on *every* request, not just once at `build()` time:

```python
async def get_current_token() -> str:
    return await token_store.get_access_token()  # e.g. refreshed from a cache or auth server


async with HTTPClient.build(
    base_url="https://api.example.com/", bearer_auth=get_current_token
) as client:
    await client.get("items/7")  # calls get_current_token() for this request
    await client.get("items/8")  # calls it again — always the latest token
```

Both mechanisms only ever produce a `Bearer` `Authorization` header — there's no separate
Basic-auth or custom-scheme option today.

## Default headers

For anything that isn't a `Bearer` token — an API key header, a custom user-agent, whatever your
API needs on every request — pass `default_headers` at `build()` time. Unlike `bearer_auth`,
these are fixed for the client's whole lifetime, resolved once, not per-request:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/",
    default_headers={"x-api-key": "my-api-key"},
) as client:
    await client.get("items/7")  # sent with every request through this client
```

Combine freely with `bearer_token`/`bearer_auth` — they set different headers
(`Authorization` vs. whatever you name here).

## Timeouts

`timeout` (seconds, default `30.0`) applies to the whole client, covering every request made
through it:

```python
async with HTTPClient.build(base_url="https://api.example.com/", timeout=5.0) as client:
    await client.get("items/7")  # raises HTTPTimeoutError if this takes longer than 5s
```

Pass `timeout=None` to disable it and fall back to pyreqwest's own default. See
[Error handling](errors.md) for `HTTPTimeoutError`.
