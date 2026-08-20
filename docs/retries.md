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
