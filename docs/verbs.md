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

`params`/`headers` also accept a `BaseModel`/`Struct` instead of a plain mapping — fields set to
`None` are omitted rather than sent as `"None"`.

## GET, with status and headers — `get_result`

Same signature as `get`, but returns a `Result` carrying the decoded body alongside the status
code and response headers:

```python
result = await client.get_result("items/7", response_data_type=ItemModel)
result.data  # ItemModel(id=7, name="item-7")
result.status  # 200
result.headers  # {"content-type": "application/json", ...}
```

Pass `headers_type` (a `BaseModel`/`Struct`) to get the response headers validated and parsed
too, via `result.typed_headers`.

## POST, PUT, PATCH

All three take the same body options — provide at most one of `json`, `form`, or `content`
(passing more than one raises `ValueError`):

```python
await client.post("items", json={"name": "new-item"})
await client.put("items/7", json=ItemModel(id=7, name="replaced"))
await client.patch("items/7", json={"name": "renamed"})
```

`json` also accepts a `BaseModel`/`Struct` directly (serialized for you). `form` builds a
multipart body from a `dict` whose values can be `str`, `int`, `bytes`, a `pathlib.Path`, or a
`(filename, bytes)` tuple for file uploads. `content` sends a raw `str`/`bytes` body as-is.

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
