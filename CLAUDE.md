# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`lothc` ("Lord Of The Http Clients") is a typed HTTP client library built on [pyreqwest](https://github.com/mostafa-hussein/pyreqwest)
(a Rust-backed HTTP client). It provides a single, consistent typed API surface — `HTTPClient` (async)
and `SyncHTTPClient` (sync) — with first-class, optional support for both **pydantic** and **msgspec** as
both decode AND encode targets (a `BaseModel`/`Struct` instance can be passed directly as `json=`, not
just used as a `response_data_type`).

pydantic and msgspec are both optional extras (see `lothc/_compat.py`) — the library must work with
neither, either, or both installed. `TypedDict`/`typeguard` support was deliberately removed (not
just never added) — see the "Dropped: TypedDict/typeguard support" note under Development notes for
why.

## Status

Git repo at `https://github.com/RogerThomas/lothc` (`main`, tracked as `origin`). Real test suite
under `tests/`, benchmark suite under `benchmarks/`. This file is the source of truth on decisions
and remaining work — keep it up to date, but keep entries terse: a fact plus the *why*, not a
narrative of how it was found.

Versioning is **SemVer**, derived from the git tag via `hatch-vcs` (`dynamic = ["version"]`) — a
release is just a pushed tag, nothing to bump by hand. SemVer (not CalVer, unlike the `yeetr` CLI
project) because this is a library other packages depend on and major/minor/patch is the signal
tooling needs to know whether an upgrade is safe.

### Releasing

`task release` (defaults to patch; `-- minor`/`-- major` for the others) checks the tree is clean
and in sync with `origin/main`, computes and pushes the next tag, then `gh release create`s a
GitHub Release. `.github/workflows/release.yml` triggers on `release: published`, builds and
`uv publish`s to PyPI via Trusted Publishing (OIDC, no stored token, `pypi` GitHub Environment),
then deploys docs to GitHub Pages.

**Follow-up not yet done**: the `pypi` environment has no deployment protection rules (anyone who
can create a GitHub Release can trigger a publish) — add a tag-pattern restriction under
Settings → Environments → pypi as defense in depth. Left for manual GitHub UI setup since
`release: published`'s ref context is the tag, not a branch, and there's no easy local way to
preview a policy change first.

**`[tool.hatch.build.targets.sdist]` needs an explicit `include` allowlist — the wheel doesn't.**
hatchling's sdist default is "everything under version control." The real `0.0.1` sdist shipped
the entire repo (420KB: `benchmarks/`, `docs/`, `tests/`, etc.) before `include = ["/lothc",
"/README.md", "/LICENSE"]` fixed it to 15.7KB. `0.0.1` itself wasn't yanked — no functional or
security impact, just bloat.

### Roadmap

Done: query params/headers (typed + raw), per-verb `timeout=`, transport error wrapping
(`HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError`), `put`/`patch`/`delete`/`head`,
SSE (`TypeAdapter`/`Decoder` support, spec-compliant reconnect — see dev notes),
`stream_get`/`stream_post` (raw chunks by default, NDJSON-style typed decode via
`response_data_type`), `download` (see dev notes), the `Data` decode-target system (`bytes`
default, `dict`, pydantic `BaseModel`, msgspec `Struct`), auth (`bearer_token` /
`bearer_auth` callable / `basic_auth` pair — at most one; per-verb `skip_auth=True`), cookies
(`cookie_store=True`), redirects (`follow_redirects`/`max_redirects`), `proxy=`, retries
(`max_retries`/`retry_methods`, real pyreqwest `with_middleware` hook, idempotent verbs by
default), typed error bodies (`error_type=`, mirrors `response_data_type`), TLS/mTLS/pool config
(`connect_timeout`, `root_certificates`, `identity_pem`, `min_tls_version`/`max_tls_version`,
`danger_accept_invalid_certs`, `https_only`, `max_connections`, `pool_idle_timeout`,
`pool_max_idle_per_host`, `pool_timeout`, `read_timeout`), request metadata (`.request:
RequestInfo` on both `Result` and `HTTPResponseError`, every verb), OAuth 2 client credentials
(`OAuthProvider`/`SyncOAuthProvider`, see dev notes), and a pytest mocking plugin (`lothc[testing]`
— see dev notes).

