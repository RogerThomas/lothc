# lothc — agent reference

Typed HTTP client on pyreqwest. `HTTPClient` (async) / `SyncHTTPClient` (sync) — identical API,
mirror methods 1:1 (drop `await`, `async with` → `with`, async iterators → sync iterators).

## Build

```python
async with HTTPClient.build(
    base_url=None,
    bearer_token=None,
    bearer_auth=None,
    default_headers=None,
    timeout=30.0,
    cookie_store=False,
    follow_redirects=True,
    max_redirects=None,
    proxy=None,
    max_retries=0,
    retry_methods=None,
) as client:
    ...
```

`bearer_token: str` (static) xor `bearer_auth: Callable[[], Awaitable[str]]` (sync client:
`Callable[[], str]`) — resolved fresh per request, at most one of the two. `default_headers`
sent on every request. `cookie_store=True` = in-memory jar. `proxy: str | None`.

## Decode targets (`response_data_type`, default `bytes`)

- `bytes` — raw, default
- `lothc.JSON` — `dict[str, Any]` subclass, zero validation
- pydantic `BaseModel` subclass
- msgspec `Struct` subclass
- `TypedDict` subclass — validated via typeguard if installed, else warns once
  (`LOTHC_SUPPRESS_TYPEGUARD_WARNING=1` to silence)
- bare `dict` / `dict[str, Any]` — NOT allowed, raises `TypeError`

## Verbs

- `get(path, *, params=None, headers=None, response_data_type=bytes, error_for_status=True) -> Data`
- `get_result(path, *, params=None, headers=None, response_data_type=bytes, response_headers_type=None, error_for_status=True) -> Result` —
  `.data .status .headers .typed_headers`
- `post/put/patch(path, *, params=None, headers=None, json=None, form=None, content=None, response_data_type=bytes, error_for_status=True) -> Data` —
  at most one of `json`/`form`/`content`, else `ValueError`
- `delete(path, *, params=None, headers=None, response_data_type=bytes, error_for_status=True) -> Data`
- `head(path, *, params=None, headers=None, response_headers_type=None, error_for_status=True) -> Result[None]`
- `sse(path, *, params=None, headers=None, response_data_type=None, id_type=str, error_for_status=True) -> Iterator[SSEEvent[TData, TId]]` —
  always yields `SSEEvent(id=, event=, data=)` (kw-only, `SSEEvent[TData, TId=str]`).
  `response_data_type` controls `.data`'s type only (default `str`); class | pydantic
  `TypeAdapter` | msgspec `Decoder`. `.event` is always `str`, never `None` (spec defaults it to
  `"message"` when absent from the wire). `.id` is genuinely `str | None` per spec — `id_type` is
  the single knob for both requiredness and type: `str` (default, required, no coercion) | any
  other bare type e.g. `int`/`uuid.UUID` (required, coerced via `id_type(raw)`) | that type
  unioned with `None` e.g. `int | None` (optional, coerced when present, `None` when absent) |
  bare `None` (optional, `.id: str | None`, never coerced) — pass a union the same way you'd
  write `response_data_type=A | B` for a discriminated union, `TId` binds to whatever you pass
- `stream_get(path, *, params=None, headers=None, response_data_type=None, error_for_status=True) -> Iterator[bytes | TLine]` —
  raw unbuffered bytes by default (safe for binary); `response_data_type` switches to
  newline-buffered per-line decode
- `stream_post(path, *, params=None, headers=None, json=None, form=None, content=None, response_data_type=None, error_for_status=True) -> Iterator[bytes | TLine]`
- `download(path, dest=None, *, params=None, headers=None, error_for_status=True) -> bytes | None` —
  memory-efficient GET for large bodies; no `dest` streams into one buffer and returns `bytes`
  (~1/3 the peak memory of `get()`), `dest: Path` streams straight to a file instead (`None`
  return, O(chunk size) memory regardless of body size)

`params`/`headers`: `dict`/`Mapping[str, str]`, or `BaseModel`/`Struct` (`None` fields omitted).
`json`: `dict` | `BaseModel` | `Struct`. `form`: `dict[str, int | bytes | str | File]`, `File =
tuple[str, bytes] | Path | BufferedIOBase`. `content`: raw `str | bytes` body.

## Errors

- `HTTPResponseError(status, body_start)` — 4xx/5xx, raised when `error_for_status=True` (default)
- `HTTPTransportError` base; `HTTPTimeoutError`, `HTTPConnectionError` subclasses — no response received
- pydantic/msgspec/typeguard validation errors propagate unwrapped (not translated)

## Retries

`max_retries` (default `0` = off), `retry_methods` (default `{GET,PUT,DELETE,HEAD}` — `POST`/
`PATCH` need explicit opt-in). Exponential backoff; retries on transport error or status in
`{429,500,502,503,504}`; honors `Retry-After`.
