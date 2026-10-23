# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`lothc` ("Lord Of The Http Clients") is a typed HTTP client library built on
[pyreqwest](https://github.com/mostafa-hussein/pyreqwest), a Rust-backed HTTP client. It exposes
one typed API, `HTTPClient` (async) and `SyncHTTPClient` (sync), with optional first-class support
for **pydantic** and **msgspec** as both decode *and* encode targets (a `BaseModel`/`Struct` can be
passed directly as `json=`).

Both libraries are optional extras (`lothc/_compat.py`): lothc must work with neither, either, or
both installed. `TypedDict`/`typeguard` support was deliberately removed (see Development notes).

This file is the source of truth on decisions and remaining work. Keep it up to date, and keep
entries terse: a fact plus the *why*, not a narrative of how it was found.

## Commands

- Install / update dependencies: `task deps-sync` / `task deps-upgrade`
- Lint (auto-fix) / format / type-check: `task lint` / `task format` / `task typecheck`
- Everything CI runs: `task check`
- Tests: `task test`, one test: `task test-one -- tests/test_foo.py::test_bar`, doctests:
  `task test-doctests`
- Pre-commit: `task precommit-install` (installs `prek`'s hook; `prek` is a Rust drop-in for
  `pre-commit`, reading the same `.pre-commit-config.yaml`, and is a uv-managed dev dependency),
  `task precommit` to run every hook against the whole repo
- Cut a release: merge release-please's release PR (see Releasing)

> [!IMPORTANT]
> The test tasks accept `PYTEST_PROFILE=agent|dev` (default `dev`). Use `PYTEST_PROFILE=agent` by
> default for concise output. If a test fails, re-run just that test with `PYTEST_PROFILE=dev` for
> full detail, e.g.
> `task test-one PYTEST_PROFILE=dev -- tests/test_get.py::test_get_returns_raw_bytes_by_default`.

> [!CAUTION]
> `task perf`/`task perf-build` start real Docker containers (`json-server` + `perf`, via
> `benchmarks/docker-compose.yml`) and run for tens of seconds to minutes. Only run them when the
> user explicitly asks for a benchmark.

> [!CAUTION]
> `task example-run` needs `task example-server` already running (it can't start one itself). If
> nothing is listening on `127.0.0.1:8701`, ask the user to start it, or start it in the
> background, rather than guessing why the connection fails.

## Releasing and packaging

- **SemVer from the git tag** via `hatch-vcs` (`dynamic = ["version"]`): a release is a tag,
  nothing to bump by hand. SemVer rather than CalVer (unlike `yeetr`) because other packages
  depend on lothc and need the major/minor/patch signal.
- **Releases are cut by [release-please](https://github.com/googleapis/release-please)**
  (`release-please-config.json`, `.release-please-manifest.json`,
  `.github/workflows/release-please.yml`), so **commit messages on `main` must be Conventional
  Commits** (`feat:`, `fix:`, `perf:`, `docs:`, `chore:`; `!` or a `BREAKING CHANGE:` footer for a
  breaking change). Each push to `main` opens or updates a release PR that bumps the manifest and
  prepends a generated `CHANGELOG.md` entry; merging it tags the release and creates the GitHub
  Release. There's no `task release` any more.
- **Pre-1.0, `feat` bumps the patch and a breaking change the minor**
  (`bump-patch-for-minor-pre-major`/`bump-minor-pre-major`), keeping lothc on 0.0.x until a
  `Release-As:` footer says otherwise. Tags are the bare version (`include-v-in-tag: false`,
  `include-component-in-tag: false`), matching the existing ones hatch-vcs reads; with the
  component, release-please looked for `lothc-0.0.19`, missed the real tag and pulled in commits
  already released. `release-type: python` skips `pyproject.toml`, whose
  version is `dynamic`, so only the manifest and changelog change.
- **`CHANGELOG.md` up to 0.0.19 is the old hand-written Keep a Changelog.** Don't add an
  `## [Unreleased]` section back: release-please inserts above the first version heading.
- **Publishing is dispatched, not triggered.** A release created with `GITHUB_TOKEN` doesn't start
  other workflows, so `release-please.yml` runs `gh workflow run release.yml --ref <tag>` (a
  `workflow_dispatch`, the documented exception). Dispatching rather than `workflow_call` keeps
  `release.yml` the top-level workflow PyPI's Trusted Publisher is registered against. Re-run a
  failed publish with the same command; `release.yml` refuses a non-tag ref.
- **`.github/workflows/release.yml`** runs the whole of `ci.yml` against the tag (`workflow_call`,
  `secrets: inherit` for Codecov), a build, an install of the built wheel with no extras plus an
  import (catches a module missing from the wheel), `uv publish` via Trusted Publishing (OIDC,
  `pypi` environment, no stored token), then the docs deploy to GitHub Pages. Codecov's
  `fail_ci_if_error: true` means a Codecov outage blocks a release. The release PR itself gets no
  CI run (a PR opened with `GITHUB_TOKEN` doesn't trigger `ci.yml`), which this covers.
- **Needs "Allow GitHub Actions to create and approve pull requests"** (Settings → Actions →
  General, enabled 2026-09-28) for release-please to open its PR.
- **CI's `test-no-extras` job** (`task test-no-extras`) runs every test module that never mentions
  pydantic/msgspec with both uninstalled (`--no-install-package`, then `uv run --no-sync`, since a
  plain `uv run` re-syncs them), because the main job always has both and would miss an unguarded
  import.
- **CI tests 3.13/3.14/3.15, plus free-threaded 3.14t/3.15t** (`test-free-threaded`: tests only,
  `PYTHON_GIL=0`, asserts the GIL really is off). lothc keeps process-wide state shared across
  threads (decoder cache, `raises=` cache, lazy `Response.headers`' first read) and users share
  one client across threads and loops, so the suite must pass with no GIL to lean on.
- **perf.py's comparison clients live in their own `perf` dependency group** (httpx, httpx2,
  aiohttp, aiosonic, niquests, requests), not `dev`: nothing in lothc/ or tests/ imports them, and
  niquests → urllib3-future → qh3 has no free-threaded 3.15 wheel. `default-groups` includes it,
  so local `uv run`/`task check` (whose basedpyright covers perf.py) still get it; the
  free-threaded jobs pass `--no-default-groups --group dev`.
- **Only pyreqwest `>=0.13.0` is supported**, so there's no lower-bound job (the suite also passed
  on 0.11.6-0.12.x).
- **README.md uses absolute URLs only.** It's the PyPI description, and PyPI can't resolve
  repo-relative paths: images → `raw.githubusercontent.com/.../main/`, docs →
  `rogerthomas.github.io/lothc/<page>/`, other files → `github.com/.../blob/main/`. A PyPI
  description is frozen per release, so a README fix only shows after the next one.
- **The sdist needs an explicit `include` allowlist** (`["/lothc", "/README.md", "/LICENSE"]`
  under `[tool.hatch.build.targets.sdist]`); the wheel doesn't. hatchling's sdist default is
  everything under version control, and 0.0.1's sdist shipped the whole repo (420KB vs 15.7KB).
  Not yanked: no functional impact.
- **TODO (manual, GitHub UI):** the `pypi` environment has no deployment protection rules, so
  anyone who can create a GitHub Release can publish. Add a tag-pattern restriction under
  Settings → Environments → pypi. Not done locally because the dispatched run's ref is the tag,
  not a branch, and there's no easy way to preview a policy change.

## Roadmap

Done: typed and raw query params/headers, per-verb `timeout=`, transport error wrapping
(`HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError`), `put`/`patch`/`delete`/`head`,
bodies on `delete`, SSE (`TypeAdapter`/`Decoder`, spec-compliant reconnect), `stream_get`/
`stream_post` (raw chunks by default, NDJSON decode via `into`), `download`, the
`into=` decode targets (`bytes` default, `dict`, any type expression via PEP 747 `TypeForm`
such as `list[Model]`, a pre-built `TypeAdapter`/`Decoder`), auth (`bearer_token` /
`bearer_auth` callable / `basic_auth` pair, at most one; per-verb `skip_auth=True`), OAuth 2
client credentials (`OAuthProvider`/`SyncOAuthProvider`), cookies (`cookie_store=True`), redirects
(`follow_redirects`/`max_redirects`), `proxy=`/`no_proxy=`, `user_agent=` (default
`python-lothc/<version>`), `http2=`, `resolve=`, `local_address=`, `tcp_keepalive=`, retries
(`max_retries`/`retry_methods`, idempotent verbs by default, via pyreqwest's `with_middleware`),
typed error bodies (`raises=`, `HTTPResponseError[E]`), TLS/mTLS/pool settings
(`connect_timeout`, `read_timeout`, `root_certificates`, `identity_pem`,
`min_tls_version`/`max_tls_version`, `danger_accept_invalid_certs`, `https_only`,
`max_connections`, `pool_idle_timeout`,
`pool_max_idle_per_host`, `pool_timeout`), request metadata (`.request: RequestInfo` on
`Response` and `HTTPResponseError`; `Response.http_version`/`.elapsed`), and a pytest mocking
plugin (`lothc[testing]`).

Not done yet: `sse_post` (SSE over POST, deferred by the user).

Maybe later, only if someone asks (they came from a code review, not a request): request/response
hooks, a generic `request()`/OPTIONS, streaming uploads, status/headers on the streaming verbs,
cookie-jar access.

Deliberately not exposed: per-algorithm compression toggles, `interface`, `tcp_nodelay`,
`referer`, CRLs. No final URL on `Response`: pyreqwest's response doesn't expose one.

## Architecture

- **`lothc/_client.py` holds everything**, deliberately one file until it earns a split.
  `lothc/__init__.py` re-exports the public surface. `lothc/_compat.py` isolates the optional
  imports (a `TYPE_CHECKING` block plus runtime `try/except ImportError` with stub fallbacks).
- **pyreqwest stays; a direct reqwest binding was prototyped and rejected (2026-10-04).** A
  one-call PyO3 binding (`send()` returns status, headers and the read body together, same
  pyo3/reqwest/tokio pins) gave async `get()` + msgspec +5% sequential / +12% at 64 concurrent
  (~8us of ~50us CPU/request), nearly all from the binding, not lothc's Python layer: that layer
  costs ~5.4us/request with the network patched out, ~3.9us of it irreducible. Not worth compiled
  wheels per platform plus reimplementing sync, streaming, multipart, TLS/pool settings, the error
  taxonomy and `lothc.testing`'s mocking. The same pass trimmed `_send_method` (0.35us).
- **`lothc/_oauth.py`** is separate not because `_client.py` earned a split but because it's built
  *on* the client: it opens a short-lived `HTTPClient` per token request and plugs into the
  existing `bearer_auth` slot as a plain callable. The clients know nothing about it.
- **The two clients are near-mirrors**: same methods, same overload shapes, one on pyreqwest's
  `Client`/`RequestBuilder`/`Response`, the other on `SyncClient`/`SyncRequestBuilder`/
  `SyncResponse`. Implement every feature on both; `tests/test_sync_async_parity.py` fails if you
  don't.
- **Both are opened as context managers**, and the pyreqwest client exists only while open, so it
  never appears in lothc's public API:

  ```python
  async with HTTPClient(base_url=..., bearer_token=..., timeout=30.0) as client:
      ...
  ```

- **Verbs**: `get`, `post`, `put`, `patch`, `delete`, `head`, `download`, `sse`, `stream_get`,
  `stream_post`. Each is a set of `@overload`s plus one real implementation. Everything except the
  three streaming verbs returns a `Response` (body on `.data`; for `download(dest=...)`, the
  written `Path`).

## Code style

Load the `write-python-code` skill (`.claude/skills/write-python-code/SKILL.md`) before writing
or editing Python here. The style guide below is load-bearing, especially in `lothc/_client.py`.
The Development notes further down are the design log for *why* the code looks the way it does.

@style-guide.md

## Testing

### Type-checking `tests/` under all four checkers

basedpyright strict is the project's own contract (`task typecheck`: zero errors, ideally zero new
`cast(...)`). `tests/` exercises the public API the way a consumer would, so it must also pass
mypy, ty and zuban: a heavily overloaded generic API can satisfy one checker's inference and trip
another's.

```
uv run mypy tests
uv run ty check tests
uv run zuban check tests
uv run basedpyright lothc tests
```

- **Fix, don't suppress, anything a real user of that checker would hit on the public API.** A
  scoped ignore for one checker is fine only for test-internal scaffolding no consumer could write
  (e.g. `tests/test_mocker.py`'s `pyright: ignore` on a direct call to pyreqwest's own
  `ClientMocker`, which lothc never exposes).
- **Look for a real fix even then.** The callable `bearer_auth` test providers in
  `tests/test_auth.py` looked like a zuban bug (it printed identical "expected"/"got" signatures
  yet rejected a `@dataclass` with `__call__`), but a plain class with an explicit `__init__`
  satisfied all four. Check whether a small, unrelated-looking change removes a disagreement
  before calling it a tool bug.

### Shared fixtures and helpers

- **`server_key` (`conftest.py`) for every per-key server state** (`/flaky`, `/retry-after*`,
  `/hits`, `/endless`, SSE reconnect counters, OAuth request logs): a fresh `uuid4` per test. The
  server lives for the session, so hand-picked keys collided silently when copy-pasted and leaked
  state into a re-run (a second `/retry-after` hit returns 200). A second key is a suffix
  (`f"{server_key}-api"`).
- **`token_server` (`tests/_oauth_server.py`) for OAuth tests.** `TokenEndpoint(...)` spells the
  `/oauth/token*` handler's query options as typed fields (checked with `is not None`, so
  `expires_in=0` is still sent). `TokenServer.provider()`/`.sync_provider()` build a provider on the
  test's key (`cached=True` uses a per-test cache dir); `.requests()`/`.grant_types()` read the
  endpoint's log. Unusual settings (`token_request=`, `client_factory=`) build their own provider
  on `token_server.url(...)`. Overrides are an `Unpack`ed `TypedDict`, so all four checkers type
  the result precisely (ty alone doesn't reject an unknown key).
- **`tests/_helpers.py`**: `echoed_headers(result)` turns `/echo-headers`' body into a lowercased
  name → value dict (last value wins; a duplicate-header test reads the raw list). Keep it free of
  pydantic/msgspec: `task test-no-extras` picks modules by grepping their own text, so an import
  through a helper would slip past it.
- **The `client`/`sync_client` fixtures over an inline default client**, unless the test is about
  the client's lifecycle (reading after close, re-entry).

### Test suite speed

~8.7s for ~850 tests; the growth since ~3.0s/385 tests is deliberate timing tests (stream idle
limits, retries) and per-verb/per-case parametrization, not overhead. The fixed floor (interpreter, plugins, `import lothc`,
collection) is ~0.45s, and a typical test costs ~2ms *including* a real HTTP round trip and its
own event loop, so sharing a client or a broader-scoped loop buys nothing and costs isolation.

What got it down from ~45s:

- **`serve_forever(poll_interval=0.01)`**, not the 0.5s default (`serve_in_background` in
  `tests/_server.py`, which every test server uses): `shutdown()` blocks until the loop next wakes,
  which charged up to 0.5s per server teardown, invisible in any test's own duration.
- **Scale a timing *pair* down together, never a lone margin.** The SSE total-timeout tests went
  from 0.6s stream vs 0.3s timeout to 0.15s vs 0.05s: a wider 3x margin at a quarter the cost.
- **`backoff_base=0.001`** on retry tests that assert *that* a retry happened. Non-zero so the real
  sleep path still runs.
- **`/slow` takes `seconds=`**, so a test that aborts early can ask for a long sleep it never waits
  out, instead of one constant sized for the strictest caller.
- **A negative control's dwell time is not the same number as a timeout.** In
  `test_sse_ctrl_c_interruptibility`, the `True` deadline is never reached on a green run (its size
  is free) but the `False` one is paid every run; splitting them (2.0 / 0.5) cut 2.63s to 0.60s,
  still ~20x over the slowest measured Ctrl-C exit.
- **Going below a default inverts what a floor proves.** Testing `backoff_base` at 0.02 (below the
  0.1 default) makes `elapsed >= x` useless, since an ignored parameter waits *longer* and still
  passes, so it asserts a bracket, `0.06 <= elapsed < 0.2`. Mutation-checked.

Deliberately still slow, because the wait is the assertion: `sse_ctrl_c_interruptibility[False]`
(~0.8s, including a 0.2s settle so the child is really blocked in `read_chunk()`; without it the
negative control flaked ~1 in 25 under load), the two SSE total-timeout tests (0.18s each), and
`test_import_lothc_succeeds_without_msgspec_or_pydantic` (0.12s, a real subprocess).

Rejected with measurements, don't re-litigate: **pytest-xdist** (`-n 4` saved ~0.8s for a new
dependency, non-deterministic ordering and a degraded `-x`, and worker startup is ~0.85s of floor,
so the payoff shrinks as the suite gets faster), **bypassing `uv run`** (~30ms), **lowering
`/events`' default interval** (load-bearing for
`test_sse_events_arrive_incrementally_not_buffered_until_stream_end`).

### Real TLS and sync/async parity

- **TLS settings run against a real HTTPS server** (`tests/_tls_server.py`, certs from `trustme`).
  `conftest.py` provides `tls_ca_pem`, `tls_client_identity_pem`, `https_base_url` and
  `mtls_base_url` (client cert required). The handshake runs on the handler thread, so a failed
  one never blocks `serve_forever`. Every negative control in `tests/test_tls.py` pins the rustls
  failure in `match=` (`UnknownIssuer`, `CertificateRequired`, `UnknownCA`, `ProtocolVersion`).
  Certs load via stdlib `load_cert_chain` from a trustme tempfile, not `configure_cert`, whose
  pyOpenSSL-typed signature is partially unknown to basedpyright. `max_connections`/
  `pool_timeout` have a real test (one slot, two concurrent `/slow`); `connect_timeout` can't be
  tested locally, since macOS completes the TCP connect even with a full backlog.
- **`tests/test_sync_async_parity.py` enforces the mirror.** Public API: same methods and
  constructors, every overload signature equal once async names map to sync ones (`_desync`).
  Implementation: every paired method body and module-level `foo`/`foo_sync` pair is compared via
  `ast`, after stripping `async`/`await` and the `Sync` markers. Intentional differences live in
  reasoned allowlists (sync-only `interruptible=`, the `_sync_stream_chunks` readers, OAuth's
  lock), and an entry that stops differing fails too.
- **Mutation runs use `python -B`.** Restoring a same-size mutation within the same second left
  Python running the mutated `.pyc` (its staleness check is size plus 1-second mtime).

### Doctests

Prefer doctests for small, self-contained algorithmic functions; they double as documentation.

## Manual example / smoke-testing

```
task example-server   # the stdlib test server on 127.0.0.1:8701 (separate terminal)
task example-run      # runs examples/run_http.py against it
```

- `examples/server.py` reuses the test suite's own stdlib handler (`tests/_server.py`), bound to a
  fixed port.
- `examples/run_http.py` doesn't yet show retries, cookies, redirects, proxy, `delete`/`head` or
  streaming; extend it when worthwhile.
- `task example-server` restarts on a real edit to either file via go-task's `watch: true` +
  `sources:` (a content checksum, so a no-op `touch` doesn't restart it). `main()` wraps
  `serve_forever()` in `contextlib.suppress(KeyboardInterrupt)` because that's how go-task stops
  the old process; otherwise every restart prints a traceback.

## Regression benchmarking (asv)

`asv` (`asv.conf.json` + `asv_bench/`) tracks lothc's **own** performance across git history.
`benchmarks/` (`task perf`) is different: it compares lothc against *other* clients at one point in
time. asv uses its own harness (`asv_runner`, `setup`/`teardown`/`setup_cache`), no pytest.

- `task asv-quick`: fast dev-loop check (no isolated env, no history saved).
- `task asv-continuous`: HEAD's parent vs HEAD (`-- <base> <branch>` for a specific pair). Skip
  `--quick`: single samples give false positives; the default `repeat=3` is reliable.
- `task asv-run -- <range>`: isolated env and fresh build per commit, e.g. `main~5..main` or
  `HEAD^!`. No args means the tip of each configured branch (`NEW` is "every new commit").
- `task asv-publish` then `task asv-preview`: the trend dashboard.

Suites: `asv_bench/bench_download.py` (`get()` vs `download()` in memory vs `dest=`, large body,
`time_*`/`peakmem_*`) and `asv_bench/bench_verbs.py` (`get()` across all four decode targets plus
`post()`, small bodies). `asv.conf.json`'s `matrix.req` installs pydantic/msgspec into every env,
since they're optional extras.

Gotchas when adding benchmarks:

- The large-body server in `setup()` must be a separate OS process (`subprocess.Popen`), never a
  thread: `peakmem_*` reads `RUSAGE_SELF.ru_maxrss`, which a server thread would contaminate.
- `environment_type: "uv"` needs an explicit `build_command` with `python -m build --wheel`: asv's
  default `pip wheel` also wheels the dependencies, and asv then can't tell which wheel is lothc.
- `setup_cache()`'s return value is pickled and may be loaded by a different process, so store
  only static data there and start live resources in `setup()`.
- `setup()` resources go on one `contextlib.ExitStack`, closed once in `teardown()`.

### Continuous benchmarking (CodSpeed)

`benchmarks/codspeed/` is a `pytest-codspeed` suite run by `.github/workflows/codspeed.yml` on
every push to `main` and every PR, in CodSpeed's CPU simulation mode. Every request goes through
`lothc_mocker`, so no socket is opened: it measures lothc's own request-building and decoding
overhead deterministically. It lives outside `tests/` (`testpaths`), so the normal suite never
runs it. `task bench-codspeed` runs it locally (plain pytest, each benchmark once); to measure
like CI does: `codspeed run --mode simulation -- uv run pytest benchmarks/codspeed --codspeed`.

### Instant no-git check: `task bench-check`

asv is keyed on commits and can't measure an uncommitted edit. `task bench-check`
(`asv_bench/quick_check.py` + `_quick_check_worker.py`) runs every `time_*` benchmark against the
working tree, each in its own subprocess so `peakmem` is per benchmark. Results are keyed by a
content hash of `lothc/`'s source in `asv_bench/.quick_check_history.json` (gitignored):

```
task bench-check                                # records current state, prints its hash
task bench-check -- --against <hash-from-above> # before/after table, prints the new hash
```

It reports the **min** of 50 time samples but `peakmem` from the *first* call only: repeating a
large-body operation in-process inflates peakmem through allocator fragmentation (~140MB became
~1195MB with no code change). Trust `peakmem` deltas; treat sub-millisecond `time` deltas as
directional only.

## Development notes

### Client lifecycle and configuration

- **`async with HTTPClient(...)`; the pyreqwest client is invisible.** The clients used to be
  `@dataclass`es opened via a `build()` classmethod, so their constructor took a pyreqwest
  `Client`. Now `__init__` takes only lothc settings and validates them (conflicting auth fails at
  construction), and entering builds the pyreqwest client, reachable only through a `_client`
  property that raises a clear error when closed. Not dataclasses (a deliberate style-guide §1
  deviation): the parameters are settings, not stored fields. A pyreqwest builder builds only
  once, so settings live in `_TransportSettings` and each entry rebuilds; that also makes a client
  re-enterable, so a module-level client works across separate `asyncio.run()` calls.
  `TlsVersion` is lothc's own `Literal`. `tests/test_public_api.py` walks every exported
  signature, overload and alias in `lothc` and `lothc.testing` for a pyreqwest type.
- **A bad `base_url` fails at construction.** pyreqwest rejects a query, a fragment, or a path not
  ending in `/`, but only on build (i.e. entry); `_is_joinable_base_url` repeats the rule in
  `_TransportSettings.__post_init__` with the same message. Joining is pyreqwest's (RFC 3986): a
  leading `/` drops the base's path prefix (`.../api/v2/` + `get("/users")` hits `/users`), and an
  absolute or protocol-relative (`//host/...`) path overrides the base, auth headers included.
- **`bearer_token`/`bearer_auth`/`basic_auth`, not `auth_token`/`auth`**: each maps to exactly one
  pyreqwest mechanism. Keep that precision if a fourth is added. `_apply_auth` applies whichever
  is set.
- **`backoff_base` is a constructor parameter** (default 0.1, delay `backoff_base * 2 ** attempt`,
  `Retry-After` still wins). It was a private field, so nobody could tune backoff.
- **`max_retry_after` caps `Retry-After` (default 60s).** A longer wait stops retrying and raises
  the 429/503, so the caller can schedule its own retry from `e.headers["Retry-After"]`
  (`Retry-After: 3600` used to sleep an hour inside `get()`). Stopping, not clamping, because an
  early retry against a server asking for a long wait is usually rejected again.
- **A 401 re-authenticates once when `bearer_auth` can `invalidate()`.** `_ReauthMiddleware`
  invalidates the rejected token on any 401 but retries only idempotent verbs (never a POST), and
  only once. The client knows nothing about OAuth: any provider implementing the
  `_InvalidatableAuth` protocol gets this. `invalidate(stale_access_token)` is a no-op if the
  provider has already moved on (so concurrent 401s can't discard each other's fresh token), and
  marks the token expired rather than dropping it, so renewal can use the refresh token.

### Requests and encoding

- **`data=` is urlencoded, `form=` is multipart**, matching requests/httpx so a ported call sends
  the same body. `multipart=`, `parts=`, `xform=`/`mform=` were rejected. "data" also names
  response bodies (`Response.data`, `SSEEvent.data`, the mocker's `data=`), accepted as the cost.
  `data=` takes `Params` and reuses its encoding (`_query_pairs(_encode_params(...))`) into
  pyreqwest's `.form()`, which takes the same pairs as `.query()`.
- **`Params`'s `list`/`tuple` value repeats the key** (`{"tag": ["a", "b"]}` → `?tag=a&tag=b`).
  Both spellings work, since `Params` has no other meaning for a list to protect. pyreqwest's
  `.query()` rejects a mapping with a list value (`BuilderError: unsupported value`) despite its
  stub allowing it, so `_query_pairs` flattens to pairs first.
- **A `Form` repeat tuple must be homogeneous.** `tuple[_FormValue, ...]` admitted
  `(b"...", "image/png")`, which reads as a file plus its content type but has no filename, and
  went out as a binary part *plus* a text part. `_FormRepeat` is a union of four homogeneous
  tuples (text, bytes, `File`, JSON), which all four checkers enforce, and `_check_form_repeat`
  enforces it at runtime. Only `Path` carries its own filename, so `tuple[Path, str]` is a `File`
  and `tuple[bytes, str]` can't be. `_form_value_kind` mirrors `_apply_form_value`'s branch
  order, not `_FormRepeat`'s member order, so the reported kind is the branch actually taken. An
  empty repeat tuple raises `ValueError`.
- **`case list()` must precede the `File` patterns in `_apply_form_value`/
  `_apply_sync_form_value`.** A `match` sequence pattern matches a `list` as well as a `tuple`, so
  `["a.png", b"..."]` was read as a file, though a list always means one JSON part. There's no
  tuple-only sequence pattern, so ordering is the fix.
- **pydantic models encode by alias**, like msgspec's `rename=`. pydantic serializes by field name
  by default, so a camelCase model went back to the API as snake_case. `_pydantic_by_alias` reads
  the model's own `serialize_by_alias` (default `True`), so lothc never contradicts
  `model_dump()`. Applied at every encode site: `json=`, `params=`, `headers=`, JSON form parts,
  and `lothc.testing` mock responses.
- **`RequestInfo` is captured before `.send()`**, since a sent request can't be read
  (`RuntimeError`). Never as one tuple expression: `built.send(), _request_info(built)` sends
  first. Same for `build_streamed()`'s `StreamRequest` in `_sse_stream`/`_line_stream`/
  `_download`.

### Responses and decoding

- **Every non-streaming verb returns a `Response`**, with no body-only mode. The verbs used to
  return the bare body, with `client.with_result.<verb>` as the escape hatch (modelled on the
  OpenAI/Anthropic SDKs). Reversed because lothc is a general-purpose client, not an SDK for a
  known API: typed SDKs return the body, while requests, httpx, aiohttp and axios return a
  response, and most callers need status/headers. A flag (`as_response=True`) was never an option:
  a return type that depends on a runtime `bool` multiplies overloads, and a non-literal `bool`
  matches none. The verbs share `_send_method`, kept one layer deep (each extra async layer is
  another frame every resume of the request's task passes through).
- **Overloads over one generic-with-cast signature.** Every verb has an `into: None = None`
  overload (`bytes`), a bare-`dict` one, an adapter one, a `TypeForm[T]` one (crossed with
  `response_headers_type` on the body verbs: 8 each, 4 on `sse`/`stream_*`), and one non-generic
  implementation. The adapter overload must precede the `TypeForm` one: ty otherwise matches a
  `TypeAdapter(...)` call against `TypeForm` and rejects it as a type expression. This is why
  there are almost no `cast()`s: the implementation never claims to return the type parameter. A
  single generic method with a value default forced `cast()` at every internal branch.
- **Bare `dict` gets its own overload, `type[dict[Any, Any]]` → `dict[str, Any]`.** `TypeForm[T]`
  resolves bare `dict` to `dict[Unknown, Unknown]` under basedpyright strict. `dict[Any, Any]`,
  not `dict[str, Any]`: with the latter zuban reveals bare `dict` as `Any`. The cost: every
  parametrized `dict[K, V]` also lands on it and types as `dict[str, Any]`, though it decodes
  (validated) via the library. Never claim something is "rejected" from a basedpyright result
  alone; check runtime behaviour.
- **`into` takes any type expression (`TypeForm`, from `typing_extensions>=4.13.0`)**, so
  `into=list[Item]` decodes a top-level array with no wrapper. `_decoder_for` (shared by
  `_decode_body` and the per-line/per-event `_decode_json_line`) maps a form to one decoder:
  `None`/`bytes` raw, `str` raw text, a JSON object (`dict`, `dict[str, Any]` or `typing.Dict[str,
  Any]`, `Mapping[str, Any]`) stdlib `json` (works with no extra), a bare `dict` subclass the same then `form(obj)`, a `TypeAdapter`/`Decoder` instance
  as-is (never cached, so one built per call can't grow the cache). Anything else is walked
  (`_form_traits`: generic args, unions, `Annotated` type and metadata, `type` aliases via
  `__value__`, TypedDict/dataclass/NamedTuple fields via `get_type_hints(include_extras=True)`,
  each alias and class once so recursive ones end): a `Struct` → msgspec `Decoder`, a pydantic
  marker → pydantic `TypeAdapter`, both → `TypeError`, neither → msgspec, else pydantic, else a
  `TypeError` naming the extras. Every verb resolves it before
  sending (`_send_method` too, then hands the decoder to `_decode_body`), since resolving it in
  `_decode_body` let a POST run and only then raise `TypeError`.
  - **A pydantic marker is a `BaseModel`, pydantic or `annotated_types` `Annotated` metadata, a
    pydantic dataclass, or anything with `__get_pydantic_core_schema__`/`__pydantic_validator__`**
    (`_is_pydantic_marker`, by metadata's package name plus those hooks). msgspec ignores all of
    them, so with both extras `list[Annotated[int, Field(gt=0)]]` used to return `[-5]`. Without
    pydantic, a marker is a `TypeError`, never an unvalidated decode. `Annotated[int, "doc"]` and
    `msgspec.Meta` don't count.
  - **A form msgspec can't decode goes to pydantic, or is a `TypeError` without it.** msgspec
    builds a `Decoder` for a "custom type" (`Path`, `deque[int]`, `IPv4Address`, a plain class)
    and then rejects every body, after the request was sent. `_has_msgspec_custom_type` walks
    `msgspec.inspect.type_info(form)` for a `CustomType` node, skipping `object` (a custom type to
    `inspect`, decoded as `Any`). A form with a `Struct` stays on msgspec: pydantic can't build one.
  - **A parametrized `dict` subclass (`OrderedDict[str, int]`, `Counter[str]`) is that same
    custom-type case**, so it's pydantic, which validates the parameters and builds the subclass.
    Without pydantic it's the `TypeError`, not the bare-subclass path, which would skip the
    parameters' validation; a bare `into=OrderedDict` still needs no extra.
  - **The JSON-object fast path matches the origin plus args, not `form == dict[str, Any]`**, which
    `typing.Dict[str, Any]` fails, so it needed an extra and took a different path.
    `Mapping[str, Any]` is included, since the `dict` it gets is one (`_is_json_object_form`).
  - **Container fields are walked because both libraries decode them field by field**: a
    TypedDict holding a `BaseModel` went to msgspec and always failed. Model classes' own fields
    aren't walked (their library's business).
  - **A bare `dict` subclass is stdlib `json` plus `form(obj)`** (`defaultdict` gets `form(None,
    obj)`, its first argument being `default_factory`), as before `into=` took type forms; msgspec
    rejects `OrderedDict` and friends. A TypedDict is a `dict` subclass at runtime, so it's
    excluded (`typing_extensions.is_typeddict`).
  - **`str` means raw text, mirroring `bytes`**: the body as UTF-8 (strict, so bad bytes are a
    `UnicodeDecodeError`, still a `ValueError`), SSE `data` as is, each streamed line, a pinned
    `HTTPResponseError[str]`'s body. It used to be JSON-parsed, so `sse(into=str)` failed on
    `data: hello`. A streamed `str` line is exactly what sat between `\n`s (a CRLF stream keeps its
    `\r`), blank lines skipped like NDJSON. NDJSON lines go to every decoder as the `bytearray`
    they were split into; the old per-line `.decode()` was a copy nothing needed.
  - **Cached in `_client.py`'s module-level `_decoders`, keyed by the form.** Measured: an
    uncached msgspec `Decoder(list[User])` ~1.5us and pydantic `TypeAdapter` ~31us per call,
    against ~100ns for the cached lookup (the old per-call `isinstance`/`issubclass` chain was
    ~150-270ns). Equal forms hash equal, so `list[User]` written inline on every call is one entry.
    An unhashable form (e.g. `Annotated[..., {}]`) still works, rebuilt per call. Streams resolve
    once per stream, before connecting.
  - **A form whose `Annotated` metadata compares by identity is never cached**
    (`identity_keyed`: a non-callable metadata object whose type keeps `object.__eq__`, or a
    dataclass holding one). pydantic's `FieldInfo` has no `__eq__`, so an inline
    `Annotated[int, Field(gt=0)]` is a new key per call: the cache grew without limit and every
    call rebuilt a TypeAdapter anyway. Rebuilt per call instead; metadata behind a `type` alias or
    inside a class doesn't count, since the alias/class is the (stable, identity-hashed) key, which
    is why the docs say to hoist `Field(...)`-style forms into an alias. Callables don't count
    either: `AfterValidator(check)` with a module-level `check` is equal across calls, and
    excluding it rebuilt the most common validator idiom (~31us) on every request.
  - **`_decoders_max_size` (4096) is the backstop** for a fresh-per-call key the rule misses (an
    inline lambda validator, a closure). Once full, new forms decode uncached and nothing is
    evicted, so the hot forms, cached first, stay cached.
  - `msgspec_installed`/`pydantic_installed` are `bool`s in `_compat.py`, not `msgspec is not None`,
    which strict basedpyright flags as unnecessary under `TYPE_CHECKING`.
  - `dict` on a non-object body raises `ValueError` (`_require_json_object`), keeping "every decode
    failure is a `ValueError`" true.
- **`into` defaults to `None`, raw `bytes`**, except `sse()` (`SSEEvent[str]`: a stream of
  named records has no raw-bytes analogue; explicit `into=bytes` gives `SSEEvent[bytes]`). On `stream_get`/`stream_post` it decodes each NDJSON line,
  not the whole body: a deliberate reuse of the name, called out in `docs/streaming.md`.
- **The type aliases (`Data`, `JSONPayload`, `Params`, `Headers`, `Form`, `File`) are named at the
  instance level.** `JSONPayload`, not `Json`, avoided a case-only clash with the old `JSON` class.
- **Decode-library validation errors propagate unwrapped** for `into`: the caller
  chose pydantic/msgspec, so its `ValidationError` is the expected one.
- **Typed error bodies are `raises=`, a subclass pinning `HTTPResponseError[E]`**, not a type
  argument (`error_type=` plus an untyped `.parsed_body` before). Python can't `except` a
  parametrized generic, so `E` must live on a real class for the `except` to type `.error`
  (`E | dict[str, Any] | None`); it's read back at runtime from the original bases
  (`_pinned_error_form`, which also binds a `Base[T]` chain), cached per class. An unbound
  parameter falls back to its PEP 696 default (`_bound_type_param`), since checkers read a bare
  `class Base[T = APIError](HTTPResponseError[T])` as `Base[APIError]`; it used to be a
  `TypeError`. A plain parameter,
  not a verb type parameter, so it adds no overloads. The constructor resolves it eagerly, so a bad
  class (unbound `E`, Struct/BaseModel mix) is a `TypeError` at `__init__`; a per-call one is
  resolved before sending (`_validate_raises`, one cached lookup, skipped for `None`), since
  resolving it only on a real 4xx/5xx let a bad class hide until production and then replace that
  status error with a `TypeError`. A `isinstance(x, HTTPResponseError)`
  narrows to `HTTPResponseError[Unknown]` under basedpyright (defaults aren't applied when
  narrowing), so tests bind `__cause__` to a local first.
- **Error decoding is best-effort, from already-fetched bytes.** `_check_status` has already read
  the body for `body_start`, and a body can't be read twice, so `_status_error` decodes bytes with
  `_decoder_for(E)`. A `ValueError` (malformed JSON, pydantic's and msgspec's validation errors)
  goes on `.parse_error` and `.error` falls back to a JSON object, else `None`; it used to
  propagate, so a proxy's HTML 502 against a pydantic error model surfaced as `ValidationError`,
  losing the status. An unusable `E` (a caller bug) still raises its `TypeError`, but at
  resolution, so a `RecursionError` (a deeply nested body) or `TypeError` (a pinned `dict`
  subclass's constructor) from the decode is the body's fault too: caught alongside `ValueError`
  and wrapped in one (`__cause__` is the original), keeping `.parse_error: ValueError | None`
  and "every decode failure is a `ValueError`". The default fallback catches `RecursionError` too.
- **`stream_get`/`stream_post` with `into=str` yield raw text lines, not NDJSON**: every line,
  blank ones as `""`, one trailing `\r` stripped. NDJSON targets still skip blank lines.
- **`HTTPResponseError` pickles as `type(self)`, carrying `.error`**, so a `raises=` subclass
  survives a process boundary (module-level classes only, as pickle finds them by name).
  `.parse_error` isn't pickled (pydantic's `ValidationError` can't be).
- **`Response.headers`/`MockRequest.headers` are a `CaseInsensitiveDict`**, a `MutableMapping`, not
  a `dict` subclass. `dict(raw_response.headers)` made `["Content-Type"]` raise and dropped
  repeated values. A `dict` subclass would silently revert to exact-match keys under
  `{**headers}`/`dict(headers)`; none of requests, niquests, httpx or httpx2 subclass `dict`
  either.
  - Iteration keeps the stored casing (requests/niquests), though reqwest lowercases incoming
    headers, so that only shows for headers lothc sets itself.
  - `__init__` appends pairs in arrival order, so every value of a repeated header stays reachable
    via `get_all` and `__getitem__` returns the first. `get_all`, not httpx's `get_list`, after
    stdlib `email.message.Message.get_all` (`http.client` responses *are* a `Message`).
  - `__eq__` compares every value against another `CaseInsensitiveDict` but only the single-valued
    view against other mappings, so `h == dict(h)` holds with two cookies. That makes equality
    non-transitive with repeated headers, as in requests; documented, not changed.
  - `__repr__` switches to the pair-list form once a name repeats.
  - The one `cast` in `__init__` covers a basedpyright artifact (it synthesizes an impossible
    `Mapping[tuple[str, str], Unknown]`).
  - **Built on first read, not per response** (`_LazyHeaders`). Eager conversion was ~2.7us of
    every request and cost ~15% throughput in `task perf` (46.9k → 39.7k req/s), and most
    callers never read `.headers`. Lazy costs 0.17us unread, 2.57us read once. `task perf` (100k
    requests, concurrency 100, 6 interleaved runs a side, medians): `lothc` 40.1k → 44.5k req/s,
    `lothc-msgspec` 49.8k → 52.1k, pyreqwest control 53.9k → 55.3k.
    - The subclass holds pyreqwest's `HeaderMap` and leaves `_store` unset, so the first access
      of any kind lands in `__getattr__`, which builds it and swaps `__class__` to plain
      `CaseInsensitiveDict`: `type()`, `repr`, pickling, identity and mutation are unchanged,
      and so is `Response`'s constructor. `__reduce_ex__` materializes first, since pickle/copy
      look it up on the class.
    - It holds the `HeaderMap`, not the response, because a read response caches its body
      (holding 40 10MB responses: ~470MB RSS; holding 150 maps: ~100MB vs ~85MB holding none).
    - `_check_status`'s error headers stay eager: off the hot path.
  - **`HeaderMap` gets its own `__init__` path** (`_store_from_header_map`): its names are
    always lowercase, so the per-header `.lower()` and the `_header_pairs` dispatch are skipped,
    2.48us → 1.98us for 13 headers (1.23us of that is `.items()` itself).
- **`.to_bytes()`, never `bytes(x.bytes())`**, to turn a `pyreqwest.bytes.Bytes` into `bytes`. And
  don't convert when the target accepts a buffer: `msgspec.json.decode()` does, so the `Struct`
  branch decodes the raw bytes directly; `pydantic.model_validate_json()` needs real `bytes`, so
  that branch calls `.to_bytes()` and still parses JSON directly, never via a dict.
- **Dropped: `TypedDict`/`typeguard` support.** Decode-only, ~3.5x slower than the compiled
  pydantic/msgspec validators, and no capability they lack. The design is in git history.

### Errors

- **Never leak pyreqwest's exception types.** `TransportError`/`RequestTimeoutError`/
  `NetworkError` are translated at every `.send()` and both SSE loops; a new one belongs in
  `_translate_transport_error`, not at a call site. `RedirectError` (past `max_redirects`) and
  `BuilderError` (from `.build()`/`.build_streamed()`) aren't `TransportError` subclasses but are
  caught alongside it and mapped to plain `HTTPTransportError`. `_send`/`_send_sync` take the
  unbuilt builder and call `.build()` inside their own `try`.
- **Status errors are separate from transport errors.** `HTTPResponseError` (a 4xx/5xx with a
  `body_start` snippet) and `HTTPTransportError` (no response at all) are different failure
  classes; don't unify them.
- **A `BuilderError` is permanent, so it's never retried or reconnected.** It's raised before
  anything reaches the network (a rejected scheme under `https_only`, a malformed URL), yet `sse()`
  used to spend its whole reconnect budget (15s) on one. `_is_permanent_transport_error` reads it
  off `__cause__`, so the public type stays `HTTPTransportError` (no new subclass). `RedirectError`
  is excluded: a redirect loop can be transient. The retry middleware only catches
  `PyreqwestTransportError`, and `.build()` runs outside `next.run`, so it never sees one.
- **A body cut off partway is retried; a corrupt one isn't.** pyreqwest reads a non-streamed body
  inside `send()`, so body errors surface inside the retry middleware. A truncated chunked body is
  already a `ReadError`; one shorter than its `Content-Length` is a `BodyDecodeError`, the same type
  as corrupt gzip. `_is_retryable_send_error` retries a decode error only when a cause is hyper's
  "error reading a body from connection" (truncation always has it, corrupt gzip/br/deflate/zstd
  never do). When testing: a "corrupt" gzip body under gzip's 10-byte header comes back as a
  `ReadError`.

### Streaming, SSE and downloads

- **For `stream_get`/`stream_post`/`download`, the client's `timeout` is a gap limit, not a total
  cap.** A total cap (pyreqwest's only per-request timeout) killed every healthy long stream at
  30s. These verbs swap it for `_streaming_default_timeout` (a year) and lothc enforces
  `_stream_idle_timeout`: the client `timeout`, unless `read_timeout` is set (pyreqwest then
  enforces the gap at the socket). An explicit per-call `timeout=` is still a total cap. `sse` has
  no gap limit, since quiet periods are legitimate there.
  - Async wraps opening the stream plus the status check, and each chunk read, in
    `asyncio.timeout` (`_within_idle_limit`/`_read_chunk_within`), never across a `yield`, so a
    slow consumer is never mistaken for a stalled server.
  - Sync can only stop waiting by reading on a worker thread (`_interruptible_chunk_iter`, the
    default whenever a limit applies). No measured throughput cost, but on a real stall the
    blocked thread and socket stay parked until the peer closes (a blocked sync read can't be
    cancelled; accepted).
  - Its queue is bounded (64 chunks) with an abandonment event: unbounded buffered the whole body
    for a slow consumer, and a blocking `put` parked an early `break`'s worker forever.
  - The sync clock restarts when the caller asks for the next chunk and polls at `idle/4`; at one
    poll per window, a clock left running across a consumer's pause was indistinguishable from a
    correct one (caught by mutation testing).
- **`streamed_read_buffer_limit(1)` at every streaming call site, `download` included.**
  pyreqwest's 64KB default withholds bytes until the limit or EOF, so a trickling SSE/NDJSON
  payload never arrived until close, and a slow download (1KB/s) looked silent for over a minute
  to the idle limit. It doesn't mean byte-at-a-time reads (the socket governs chunking); measured
  no cost (100MB over loopback in 79ms vs 80ms).
- **`sse()` never lets the client's total `timeout` touch the stream.** An SSE body never
  finishes, so `_sse_stream` passes a one-year default unless the caller gives `timeout=` (which
  then bounds one connection attempt). `read_timeout` is the real stall detector and surfaces as
  `HTTPTimeoutError`.
- **`sse()` reconnects per the WHATWG EventSource model**, in `_sse_stream` itself (the retry
  middleware wraps `send()`, which returns once headers arrive). On a transport error it reconnects
  with `Last-Event-ID` until `max_reconnects` *consecutive* reconnects yield no event (reset on
  every event; `None` unlimited, `0` fail fast). A clean close returns unless
  `reconnect_on_close=True`; a 204 always returns. `id:` persists until an explicit empty `id:`;
  `retry:` overrides the delay mid-stream; all three line terminators work across chunk splits; a
  leading BOM is stripped once per connection; a non-204 2xx with the wrong Content-Type raises
  `ValueError`.
- **`SSEEvent[TData]` has `id: str = ""` and no id options.** `""` is what the spec's last-event-id
  buffer and a browser's `lastEventId` use; an empty buffer sends no `Last-Event-ID`. It replaced
  `id_type=` plus `allow_missing_id=`, which cost twelve overloads per client, a second type
  parameter and a hand-written covariant class (a dataclass's `__replace__` makes it invariant).
  Convert with `int(event.id)`. Not `frozen=True`, since `.data` can be a `dict`.
  `into` applies to `.data` only; `sse()` always yields a full `SSEEvent`.
- **`sse`/`stream_get`/`stream_post` are typed `Generator`/`AsyncGenerator`, not `Iterator`**,
  so callers can `close()`/`aclose()` after stopping early; an async generator abandoned by
  `break` otherwise stays open until the loop's finalizer runs. `tests/test_stream_close.py` checks
  both the typing and that the server sees the hang-up.
- **`download(path, dest=None)` exists because `get()`'s `bytes` path holds ~3 copies of a body at
  peak** (pyreqwest's buffering plus lothc's own copy). `download()` streams into a `bytearray`
  (`+=`, which takes pyreqwest's buffer chunks without a per-chunk copy): ~two-thirds of `get()`'s
  peak (105MB vs 154MB on a 50MB body; the final `bytes(buffer)` is the second copy). With `dest`
  it streams to a file at O(chunk size) via `_atomic_download_file` (hidden sibling + rename, so a
  failure never leaves a truncated file or clobbers an existing one). `get()` is unchanged: for
  the small bodies lothc targets, the copies are free.
- **`download()` returns `Response[bytes]`, or `Response[Path]` with `dest=`.** It returned the bare
  body/`None` after the other verbs moved to `Response`, for no reason but history. On the sync
  client the raw response never leaves `_sync_stream_chunks` (on a worker thread when an idle limit
  applies), so the status line and headers come back through a `head_sink` list, appended before
  any chunk is queued so the queue hand-off orders it first. No `response_headers_type=` yet
  (`typed_headers` is always `None`).

### Optional extras and typing

- **The `_compat.py` fallback stubs must be subscriptable** (a trivial `__class_getitem__`), or
  `import lothc` crashes without an extra: `_client.py`'s `Decoder[Any]`/`TypeAdapter[Any]`
  annotations are evaluated at import.
- **Public type aliases reference structural `Protocol`s, not the real classes.** With an extra
  absent, strict checkers showed `Unknown` on unrelated public overloads, even for consumers who
  never touch that library, and a `try/except` around the `TYPE_CHECKING` import doesn't help
  (pyright ignores the `except` once `TYPE_CHECKING` is `True`). So `Data`/`Params`/`Headers`/
  `JSONPayload`/`into` use `StructTyping`/`BaseModelTyping`/etc. (`_compat.py`),
  which import nothing; real subclasses satisfy them structurally. Runtime dispatch still uses the
  real classes. basedpyright can't exclude a nominal class from a `match` fallback once a union's
  members are Protocols, so affected sites have an explicit narrowing guard, not a suppression.
- **On Python 3.13 annotations are evaluated eagerly** (no PEP 649 until 3.14), so a class used in
  an annotation must be defined above it. Move the definition up rather than quoting the
  annotation.

### OAuth (`lothc/_oauth.py`)

- **Token requests use `data=`** (urlencoded, per the RFC). The Basic header is built by lothc
  (`quote(..., safe="")` per half, per RFC 6749 §2.3.1, not `quote_plus`), not via the internal
  client's `basic_auth=`. `client_auth` is ignored on the model path (the model carries
  credentials).
- **Aliased pydantic `token_request=`/`token_response=` models need
  `ConfigDict(validate_by_name=True)`** so lothc can construct them by field name; msgspec
  `field(name=...)` needs nothing. `TokenRequestTyping`/`TokenRefreshRequestTyping` are
  `Callable[..., JSONPayload]`, not exact-signature `Protocol`s, which reject an aliased model on 3
  of 4 checkers. `TokenResponseTyping` needs only `.access_token`/`.expires_in`; `.refresh_token`
  is read with `getattr(..., None)` since RFC 6749 §5.1 makes it optional.
- **The providers are plain classes with a keyword-only `__init__`, not dataclasses**: zuban alone
  rejects a dataclass instance as `bearer_auth=`.
- **Expiry and refresh:** `_CachedToken.expires_at` is wall-clock (`time.time()`, read back by
  later processes), stored as ISO 8601; a naive timestamp on read is rejected. `refresh_leeway` is
  clamped to `expires_in / 2` (the 300s default against a 60s token was expired at birth).
  `default_expires_in` covers servers that omit `expires_in`; neither is a `ValueError`. A refresh
  response without `refresh_token` keeps the old one (RFC 6749 §6). Refresh falls back to minting
  only on a 400 (RFC 6749's `invalid_grant` for a revoked refresh token) or a 401, which some
  servers send instead and which, with the disk cache, would otherwise wedge every process.
- **`OAuthTokenError`** (`.token_url`, original as `__cause__`) wraps everything `_renew` raises,
  because `bearer_auth` runs inside the caller's own verb call and an unwrapped error would be
  misattributed.
- **The `asyncio.Lock` is created lazily per running loop**, since a loop-bound lock breaks a
  module-level provider reused across `asyncio.run()` calls.
- **`client_factory`** (default `HTTPClient`, called with no args) replaced a `timeout` parameter;
  `functools.partial(HTTPClient, ...)` covers timeout/proxy/TLS for the token endpoint.
- **No public override hooks.** The private `_mint`/`_refresh` split is the seam if ever needed
  (style-guide §10 forbids `__call__` routing through public methods).
- **Token cache:** `token_cache_dir` must exist at construction. Each file is
  `<host>-<sanitised client_id>-<sha256(token_url, client_id, scope)[:12]>.json`, because one
  client ID can serve several token URLs or scopes and may contain unsafe characters; an in-file
  `token_url`/`client_id`/`scope` guard is a second check. Written via `tempfile.mkstemp` (`0600`)
  + `Path.replace`, plain sync IO. Not encrypted: stdlib has no cipher, a key derived from the
  client secret protects little (the secret can mint tokens itself), and AWS CLI and gcloud also
  use `0600` plaintext.
- **`invalidate()` marks the cache file's token expired too**, if the file still holds the
  rejected token. Only memory was cleared, so after a revocation every new process reloaded the
  dead token, and a POST's 401 is never retried. Marked expired rather than deleted (as in
  memory), so the next process can still use the refresh token. A file holding another token
  (already renewed elsewhere) is left alone; an unusable one is ignored; a failed write warns.
  The sync provider takes its lock so a concurrent renewal can't land between the read and the
  rewrite; the async one has no `await` in `invalidate`, so it's already atomic.

### `lothc.testing`

- **No pyreqwest type in its public API and no builder pattern.** A handler returns a
  `MockResponse` (`@dataclass(slots=True)`), turned into a real pyreqwest response by
  `_apply_mock_response`. Handlers, predicates and `get_requests()` see a `MockRequest` (`slots`,
  not `frozen`, since a `dict` field can't be truly frozen), built by `_mock_request_from`, the
  only code touching pyreqwest's `Request`. `url=` matching takes `str | re.Pattern[str]`.
  `MockRequest.headers` is a `CaseInsensitiveDict`; `MockRequest.query` (parsed, one value per key)
  sits beside the raw `query_string`.
- **Matching shares the real request path's encoding.** `params=`/`headers=` use
  `_encode_params`/`_encode_headers`; `data=` uses `_encode_json_payload`, byte-identical to
  `_attach_json_body`. `params=` is a **subset** match whose string comes from pyreqwest's own
  query encoder, so an invalid value like `None` raises as a real request would. It narrows with one
  `mock.match_query(dict)` (pyreqwest's `query_dict_multi_value` already gives the per-key
  `str | list[str]` shape). An empty `list`/`tuple` raises `ValueError`, since
  `Url.parse_with_params` silently drops an empty-valued key, which would match any query.
  `_query_param_match_values` imports `_client.py`'s `_QueryValue` so the two can't drift.
- **`LOTHCMocker._add_response` validates and encodes before registering**: pyreqwest registers a
  `Mock` as soon as the verb method returns, so raising afterwards would leave an unconfigured
  mock matching every later request.
- **`data=` is applied before `headers=`**: `.body_json()` *appends* a Content-Type while
  `.headers()` replaces same-key values, so the other order sends two. `LOTHCMock.with_data`
  re-applies already-set headers, so the chainable API works in either order.
- **`lothc_mocker` is strict by default** (pyreqwest's plugin passes through). Opt out with
  `@pytest.mark.lothc_mocker(strict=False)` or `.strict(enabled=False)`.
- **Async detection checks `__call__` too** (`_is_async_callable`):
  `inspect.iscoroutinefunction` alone treats an object with an `async def __call__` as sync.
- **Deliberately not collapsed:** `_wrap_custom_handler`/`_wrap_custom_matcher` (different return
  types and post-processing) and the six `add_*_response` methods (matching `_client.py`'s
  explicit-per-verb convention).