Not done yet: nothing outstanding right now — see git history/this file's own dev-notes for what's
landed and why.

## Testing

- Run the test suite: `task test`
- Run a single test in isolation: `task test-one -- tests/test_foo.py::test_bar`
- Run doctests: `task test-doctests`

> [!IMPORTANT]
> All of the above accept `PYTEST_PROFILE=agent|dev` (default `dev`).
> Use `PYTEST_PROFILE=agent` by default — it produces concise output suited for agent consumption.
> If a test fails, re-run that single failing test in isolation with `PYTEST_PROFILE=dev` for full detail to help you debug.
> Example: `task test-one PYTEST_PROFILE=dev -- tests/test_get.py::test_get_returns_raw_bytes_by_default`.

> [!CAUTION]
> `task perf`/`task perf-build` spin up real Docker containers (a separate `json-server` + `perf`
> service via `benchmarks/docker-compose.yml`) and can run for tens of seconds to minutes depending
> on `--total-requests`/`--concurrency`. Do not run these by default; only run when the user
> explicitly asks for a benchmark.

> [!CAUTION]
> `task example-run` requires `task example-server` already running in a separate terminal —
> it has no built-in fallback to start the server itself. If there's no server listening on
> `127.0.0.1:8701`, ask the user to start it (or start it yourself in the background) rather than
> guessing why `example-run` is failing to connect.

### Type-checking `tests/` across all 4 major type checkers

`basedpyright` is this project's own primary checker (see Architecture below), but `tests/`
(which exercises lothc's public API the way a real consumer would, not lothc's internals) should
also type-check cleanly under `mypy`, `ty`, and `zuban` — different lothc users reach for
different tools, and a heavily-`@overload`'d, generic public API can type-check fine under one
checker's inference rules while tripping up another's:

```
uv run mypy tests
uv run ty check tests
uv run zuban check tests
uv run basedpyright lothc tests
```

**The bar for "must actually fix, not suppress"**: does this affect a real lothc *user*, using
that specific checker, on lothc's *public* API? If yes, it must be genuinely fixed — an ignore
comment is not acceptable there, because it'd be hiding a real problem a real consumer would hit.
If the complaint is purely about test-internal scaffolding that no consumer of the published
package could ever write or run (e.g. a `builtins.__import__` monkeypatch used to simulate a
missing optional dependency — see `tests/test_compat_fallbacks.py`), a scoped ignore comment for
that one checker is fine, since nothing outside lothc's own dev-time test suite is affected.

**Always look for a real fix before reaching for an ignore, even on the internal-scaffolding
side** — confirmed twice in practice that one exists more often than expected:
- `tests/test_compat_fallbacks.py`'s `builtins.__import__ = _blocking_import` needed a real
  signature fix (matching `__import__`'s exact parameter names/types instead of a loose
  `*args, **kwargs`) before three of the four checkers agreed it was fine — only `ty` still
  disagreed afterward, and its own error message printed the two signatures as textually
  identical text while still calling them incompatible, confirming a genuine `ty` limitation
  rather than a real mismatch left to fix. That one got a justified `# ty: ignore[...]`.
- `tests/test_auth.py`'s callable `bearer_auth` test providers (`_CountingAuthProvider`/
  `_SyncCountingAuthProvider`) looked like the same situation at first — `zuban` alone rejected a
  `@dataclass`-decorated class's `__call__` against `Callable[[], Awaitable[str]]`, again with
  "expected"/"got" printed identically in its own error. But this one *did* have a real fix:
  dropping `@dataclass` in favor of a plain class with an explicit `__init__` (deviating from
  style-guide.md's usual `@dataclass` preference, deliberately, with a comment explaining why)
  satisfied all four checkers with zero suppression at all. Don't stop at "the other three agree
  it's a tool bug" — check whether a small, unrelated-looking change (here, the class decorator,
  not the type annotation) removes the disagreement first.

### Doctests

