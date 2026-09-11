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

### Repeated query params

A `list`/`tuple` of `str | int | float | bool` in `params` sends that key once per element,
verbatim — lothc has no opinion on what an element means (it's never a `key=value` pair to lothc,
just another occurrence of the key):

```python
await client.get("items", params={"tag": ["ready", "pending"], "limit": 10})
# -> ?tag=ready&tag=pending&limit=10
```

## GET, with status and headers — `get_result`

Same signature as `get`, but returns a `Result` carrying the decoded body alongside the status
code and response headers:

```python
result = await client.get_result("items/7", response_data_type=ItemModel)
result.data  # ItemModel(id=7, name="item-7")
result.status  # 200
result.headers  # {"content-type": "application/json", ...}
result.request.method  # "GET"
result.request.url  # "https://api.example.com/items/7"
result.request.path  # "/items/7"
result.request.host  # "api.example.com"
```

`result.request` is the request actually sent — useful for logging, or for telling apart which
call a `Result` came from when you're juggling several. It reflects the target you asked for, not
necessarily the one a final response came from if redirects were followed.

Pass `response_headers_type` (a `BaseModel`/`Struct`) to get the *response* headers validated and parsed
too, via `result.typed_headers`. Header names are lowercased and `-` becomes `_` before matching
against your type's field names, so a `Content-Type` response header maps onto a `content_type`
field:

```python
class ItemHeaders(BaseModel):
    content_type: str | None = None


result = await client.get_result(
    "items/7", response_data_type=ItemModel, response_headers_type=ItemHeaders
)
result.typed_headers.content_type  # "application/json"
```

`response_headers_type` works the same way on `head()` — see below.

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

Each has a `_result` variant too — `post_result`/`put_result`/`patch_result` — same body options,
but returning a `Result` alongside status and headers, exactly like `get_result` above (including
the same `response_headers_type` option):

```python
result = await client.post_result("items", json={"name": "new-item"}, response_data_type=ItemModel)
result.data  # ItemModel(...)
result.status  # 200
```

### Multipart forms and file uploads

`form` builds a real `multipart/form-data` body from a `dict`. Each value's type decides how
it's sent:

- `str`/`int` — a plain form field.
- `bytes` — a form field too (no filename), for raw binary data that isn't a "file" as such.
- `list`/`dict` (or a `BaseModel`/`Struct` instance) — JSON-encoded as that one part's body, with
  `Content-Type: application/json` set automatically.
- A **file**, in one of four shapes:
  - `pathlib.Path` — read and streamed from disk; filename is the path's own `.name`.
  - `(filename, bytes | Path | file_object)` — an explicit filename paired with the content.
  - `(filename, bytes | Path | file_object, content_type)` — same, plus an explicit content-type
    override.
  - `(Path, content_type)` — keeps the path's own auto-derived filename, but overrides just the
    content-type.
  - An already-opened binary file object (anything `io.BufferedIOBase`, e.g. `open(path, "rb")` or
    a `BytesIO`) also works directly, filename taken from `.name` if the object has one.
- **A `tuple` of any of the above** repeats that field name once per element — multiple parts,
  all sharing the same name (a real `multipart/form-data` capability, not something most HTTP
  client libraries expose). Note this is a `tuple` specifically, not a `list` — a `list` value
  always means "JSON-encode me as one part," never "repeat."

By default, a file part's content-type is guessed from its filename's extension (via the stdlib
`mimetypes` module) unless you gave one explicitly. Pass `infer_mime_type_from_file_extension=False`
to disable guessing — the part then goes out with no `Content-Type` header at all unless you set
one explicitly.

```python
from pathlib import Path

await client.post(
    "upload",
    form={
        "note": "shiny",
        "avatar": b"raw-bytes-field",  # a field, not a file (no filename)
        "manual": Path("pikachu-manual.pdf"),  # a file, filename + content-type inferred
        "photo": ("photo.png", b"...png-bytes..."),  # a file, explicit filename, mime inferred
        "scan": ("scan.bin", b"...bytes...", "application/pdf"),  # explicit content-type override
        "tags": ["shiny", "starter"],  # one part, JSON-encoded array body
        "photos": (("a.png", b"..."), ("b.png", b"...")),  # two parts, both named "photos"
    },
)
```

## DELETE

```python
await client.delete("items/7")  # bytes by default
item = await client.delete("items/7", response_data_type=ItemModel)
```

`delete_result` mirrors `get_result` too — same `response_data_type`/`response_headers_type`
options, returning a `Result` instead of the bare decoded body.

## HEAD

Headers-only — no body is ever decoded, so there's no `response_data_type`:

```python
result = await client.head("items/7")
result.status  # 200
result.headers
```

Same `response_headers_type` option as `get_result`, via `result.typed_headers`.

## Downloading large bodies — `download`

`get()`'s default `bytes` return is fine for the small JSON bodies this library is designed
around, but for something genuinely large (a presigned S3 GET URL, a big export) it costs extra
memory copies internally. `download()` is a `get`-shaped verb built to avoid that:

```python
body = await client.download("exports/large-file.csv")  # bytes, ~1/3 the peak memory of get()

await client.download("exports/large-file.csv", dest=Path("large-file.csv"))  # None returned
```

With no `dest`, the body still ends up fully in memory as `bytes`, just streamed into one buffer
instead of copied several times along the way. Pass `dest: Path` to stream straight to disk
instead — memory then stays O(chunk size) regardless of how large the body is, and the call
returns `None` rather than the body. `download()` only ever does a `GET`; there's no
`json`/`form`/`content` body option and no `response_data_type` — the response is always raw
bytes, on disk or in memory. Same `params`/`headers`/`error_for_status` as every other verb.
