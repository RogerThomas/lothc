# Changelog

All notable changes to lothc are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and lothc uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). Releases up to and including 0.0.16
predate this file; see [GitHub Releases](https://github.com/RogerThomas/lothc/releases) for them.

## [Unreleased]

### Fixed

- The logo, benchmark chart and doc links on the PyPI page (the README used repo-relative
  paths, which only resolve on GitHub).

## [0.0.17] - 2026-09-23

### Added

- `data=` on `post`/`put`/`patch`/`delete`/`stream_post`: a urlencoded
  (`application/x-www-form-urlencoded`) body from a `dict` or a `BaseModel`/`Struct`, taking the
  same values as `params=`.
- `delete()` accepts a body (`json=`/`data=`/`form=`/`content=`), like `post()`.
- `client.with_result.<verb>` (`get`/`post`/`put`/`patch`/`delete`) returns a `Result` with the
  status, headers and request alongside the decoded body.
- `Result.http_version` and `Result.elapsed` (seconds for the whole call, retries included).
- A pydantic `TypeAdapter` or msgspec `Decoder` is accepted as `response_data_type` on every verb,
  e.g. `TypeAdapter(list[Item])` for a top-level JSON array.
- `HTTPResponseError.headers` and `.body` (the whole error body), and it now pickles.
- `HTTPResponseError.parse_error`: an error body that doesn't match `error_type` no longer hides
  the error. You get the `HTTPResponseError` with `parsed_body=None` and the reason in
  `.parse_error`.
- Client settings `user_agent=`, `no_proxy=`, `http2=`, `resolve=`, `local_address=` and
  `tcp_keepalive=`.
- A default `User-Agent` of `python-lothc/<version>`.
- `max_retry_after=` (default 60s): a `Retry-After` longer than this ends retrying and raises the
  429/503 instead of blocking the call. `None` honours any wait.
- A `401` on an idempotent verb re-authenticates once when `bearer_auth` has an `invalidate()`
  method. `OAuthProvider`/`SyncOAuthProvider` implement it, so a revoked OAuth token is replaced
  automatically. `POST`/`PATCH` are never replayed.
- `form=` accepts `float` and `bool` values (a `bool` goes out as `true`/`false`).
- `download(dest=...)` accepts a `str` or any path-like, not just `Path`.
- `TlsVersion`, `WithResult` and `SyncWithResult` are exported from `lothc`.

### Changed

- Clients are constructed directly and opened as context managers:
  `async with HTTPClient(base_url=...)`, not `HTTPClient.build(...)`. A client can be re-entered,
  so a module-level client works across separate `asyncio.run()` calls. `OAuthProvider`'s
  `client_factory` default follows (`HTTPClient` / `SyncHTTPClient`).
- `SSEEvent.id` is always a `str`, `""` when the server has sent no `id:` (the `EventSource`
  default). `SSEEvent` takes one type parameter, `SSEEvent[TData]`.
- `sse`, `stream_get` and `stream_post` are typed as (async) generators, so they can be closed
  early with `aclosing`/`closing` or `.aclose()`/`.close()` to release the connection straight
  away.
- For `stream_get`, `stream_post` and `download`, the client's `timeout` is now the longest
  allowed gap between chunks rather than a cap on the whole transfer, so a long healthy download
  is no longer cut off at 30s. A per-call `timeout=` still caps the whole call.
- pydantic models are encoded by alias everywhere lothc encodes one (`json=`, `params=`,
  `headers=`, JSON form parts, `lothc.testing` mock responses), unless the model sets
  `serialize_by_alias=False`. `serialize_by_alias=True` is no longer needed.
- `retry_methods` takes any collection of method names, case-insensitively. An empty one turns
  retries off.
- `json=` always sends `Content-Type: application/json`, replacing one the caller set rather than
  sending two.
- Using a client after its `with` block has exited raises `RuntimeError`.

### Removed

- `HTTPClient.build()` / `SyncHTTPClient.build()`.
- `get_result`, `post_result`, `put_result`, `patch_result` and `delete_result`; use
  `client.with_result.<verb>`.
- `sse()`'s `id_type=` and `allow_missing_id=`; convert `event.id` yourself.

### Fixed

- A connection that drops partway through a response body (including a body shorter than its
  `Content-Length`) is now retried like any other transport failure and raises
  `HTTPConnectionError`. A body that can never decode, like corrupt gzip, is not retried.
- A redirect loop is no longer retried by the plain verbs.
- A failed `download(dest=...)` never leaves a truncated file behind or touches an existing file
  at `dest`.
- Sync streams no longer buffer the whole body in memory behind a slow consumer, and stop their
  worker thread when abandoned while data is still flowing.

[Unreleased]: https://github.com/RogerThomas/lothc/compare/0.0.17...HEAD
[0.0.17]: https://github.com/RogerThomas/lothc/compare/0.0.16...0.0.17