Prefer doctests for small, self-contained algorithmic functions — they double as inline documentation.
Run with `task test-doctests`.

## Common commands

- Install dependencies: `task deps-sync`
- Update dependencies: `task deps-upgrade`
- Lint (auto-fix): `task lint`
- Format: `task format`
- Type-check: `task typecheck`
- Everything CI runs: `task check`
- Install the git pre-commit hook: `task precommit-install` (`prek` — a fast Rust drop-in for
  `pre-commit` that reads the same `.pre-commit-config.yaml` — is a `uv`-managed dev dependency,
  no separate install needed)
- Run all pre-commit hooks against the whole repo: `task precommit`
- Cut a release: `task release` (see "Releasing" under Status above)

## Manual example / smoke-testing

Beyond the automated test suite (see Testing above), there's also a manual showcase script for
eyeballing real output:

```
task example-server   # starts the stdlib test server on 127.0.0.1:8701 (separate terminal)
task example-run       # runs examples/run_http.py against it, showcasing every feature
```

`examples/server.py` reuses the same stdlib-only handler the automated test suite runs against
(`tests/_server.py` — no jero, no ASGI framework), just bound to a fixed port instead of an
OS-assigned one. `examples/run_http.py` doesn't yet showcase every feature added this session
(retries, cookies, redirects, proxy, delete/head, streaming) — extend it when that's worth doing.

`task example-server` restarts automatically on a real edit to `examples/server.py` or
`tests/_server.py`, via go-task's own built-in `watch: true` + `sources:` (Taskfile.yml) — no new
dependency, and no in-process module reloading (the whole process restarts). go-task's watcher
uses a content checksum, not mtime, so a no-op `touch` correctly does **not** trigger a restart —
only confirmed by testing both cases live, not assumed. `examples/server.py`'s `main()` wraps
`serve_forever()` in `contextlib.suppress(KeyboardInterrupt)`, since that's the signal go-task
sends the old process to stop it before starting the new one — without it, every restart printed a
raw `KeyboardInterrupt` traceback to the terminal.

## Regression benchmarking (asv)

`asv` (`asv.conf.json` + `asv_bench/`) tracks lothc's **own** performance across git history —
different from `perf.py`/`benchmarks/`, which compares lothc against *other* HTTP client libraries
at a point in time. asv has its own discovery/execution harness (`asv_runner`, its own
`setup`/`teardown`/`setup_cache` convention) — no pytest involved anywhere in that path.

- Fast dev-loop check (no isolated env, no history saved): `task asv-quick`
- "Did my last commit regress anything?": `task asv-continuous` (benchmarks HEAD's parent vs HEAD
  side by side; pass `-- <base> <branch>` for a specific pair). Skip `--quick` — too noisy
  (single-sample false positives); the real `repeat=3` sampling is reliable.
