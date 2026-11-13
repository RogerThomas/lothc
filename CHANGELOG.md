# Changelog

All notable changes to lothc are recorded here, and lothc uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). From 0.0.20 on, entries are generated
by [release-please](https://github.com/googleapis/release-please) from Conventional Commit
messages; 0.0.17 to 0.0.19 were hand-written in the
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format. Releases up to and including
0.0.16 predate this file; see [GitHub Releases](https://github.com/RogerThomas/lothc/releases).

## [0.3.0](https://github.com/RogerThomas/lothc/compare/0.2.3...0.3.0) (2026-11-13)


### ⚠ BREAKING CHANGES

* `native` is no longer a bool, and clients on Linux and macOS now use the native transport unless built with native="off".

### Features

* make the native transport the default, with native="auto"|"off"|"on" ([368b248](https://github.com/RogerThomas/lothc/commit/368b24816602901fb2c2ddff063cc6be217425cd))


### Documentation

* record why lothc's wheels use neither BOLT nor target-cpu ([dff8db2](https://github.com/RogerThomas/lothc/commit/dff8db2a8cca01c2c04409c7ba34795e4416050c))
* record why lothc's wheels use neither BOLT nor target-cpu ([c449676](https://github.com/RogerThomas/lothc/commit/c449676d96328ca6479ee88452958c734fc0c601))
* show the native transport's benchmark results on the home page, README and Benchmarks page ([848201d](https://github.com/RogerThomas/lothc/commit/848201dd9c49661a4f796de1556f7b681b1b928f))

## [0.2.3](https://github.com/RogerThomas/lothc/compare/0.2.2...0.2.3) (2026-11-12)


### Features

* add an opt-in native transport (native=True) and split lothc into modules ([9fcac82](https://github.com/RogerThomas/lothc/commit/9fcac82cb2bec4ed0a02c10bc66127c6c3c4bd0e))
* add an opt-in native transport (native=True) and split lothc into modules ([359d21b](https://github.com/RogerThomas/lothc/commit/359d21bf58cd166ba78b7b74f8b167f940328fa7))
* lothc's own mock engine, and the mocker on the native transport ([8647dc4](https://github.com/RogerThomas/lothc/commit/8647dc49ed211e8d80f2b1d5d49a8a5150421271))
* native keeps a cookie jar for cookie_store=True as reqwest's does ([b0be814](https://github.com/RogerThomas/lothc/commit/b0be81457b8b48c533ce1c48bd80aa2e23a07f08))
* native sends multipart form= bodies as pyreqwest does ([c831207](https://github.com/RogerThomas/lothc/commit/c8312071d9a2bcf0c997125187a22560aa8cf250))
* native supports max_connections= and pool_timeout=, and the sync pool hands freed connections to waiting requests ([fabd836](https://github.com/RogerThomas/lothc/commit/fabd836ca11fb463ac15bd680bd76ae07a835561))
* native supports resolve=, local_address=, tcp_keepalive=, connect_timeout= and read_timeout= ([d7818cd](https://github.com/RogerThomas/lothc/commit/d7818cdd89a74fb7cc0fe4d8767bdf1ad56528fc))
* SOCKS4/4a and https:// proxies on the native transport ([ab61487](https://github.com/RogerThomas/lothc/commit/ab614870da19767ae88ae84e349e50f5f48e6c72))


### Bug Fixes

* build lothc._native on Windows, as a module of stubs ([bb5408d](https://github.com/RogerThomas/lothc/commit/bb5408dcdc4a974f73327d9b5cf7ceaf315085c6))
* native matches pyreqwest for an open async stream after close and a per-call timeout near zero ([55f23fa](https://github.com/RogerThomas/lothc/commit/55f23fa5d242cd4d2bbe32ccc62cb7090ed18609))
* native never reuses a connection the server has closed ([6bb5062](https://github.com/RogerThomas/lothc/commit/6bb50625c3b05259ca14e4e7fcc1448dfca52aca))
* native TLS no longer overflows rustls's plaintext buffer on mixed record sizes ([b542d21](https://github.com/RogerThomas/lothc/commit/b542d2144d56d4467e615442b1f98d94c66f6f4b))
* report public classes as lothc.&lt;Name&gt; in tracebacks and reprs ([007a39d](https://github.com/RogerThomas/lothc/commit/007a39d96b1e78a0072e99c38f9b28e9dea25fdf))
* send one Last-Event-ID on SSE reconnect, exact ints past 64 bits, form= with retries, and no BuilderError on client entry ([31614db](https://github.com/RogerThomas/lothc/commit/31614db102c02331db905805928047e3b69f0281))
* send one Last-Event-ID on SSE reconnect, exact ints past 64 bits, form= with retries, and no BuilderError on client entry ([81ff3c6](https://github.com/RogerThomas/lothc/commit/81ff3c62925a185c6a7d0fd287ef02c607830224))


### Performance Improvements

* answer a mocked request from a response encoded once, matching only what a mock checks ([942bf3d](https://github.com/RogerThomas/lothc/commit/942bf3d100f9d60c16e1e861e242159965b3d9c2))
* benchmark lothc's native transport alongside the default one ([7e40622](https://github.com/RogerThomas/lothc/commit/7e40622fd5c91ba521fb9ce6edb115986a0765db))


### Documentation

* benchmark table for the native transport ([742cd75](https://github.com/RogerThomas/lothc/commit/742cd750bf5c7cd24d4235e4615ede6178518858))
* CI's platform matrix and native runs, and the Windows stub module ([db1846a](https://github.com/RogerThomas/lothc/commit/db1846a4075584972ef6fa53437eb5a4be3abf32))
* CLAUDE.md on the PGO wheels, and no more hatch-vcs ([c1e05ed](https://github.com/RogerThomas/lothc/commit/c1e05edfcfc278bbf00343a96e1ccefd1dfaa794))
* CLAUDE.md on waiting for evidence in tests, and Nagle on the test servers ([23e744c](https://github.com/RogerThomas/lothc/commit/23e744cf22949b1f6b0ebc35ae3f559a3fb64ea7))
* drop SOCKS4 and https:// proxies from native's unported list ([c8a1459](https://github.com/RogerThomas/lothc/commit/c8a14595057adbf277123e2c4877156704d57383))
* link to pyreqwest's real repository ([4951bd1](https://github.com/RogerThomas/lothc/commit/4951bd10fad78e7b0ba21640f142a68972170c3a))
* link to pyreqwest's real repository ([1cc53d1](https://github.com/RogerThomas/lothc/commit/1cc53d1c42a73b05ce5a82489a03a345a04bde42))
* only http2= fails under task test-native ([15d6d1b](https://github.com/RogerThomas/lothc/commit/15d6d1bf98595d03a2ac916e797985f92568360c))
* say which platforms have wheels, and that Windows has no native transport ([7c934ac](https://github.com/RogerThomas/lothc/commit/7c934ace189243d337357cff15da041be10b01da))
* the mocker is built on native, on lothc's own engine ([130d09e](https://github.com/RogerThomas/lothc/commit/130d09eaad975643d8ad99892f774ce27252a723))

## [0.2.2](https://github.com/RogerThomas/lothc/compare/0.2.1...0.2.2) (2026-11-05)


### Features

* add options() verb, retried by default ([72b069e](https://github.com/RogerThomas/lothc/commit/72b069e8804d604cb097c72b694e6ed6b8ebfc71))
* add options() verb, retried by default ([347db40](https://github.com/RogerThomas/lothc/commit/347db4083365916b1dda3ef97bb8736897ac29c9))

## [0.2.1](https://github.com/RogerThomas/lothc/compare/0.2.0...0.2.1) (2026-10-23)


### Bug Fixes

* harden error-body decoding and raw text stream lines ([44b4228](https://github.com/RogerThomas/lothc/commit/44b42288a47c69dca435963742ec9e7ca816a91e))
* route into= forms msgspec can't decode to pydantic, normalise dict/Mapping[str, Any] ([52044b5](https://github.com/RogerThomas/lothc/commit/52044b56160b56d773094609fa002d42e7d5b2f1))
* route into= forms msgspec can't decode to pydantic, normalise dict/Mapping[str, Any] ([c3fc9b8](https://github.com/RogerThomas/lothc/commit/c3fc9b8e723e4ef15c29ff2a4c19cbf57a20a650))

## [0.2.0](https://github.com/RogerThomas/lothc/compare/0.1.0...0.2.0) (2026-10-23)


### ⚠ BREAKING CHANGES

* `response_data_type=` is now `into=`; `error_type=` and `HTTPResponseError.parsed_body` are replaced by `raises=` and `.error`.

### Features

* into= takes any type expression; raises= gives typed error bodies ([a35cef9](https://github.com/RogerThomas/lothc/commit/a35cef9a2d2c294e436d53364e3f57e8103f26aa))


### Bug Fixes

* route, cache and resolve into= forms correctly ([ed74e85](https://github.com/RogerThomas/lothc/commit/ed74e850dad63213847c15cd45fbb04c3e184d22))
* stop reloading a revoked OAuth token from the disk cache ([4e6e4be](https://github.com/RogerThomas/lothc/commit/4e6e4bea1b34dc0e2a790617c31016587e954dd2))
* stop reloading a revoked OAuth token from the disk cache ([81b60fa](https://github.com/RogerThomas/lothc/commit/81b60fabab8914e46ba22fd666c11d0306447994))


### Performance Improvements

* build Response.headers on first read ([d6be065](https://github.com/RogerThomas/lothc/commit/d6be06508053a7ded4494147cff82d3587db0500))
* build Response.headers on first read ([3971080](https://github.com/RogerThomas/lothc/commit/3971080c62b51d87d06c7e98b5feb457a96c31f7))

## [0.1.0](https://github.com/RogerThomas/lothc/compare/0.0.19...0.1.0) (2026-09-28)


### ⚠ BREAKING CHANGES

* download() returns a Response

### Features

* download() returns a Response ([bb4a970](https://github.com/RogerThomas/lothc/commit/bb4a970072449c2f115777fefc7322b67fd73e42))


### Documentation

* tidy CLAUDE.md and inline the style guide ([df84580](https://github.com/RogerThomas/lothc/commit/df84580f792b9343a866071a0b5376314ea00e24))

## [0.0.19] - 2026-09-25

### Changed

- `get`/`post`/`put`/`patch`/`delete` now return a `Response` instead of the bare body: the
  decoded body is on `.data`, alongside `.status`, `.headers`, `.typed_headers`, `.request`,
  `.http_version` and `.elapsed`. `response_headers_type=` is accepted on every one of them.
  Migrate `item = await client.get(...)` to `item = (await client.get(...)).data`.
- `Result` is renamed to `Response`.
- `OAuthProvider`/`SyncOAuthProvider` take `token_cache_dir=` instead of `token_cache_path=`. Each
  provider writes its own file there, named for the token URL's host, the client ID and a short
  hash (`auth.example.com-my-client-9b73c8cc1d16.json`), so several providers can share one
  directory.

### Removed

- `client.with_result` (`WithResult`/`SyncWithResult`): the verbs themselves now return what it
  did. Replace `client.with_result.get(...)` with `client.get(...)`.

## [0.0.18] - 2026-09-23

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

[0.0.19]: https://github.com/RogerThomas/lothc/compare/0.0.18...0.0.19
[0.0.18]: https://github.com/RogerThomas/lothc/compare/0.0.17...0.0.18
[0.0.17]: https://github.com/RogerThomas/lothc/compare/0.0.16...0.0.17
