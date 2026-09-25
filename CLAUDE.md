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

Publishing needs the tests to pass first: `release.yml` runs the whole of `ci.yml` (via
`workflow_call`, `secrets: inherit` for Codecov) against the tagged commit, and before
`uv publish` installs the built wheel with no extras and imports it, catching a module missing
from the wheel. Side effect: ci.yml's Codecov step has `fail_ci_if_error: true`, so a Codecov
outage blocks a release. CI's `test-no-extras` job (and `task test-no-extras`) runs every test
module that never mentions pydantic/msgspec with both uninstalled (`--no-install-package`, then
`uv run --no-sync`, since a plain `uv run` re-syncs them back), because the main job always has
both and would miss an unguarded import. Only pyreqwest 0.13.0 is supported (`>=0.13.0`), so
there's no lower-bound job; for the record the suite also passed on 0.11.6-0.12.x.

Release notes are hand-written in `CHANGELOG.md` (Keep a Changelog). Before `task release`, rename
`## [Unreleased]` to `## [X.Y.Z] - <date>` (the version the bump will produce), add a fresh empty
`## [Unreleased]` above it plus the compare links at the bottom, and commit. `task release` posts
that section (via `scripts/release_notes.py`) as the GitHub release notes with a compare link,
and refuses to tag anything if the section is missing or empty. It used `--generate-notes`, which
only lists merged PRs, so releases pushed straight to `main` got nothing but a compare link.

**Follow-up not yet done**: the `pypi` environment has no deployment protection rules (anyone who
can create a GitHub Release can trigger a publish) — add a tag-pattern restriction under
Settings → Environments → pypi as defense in depth. Left for manual GitHub UI setup since
`release: published`'s ref context is the tag, not a branch, and there's no easy local way to
preview a policy change first.

**README.md uses absolute URLs only.** It's also the PyPI project description, and PyPI can't
resolve repo-relative paths: images point at `raw.githubusercontent.com/.../main/`, docs links at
the docs site (`rogerthomas.github.io/lothc/<page>/`), other files at `github.com/.../blob/main/`.
A PyPI description is frozen per release, so a README fix only shows after the next one.

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
(`cookie_store=True`), redirects (`follow_redirects`/`max_redirects`), `proxy=`/`no_proxy=`,
`user_agent=` (default `python-lothc/<version>`), `http2=`, `resolve=`, `local_address=`,
`tcp_keepalive=`, retries
(`max_retries`/`retry_methods`, real pyreqwest `with_middleware` hook, idempotent verbs by
default), typed error bodies (`error_type=`, mirrors `response_data_type`), TLS/mTLS/pool config
(`connect_timeout`, `root_certificates`, `identity_pem`, `min_tls_version`/`max_tls_version`,
`danger_accept_invalid_certs`, `https_only`, `max_connections`, `pool_idle_timeout`,
`pool_max_idle_per_host`, `pool_timeout`, `read_timeout`), request metadata (`.request:
RequestInfo` on both `Response` and `HTTPResponseError`, every verb; `Response.http_version`/
`.elapsed`), bodies on `delete`, OAuth 2 client credentials
(`OAuthProvider`/`SyncOAuthProvider`, see dev notes), and a pytest mocking plugin (`lothc[testing]`
— see dev notes).

Not done yet: `sse_post` (SSE over POST, deferred by the user). Maybe later, only if someone asks
(dropped by the user; they came from a code review, not a request): request/response hooks, a
generic `request()`/OPTIONS, streaming uploads, status/headers on stream/download calls,
cookie-jar access. Deliberately not exposed: per-algorithm compression
toggles, `interface`, `tcp_nodelay`, `referer`, CRLs. No final URL on `Response`: pyreqwest's
response doesn't expose one.

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

### Test suite speed

The suite ran in ~3.0s (385 tests), down from ~45s; it's ~7.6s at 650 tests now, the growth being the deliberate timing tests (stream idle limits, retries) rather than overhead. It got there in two passes, and the useful
lesson is what the time actually was: **not** per-test overhead. Measured, the fixed floor
(interpreter + plugins + `import lothc` + collection) is ~0.45s, and ~360 of the tests cost ~2ms
each including a real HTTP round trip. Everything else was a handful of deliberate sleeps.

