---
icon: lucide/octagon-alert
---

# Error handling

Two separate failure classes, never unified:

- **`ResponseError`** — the server answered with a 4xx/5xx. Carries `.status` and `.body_start`
  (the first 100 bytes of the body, for a quick look without decoding).
- **`TransportError`** — never got a response at all: a `HTTPTimeoutError` or `HTTPConnectionError`
  subclass. pyreqwest's own exception types never leak through; they're translated to these at
  every call site.

```python
from lothc import HTTPClient, HTTPConnectionError, HTTPTimeoutError, ResponseError

async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
    try:
        await client.get("pokemon/does-not-exist")
    except ResponseError as e:
        print(e.status, e.body_start)
    except HTTPTimeoutError:
        print("took too long")
    except HTTPConnectionError:
        print("never reached the server")
```

A validation error from whichever decode library you picked (`pydantic.ValidationError`,
`msgspec.ValidationError`, `typeguard.TypeCheckError`) is never wrapped — it propagates as-is,
since choosing that library as a `response_data_type` is opting into its own exception too.
