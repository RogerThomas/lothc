# CLAUDE.md

`lothc` ("Lord Of The Http Clients") is a typed HTTP client: `HTTPClient` (async) and
`SyncHTTPClient` (sync), on [pyreqwest](https://github.com/MarkusSintonen/pyreqwest) by default or
lothc's own native transport with `native=True`, with pydantic and msgspec as optional extras
(`lothc/_compat.py`) for decoding and encoding. lothc must work with neither, either or both
installed.

This file records decisions and the *why*, tersely. Don't add narrative or anything the code,
a docstring or a test already says.

## Commands

- Dependencies: `task deps-sync` / `task deps-upgrade`
- Lint / format / type-check: `task lint` / `task format` / `task typecheck`; everything CI
  runs: `task check`
- Tests: `task test`, one test: `task test-one -- tests/test_foo.py::test_bar`, doctests:
  `task test-doctests`, every test on the native transport: `task test-native`. Pass
  `PYTEST_PROFILE=agent` for concise output; re-run a failure with `PYTEST_PROFILE=dev`.
- Pre-commit (`prek`, a drop-in for `pre-commit`): `task precommit-install`, `task precommit`
- Benchmarks: `task bench-check` (working tree, before/after via `-- --against <hash>`),
  `task asv-*` (across git history), `task bench-codspeed` (the CI CodSpeed suite)

> [!CAUTION]
> `task perf`/`task perf-build` start Docker containers and run for minutes: only when the user
> asks for a benchmark. `task example-run` needs `task example-server` already running on
> `127.0.0.1:8701`.

## Releasing and CI

- **SemVer, a static `version` in `pyproject.toml` that release-please bumps**: maturin has no
  VCS versioning. SemVer because other packages depend on lothc; pre-1.0, `feat` bumps the patch
  and a breaking change the minor.
- **Built by maturin**: `lothc._native` comes from the crate in `native/` (typed by
  `lothc/_native.pyi`), so every environment needs a Rust toolchain, and `uv sync` rebuilds it
  when `native/` changes (`[tool.uv] cache-keys`).
- **TODO: releases are blocked** until `release.yml` builds a per-platform wheel matrix with PGO.
- **release-please cuts releases**, so commit messages on `main` must be Conventional Commits.
  Tags are the bare version (no `v`, no component), which hatch-vcs reads. Never add an
  `## [Unreleased]` section to `CHANGELOG.md`.
- **Publishing is dispatched** (`gh workflow run release.yml --ref <tag>`), since a release made
  with `GITHUB_TOKEN` triggers no workflow. `release.yml` stays top-level for PyPI's Trusted
  Publisher, runs all of CI against the tag, and refuses a non-tag ref.
- **CI:** 3.13/3.14/3.15 plus free-threaded 3.14t/3.15t with `PYTHON_GIL=0` (lothc shares
  state across threads, and users share a client across threads), and `test-no-extras` with
  pydantic/msgspec uninstalled. The `perf` group's clients stay out of `dev`: one has no
  free-threaded wheel. Each of those runs the suite on native too, and `test-platforms` adds
  Linux Arm, macOS Arm and Intel, and Windows (pyreqwest only) on 3.14. macOS runners turn timer
  coalescing off, or a 0.02s sleep takes ~0.08s.
- **README.md uses absolute URLs only**, since it's the PyPI description.
- **Check the sdist's file list after a packaging change**: a default once shipped the whole repo.
- **TODO (GitHub UI):** add a tag-pattern protection rule to the `pypi` environment.

## Scope

- **Not done yet:** `sse_post` (deferred by the user).
- **Only if someone asks:** request/response hooks, a generic `request()`, streaming uploads,
  status/headers on the streaming verbs, cookie-jar access.
- **Deliberately not exposed:** per-algorithm compression toggles, `interface`, `tcp_nodelay`,
  `referer`, CRLs, a final URL on `Response` (pyreqwest has none).

## Architecture

- **`lothc/` is split by concern**, imports running one way: types, headers, response, decode,
  errors, encode, then form/streaming/SSE/settings/retry, then the two transports, then
  `_client.py` and `_sync_client.py`. `lothc/_oauth.py` is built *on* the client.
- **Top-level names are unique across `lothc/`**: the parity test pairs twins by name across
  modules.
- **A `_` name is private to the package, not the module**: basedpyright's private-usage and
  unused-function rules are off for `lothc/` only, and pylint's `duplicate-code` is off, since the
  verbs' argument lists repeat by design.
- **Clients over transports.** The clients keep the public API; a transport (pyreqwest or native)
  does auth, sending, status checks, decoding and streaming. Each verb returns the transport's
  call directly, with no wrapper coroutine.
- **The two clients, and the two transports, are near-mirrors**; implement every feature on both.
  `tests/test_sync_async_parity.py` compares their signatures and bodies, with reasoned
  allowlists.
- **Clients are context managers**, and the pyreqwest client exists only while one is open, so
  no pyreqwest type ever appears in the public API (`tests/test_public_api.py` checks).

## Code style

Load the `write-python-code` skill (`.claude/skills/write-python-code/SKILL.md`) before writing
or editing Python here.

@style-guide.md

## Testing

- **`tests/` must pass all four checkers** (basedpyright, mypy, ty, zuban), since an overloaded
  generic API can satisfy one and trip another. Fix, don't suppress, anything a user of that
  checker would hit; look for a real fix before calling it a tool bug.
- **`server_key`** for every per-key server state (the server lives for the session), and the
  `client`/`sync_client` fixtures unless the test is about the client's lifecycle.
- **Keep `tests/_helpers.py` free of pydantic/msgspec**: `test-no-extras` picks modules by
  grepping their own text.
- **Proxy tests call `clear_proxy_variables`**: reqwest applies a developer's `HTTP_PROXY` to
  loopback too.
- **`tests/test_wire_format.py` is the wire contract** (request line, header order and casing,
  body). Regenerate a value only for a deliberate change.
- **TLS tests use a real HTTPS server** (`trustme`), and pin the rustls failure in `match=`.
  Wording differs between platforms; CI covers Linux, macOS and Windows.
- **Mutation runs use `python -B`**: a same-size edit restored within a second leaves a stale
  `.pyc`.
- **`tests/test_transport_wire_parity.py` sends every case through both transports** and compares
  what the server (or proxy) received, byte for byte. It unsets `LOTHC_TRANSPORT` first, since
  that override would make both clients native.
- **Under `task test-native`, only the `http2=` refusals may fail**: anything else is a native
  bug. CI deselects them by name (`NATIVE_DESELECT` in `ci.yml`), so add any new one there.
- **Speed (~45s):** test servers poll at 0.01s; scale a timing pair down together, never a lone
  margin; `backoff_base=0.001` where only *that* a retry happened matters; a test below a
  default asserts a bracket, not a floor. Rejected: pytest-xdist.
- Prefer doctests for small algorithmic functions.

## Native transport

- **Why it exists:** async `get(into=Struct)` runs at about 2x pyreqwest's throughput. A
  2026-10-04 reqwest binding gained only 5-12% because it kept tokio's cross-thread hop, so this
  one has none.
- **The design:** hyper 1.12's `client::conn::http1` over sockets the asyncio loop watches,
  polled by a single-thread executor on the loop thread, with futures created in Python. Sync
  drives the same futures on the calling thread in `poll(2)` with the GIL released; a sync
  wrapper over a background loop measured 2x the CPU.
- **Selected with `native=True`**, read once at construction. `LOTHC_TRANSPORT=native` is a
  test-only override that wins over the argument (a `bool` can't tell an explicit `False` from
  the default); user docs don't mention it.
- **POSIX only, and pyreqwest stays as Windows' transport** (decided 2026-11-10): native needs
  `poll(2)` and `loop.add_reader`, which Windows' default loop lacks, and an I/O thread there would
  bring back the cross-thread hop native exists to avoid. So `native=True` raises on Windows,
  where the crate is `cfg(unix)`-gated down to a stub module of the same names, so lothc's
  imports don't change.
- **It must match pyreqwest exactly**: the same wire bytes, results and exception classes. The
  only accepted difference is that a transport error's `str()` is a flat cause chain. Crate pins
  follow pyreqwest 0.15.0's lockfile, and each feature uses reqwest's own crates where they exist
  (hyper-util's proxy matcher, compression-codecs), never a hand port.
- **Anything unported raises `NotImplementedError`**, never a silent difference: HTTP/2.
- **Known differences that don't raise:**
  - `form=` files are read on the loop thread in reads of up to 64 KiB (pyreqwest reads them off
    it), so a slow filesystem stalls the loop.
  - A sync client's expired idle connections stay open until its next request (no thread to
    reap on), and its DNS lookup isn't bounded by `connect_timeout`.
  - A sync stream never times out a slow consumer under `read_timeout`: it reads only when
    asked, where pyreqwest's read-ahead fills and then times out.
  - A sync request in a blocking TCP connect sees a connection another thread freed only once
    its connect returns.
  - A request cancelled while waiting for a `max_connections` slot leaves the queue and is never
    sent, deliberately (decided 2026-11-11): pyreqwest still sends it, so a caller who saw the
    cancel and retried a POST would submit it twice.
  - Addresses are tried in order (no happy eyeballs), and an async client is bound to the loop
    that opened it.
- **Redirects run in Rust; retries and re-auth in Python.** A hop's headers must be reqwest's,
  in reqwest's order, which only the same Rust calls produce. Retries wrap the whole redirect
  chain, as pyreqwest's middleware does.
- **Streaming shares one Python implementation with pyreqwest**: a transport supplies only a
  chunk source. Native reads at most two chunks ahead and stops reading the socket while nobody
  reads, so a slow consumer never buffers a whole body.
- **Measured choices:** one shared deadline timer and heap (a per-request `call_later` cost
  ~1.3us); lazy headers; PGO is worth ~6-7%.
- **HTTP/2 is deferred** until someone asks (decided 2026-11-10): it's the largest port and rarely
  used, so `http2=True` stays on pyreqwest.
- **The mocker runs through native's own request code** (decided 2026-11-10), answering from
  Python handlers instead of the network, so mocked tests still see the exact request sent.
  The engine is lothc's own (`lothc/_mocking.py`, decided 2026-11-11), not a port of pyreqwest's
  Apache-2.0 mocker; both transports check one hook, and nothing patches pyreqwest.
- **Not built yet:** wheels.

## Decisions

### Configuration
- **Settings are validated at construction**, including a bad `base_url`, and the pyreqwest
  client is built on each entry, so a client is re-enterable across `asyncio.run()` calls.
- **`base_url` joining is RFC 3986**: a leading `/` drops the base's path, and an absolute URL
  overrides the base, auth included.
- **Auth is `bearer_token`/`bearer_auth`/`basic_auth`**, one pyreqwest mechanism each, at most one.
- **`max_retry_after` (60s) stops retrying instead of clamping**, leaving the caller to schedule
  from `Retry-After`; an early retry against a long wait is usually rejected again.
- **A 401 re-authenticates once**, idempotent verbs only, through any `bearer_auth` that can
  `invalidate()`. A stale invalidation is a no-op, so concurrent 401s can't discard a fresh token.

### Requests
- **`data=` is urlencoded and `form=` multipart**, as in requests/httpx.
- **A `list`/`tuple` in `Params` repeats the key.**
- **A `Form` repeat tuple must be homogeneous**: a mixed one read as a file but had no filename.
- **pydantic models encode by alias** (their own `serialize_by_alias`) at every encode site.
- **`RequestInfo` is captured before `.send()`**: a sent request can't be read.

### Responses and decoding
- **Every non-streaming verb returns a `Response`**: lothc is a general client, and most callers
  need status and headers. No body-only flag, since a runtime `bool` can't drive overloads.
- **Overloads, not one generic signature with casts.** The adapter overload must precede the
  `TypeForm` one (ty), and bare `dict` has its own (`dict[Any, Any]`, for zuban).
- **`into=` takes any type expression.** A `Struct` goes to msgspec and a pydantic marker to
  pydantic; both is a `TypeError`, and so is a type msgspec can't decode when pydantic is
  missing. That's always decided before sending, never after.
- **Decoders are cached by form** (a `TypeAdapter` costs ~31us to build), except for forms whose
  metadata compares by identity, which would grow the cache forever; 4096 entries is the
  backstop.
- **`str` means raw text**, like `bytes`. Every decode failure is a `ValueError`; a decode
  library's validation errors propagate unwrapped.
- **`raises=` is a subclass pinning `HTTPResponseError[E]`**, because Python can't `except` a
  parametrized generic. It's resolved before sending, and the error body is decoded
  best-effort, so a proxy's HTML 502 still surfaces as a status error.
- **`Response.headers` is a `CaseInsensitiveDict`**, a `MutableMapping` rather than a `dict`
  subclass (which would lose case-insensitivity under `dict(h)`). It's built on first read
  (~15% throughput).
- **Dropped:** `TypedDict`/`typeguard` decoding (slower, no capability the libraries lack).

### Errors
- **Never leak pyreqwest's exception types.** Transport errors map to `HTTPTransportError` and
  its subclasses; a client build error is a plain `ValueError`.
- **Status errors stay separate from transport errors.**
- **A `BuilderError` is permanent**: never retried or reconnected.
- **A body cut off partway is retried; a corrupt one isn't.**
- **A multipart `form=` request is sent once, never retried or re-authenticated:** its streamed
  body can't be cloned, and buffering it instead means a second multipart encoder holding uploads
  in memory. reqwest's own retry policy skips such a body too.

### Streaming, SSE and downloads
- **On streaming verbs the client `timeout` is a gap limit, not a total cap**, which killed
  healthy long streams; a per-call `timeout=` is still a total cap. Sync enforces it by reading
  on a worker thread (bounded queue), since a blocked read can't be cancelled.
- **`streamed_read_buffer_limit(1)` everywhere**, or trickling bytes are withheld until EOF.
- **`sse()` follows the WHATWG EventSource model** for reconnects; the client's total `timeout`
  never touches the stream. `SSEEvent.id` is a `str`, `""` when absent, as in the spec.
- **Streaming verbs are typed `Generator`/`AsyncGenerator`**, so callers can close them early.
- **`download()` exists to cut `get()`'s peak memory**, and writes `dest=` atomically.

### Optional extras and typing
- **`_compat.py`'s fallback stubs must be subscriptable**: annotations are evaluated at import.
- **Public aliases use structural `Protocol`s**, so a missing extra doesn't turn unrelated
  overloads `Unknown` for strict checkers.
- **On 3.13, define a class before an annotation uses it**; don't quote the annotation.

### OAuth (`lothc/_oauth.py`)
- **Token requests are urlencoded**, with lothc building the Basic header per RFC 6749 §2.3.1.
- **The providers are plain classes, not dataclasses**: zuban rejects a dataclass as
  `bearer_auth=`.
- **Expiry is wall-clock**, and `refresh_leeway` is clamped to half the lifetime. A failed
  refresh mints again only on 400/401.
- **`OAuthTokenError` wraps everything renewal raises**, since it surfaces inside the caller's
  own request.
- **The lock is created per running loop**, so a module-level provider survives `asyncio.run()`.
- **The token cache is `0600` plaintext, one file per token URL, client and scope**, as the AWS
  CLI and gcloud do. `invalidate()` also marks the cached token expired, so a new process doesn't
  reload a revoked one.

### `lothc.testing`
- **No pyreqwest type in its public API, and no builder pattern.**
- **Matching shares the real request path's encoding**, so a mock matches what a real request
  sends.
- **Strict by default**, unlike pyreqwest's plugin.

## Pitfalls

Each of these was a real bug or a lost hour once.

- **In the `form=` `match`, `case list()` comes before the `File` patterns**: a sequence pattern
  matches a list too, so `["a.png", b"..."]` was read as a file.
- **`_LazyHeaders` holds pyreqwest's `HeaderMap`, not the response**: a read response caches its
  body (40 held 10MB responses: ~470MB).
- **`.to_bytes()`, never `bytes(x.bytes())`**, and don't convert at all when the decoder takes a
  buffer (msgspec does; pydantic needs `bytes`).
- **Keep `_send_method` one async layer deep**: each extra layer is a frame on every resume.
- **The async idle limit wraps each chunk read, never a `yield`**, or a slow consumer looks like
  a stalled server.
- **The mocker validates before registering** (a rule matches from the moment it's added) **and
  applies `data=` before `headers=`** (`.body_json()` appends a Content-Type).
- **OAuth:** aliased pydantic token models need `ConfigDict(validate_by_name=True)`, and the
  token-request types are `Callable[..., JSONPayload]` because a `Protocol` fails 3 of 4
  checkers.
- **`msgspec_installed`/`pydantic_installed` are `bool`s**, not `msgspec is not None`, which strict
  basedpyright flags as unnecessary.
- **A test about `raises=` binds `__cause__` to a local first**: basedpyright narrows
  `isinstance(x, HTTPResponseError)` to `HTTPResponseError[Unknown]`.
- **`/events`' default interval is load-bearing** for the test that SSE events arrive
  incrementally.
- **asv:** run the large-body server as a separate process (a thread skews `peakmem`), keep
  `build_command` on `python -m build --wheel`, and store only static data from `setup_cache()`
  (it's pickled).
- **`bench-check` measures `peakmem` on the first call only** (repeats fragment the allocator);
  treat sub-millisecond `time` deltas as directional.
- **`test-no-extras` runs `uv run --no-sync`**, or a plain `uv run` reinstalls the extras.
- **release-please needs "Allow GitHub Actions to create and approve pull requests"** (Settings →
  Actions → General).
