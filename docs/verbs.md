---
icon: lucide/route
---

# Verbs

Every verb below exists on both `HTTPClient` and `SyncHTTPClient`. All examples assume:

```python
async with HTTPClient.build(base_url="https://api.example.com/") as client:
    ...
```

## GET

```python
body = await client.get("items/7")  # bytes by default
item = await client.get(
    "items/7",
    params={"q": "pikachu", "page": 2},
    headers={"x-custom": "header-value"},
    response_data_type=ItemModel,
)
```

### Typed params and headers

`params`/`headers` also accept a `BaseModel`/`Struct` instead of a plain mapping — fields set to
`None` are omitted rather than sent as `"None"`:

```python
from msgspec import Struct
from pydantic import BaseModel


class SearchParams(BaseModel):
    q: str
    page: int
    limit: int | None = None  # omitted from the query string entirely, not sent as "None"


class SearchStructParams(Struct):
    q: str
    page: int
    cursor: str | None = None  # same omission behavior, msgspec Struct instead of BaseModel


result = await client.get(
    "items", params=SearchParams(q="pikachu", page=1), response_data_type=SearchResult
)
result = await client.get(
    "items", params=SearchStructParams(q="pikachu", page=1), response_data_type=SearchResult
)
```

`headers` works the same way for outgoing request headers — pass a `BaseModel`/`Struct` instead
of a `dict[str, str]` and get the same `None`-omission for free.

## GET, with status and headers — `get_result`

Same signature as `get`, but returns a `Result` carrying the decoded body alongside the status
code and response headers:

```python
result = await client.get_result("items/7", response_data_type=ItemModel)
result.data  # ItemModel(id=7, name="item-7")
result.status  # 200
result.headers  # {"content-type": "application/json", ...}
```

Pass `headers_type` (a `BaseModel`/`Struct`) to get the *response* headers validated and parsed
too, via `result.typed_headers`. Header names are lowercased and `-` becomes `_` before matching
against your type's field names, so a `Content-Type` response header maps onto a `content_type`
field:

```python
class ItemHeaders(BaseModel):
    content_type: str | None = None


result = await client.get_result("items/7", response_data_type=ItemModel, headers_type=ItemHeaders)
result.typed_headers.content_type  # "application/json"
```

`headers_type` works the same way on `head()` — see below.

## POST, PUT, PATCH

All three take the same body options — provide at most one of `json`, `form`, or `content`
(passing more than one raises `ValueError`):

```python
await client.post("items", json={"name": "new-item"})
await client.put("items/7", json=ItemModel(id=7, name="replaced"))
await client.patch("items/7", json={"name": "renamed"})
```

`json` also accepts a `BaseModel`/`Struct` directly (serialized for you). `content` sends a raw
`str`/`bytes` body as-is.

### Multipart forms and file uploads

`form` builds a real `multipart/form-data` body from a `dict`. Each value's type decides how
it's sent:

- `str`/`int` — a plain form field.
- `bytes` — a form field too (no filename), for raw binary data that isn't a "file" as such.
- `pathlib.Path` — a file part, read and streamed from disk; the filename sent is the path's own
  `.name`.
- `(filename, bytes)` — a file part with an explicit filename, for in-memory content.

```python
from pathlib import Path

await client.post(
    "upload",
    form={
        "note": "shiny",
        "avatar": b"raw-bytes-field",  # a field, not a file (no filename)
        "manual": Path("pikachu-manual.pdf"),  # a file, filename = "pikachu-manual.pdf"
        "photo": ("photo.png", b"...png-bytes..."),  # a file, explicit filename
    },
)
```

## DELETE

```python
await client.delete("items/7")  # bytes by default
item = await client.delete("items/7", response_data_type=ItemModel)
```

## HEAD

Headers-only — no body is ever decoded, so there's no `response_data_type`:

```python
result = await client.head("items/7")
result.status  # 200
result.headers
```

Same `headers_type` option as `get_result`, via `result.typed_headers`.
