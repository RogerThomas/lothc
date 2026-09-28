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
`stream_post` (raw chunks by default, NDJSON decode via `response_data_type`), `download`, the
`Data` decode targets (`bytes` default, `dict`, `BaseModel`, `Struct`), auth (`bearer_token` /
`bearer_auth` callable / `basic_auth` pair, at most one; per-verb `skip_auth=True`), OAuth 2
client credentials (`OAuthProvider`/`SyncOAuthProvider`), cookies (`cookie_store=True`), redirects
(`follow_redirects`/`max_redirects`), `proxy=`/`no_proxy=`, `user_agent=` (default
`python-lothc/<version>`), `http2=`, `resolve=`, `local_address=`, `tcp_keepalive=`, retries
(`max_retries`/`retry_methods`, idempotent verbs by default, via pyreqwest's `with_middleware`),
typed error bodies (`error_type=`), TLS/mTLS/pool settings (`connect_timeout`, `read_timeout`,
`root_certificates`, `identity_pem`, `min_tls_version`/`max_tls_version`,
`danger_accept_invalid_certs`, `https_only`, `max_connections`, `pool_idle_timeout`,
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
  (e.g. the `builtins.__import__` monkeypatch in `tests/test_compat_fallbacks.py`).
- **Look for a real fix even then.** That monkeypatch needed `__import__`'s exact signature instead
  of `*args, **kwargs` before three checkers agreed; only ty still objected, printing two
  identical signatures as incompatible (a genuine ty bug, hence one `# ty: ignore[...]`). The
  callable `bearer_auth` test providers in `tests/test_auth.py` looked like the same zuban-only
  bug, but dropping `@dataclass` for a plain class with an explicit `__init__` satisfied all four.
  Check whether a small, unrelated-looking change removes a disagreement before calling it a tool
  bug.

### Test suite speed

~7.6s for ~650 tests; the growth since ~3.0s/385 tests is deliberate timing tests (stream idle
limits, retries), not overhead. The fixed floor (interpreter, plugins, `import lothc`,
collection) is ~0.45s, and a typical test costs ~2ms *including* a real HTTP round trip and its
own event loop, so sharing a client or a broader-scoped loop buys nothing and costs isolation.

What got it down from ~45s:

- **`serve_forever(poll_interval=0.01)`**, not the 0.5s default (`tests/conftest.py`,
  `tests/test_sse_interrupt.py`): `shutdown()` blocks until the loop next wakes, which charged up
  to 0.5s per server teardown, invisible in any test's own duration.
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
  matches none. The verbs share `_send_method` → `_send_with_body`.
- **Overloads over one generic-with-cast signature.** Every verb has a no-`response_data_type`
  overload (`bytes`), a bare-`dict` one, a generic one, an adapter one, and one non-generic
  implementation, which is why there are almost no `cast()`s: the implementation never claims to
  return the type parameter. A single generic method with a value default forced `cast()` at every
  internal branch.
- **Bare `dict` is a valid `response_data_type`; `dict[str, Any]` is not.** A generic
  `type[TData]` overload resolves bare `dict` to `dict[Unknown, Unknown]` under basedpyright strict,
  hence the extra non-generic overload hardcoding `type[dict[str, Any]]` (which replaced a
  dedicated `JSON` class). Never claim something is "rejected" from a basedpyright result alone;
  check runtime behaviour.
- **`TypeAdapter`/`Decoder` are valid `response_data_type`s on every verb**: the way to decode a
  top-level JSON array (`TypeAdapter(list[Item])`). The plain-verb implementations return `object`,
  since an overload returning an unbounded `TAdapted` is otherwise inconsistent with them.
  `_decode_body` handles adapters before its "must be a class" check. `dict` on a non-object body
  raises `ValueError` (`_require_json_object`), keeping "every decode failure is a `ValueError`"
  true.
- **`response_data_type` defaults to `bytes`**, except `sse()` (`SSEEvent[str]`: a stream of named
  records has no raw-bytes analogue). On `stream_get`/`stream_post` it decodes each NDJSON line,
  not the whole body: a deliberate reuse of the name, called out in `docs/streaming.md`.
- **The type aliases (`Data`, `JSONPayload`, `Params`, `Headers`, `Form`, `File`) are named at the
  instance level.** `JSONPayload`, not `Json`, avoided a case-only clash with the old `JSON` class.
- **Decode-library validation errors propagate unwrapped** for `response_data_type`: the caller
  chose pydantic/msgspec, so its `ValidationError` is the expected one.
- **`error_type` is best-effort, and decodes from already-fetched bytes.** `_check_status` has
  already read the body for `body_start`, and a body can't be read twice, so `_decode_error_body`
  mirrors `_decode_body`'s branch order on bytes. A body that doesn't match gives
  `parsed_body=None` plus `.parse_error` and still raises `HTTPResponseError`; it used to
  propagate, so a proxy's HTML 502 against `error_type=ErrorModel` surfaced as pydantic's
  `ValidationError`, losing the status. An unusable `error_type` (a caller bug) still raises its
  `TypeError`. `.parse_error` isn't pickled (pydantic's `ValidationError` can't be).
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
  `response_data_type` applies to `.data` only; `sse()` always yields a full `SSEEvent`.
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
  `JSONPayload`/`response_data_type` use `StructTyping`/`BaseModelTyping`/etc. (`_compat.py`),
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
  only on a 400.
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
