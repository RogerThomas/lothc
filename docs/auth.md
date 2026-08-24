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

`bearer_token`/`bearer_auth` both only ever produce a `Bearer` `Authorization` header. For a
Basic-auth `username`/`password` pair instead, pass `basic_auth` — provide at most one of the
three:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/", basic_auth=("my-username", "my-password")
) as client:
    await client.get("items/7")
```

`password` may be `None` for a username with no password. There's no other custom-auth-scheme
option today.

Every verb also takes `skip_auth`, which omits the `Authorization` header for that one call —
useful when a client configured with `bearer_token`/`bearer_auth`/`basic_auth` also needs to hit a
differently-authenticated target through the same instance, e.g. a presigned S3 URL that must
never see your API's own token:

```python
async with HTTPClient.build(
    base_url="https://api.example.com/", bearer_token="my-static-token"
) as client:
    await client.get("items/7")  # gets the Authorization header
    await client.get("https://presigned-bucket.example.com/file", skip_auth=True)  # doesn't
```

`skip_auth=True` also skips *calling* `bearer_auth` for that request — if refreshing the token is
expensive (a network round-trip to an auth server, say), that cost isn't paid on a skipped call.

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

Combine freely with `bearer_token`/`bearer_auth`/`basic_auth` — they set different headers
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

Every verb also takes its own `timeout`, overriding the client's for that one call only:

```python
async with HTTPClient.build(base_url="https://api.example.com/", timeout=5.0) as client:
    await client.get("items/7")  # uses the client default, 5s
    await client.get("exports/large-file.csv", timeout=60.0)  # this call gets 60s instead
```

There's no way to make a single call wait forever when the client itself has a finite
`timeout` — pyreqwest's own per-request `.timeout()` only ever accepts a duration, never a
sentinel meaning "no timeout." To remove the limit entirely, build the client with
`timeout=None` instead.