- Real run across history (isolated env + fresh build per commit): `task asv-run -- <range>`, e.g.
  `main~5..main`, or `HEAD^!` for one commit. No args = tip of each configured branch (asv's own
  default, not "every new commit" — that's the `NEW` range spec).
- Trend dashboard: `task asv-publish` then `task asv-preview`
- "I'm mid-edit, uncommitted — does this help or hurt?": `task bench-check` (below) — asv is keyed
  on commit hashes and fundamentally can't answer this; `bench-check` is additive, not a
  replacement, reusing the same benchmark definitions.

Two suites: `asv_bench/bench_download.py` (`get()` vs `download()` bytes-mode vs `dest=Path`
against a large body, `time_*`/`peakmem_*`) and `asv_bench/bench_verbs.py` (`get()` across all
four decode targets plus `post()`, small realistic bodies via the same stdlib server the pytest
suite uses). `asv.conf.json`'s `matrix.req` explicitly installs pydantic/msgspec into every
isolated asv environment since they're optional extras, not hard dependencies.

### Instant no-git benchmark check

`task bench-check` (`asv_bench/quick_check.py` + `_quick_check_worker.py`) runs every `time_*`
method directly against the dirty working tree — no asv CLI, no commit. Each benchmark runs in its
own subprocess so `peakmem` is a true per-benchmark reading, not a whole-run high-water mark.
Results are keyed by a content hash of `lothc/`'s own source (not a git sha) into
`asv_bench/.quick_check_history.json` (gitignored). With no `--against`, it just prints the
current hash; pass `--against <hash>` (from any earlier run) for a before/after table:

```
task bench-check                                    # records current state, prints its hash
task bench-check -- --against <hash-from-above>      # diffs vs that hash, prints its own new hash
```

Reports **min** time across 50 samples (timeit's own approach), but `peakmem` from only the
*first* call — repeating a large-body operation in one process inflates peakmem via allocator
fragmentation, not anything the benchmarked code did (confirmed: a naive repeat-loop pushed
`peakmem_download_bytes` from ~140MB to ~1195MB with zero code change). Trust `peakmem` deltas at
face value; treat sub-millisecond `time` deltas as directional only (genuine process-to-process
variance, not something more in-process samples fix) — `asv continuous`'s own significance testing
handles that properly for real historical runs.

Gotchas worth knowing before adding more benchmarks here:

- The large-object HTTP server in `setup()` must be a genuine separate OS process
  (`subprocess.Popen`), never a thread in this process — `peakmem_*` measures
  `RUSAGE_SELF.ru_maxrss`, so a server thread sharing the process would contaminate the reading.
- `environment_type: "uv"` needs an explicit `build_command` using `python -m build --wheel`, not
  asv's default `pip wheel` — the default also wheels lothc's dependencies into the same cache dir,
  and asv can't then tell which wheel is the project's own.
- `setup_cache()`'s return value is pickled and can be loaded by a *different process* than the
  one that produced it — never rely on a live resource (thread, subprocess handle) started there
  still being alive when a benchmark runs; only store picklable static data, start live resources
  in `setup()`.
- Resources acquired in `setup()` are managed via one `contextlib.ExitStack`, closed once in
  `teardown()` — not several independent calls in a fixed order.

## Architecture

Everything lives in `lothc/_client.py` (one file, deliberately — split it once it earns a split).
`lothc/__init__.py` re-exports the public surface. `lothc/_compat.py` isolates the optional
pydantic/msgspec imports (`TYPE_CHECKING` block + runtime `try/except ImportError` with stub
fallback classes). `lothc/_oauth.py` (`OAuthProvider`/`SyncOAuthProvider`) is a separate module
not because `_client.py` earned a split, but because it's a utility built *on* the client — it
imports `HTTPClient`/`SyncHTTPClient` and opens a short-lived one per token request — and plugs
into the existing `bearer_auth` slot as a plain callable; the clients themselves gained no new
parameter for it and know nothing about it.

### The two clients

`HTTPClient` (async) and `SyncHTTPClient` (sync) are near-mirrors of each other — same methods, same
overload shapes, one built on pyreqwest's `Client`/`RequestBuilder`/`Response`, the other on
`SyncClient`/`SyncRequestBuilder`/`SyncResponse`. When adding a feature, implement it on both and verify
both — it's easy to update one and forget the other.

Both are plain `@dataclass`es (see style guide) built via a `classmethod` + context manager:

```python
async with HTTPClient.build(base_url=..., bearer_token=..., timeout=30.0) as client:
    ...
```

### Verbs

`get`, `get_result`, `post`, `put`, `patch`, `delete`, `head`, `sse`, `stream_get`, `stream_post`,
`download` — each is a set of `@overload`s plus one real implementation. See style guide for *why*
overloads are used instead of a single generic signature.

### Code style

Use the `write-python-code` skill (`.claude/skills/write-python-code/SKILL.md`) before writing or
editing Python code here — it covers this project's own conventions plus when to read
`./style-guide.md` in full (required before touching `lothc/_client.py` specifically). The
"Development notes" list below **is** the design log for *why* the code looks the way it does —
the skill covers *how* to write new code that matches it.

## Development notes

- **`OAuthProvider`/`SyncOAuthProvider` (`lothc/_oauth.py`).** RFC path sends the token request as
  `content=urlencode({...})` with an explicit `content-type: application/x-www-form-urlencoded`
  header (lothc's `form=` is multipart-only, no urlencoded option). An aliased pydantic
  `token_request=`/`token_response=` model needs `ConfigDict(validate_by_name=True,
  serialize_by_alias=True)` — lothc's `json=` encoding doesn't use `by_alias`; msgspec
  `field(name=...)` needs nothing. Both provider classes are plain classes with an explicit
  keyword-only `__init__`, not `@dataclass` — zuban alone rejects a dataclass instance as
  `bearer_auth=` (confirmed a real zuban-only quirk, not a type error). `TokenRequestTyping`/
  `TokenRefreshRequestTyping` are `Callable[..., JSONPayload]` aliases, not exact-signature
  `Protocol`s — a Protocol rejects an aliased pydantic model on 3 of 4 checkers, since they
  synthesize the constructor from the alias names. `_CachedToken.expires_at` is wall-clock
  (`time.time()`, never `monotonic()`, since it's read back by a later process) and persisted as
  an ISO 8601 string; a naive timestamp on read is rejected as unusable. `client_auth` is ignored
  on the model path (the model already carries credentials). No public override hooks — the
  private `_mint`/`_refresh` split is the extension seam if ever needed (style-guide.md §10 forbids
  `__call__` routing through public methods). Cache write is plain sync file IO via
  `tempfile.mkstemp` (`0600`) + `Path.replace`. `refresh_leeway` is clamped to `expires_in / 2` (a
  300s default against a 60s token was expired at birth); a refresh response without
  `refresh_token` keeps the old one (RFC 6749 §6); `scope` joined `token_url`/`client_id` in the
  cache key. The `asyncio.Lock` is created lazily per running loop, not in `__init__` — a
  module-level provider reused across separate `asyncio.run()` calls otherwise raises (a loop-bound
  lock survives past the loop that created it). The Basic header is built by lothc itself
  (`quote(..., safe="")` per half, not `quote_plus`, matching RFC 6749 §2.3.1) rather than via the
  internal client's own `basic_auth=`. `default_expires_in` covers servers that omit `expires_in`;
  neither present is a `ValueError`. Refresh→mint fallback is 400-only; anything else propagates.
  `OAuthTokenError` (`.token_url`, original as `__cause__`) wraps everything `_renew` raises — the
  `bearer_auth` call happens *inside* the caller's own verb call, so an unwrapped error there would
  misattribute to the wrong call. `client_factory` (default `HTTPClient.build`, called with no
  args) replaced a `timeout` param — `functools.partial(HTTPClient.build, ...)` covers
  timeout/proxy/TLS for the token endpoint. `TokenResponseTyping` is a Protocol union needing only
  `.access_token`/`.expires_in`; `.refresh_token` is read via `getattr(..., "refresh_token", None)`
  so a model that omits it entirely is still valid (RFC 6749 §5.1 makes it optional). A
  `token_cache_path` whose parent doesn't exist fails at construction, not on first write.
- **`sse()` never lets the client's total `timeout` touch the stream, and reconnects per the
  WHATWG EventSource model.** pyreqwest's `timeout` runs connect-to-body-finish, and an SSE body
  never finishes — `_sse_stream` passes a one-year default whenever the caller gives no `timeout=`;
  an explicit `timeout=` still bounds one connection attempt. `read_timeout` (idle-gap-between-
  chunks) is the real stall detector and surfaces as `HTTPTimeoutError` via the existing transport
  translation. The retry middleware can't help mid-stream (it wraps `send()`, which returns once
  headers arrive) — reconnect instead lives in `_sse_stream` itself, looping with a
  `Last-Event-ID` header on transport error, until `max_reconnects` *consecutive* reconnects yield
  no event (reset on every event; `None` = unlimited, `0` = fail fast). A clean close returns
  unless `reconnect_on_close=True`; a 204 always returns. Spec semantics: `id:` persists across
  events until an explicit empty `id:` clears it; `retry:` overrides the reconnect delay
  mid-stream; all three line terminators are handled without fabricating a record boundary across
  a chunk split; a leading BOM is stripped once per connection; a 2xx (non-204) with the wrong
  Content-Type raises `ValueError`.
- **The `_compat.py` fallback stub classes must be subscriptable**, or `import lothc` crashes
  outright when an optional extra is missing (`_client.py` has unquoted `Decoder[Any]`/
  `TypeAdapter[Any]` annotations evaluated eagerly at import time) — fixed by giving the fallback
  stubs a trivial `__class_getitem__`. Separately: a strict-mode checker run against a *consumer's*
  code with an extra genuinely absent showed `Unknown` on unrelated public overloads, even when
  that consumer never touches the missing library. Wrapping the `TYPE_CHECKING`-branch import in
  its own `try/except` does **not** fix this — pyright ignores the `except` branch entirely once
  `TYPE_CHECKING` is `True`. The real fix: `Data`/`Params`/`Headers`/`JSONPayload`/
  `response_data_type` reference checker-local structural `Protocol`s (`StructTyping`/
  `BaseModelTyping`/etc. in `_compat.py`) instead of the nominal `Struct`/`BaseModel` classes —
  these never import anything, so they're never `Unknown` regardless of environment, and a real
  subclass satisfies them structurally for free when the packages *are* installed. Runtime
  `isinstance`/`match` dispatch still uses the real nominal classes, never the Protocols. One
  side effect: basedpyright can exclude a nominal class from a sequential `issubclass` if-chain but
  not from a `match` fallback once a union's members are Protocols — fixed with an explicit
  narrowing guard at each affected site, not a suppression.
- **Never leak the backend's exception types.** pyreqwest's `TransportError`/`RequestTimeoutError`/
  `NetworkError` are translated to lothc's own at every `.send()` and both SSE loops — a new
  pyreqwest exception that should count as a transport failure gets added to
  `_translate_transport_error`, not handled at the call site. `RedirectError` (past
  `max_redirects`) and `BuilderError` (from `.build()`/`.build_streamed()` itself) are **not**
  `TransportError` subclasses despite behaving like one — both are caught alongside it at every
  call site and mapped to the same plain `HTTPTransportError`. `_send`/`_send_sync` take the
  *unbuilt* request builder and call `.build()` inside their own try, so every caller stays simple.
- **`lothc.testing` never exposes pyreqwest internals in its public API** — no pyreqwest type a
  caller imports, constructs, or receives, and no builder pattern. `MockResponse` (plain
  `@dataclass(slots=True)`) is what a `match_request_with_response` handler returns; lothc builds
  the real pyreqwest response internally via `_apply_mock_response`. `MockRequest` (also
  `slots=True`, deliberately **not** `frozen=True` — its `headers` dict field means `frozen=True`
  couldn't deliver real immutability/hashability anyway) is what a handler/predicate receives and
  `get_requests()` returns, built from the real `Request` by `_mock_request_from` — the only place
  this module still touches it. `url=` matching is `str | re.Pattern[str]` only (pyreqwest's `Url`
  object is dropped — a string already matches the exact URL). A repeated header collapses to its
  first value in `MockRequest.headers`, matching `Result.headers`'s own convention elsewhere.
  `add_*_response`'s `params=`/`data=`/`headers=` share encoding with the real request path
  (`_encode_params`/`_encode_json_payload`/`_encode_headers` in `_client.py`); `params=`'s match
  string is derived from pyreqwest's own real query encoder rather than reimplemented (so an
  invalid value like `None` raises the same error a real request would) and is a **subset** match,
  not exact. `LOTHCMocker._add_response` validates/encodes everything before calling the pyreqwest
  verb-registration method, not after — pyreqwest registers a `Mock` the instant that call
  returns, so raising afterward would leave a bare, unconfigured `Mock` silently matching any later
  request. `data=` must always be applied before `headers=` when building a response — pyreqwest's
  `.body_json()` *appends* its own Content-Type while `.headers()` merges with same-key *replace*,
  so the wrong order leaves two Content-Type values on the wire; `LOTHCMock.with_headers`/
  `with_data` (the public chainable API) handle this regardless of caller order by having
  `with_data` re-apply any already-set headers afterward. `lothc_mocker` is strict by default
  (pyreqwest's own plugin defaults to passthrough) — opt out with
  `@pytest.mark.lothc_mocker(strict=False)` or `.strict(enabled=False)` mid-test.
  `_wrap_custom_handler`/`_wrap_custom_matcher` and the six `add_*_response` methods are
  deliberately not collapsed further despite looking similar — different return types/
  post-processing for the former, and matching `_client.py`'s own explicit-per-verb convention for
  the latter. Async/sync dispatch for a custom matcher/handler checks `__call__` too
  (`_is_async_callable`), not just `inspect.iscoroutinefunction` on the callable itself — the
  latter alone misclassifies a callable object whose `__call__` is `async def` as sync (confirmed
  live), silently running it through the wrong path. `MockRequest.query` (parsed,
  single-value-per-key) sits alongside the existing raw `query_string`, mirroring `.headers`'s own
  ergonomics.
- **`RequestInfo` must be captured *before* `.send()`, not after** — a consumed request genuinely
  can't be read once sent (`RuntimeError`). `_send`/`_send_sync` build the request, capture
  `RequestInfo`, *then* send — never as a single tuple expression (`built.send(),
  _request_info(built)` evaluates left-to-right, so `.send()` would already have consumed it). Same
  discipline for `build_streamed()`'s `StreamRequest`, used by `_sse_stream`/`_line_stream`/
  `_download`.
- **`error_type` decodes from already-fetched bytes, never the live response object** — by the
  time `_check_status` raises, the body's already been read once for `body_start`, and a pyreqwest
  body can't be read twice. `_decode_error_body` mirrors `_decode_body`'s branch order on plain
  bytes instead of reusing it. A decode failure against `error_type` propagates unwrapped, same
  rule as `response_data_type`.
- **Bare `dict` is a valid `response_data_type`; a subscripted `dict[str, Any]` is not.** A single
  generic `type[TData]` overload resolves bare `dict` to `dict[Unknown, Unknown]` under
  basedpyright strict (a generic overload copies the argument's own static type, it doesn't fill in
  type arguments from `Data`'s member list) — fixed with a second, non-generic overload per verb
  hardcoding `response_data_type: type[dict[str, Any]]`. The former dedicated `JSON` class was
  removed once this landed. Lesson: never claim something is "rejected/enforced" from a
  basedpyright result alone — verify the actual runtime behavior.
- **Overload-pairs over a single generic-with-cast signature.** Every verb has a no-`response_data_type`
  overload (`bytes`), a generic one, and one real non-generic implementation — this is why there
  are almost no `cast()` calls anywhere: the implementation never claims to return the type
  parameter itself. A single generic method with a value-default was tried and rejected — it forces
  `cast()` at every internal branch.
- **The `Data`/`JSONPayload`/`Params`/`Headers`/`Form`/`File` type aliases are named at the
  *instance* level**, not the class level. `JSONPayload` (not `Json`) was named that way
  deliberately to avoid a case-only collision with the former `JSON` response-decode class
  (removed).
- **`Params`'s `list[...]`/`tuple[...]` value means "repeat this query key once per element"** (e.g.
  `{"tag": ["a", "b"]}` → `?tag=a&tag=b`) — a plain `list`, not a tuple like `Form` uses for the
  same idea, since `Params` has no prior bare-list meaning to disambiguate from. pyreqwest's own
  `RequestBuilder.query()` rejects a `Mapping` whose value is a list (`BuilderError: unsupported
  value`, confirmed live) even though its declared stub type allows it and `Url.parse_with_params`
  genuinely does accept it — `_query_pairs` flattens to a flat list of pairs first, the one shape
  that actually works. `lothc.testing`'s mock side narrows via a single `mock.match_query(dict)`
  call instead of a `match_query_param` loop, since pyreqwest's own `query_dict_multi_value`
  already returns the right per-key `str | list[str]` shape and `match_query`'s dict form is
  exact-order-sensitive on a `list[str]` value.
- **`bearer_token`/`bearer_auth`/`basic_auth` — not `auth_token`/`auth`.** Named precisely because
  each maps to exactly one pyreqwest mechanism (`.bearer_auth()`/`.basic_auth()`); keep this
  precise if a fourth mechanism is ever added. `_apply_auth` (was `_apply_bearer_auth`) applies
  whichever of the three is configured.
- **`response_data_type` defaults to `bytes` everywhere** except `sse()`, whose bare default stays
  `SSEEvent[str]` — a stream of named records has no single "raw bytes" analogue.
- **`SSEEvent[TData, TId = str]`** — the class's own `TId` default must track `sse()`'s actual
  default (these disagreeing was a real bug once). `.event` is always `str` (spec default
  `"message"`); `.id` is genuinely `str | None` per spec. `id_type`/`allow_missing_id` are two
  independent knobs (type coercion vs. requiredness) — a union-accepting `id_type` design was
  tried and rejected: it breaks `mypy`/`ty`/`zuban`'s handling of a real `UnionType` value where
  `basedpyright` alone would decompose it. `response_data_type` scopes to `.data` only — `sse()`
  must never yield the decoded value bare, always inside a full `SSEEvent`.
- **`response_data_type` means something different on `stream_get`/`stream_post` than everywhere
  else** — whole-body decode on `get`/`post`/etc., per-NDJSON-line decode on the streaming verbs.
  Deliberate name reuse for consistency, not an oversight — called out in `docs/streaming.md`.
- **`download(path, dest=None)` exists because `get()`'s default `bytes` path does ~3 full copies
  of a large body at peak** (pyreqwest's own internal buffering, plus lothc's own
  `bytes(await raw_response.bytes())` copy). `download()` streams via `build_streamed()` into a
  `bytearray` (`+=`, not `.extend()` — accepts pyreqwest's buffer-protocol chunks directly with no
  per-chunk copy), roughly a third of the peak memory; `dest: Path` streams straight to a file
  instead, O(chunk size) regardless of body size. Deliberately did not change `get()`'s own default
  path — the small JSON bodies this library is designed around make the extra copies free in
  practice, and `download()` is additive for the large-body case specifically.
- **`.to_bytes()`, never `bytes(the_thing.bytes())`**, to turn a `pyreqwest.bytes.Bytes` into a
  real Python `bytes` — `.to_bytes()` is the conversion method the underlying crate provides.
- **Don't convert at all when the target library accepts a buffer-protocol object directly.**
  `msgspec.json.decode()` does, so the `Struct` branch decodes straight from the raw bytes with no
  extra copy; `pydantic.model_validate_json()` needs a real `str`/`bytes`, so that branch calls
  `.to_bytes()` first — and parses the JSON bytes directly rather than round-tripping through a
  dict first.
- **Dropped: `TypedDict`/`typeguard` support.** Was decode-only, ~3.5x slower than the compiled
  pydantic/msgspec validators with no capability they didn't already cover — removed deliberately,
  not just left unmaintained. The old design is preserved in git history if ever needed again.
- **pyreqwest's `streamed_read_buffer_limit` defaults to 64KB and silently defeats real-time
  streaming below that size** — its streamed body reader withholds received bytes until either
  the limit is hit or EOF, so small, slowly-trickling payloads (SSE, NDJSON) never arrive until the
  connection closes. Fixed by setting it to `1` at all 4 real-time streaming call sites
  (`sse`/`stream_get`/`stream_post`, both clients) — confirmed this does *not* mean byte-at-a-time
  reads (chunk size/count are governed by the OS socket regardless), so there's no throughput cost.
  Deliberately left `download()`'s two call sites untouched — bulk transfer benefits from the
  larger default and has no low-latency requirement.
- **Status errors are separate from transport errors.** `HTTPResponseError` (4xx/5xx with a
  body_start snippet) is a different failure class from `HTTPTransportError` (never got a response
  at all) — don't unify them.
- **Validation errors from the chosen decode library are NOT wrapped.** A `pydantic.ValidationError`
  or `msgspec.ValidationError` propagates natively — the user opted into that library by choosing
  it as a `response_data_type`, so its own exception is the expected one to see.
- **basedpyright strict mode is the contract.** Every change must pass `task typecheck` with zero
  errors and, ideally, zero new `cast(...)` calls.
