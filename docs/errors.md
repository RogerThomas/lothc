---
icon: lucide/octagon-alert
---

# Error handling

Two separate failure classes, never unified:

- **`HTTPResponseError`** — the server answered with a 4xx/5xx. Carries `.status` and `.body_start`
  (the first 100 bytes of the body, for a quick look without decoding), plus `.parsed_body`.
- **`HTTPTransportError`** — never got a response at all: a `HTTPTimeoutError` or `HTTPConnectionError`
  subclass. pyreqwest's own exception types never leak through; they're translated to these at
  every call site.

```python
from lothc import HTTPClient, HTTPConnectionError, HTTPResponseError, HTTPTimeoutError

async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
    try:
        await client.get("pokemon/does-not-exist")
    except HTTPResponseError as e:
        print(e.status, e.body_start)
    except HTTPTimeoutError:
        print("took too long")
    except HTTPConnectionError:
        print("never reached the server")
```

## Typed error bodies

`.parsed_body` is `None` unless you pass `error_type` — every verb takes it, mirroring
`response_data_type`, and it decodes the 4xx/5xx body onto `HTTPResponseError.parsed_body`
instead of leaving it `None`:

```python
from pydantic import BaseModel


class APIError(BaseModel):
    type: str
    title: str
    status: int


async with HTTPClient.build(base_url="https://api.example.com/") as client:
    try:
        await client.get("items/7", error_type=APIError)
    except HTTPResponseError as e:
        print(e.parsed_body)  # APIError(type=..., title=..., status=...)
```

A validation error from whichever decode library you picked (`pydantic.ValidationError`,
`msgspec.ValidationError`) is never wrapped, on either the success (`response_data_type`) or
error (`error_type`) path — it propagates as-is, since choosing that library is opting into its
own exception too.