Two measurements worth not re-deriving, both of which killed a plausible-sounding idea:

- **Per-test client construction is ~2ms, not ~10ms** (an earlier estimate here was wrong), and
  most of that is the round trip, not the construction. Sharing an `HTTPClient` across a module
  buys nothing and costs isolation.
- **Per-test event-loop setup is inside that same 2ms.** No uvloop, no broader-scoped loop.

What actually did it:

- **`serve_forever(poll_interval=0.01)`, not the 0.5s default** (`tests/conftest.py`,
  `tests/test_sse_interrupt.py`). `shutdown()` blocks until the loop next wakes, so the default
  charged up to half a second per server teardown — invisible in any test's own reported duration.
- **Scale a timing *pair* down together, never a lone margin.** Where a test asserts a ratio (a
  server sleep vs. a client timeout), both numbers shrink and the assertion is bit-for-bit
  identical: the SSE total-timeout tests went from 0.6s stream vs 0.3s timeout to 0.15s vs 0.05s,
  which is a *wider* 3x margin at a quarter the cost. A green run never waits on that timeout at
  all, since `sse()` substitutes a one-year default.
- **`backoff_base=0.001` on the ten retry tests that assert *that* a retry happened**, never how
  long it waited. Deliberately non-zero, so the same real sleep path still runs.
- **`/slow` takes a `seconds=` query param**, so a test that aborts early can ask for a long sleep
  it never waits out, while the two tests that deliberately run it to completion ask for a short
  one. One shared constant had to be sized for the strictest caller and overcharged the rest.
- **A negative control's dwell time is not the same number as a timeout.** In
  `test_sse_ctrl_c_interruptibility` the `interruptible=True` deadline is an upper bound a green
  run never reaches (so its size is free) while the `False` one is paid in full every run. They
  were one shared 2s value; splitting them (2.0 / 0.5) cut the negative control 2.63s -> 0.60s,
  still ~20x over the slowest measured Ctrl-C exit (~25ms).

**Going below a default inverts what a floor proves.** The `backoff_base` scaling test used to
set 0.2 (above the 0.1 default) and assert `elapsed >= 0.6`. Testing at 0.02
makes a floor useless — an ignored parameter would wait *longer* and still pass — so it asserts a
bracket, `0.06 <= elapsed < 0.2`. That is strictly more discriminating than the old floor, which
could not detect "waited far too long". Confirmed by mutation: deleting the `backoff_base=` kwarg
fails both tests at the ceiling.

Deliberately still slow, because the wait *is* the assertion: `sse_ctrl_c_interruptibility[False]`
(~0.8s: a 0.2s settle before Ctrl-C, so the child is really blocked in `read_chunk()` rather than
racing the byte while still running Python bytecode, which flaked the negative control ~1 in 25
under load, plus the 0.5s dwell), the two SSE total-timeout tests (0.18s each), and
`test_import_lothc_succeeds_without_msgspec_or_pydantic` (0.12s, a real subprocess).

Rejected with measurements, so don't re-litigate: **pytest-xdist** (actually installed and run —
`-n 4` is 2.54s wall vs 3.43s serial, so ~0.8s for a new dev dependency, non-deterministic
ordering and degraded `-x`; the payoff *shrinks* as the suite gets faster, since worker startup is
~0.85s of floor); **bypassing `uv run`** (~30ms); **lowering `/events`' default interval**
(load-bearing for `test_sse_events_arrive_incrementally_not_buffered_until_stream_end`, which
asserts on the per-event deltas).

### Real TLS and sync/async parity tests

