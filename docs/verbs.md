---
icon: lucide/route
---

# Verbs

Every verb below exists on both `HTTPClient` and `SyncHTTPClient`. All examples assume:

```python
async with HTTPClient(base_url="https://api.example.com/") as client:
    ...
```

!!! warning "Write paths without a leading `/`"

    Paths join onto `base_url` by the standard URL rules, so a leading `/` replaces the base's
    own path instead of adding to it:

    ```python
    async with HTTPClient(base_url="https://api.example.com/v2/") as client:
        await client.get("users")  # https://api.example.com/v2/users
        await client.get("/users")  # https://api.example.com/users — the /v2/ is gone
    ```

    `base_url` itself must end with `/` when it has a path (`.../v2/`, not `.../v2`); lothc
    raises `ValueError` otherwise.

## GET

```python
response = await client.get("items/7")
response.data  # the body as bytes, by default

response = await client.get(
    "items/7",
    params={"q": "pikachu", "page": 2},
    headers={"x-custom": "header-value"},
    response_data_type=ItemModel,
)
response.data  # ItemModel(id=7, name="item-7")
```

Every verb returns a `Response`: the decoded body on `.data`, plus the status, headers and more
(see [The `Response`](#the-response) below).

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


response = await client.get(
    "items", params=SearchParams(q="pikachu", page=1), response_data_type=SearchResult
)
response = await client.get(
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

### Top-level arrays and other shapes

`response_data_type` also accepts a pydantic `TypeAdapter` or a msgspec `Decoder`, which is how a
top-level JSON array (or any shape that isn't one model) gets a typed decode. Build them once and
reuse them:

```python
from msgspec.json import Decoder
from pydantic import TypeAdapter

items_adapter = TypeAdapter(list[ItemModel])
response = await client.get("items", response_data_type=items_adapter)
response.data  # list[ItemModel]
response = await client.get("items", response_data_type=Decoder(list[ItemStruct]))
response.data  # list[ItemStruct]
```

`response_data_type=dict` is for a JSON *object*; decoding an array (or a string, number, `null`)
into it raises a `ValueError` that says what the body actually was.

## The `Response`

`get`, `post`, `put`, `patch`, `delete` and `head` all return a `Response`:

```python
response = await client.get("items/7", response_data_type=ItemModel)
response.data  # ItemModel(id=7, name="item-7")
response.status  # 200
response.headers  # {"content-type": "application/json", ...}
response.request.method  # "GET"
response.request.url  # "https://api.example.com/items/7"
response.request.path  # "/items/7"
response.request.host  # "api.example.com"
response.http_version  # "HTTP/1.1"
response.elapsed  # 0.042: seconds from sending to having the whole response
```

`response.elapsed` includes any retries, since it spans the whole call.

`response.request` is the request actually sent — useful for logging, or for telling apart which
call a `Response` came from when you're juggling several. It reflects the target you asked for, not
necessarily the one a final response came from if redirects were followed.

`response.headers` is a `CaseInsensitiveDict`, so header names compare case-insensitively the way
HTTP itself defines them (RFC 9110 §5.1) — `response.headers["Content-Type"]`,
`response.headers["content-type"]` and `"CONTENT-TYPE" in response.headers` are all the same lookup.
It's a `MutableMapping`, not a `dict` subclass (the same choice requests, niquests and httpx all
make), so `isinstance(response.headers, dict)` is `False`; iterating it yields keys with the casing
they arrived in, and only lookups, `in` and `==` ignore case.

A header sent more than once — `Set-Cookie`, most often — keeps every value. Indexing gives the
first, as it always has; `get_all` gives all of them, in arrival order, and `[]` for a header that
wasn't sent at all:

```python
response.headers["set-cookie"]
# 'session=abc; Path=/'

response.headers.get_all("Set-Cookie")
# ['session=abc; Path=/', 'csrf=xyz; Path=/', 'theme=dark; Path=/']

response.headers.get_all("never-sent")
# []
```

Iteration, `.items()` and `dict(response.headers)` stay single-valued (one entry per header name,
its first value), so `get_all` is the only way to see a repeated header's extra values.

Equality follows the same split. Two header maps compare every value, so two responses that differ
only in a dropped `Set-Cookie` aren't equal. Against a plain `dict`, which can only hold one
value per name, only the first value of each is compared. So with repeated headers, equality isn't
transitive: two maps can each equal the same `dict` without equalling each other (requests'
`CaseInsensitiveDict` behaves the same way).

Pass `response_headers_type` (a `BaseModel`/`Struct`) to get the *response* headers validated and parsed
too, via `response.typed_headers`. Header names are lowercased and `-` becomes `_` before matching
against your type's field names, so a `Content-Type` response header maps onto a `content_type`
field:

```python
class ItemHeaders(BaseModel):
    content_type: str | None = None


response = await client.get(
    "items/7", response_data_type=ItemModel, response_headers_type=ItemHeaders
)
response.typed_headers.content_type  # "application/json"
```

`response_headers_type` works the same way on `head()` — see below.

## POST, PUT, PATCH

All three take the same body options — provide at most one of `json`, `data`, `form` or
`content` (passing more than one raises `ValueError`):

```python
await client.post("items", json={"name": "new-item"})
await client.put("items/7", json=ItemModel(id=7, name="replaced"))
await client.patch("items/7", json={"name": "renamed"})
```

`json` also accepts a `BaseModel`/`Struct` directly (serialized for you, by alias for both
libraries: a pydantic model is encoded by its aliases unless it sets `serialize_by_alias=False`,
matching msgspec's `rename=`), or a top-level list
for a JSON array body; it always sends `Content-Type: application/json`, replacing any you set.

`data` sends a urlencoded body (`application/x-www-form-urlencoded`, what a plain HTML form
submits), as `data=` does in requests and httpx. It takes the same values as `params`: a `dict`
(a `list`/`tuple` value repeats the key, a `bool` goes out as `true`/`false`) or a
`BaseModel`/`Struct`, whose `None` fields are omitted:

```python
await client.post("login", data={"user": "ash", "pass": "pikachu"})  # user=ash&pass=pikachu
```

`form` sends a `multipart/form-data` body: fields, JSON parts, raw bytes and files (see
below).

`content` sends a raw `str`/`bytes` body as-is and sets **no** `Content-Type` at all (unlike
httpx/requests, which default a `str` to `text/plain`), so pass your own in `headers` if the
server needs one:

```python
await client.post("render", content="# Title", headers={"content-type": "text/markdown"})
```

Each returns a `Response` like `get`'s, including the same `response_headers_type` option:

```python
response = await client.post("items", json={"name": "new-item"}, response_data_type=ItemModel)
response.data  # ItemModel(...)
response.status  # 201
```

### Multipart forms — `form`

`form` builds a real `multipart/form-data` body from a `dict`, one part per key. Each value's
type decides how it's sent:

- `str`/`int`/`float`/`bool` — a plain form field. A `bool` goes out as `"true"`/`"false"`,
  the same way `params=` sends one.
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
  always means "JSON-encode me as one part," never "repeat" and never a file, whatever it holds.
  Every element must be the same kind of value — all fields, all raw binary, all files, or all
  JSON. A mixed tuple is a type error, and raises `TypeError` if you reach it at runtime anyway;
  an empty tuple raises `ValueError` rather than quietly contributing no parts at all. In
  particular `(b"...", "image/png")`
  is **not** a file paired with its content-type: only a `Path` carries a filename of its own
  (hence the `(Path, content_type)` shape above), so `bytes` always needs the explicit
  `(filename, content, content_type)` spelling instead.

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
response = await client.delete("items/7", response_data_type=ItemModel)
response.data  # ItemModel(...)
```

A body is rare on a DELETE but allowed (some APIs, like bulk or delete-by-query endpoints, need
one), with the same `json`/`data`/`form`/`content` options as `post`:

```python
await client.delete("items", json={"ids": [7, 8]})
```

## HEAD

Headers-only — no body is ever decoded, so there's no `response_data_type`:

```python
response = await client.head("items/7")
response.status  # 200
response.headers
response.data  # always None
```

It takes the same `response_headers_type` option as `get`, via `response.typed_headers`.

## Downloading large bodies — `download`

`get()`'s default `bytes` return is fine for the small JSON bodies this library is designed
around, but for something genuinely large (a presigned S3 GET URL, a big export) it costs extra
memory copies internally. `download()` is a `get`-shaped verb built to avoid that:

```python
body = await client.download("exports/large-file.csv")  # bytes, ~2/3 the peak memory of get()

await client.download("exports/large-file.csv", dest="large-file.csv")  # None returned
```

With no `dest`, the body still ends up fully in memory as `bytes`, just streamed into one buffer
instead of copied several times along the way: measured on a 50MB body, `download()` peaks at
about 105MB against `get()`'s 154MB. Pass `dest` (a `str` or any path-like) to stream straight to
disk instead — memory then stays O(chunk size) regardless of how large the body is, and the call
returns `None` rather than the body. The client's `timeout` is the longest allowed gap between
chunks here, not a cap on the whole download, so a long download of a big file isn't cut off at
30s; see [Streaming → Timeouts](streaming.md#timeouts).

The file only appears at `dest` once the whole body has arrived. It's written to a hidden sibling
first and renamed into place on success, so a download that fails partway (a dropped connection,
a cancelled task) never leaves a truncated file behind, and an existing file at `dest` is left
exactly as it was. Writes happen on the calling thread even for the async client: each chunk
write takes microseconds, and moving them to a thread measured 2.5-3.8x slower. `download()` only ever does a `GET`; there's no
`json`/`data`/`form`/`content` body option and no `response_data_type` — the response is always raw
bytes, on disk or in memory. Same `params`/`headers`/`error_for_status` as every other verb.