- **TLS settings run against a real HTTPS server** (`tests/_tls_server.py`, certs minted by
  `trustme`, a dev dependency). `conftest.py` provides `tls_ca_pem`, `tls_client_identity_pem`,
  `https_base_url` and `mtls_base_url` (client cert required). The handshake runs on the handler
  thread, so a failed one never blocks `serve_forever`. Every negative control in
  `tests/test_tls.py` pins the rustls failure in `match=` (`UnknownIssuer`,
  `CertificateRequired`, `UnknownCA`, `ProtocolVersion`) so it can't pass for an unrelated
  reason. Certs load via stdlib `load_cert_chain` from a trustme tempfile, not trustme's
  `configure_cert`, whose pyOpenSSL-typed signature is partially unknown to basedpyright.
  `max_connections`/`pool_timeout` have a real test (one slot, two concurrent `/slow`);
  `connect_timeout` can't be tested locally, since macOS completes the TCP connect even with a
  full backlog.
- **`tests/test_sync_async_parity.py` enforces the async/sync mirror.** Public API: same methods
  and constructors, every overload's signature equal once async names map onto sync ones
  (`_desync`). Implementation: every paired method body and module-level `foo`/`foo_sync` pair is
  compared from source via `ast`, after stripping `async`/`await` and the `Sync` markers.
  Intentional differences live in reasoned allowlists (sync-only `interruptible=`, the
  `_sync_stream_chunks` readers, OAuth's lock), and an entry that stops differing fails. So a
  feature added to one client only fails it. Mutation-checked: missing parameter, changed default,
  missing method and body drift each fail.

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

Both are constructed with their settings and opened as a context manager; the pyreqwest client
they wrap only exists while open, so it never appears in their public API:

```python
async with HTTPClient(base_url=..., bearer_token=..., timeout=30.0) as client:
    ...
```

### Verbs

`get`, `post`, `put`, `patch`, `delete`, `head`, `sse`, `stream_get`, `stream_post`, `download` —
each is a set of `@overload`s plus one real implementation. See style guide for *why* overloads are
used instead of a single generic signature. `get`/`post`/`put`/`patch`/`delete`/`head` return a
`Response` (body on `.data`); the streaming verbs and `download` don't.

### Code style

Use the `write-python-code` skill (`.claude/skills/write-python-code/SKILL.md`) before writing or
editing Python code here — it covers this project's own conventions plus when to read
`./style-guide.md` in full (required before touching `lothc/_client.py` specifically). The
"Development notes" list below **is** the design log for *why* the code looks the way it does —
the skill covers *how* to write new code that matches it.

## Development notes

- **`async with HTTPClient(...)`, not `HTTPClient.build(...)`: the pyreqwest client is invisible.**
  The clients used to be `@dataclass`es opened through a `build()` classmethod, so their public
  constructor was `HTTPClient(<pyreqwest Client>, ...)`, a pyreqwest type in lothc's own API. Now
  `__init__` takes only lothc settings (validated there, so e.g. conflicting auth fails at
  construction), and `__aenter__`/`__enter__` build and open the pyreqwest client, which exists
  only while open behind a `_client` property that raises a clear error otherwise. Deliberately
  not dataclasses (style-guide §1 deviation): constructor params are settings, not stored fields.
  A pyreqwest builder can only be built once ("Client was already built"), so settings live in a
  `_TransportSettings` record and each entry rebuilds, which also makes a client re-enterable, so
  a module-level client works across separate `asyncio.run()` calls. `TlsVersion` is lothc's own `Literal` alias, not
  pyreqwest's. `tests/test_public_api.py` enforces all of this by walking every exported
  signature, overload and type alias in `lothc` and `lothc.testing` for a pyreqwest type; run
  against the old code, it flags exactly the four leaking constructors.
- **A bad `base_url` fails at construction, mirroring pyreqwest's rule.** pyreqwest rejects a
  `base_url` with a query, a fragment, or a path not ending in `/` (without the slash a relative
  path would replace the last segment), but only when the client is built, i.e. on entry.
  `_is_joinable_base_url` repeats that rule in `_TransportSettings.__post_init__` with the same
  message. Joining itself is pyreqwest's (RFC 3986): a leading `/` in a path drops the base's own
  path prefix (`base_url=".../api/v2/"` + `get("/users")` hits `/users`), and an absolute or
  protocol-relative (`//host/...`) path overrides the base, taking auth headers with it.
- **`OAuthProvider`/`SyncOAuthProvider` (`lothc/_oauth.py`).** RFC path sends the token request
  with `data=` (urlencoded). An aliased pydantic `token_request=`/`token_response=` model needs `ConfigDict(validate_by_name=True)` so lothc can
  construct it by field name (it used to need `serialize_by_alias=True` too, before lothc encoded
  pydantic models by alias; see below); msgspec `field(name=...)` needs nothing. Both provider classes are plain classes with an explicit
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
  misattribute to the wrong call. `client_factory` (default `HTTPClient`, called with no
  args) replaced a `timeout` param — `functools.partial(HTTPClient, ...)` covers
  timeout/proxy/TLS for the token endpoint. `TokenResponseTyping` is a Protocol union needing only
  `.access_token`/`.expires_in`; `.refresh_token` is read via `getattr(..., "refresh_token", None)`
  so a model that omits it entirely is still valid (RFC 6749 §5.1 makes it optional). A
  `token_cache_dir` that doesn't exist fails at construction, not on first write. It replaced a
  single `token_cache_path` file so providers can share one directory: `_token_cache_file` names
  each file `<host>-<sanitised client_id>-<sha256(token_url, client_id, scope)[:12]>.json`, not
  just `{client_id}.json`, because one client ID can be used against several token URLs or scopes
  (cached separately) and may contain characters unsafe in a filename. No leading dot, and the
  in-file `token_url`/`client_id`/`scope` guard stays as a second check. Not encrypted (considered
  and rejected): stdlib has no cipher, so it'd need `cryptography`, and a key derived from the
  client secret protects little, since the secret can mint tokens itself; AWS CLI and gcloud also
  rely on `0600` plaintext.
- **For `stream_get`/`stream_post`/`download`, the client's `timeout` is a gap limit, not a total
  cap.** A total cap (pyreqwest's only per-request timeout) killed every healthy long stream or
  large download at 30s. So these verbs swap it for `_streaming_default_timeout` (a year, as `sse`
  does) and lothc enforces `_stream_idle_timeout` itself: the client `timeout`, unless
  `read_timeout` was set (pyreqwest then enforces the gap at the socket). An explicit per-call
  `timeout=` keeps meaning a total cap. `sse` gets no gap limit, since quiet periods are
  legitimate there. pyreqwest has no per-request read timeout and `read_chunk()` takes no timeout,
  so: async wraps opening the stream plus the status check, and each chunk read, in
  `asyncio.timeout` (`_within_idle_limit`/`_read_chunk_within`), never across a `yield`, so a
  slow consumer is never mistaken for a stalled server. Sync can only stop waiting by reading on a
  worker thread (`_interruptible_chunk_iter`, now also the default path whenever a limit
  applies): measured no throughput cost, but on a genuine stall the blocked read's thread and
  socket stay parked until the peer closes (accepted; a blocked sync read can't be cancelled).
  That queue is bounded (64 chunks) with an abandonment event, because an unbounded one made a
  slow consumer buffer the whole body in RAM, and a plain blocking `put` would have left an early
  `break`'s worker parked forever. The sync clock restarts when the caller asks for the next chunk
  (the worker reads ahead), and polls at `idle/4`: at one poll per window, a clock left running
  across a consumer's pause couldn't be told apart from a correct one, which mutation testing
  caught.
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
  object is dropped — a string already matches the exact URL). `MockRequest.headers` is a
  `CaseInsensitiveDict` like `Response.headers`, so a repeated header keeps every value (`get_all`).
  `add_*_response`'s `params=`/`data=`/`headers=` share encoding with the real request path
  (`_encode_params`/`_encode_headers` in `_client.py`; `data=` uses `testing.py`'s own `_encode_json_payload`, byte-identical to the real `_attach_json_body`); `params=`'s match
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
  bytes instead of reusing it. Decoding is **best-effort** (`_try_decode_error_body`): a body
  that doesn't match `error_type` gives `parsed_body=None` plus `.parse_error` (the library's own
  `ValueError`, which every decode failure here is) and still raises `HTTPResponseError`. It used
  to propagate unwrapped like `response_data_type`, so a proxy's 502 HTML page against
  `error_type=ErrorModel` raised pydantic's `ValidationError` instead, losing the status and
  skipping `except HTTPResponseError`. An unusable `error_type` is a caller bug and its
  `TypeError` still propagates; `.parse_error` isn't pickled (pydantic's `ValidationError` can't
  be).
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
- **Every body verb returns a `Response`; there's no body-only mode.** It used to return the bare
  decoded body, with `client.with_result.<verb>` (a `Result`) as the escape hatch, modelled on the
  OpenAI/Anthropic SDKs' `with_raw_response`. Reversed because lothc is a general-purpose ("raw")
  client, not an SDK for one known API: a research pass found typed SDKs return the body (their
  authors know every endpoint's shape), while general clients (requests, httpx, aiohttp, axios)
  return a response object, and the maintainer expects most callers to need status/headers. So
  `Result` became `Response` (`.data`, `.status`, `.headers`, `.typed_headers`, `.request`,
  `.http_version`, `.elapsed`), `response_headers_type=` moved onto the verbs, and `WithResult`/
  `SyncWithResult`/`with_result` were deleted. A flag (`as_response=True`) was never an option:
  a return type depending on a runtime `bool` multiplies overloads and a non-literal `bool` matches
  none. The verbs share `_send_method` (the method as a string) → `_send_with_body`.
- **A `BuilderError` is permanent, so it's never reconnected.** pyreqwest raises it from
  `.build()`/`.build_streamed()` before anything reaches the network (a rejected scheme under
  `https_only`, a malformed URL), so retrying cannot change the outcome — yet `sse()` used to
  spend its whole `max_reconnects=5` × `reconnect_delay=3.0` budget on one, making a deterministic
  misconfiguration take 15s to surface. `_is_permanent_transport_error` reads it off `__cause__`
  (every translation site sets it via `raise ... from error`), so the public exception type is
  unchanged and `except HTTPTransportError` still catches it — chosen over a new public subclass
  to keep the surface flat. `RedirectError` is deliberately excluded: a redirect loop can be
  transient. The retry middleware needed no change — it only catches `PyreqwestTransportError`,
  and `.build()` runs outside `next.run` anyway, so a `BuilderError` never reached it.
- **A body cut off partway is retried, a corrupt one isn't.** pyreqwest reads a non-streamed
  body inside `send()`, so a body error surfaces inside the retry middleware's `next.run` and can
  be retried there. A truncated chunked body is already a `ReadError` (a `TransportError`); one
  shorter than its `Content-Length` is a `BodyDecodeError`, the same type as a corrupt gzip body.
  `_is_retryable_send_error` retries a decode error only when one of its causes is hyper's
  "error reading a body from connection" (probed: truncation always has it, corrupt gzip/br/
  deflate/zstd never do). Gotcha when testing: a "corrupt" gzip body shorter than gzip's 10-byte
  header ends the decoder early and comes back as a `ReadError`, i.e. already retryable.
- **`backoff_base` is a client constructor parameter, not just a `_RetryMiddleware` field default.** It was
  private, so a user on a retry-heavy path had no way to tune backoff at all (and the retry tests
  couldn't shrink their ~2s of real sleeping). Same default (0.1), same
  `backoff_base * 2 ** attempt` formula, `Retry-After` still wins over the computed delay.
- **`Response.headers`/`MockRequest.headers` are a `CaseInsensitiveDict`, not a `dict`.** They were
  `dict(raw_response.headers)`, so `result.headers["Content-Type"]` raised `KeyError` while
  `["content-type"]` worked — pyreqwest's `HeaderMap` is itself a case-insensitive multi-value
  map, and flattening it to a plain `dict` threw both properties away. Checked against the field:
  requests, niquests, httpx and httpx2 all expose a `MutableMapping`, and *none* subclasses
  `dict` — a `dict` subclass can only override the Python-level lookups, so `{**headers}` and
  `dict(headers)` would silently revert to exact-match keys. Iteration keeps the stored casing
  (requests/niquests behaviour; httpx lowercases instead) — though reqwest lowercases before
  lothc ever sees a header, so in practice that only shows for headers lothc sets itself. The
  `__init__` iterates pairs and *appends* rather than building from `dict(data)`, which is what
  keeps every value of a repeated header (`set-cookie`) reachable via `get_all`; appending in
  arrival order is also what keeps `__getitem__` on the first value, matching what a plain
  `dict(raw_response.headers)` always returned. `get_all` (not httpx's `get_list`) because
  stdlib `email.message.Message.get_all` is the precedent for exactly this data — `http.client`
  responses *are* a `Message` — and `list` names the return container that `-> list[str]` already
  states, where `all` names the semantics; urllib3 offers both spellings, so neither camp is
  surprised. `__eq__` compares every value against another `CaseInsensitiveDict` (two responses
  differing only in a dropped `set-cookie` must not compare equal) but only the single-valued
  view against any other mapping, which is all the other side holds. That makes equality
  non-transitive with repeated headers; documented rather than changed, since the alternative (a
  plain-dict comparison returning `False` whenever a header repeats) would make `h == dict(h)`
  false for any response with two cookies. requests' `CaseInsensitiveDict` behaves the same way. `__repr__` switches to the
  pair-list form as soon as a name repeats, so it can't render three cookies as one entry. The
  one `cast` in `__init__` covers a basedpyright artifact, not a real case: it narrows the
  `Iterable[tuple[str, str]]` member against `Mapping` too, synthesizing a
  `Mapping[tuple[str, str], Unknown]` the declared type can't produce.
- **`data=` is urlencoded, `form=` is multipart.** There used to be no urlencoded option at all
  (OAuth built `content=urlencode(...)` plus a header by hand). `data=` matches requests/httpx,
  where `data=` is the urlencoded one, so a call brought over from them sends the same body.
  Settled after trying `form=`/`parts=`; `multipart=`, `xform=`/`mform=` were also rejected.
  "data" also names response bodies here (`Response.data`, `SSEEvent.data`, the mocker's `data=`),
  accepted as the cost of matching requests/httpx. `data=` takes `Params` and reuses its encoding
  (`_query_pairs(_encode_params(...))`) into pyreqwest's native `.form()`, which takes the same
  pairs as `.query()` (probed: repeats keys, `true`/`false`).
- **A `Form` repeat tuple must be homogeneous** — `tuple[_FormValue, ...]` also admitted
  `(b"...", "image/png")`, which reads like a file plus its content-type but has no filename to be
  one, and silently went out as a binary part *plus* a text part reading `"image/png"` (confirmed
  on the wire). `_FormRepeat` is now a union of four homogeneous tuples (text / bytes / `File` /
  JSON), which all four checkers reject that shape against while still accepting every legitimate
  repeat, and `_check_form_repeat` raises the same rule at runtime for anyone past the checker.
  Only `Path` carries its own filename, which is why `tuple[Path, str]` is a `File` shape and
  `tuple[bytes, str]` can't be. `_form_value_kind` deliberately mirrors `_apply_form_value`'s
  *branch order*, not `_FormRepeat`'s member order, so a value's reported kind is always the
  branch it actually takes. An empty repeat tuple raises `ValueError` — no type can express
  "non-empty" here, and it would otherwise contribute no parts at all (same call as `Params`'s
  empty-sequence rejection in `lothc.testing`).
- **`case list()` must precede the `File` patterns in `_apply_form_value`/`_apply_sync_form_value`**
  — a `match` sequence pattern matches a `list` as happily as a `tuple`, so `["a.png", b"..."]`
  was read as a `(filename, content)` file despite the documented rule that a list *always* means
  "JSON-encode me as one part." It now takes the JSON branch and stdlib `json` raises on the bytes
  natively, per the "encode errors propagate unwrapped" rule. Ordering is the whole fix: there's
  no tuple-only sequence-pattern syntax to reach for instead.
- **`Params`'s `list[...]`/`tuple[...]` value means "repeat this query key once per element"** (e.g.
  `{"tag": ["a", "b"]}` → `?tag=a&tag=b`) — both spellings are accepted (unlike `Form`, which uses
  `tuple` specifically to disambiguate from a bare `list` meaning something else there; `Params`
  has no such prior meaning to protect, so there's nothing to disambiguate and both are just "a
  sequence of values"). pyreqwest's own `RequestBuilder.query()` rejects a `Mapping` whose value
  is a list/tuple (`BuilderError: unsupported value`, confirmed live) even though its declared
  stub type allows it and `Url.parse_with_params` genuinely does accept it — `_query_pairs`
  flattens to a flat list of pairs first, the one shape that actually works. `lothc.testing`'s
  mock side narrows via a single `mock.match_query(dict)` call instead of a `match_query_param`
  loop, since pyreqwest's own `query_dict_multi_value` already returns the right per-key
  `str | list[str]` shape and `match_query`'s dict form is exact-order-sensitive on a `list[str]`
  value; an empty `list`/`tuple` is rejected outright there (`ValueError`) rather than silently
  registering a mock that matches any query string — confirmed live that `Url.parse_with_params`
  drops an empty-valued key entirely instead of erroring, which would otherwise defeat the
  narrowing silently. `lothc.testing`'s `_query_param_match_values` imports `_client.py`'s private
  `_QueryValue` type alias rather than inline-duplicating it, so the two can't drift apart.
- **`bearer_token`/`bearer_auth`/`basic_auth` — not `auth_token`/`auth`.** Named precisely because
  each maps to exactly one pyreqwest mechanism (`.bearer_auth()`/`.basic_auth()`); keep this
  precise if a fourth mechanism is ever added. `_apply_auth` (was `_apply_bearer_auth`) applies
  whichever of the three is configured.
- **`response_data_type` defaults to `bytes` everywhere** except `sse()`, whose bare default stays
  `SSEEvent[str]` — a stream of named records has no single "raw bytes" analogue.
- **`SSEEvent[TData]` with `id: str = ""`, no id options.** A missing id reads as `""`, which is
  what the spec's last-event-id buffer and a browser's `lastEventId` both use, so `.id` is
  always `str` with no flag. This replaced `id_type=` (coercion) plus `allow_missing_id=`
  (`None` vs. raising): together they cost twelve overloads per client, a second type parameter,
  and a hand-written covariant class, since `Literal[False]`/`bool` overloads only stop
  overlapping if `SSEEvent` is covariant, which no dataclass is (its `__replace__` makes the
  fields invariant). Converting is `int(event.id)` at the call site. Not `frozen=True`, since
  `.data` can be a `dict` (style-guide §1). An empty buffer sends no `Last-Event-ID`.
  `response_data_type` scopes to `.data` only — `sse()` must never yield the decoded value bare,
  always inside a full `SSEEvent`.
- **`sse`/`stream_get`/`stream_post` are typed `Generator`/`AsyncGenerator`, not `Iterator`.**
  They always were generators at runtime, but `Iterator`/`AsyncIterator` has no `close()`/
  `aclose()`, so a caller who stopped reading early couldn't release the connection without a
  cast, and an async generator abandoned by `break` stays open until the loop's finalizer runs.
  `tests/test_stream_close.py` checks both halves: the calls type-check (mypy rejects
  them against the old types) and the server really sees the hang-up (it doesn't without the close).
- **`response_data_type` means something different on `stream_get`/`stream_post` than everywhere
  else** — whole-body decode on `get`/`post`/etc., per-NDJSON-line decode on the streaming verbs.
  Deliberate name reuse for consistency, not an oversight — called out in `docs/streaming.md`.
- **`download(path, dest=None)` exists because `get()`'s default `bytes` path does ~3 full copies
  of a large body at peak** (pyreqwest's own internal buffering, plus lothc's own
  `bytes(await raw_response.bytes())` copy). `download()` streams via `build_streamed()` into a
  `bytearray` (`+=`, not `.extend()` — accepts pyreqwest's buffer-protocol chunks directly with no
  per-chunk copy), about two-thirds of `get()`'s peak (measured 105MB vs 154MB on a 50MB body;
  an earlier "a third" claim here was wrong, since the final `bytes(buffer)` is a second full
  copy); `dest` streams straight to a file instead, O(chunk size) regardless of body size, via
  `_atomic_download_file` (hidden sibling + rename, so a failed download never leaves a truncated
  file and never clobbers an existing one). Deliberately did not change `get()`'s own default
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
  `download()` was once left on the default on the (unmeasured) belief that bulk transfer benefits
  from the larger buffer. It now uses `1` too, because the stream idle limit needs chunks as they
  arrive: a slow but healthy download (1KB/s) would otherwise look silent for over a minute. Measured
  no cost (100MB over loopback in 79ms vs 80ms, similar chunk counts).
- **Status errors are separate from transport errors.** `HTTPResponseError` (4xx/5xx with a
  body_start snippet) is a different failure class from `HTTPTransportError` (never got a response
  at all) — don't unify them.
- **Validation errors from the chosen decode library are NOT wrapped** — for `response_data_type`.
  A `pydantic.ValidationError` or `msgspec.ValidationError` propagates natively: the user opted
  into that library by choosing it, so its own exception is the expected one to see. `error_type`
  is the deliberate exception (best-effort, see its note above), because an error body is where a
  server is least likely to honour its own contract.
- **`TypeAdapter`/`Decoder` are valid `response_data_type`s on every verb**, as they already were
  on `sse()`/`stream_*`: the way to decode a top-level JSON array (`TypeAdapter(list[Item])`).
  Each verb gained one overload returning the adapted type, and the ten plain-verb
  implementations return `object` rather than `Data`, since an overload returning an unbounded
  `TAdapted` is otherwise "not consistent" with its implementation. `_decode_body` handles the
  adapters before the "must be a class" validation (an adapter is an instance). `dict` on a
  non-object body raises a clear `ValueError` (`_require_json_object`) instead of `dict()`'s own
  `TypeError`, which also keeps "every decode failure is a `ValueError`" true for `error_type`.
- **pydantic models encode by alias, like msgspec's `rename=`.** pydantic validates by alias but
  serializes by field name by default, so a camelCase model decoded from an API went back to it
  as snake_case. `_pydantic_by_alias` reads the model's own `serialize_by_alias` (default `True`),
  so an explicit `False` is still honoured and lothc never contradicts a model's `model_dump()`.
  Applied at every encode site: `json=`, `params=`, `headers=`, JSON form parts, and
  `lothc.testing` mock responses.
- **`max_retry_after` caps `Retry-After` (default 60s).** A longer wait ends retrying and the
  429/503 is raised, so the caller can schedule its own retry from `e.headers["Retry-After"]`
  (`Retry-After: 3600` used to sleep an hour inside `get()`). Stopping rather than clamping,
  because retrying early against a server that asked for a long wait is usually rejected again.
- **A 401 re-authenticates once when `bearer_auth` can `invalidate()`.** `_ReauthMiddleware`
  invalidates the rejected token on *any* 401 (so the next call gets a fresh one) but retries
  only idempotent verbs, never replaying a POST, and only once (a second 401 is raised). The
  client knows nothing about OAuth: it checks for the `_InvalidatableAuth` protocol, which any
  custom provider can implement. `invalidate(stale_access_token)` is a no-op if the provider has
  already moved on, so concurrent 401s can't discard each other's fresh token; it marks the token
  expired rather than dropping it, so renewal goes through the refresh token when there is one.
  Mutation-testing this turned up a harness pitfall: restoring a same-size mutation within the
  same second left Python running the mutated `.pyc` (its check is size plus 1-second mtime), so
  mutation runs use `python -B`.
- **basedpyright strict mode is the contract.** Every change must pass `task typecheck` with zero
  errors and, ideally, zero new `cast(...)` calls.
