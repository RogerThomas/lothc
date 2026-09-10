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

A git repo exists, pushed to `https://github.com/RogerThomas/lothc` (`main`, tracked as `origin`).
A real test suite exists under `tests/` (see Testing below) and a benchmark suite under
`benchmarks/` — this file is no longer the only thing that survives between sessions, but it's
still the source of truth on decisions and remaining work, so keep it up to date.

Versioning is **SemVer** (e.g. `1.0.0`, no "v" prefix), derived from the git tag via `hatch-vcs`
(`dynamic = ["version"]` in `pyproject.toml`, already wired up) — a release is just a pushed tag,
nothing to bump by hand. SemVer rather than CalVer (unlike the `yeetr` CLI project) because this is
a library other packages depend on: major/minor/patch is the signal pip's resolver, Dependabot,
etc. rely on to know whether an upgrade is safe, which a date can't communicate.

### Releasing

`task release` (defaults to a patch bump; `task release -- minor`/`task release -- major` for the
others). Since the version comes entirely from the git tag (hatch-vcs), this task has nothing to
bump in `pyproject.toml` or commit — it just: checks the tree is clean and `main` is in sync with
`origin/main`, computes the next tag from the latest existing one (`0.0.0` baseline if none exist
yet — the project's real first release is `0.0.1`), tags it, pushes the tag, then
`gh release create` to publish a GitHub Release from it.

That GitHub Release is the trigger — `.github/workflows/release.yml` runs on `release: published`
and does the actual work: builds the wheel/sdist and `uv publish`s to PyPI using **Trusted
Publishing (OIDC)** — no PyPI API token stored anywhere — via a `pypi` GitHub Environment (must be
registered as the trusted publisher in the PyPI project's own settings, pointing at this repo +
`release.yml` + environment name `pypi`), then deploys the docs to GitHub Pages via a
`github-pages` environment (Pages is enabled with `build_type: workflow`, i.e. Actions-deployed,
not the legacy branch-based build).

This deliberately does **not** mirror the `yeetr`/`jero`-style release task line for line: those
projects keep a static `version` in `pyproject.toml` (bumped via `uv version --bump`, committed,
then tagged), which makes sense for their own non-hatch-vcs versioning — but for lothc that would
reintroduce exactly the "remember to bump and commit before tagging" ceremony hatch-vcs exists to
remove, and add a version-drift failure mode (tag doesn't match the committed version) that simply
can't happen here since the tag *is* the version. Copying that pattern here would've been strictly
worse, not just different.

**Follow-up not yet done**: the `pypi` environment currently has no deployment protection rules
(anyone who can create a GitHub Release can trigger a publish) — consider adding a tag-pattern
restriction under Settings → Environments → pypi → "Deployment branches and tags" as defense in
depth on top of the trusted-publisher config. Left for manual setup in the GitHub UI rather than
scripted via the API, since this workflow triggers on `release: published` (ref context is the
*tag*, not a branch) and a misconfigured policy risks silently blocking legitimate releases with
no easy local way to preview it first.

**`[tool.hatch.build.targets.sdist]` needs an explicit `include` allowlist — the wheel doesn't.**
hatchling's default sdist behavior is "everything under version control," not "everything the
wheel needs." Caught this live in the real `0.0.1` release: the published sdist on PyPI was
420KB and contained the *entire* repo — `perf.py`, `benchmarks/` (Docker + Rust source), `asv_bench/`,
all of `tests/`, all of `docs/`, `CLAUDE.md`/`AGENTS.md`/`style-guide.md`, `.github/workflows/`,
`uv.lock`, none of which an installer needs. The wheel was already fine (verified separately —
`packages = ["lothc"]` under `[tool.hatch.build.targets.wheel]` already scopes it to just the
package), only the sdist was the problem. Fixed with `include = ["/lothc", "/README.md",
"/LICENSE"]` under the sdist target — rebuilt and verified live: 15.7KB, just `lothc/`, `LICENSE`,
`README.md`, plus `PKG-INFO`/`pyproject.toml` (hatchling always keeps those, needed to rebuild
from the sdist alone) and `.gitignore` (a harmless hatchling default it retains regardless of
`include`). `0.0.1` itself was left alone rather than yanked/deleted — the bloat has no functional
or security impact on anyone who installs it, so it didn't meet the bar for that; `0.0.2`+ ships
the fixed sdist.

### Roadmap — what's done, what's next

Done: query params (typed + raw), per-request headers (typed + raw), timeouts (client-level, plus
a per-verb `timeout=` override on every verb on both clients), transport error
wrapping (`HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError`), `put`/`patch`/`delete`/`head`, SSE (with
`TypeAdapter`/`Decoder` support, spec-compliant reconnect — see the SSE dev note below), `stream_get`/`stream_post` (raw chunks by default — unbuffered, safe
for arbitrary binary content; pass `response_data_type` to switch to newline-buffered NDJSON-style typed
decoding instead — the buffering is conditional on that param, not always-on), `download` (see the
large-object note below), the `Data` decode-target
system (`bytes` default, plain `dict`, pydantic `BaseModel`, msgspec `Struct`), auth (static
`bearer_token`, a per-request-refreshed `bearer_auth` callable, or a `basic_auth`
`(username, password)` pair — provide at most one; plus a per-verb `skip_auth=True` override to
omit auth for one call on both clients), cookie/session
support (`cookie_store=True`), redirect control (`follow_redirects`/`max_redirects`), proxy config
(`proxy=`), retries (`max_retries`/`retry_methods`, implemented as a real pyreqwest
`with_middleware` hook — backoff + `Retry-After` honored, defaults to idempotent verbs
`get`/`put`/`delete`/`head`, `post`/`patch` require explicit opt-in via `retry_methods`), typed
error bodies (a per-verb `error_type=SomeModel` param, mirroring `response_data_type`, that
decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it `None`), and
TLS/mTLS/connection-pool config (`connect_timeout`, `root_certificates`, `identity_pem`,
`min_tls_version`/`max_tls_version`, `danger_accept_invalid_certs`, `https_only`,
`max_connections`, `pool_idle_timeout`, `pool_max_idle_per_host`, `pool_timeout`, `read_timeout` — all
client-level only, same as redirects/proxy/cookies above), and request metadata
(`.request: RequestInfo` — method/url/path/host of the request actually sent) on both `Result`
(`get_result`/`post_result`/`put_result`/`patch_result`/`delete_result`/`head`) and on
`HTTPResponseError` itself (every verb, including `sse`/`stream_get`/`stream_post`/`download` —
knowing which request failed matters more than which one succeeded, especially with several in
flight concurrently), and OAuth 2 client credentials (`OAuthProvider`/`SyncOAuthProvider` in
`lothc/_oauth.py`, a ready-made `bearer_auth` provider: RFC 6749 form-encoded mint/refresh with
`client_auth="basic"|"body"` + `scope`, or a non-RFC JSON endpoint described by the user's own
`token_request`/`token_refresh_request`/`token_response` pydantic/msgspec classes; leeway-based
renewal (`refresh_leeway`, default 300s, clamped to half the token's lifetime;
`default_expires_in` for servers that omit `expires_in`), refresh-then-fallback-to-mint on a 400
only (anything else is an `OAuthTokenError`), one renewal under concurrency via a lock, a
`client_factory` (default `HTTPClient.build`) for the token endpoint's own client config, and an
optional `token_cache_path` — atomic `0600` JSON, keyed on `token_url` + `client_id` + `scope` —
see the OAuth dev note below), and a pytest mocking plugin (`lothc[testing]`, `lothc/testing.py` —
the `lothc_mocker` fixture, a thin adapter over pyreqwest's own `client_mocker` plugin; see the
"Never expose pyreqwest internals" dev note below for its one deliberate design rule).

Not done yet: nothing outstanding right now — see git history/this file's own dev-notes below for
what's landed and why. (`lothc.testing`'s `Request`/`Url` re-export was flagged here as a known
partial exception to "never expose pyreqwest internals" in an earlier pass; fully resolved since —
see that dev note below. No pyreqwest type appears in `lothc.testing`'s public API anymore.)

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

`asv` (airspeed velocity, `asv.conf.json` + `asv_bench/`) tracks lothc's **own** performance across
its git history — a different job from `perf.py`/`benchmarks/` above, which compares lothc against
*other* HTTP client libraries at a single point in time. asv never runs under pytest — it has its
own discovery/execution harness (`asv_runner`), with its own `setup`/`teardown`/`setup_cache` class
convention playing the role a pytest fixture would; there is no `@pytest.fixture` inside an
asv-discovered benchmark class because pytest is never imported anywhere in that path.

- Fast dev-loop check (no isolated env, no history, nothing saved): `task asv-quick`
- **"I made a small change and committed it — did it regress anything?"**: `task asv-continuous`
  (builds + benchmarks HEAD's parent and HEAD side by side, prints a before/after table with
  significant changes flagged; pass `-- <base> <branch>` for a specific pair). Skip `--quick` here —
  verified live that it's too noisy (single sample) and throws false-positive "regressions" comparing
  the *same* two commits to themselves; the real `repeat=3` sampling correctly reports no change.
- Real run across commit history (isolated env + fresh build per commit): `task asv-run -- <range>`,
  e.g. `task asv-run -- main~5..main`, or `task asv-run -- HEAD^!` for just one commit. With no args,
  benchmarks just the tip of each configured branch (`asv run`'s actual default — not "every new
  commit", which is what the special range spec `NEW` does instead).
- View results as a trend dashboard: `task asv-publish` then `task asv-preview`
- **"I'm mid-edit, haven't committed, does this help or hurt?"**: `task bench-check` — see
  "Instant no-git benchmark check" below. asv fundamentally can't answer this (it's keyed on commit
  hashes), which is a different problem than asv being broken or avoidable — `bench-check` is a
  separate, additive tool for exactly this case, reusing the exact same benchmark methods.

Two suites exist so far:

- `asv_bench/bench_download.py`: `get()` vs `download()` bytes-mode vs `download(dest=Path)` against
  a large body, both `time_*` and `peakmem_*`.
- `asv_bench/bench_verbs.py`: `get()` against all four decode targets it supports (raw `bytes`,
  plain `dict`, pydantic `BaseModel`, msgspec `Struct`) plus `post()`, against small, realistic
  JSON bodies via the exact same stdlib server the real pytest suite runs against
  (`tests/_server.py`, loaded the same importlib way `examples/server.py` does — no `sys.path`
  hack). Deliberately simpler than `bench_download.py`: a body this small never meaningfully skews
  a `peakmem_*` measurement, so there's no need for a separate-process server, and no
  `setup_cache()` since there's no expensive fixture to cache. Because pydantic/msgspec are
  optional extras, not lothc's own hard dependency, `asv.conf.json`'s `matrix.req` explicitly
  installs both into every real isolated `asv run`/`asv continuous` environment — otherwise those
  benchmark methods would import-error in a from-scratch build.

### Instant no-git benchmark check

`task bench-check` (`asv_bench/quick_check.py` + `asv_bench/_quick_check_worker.py`) runs every
`time_*` method across both suites above directly — no asv CLI, no commit, no `git stash`, works
against a dirty working tree exactly as it sits on disk. Each benchmark still runs in its own
subprocess (`_quick_check_worker.py`) so `peakmem` stays a true per-benchmark reading rather than a
whole-run high-water mark.

Mechanism: every run computes a **content hash of `lothc/`'s own source** (sha256 over each `.py`
file's relative path + bytes, sorted — not a git sha, no commit involved) and records this run's
results keyed by that hash into `asv_bench/.quick_check_history.json` (gitignored, an
ever-growing `{hash: {benchmark: {time, peakmem}}}` map, not a single overwritten file). With no
`--against`, it prints only `lothc hash: <hash>` — there's nothing to diff against until you name
one. Pass `--against <hash>` (a hash from *any* earlier run, not just the last one) to run again
and get a real before/after table against that specific point — deliberately explicit instead of
the rolling "always vs. the last run" behavior an earlier version of this tool had, so you choose
whether you're comparing against your original clean state or against your last edit:

```
task bench-check                              # records current state, prints its hash, no table
# ... edit code ...
task bench-check -- --against <hash-from-above>   # diffs vs that hash, prints ITS OWN new hash
# ... edit more ...
task bench-check -- --against <original-hash>     # compare against the ORIGINAL again
task bench-check -- --against <the-previous-run's-hash>  # or against just the last edit instead
```

(`yeetr` only turns *keyword-only* function parameters into `--flags` — a plain positional
parameter becomes a positional CLI argument instead. `main()`'s `against`/`history_file` params
are declared after a bare `*` for exactly this reason, confirmed live: without it, `--against`
wasn't recognized as a flag at all.)

This is deliberately additive to `asv run`/`asv continuous`, not a replacement — the two answer
different questions (uncommitted dev-loop iteration vs. tracked regression history across real
commits) and both reuse the exact same `bench_download.py`/`bench_verbs.py` benchmark definitions,
so there's only ever one definition of what each benchmark measures.

`_quick_check_worker.py` runs each benchmark's method 50 times in its own subprocess (1 + 49 more)
and reports the **min** time (timeit's own approach — real time can only be inflated by scheduler
noise, never deflated below the true floor, so the minimum across many samples converges on it) but
`peakmem` from **only the first call** — `ru_maxrss` is a whole-process high-water mark, so timing
repeats are safe (they only make the *time* reading more stable) but repeating a large-body
operation (e.g. `download()` against a 50MB body) many times in one process inflates peakmem via
allocator fragmentation across repeated large allocations, not anything the code being benchmarked
actually did — confirmed live: a naive repeat-loop pushed `peakmem_download_bytes` from ~140MB to
~1195MB with zero code change, before capturing `ru_maxrss` right after call #1 fixed it. Even with
50 samples, `VerbSuite`'s sub-millisecond benchmarks (a local HTTP round-trip, ~0.1-0.25ms) still
show real run-to-run spread (confirmed live: 0.85x-1.26x across three consecutive invocations with
*no* code change) — that's genuine process-to-process variance (CPU frequency scaling, scheduler
state, background load differing between each separate subprocess launch), not something more
samples *within* one process run can correct for. Trust `peakmem` deltas from this tool at face
value; treat `time` deltas on anything sub-millisecond as directional only unless the ratio is
large — small swings there are noise, not signal. `asv continuous`'s own significance testing
handles exactly this problem statistically for the real historical runs; this instant tool doesn't
attempt to replicate that.

A few things that were real gotchas building `bench_download.py`, worth knowing before adding more
benchmarks here:

- **The large-object HTTP server in `setup()` MUST be a genuine separate OS process (`subprocess.Popen`
  running stdlib `http.server`), never a thread in this process.** `peakmem_*` benchmarks measure
  `resource.getrusage(RUSAGE_SELF).ru_maxrss` — a server thread sharing this process would have its
  own memory counted toward the very number being tracked, contaminating it.
- **`environment_type: "uv"` + an explicit `build_command` using `python -m build --wheel -o
  {build_cache_dir} {build_dir}`, not the default.** asv's own default build step runs `pip wheel -w
  {build_cache_dir} {build_dir}`, which also dumps wheels for lothc's *dependencies* (pyreqwest) into
  the same cache directory — asv's install step then can't tell which of the multiple `.whl` files in
  there is "the" project wheel (`Found multiple wheels ... Cannot decide correct one`). `python -m
  build --wheel` only ever produces the target project's own wheel, so this doesn't happen.
- `setup_cache()`'s return value is pickled to disk and can be loaded by a **different process** than
  the one that produced it (confirmed by reading `asv_runner`'s actual source) — never rely on a
  resource started inside `setup_cache()` (a thread, a live subprocess handle) still being alive when
  a benchmark actually runs. Only put picklable, static data there (e.g. the large fixture file's
  path) and start any live resources fresh in `setup()`/tear them down in `teardown()`.
- Resources acquired in `setup()` (the server subprocess, the client, a per-run temp directory for
  the `download(dest=Path)` case) are managed via a single `contextlib.ExitStack`, closed once in
  `teardown()` — not several independent `.terminate()`/`.cleanup()`/`__exit__()` calls in a fixed
  order.

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

### Read style-guide.md before making code changes

Always read `./style-guide.md` before touching `lothc/_client.py` — the conventions there
(overload-pairs-over-casts, the type alias vocabulary, naming rules) are load-bearing; deviating from
them silently reintroduces bugs this project has already paid to fix once. There's no git history or
project memory to check yet (see Status above) — the list below **is** the design log until one builds up.

Before writing any code, tell the user that you've read this file AND read and fully understand
`./style-guide.md`, and are about to proceed with code changes.

## Development notes

- **`OAuthProvider`/`SyncOAuthProvider` (`lothc/_oauth.py`) — the non-obvious findings.**
  (1) lothc's `form=` is multipart only (`request_builder.multipart(...)`), there is no
  urlencoded form option, so the RFC 6749 token request is sent as
  `content=urllib.parse.urlencode({...})` with an explicit per-request
  `headers={"content-type": "application/x-www-form-urlencoded"}` — confirmed via the test
  server recording exactly that content type. (2) `_attach_body` encodes a pydantic `json=`
  payload via `model_dump(mode="json")` with no `by_alias=True`, so an *aliased* pydantic
  request model needs `model_config = ConfigDict(validate_by_name=True,
  serialize_by_alias=True)` (`validate_by_name` so lothc can construct it as
  `Cls(client_id=..., client_secret=...)`, `serialize_by_alias` so the aliases go over the
  wire); confirmed against a real non-RFC endpoint — with the config: 200, without it: `400
  "clientID must not be blank"`. msgspec `field(name=...)` needs nothing. `_attach_body` was
  deliberately left alone: this is documented in `docs/auth.md` instead, since it applies to any
  aliased pydantic `json=` payload, not just this one. (3) Both provider classes are plain
  classes with an explicit keyword-only `__init__`, not `@dataclass(kw_only=True)` — the plan's
  own decision procedure: written as a dataclass first, then `uv run zuban check tests` on a test
  passing one as `bearer_auth=` produced `Argument "bearer_auth" to "build" of "HTTPClient" has
  incompatible type "OAuthProvider"; expected "Callable[[], Awaitable[str]] | None"  [arg-type]`
  (and the sync mirror) while basedpyright/mypy/ty all accepted it — the exact zuban quirk
  `tests/test_auth.py`'s `_CountingAuthProvider` already documents. The plain class passes all
  four. (4) `TokenRequestTyping`/`TokenRefreshRequestTyping` are `Callable[..., JSONPayload]`
  aliases, not `Protocol`s with the exact `(*, client_id: str, client_secret: str)` keyword
  signature: that Protocol was tried first and confirmed live to accept a msgspec `Struct` on all
  four checkers but to reject an aliased pydantic model on basedpyright, mypy AND zuban (ty
  accepted) — pydantic's `Field(alias=...)` is a `dataclass_transform` field specifier, so the
  checkers synthesize the constructor as `(*, clientID: str, clientSecret: str, ...)` (verbatim
  from basedpyright: `Extra parameter "clientID" / Missing keyword parameter "client_id"`), even
  though `validate_by_name=True` makes `Cls(client_id=...)` work at runtime. Since an aliased
  request model *is* the model path's main use case, `...` keeps the return-type check (must be a
  valid `json=` payload) and leaves kwargs to the runtime. `TokenResponseTyping` stays a real
  property-based Protocol — attribute reads have no such alias problem. A request-only pydantic
  model can also use `Field(serialization_alias=...)` + `ConfigDict(serialize_by_alias=True)`
  with no `validate_by_name` at all, confirmed on all four checkers and at runtime. (5)
  `_CachedToken.expires_at` is wall-clock `time.time()`, never `time.monotonic()`, because it's
  persisted to the cache file and read back by a later process where a monotonic reading is
  meaningless. In the file it's an ISO 8601 UTC string at whole-second precision (`2026-09-08T10:56:40Z`, floored), not the raw epoch float — the file's only reader is a human checking why a token did or didn't renew; `_format_expires_at`/`_parse_expires_at` convert at the boundary and the in-memory value stays a float for the timestamp compare. A naive timestamp in the file is rejected as unusable (→ re-mint) rather than guessed at. (6) `client_auth` is ignored on the model path: the request model carries the
  credentials, and sending them a second time as HTTP Basic would be wrong for an API that
  doesn't speak RFC 6749 in the first place. (7) No subclass override hooks (e.g. a public
  `mint()`/`refresh()` to override) were added, deliberately: `__call__` would then have to call
  those public methods, which style-guide.md §10 forbids; the private `_mint`/`_refresh` split is
  the extension seam if that's ever wanted, a decision for later. (8) The cache write is plain
  sync file IO inside the async `__call__` — a few hundred bytes, once per token lifetime — via
  `tempfile.mkstemp` (which is exactly `O_EXCL` + `0600` in the same directory) then
  `Path.replace`. (9) pylint's `max-attributes` was bumped 7 → 13 in `pyproject.toml` for these
  two classes (11 keyword-only options + token + lock), and `S105` joined `S106` in the tests
  per-file-ignores — both hardcoded-password false positives on literal test values like
  `"token-1"`. Test server: `POST /oauth/token` (RFC), `/oauth/token-custom` (JSON, camelCase),
  `/oauth/token-boom` (500), each recording what it received per `key` into a `_token_requests`
  ClassVar, read back via `GET /oauth/token-requests?key=` so `tests/test_oauth.py` asserts wire
  shape through a public endpoint, never via provider internals.
  **Code-review round, all behaviour changes:** (10) `refresh_leeway` is clamped to
  `expires_in / 2` — a 300s default against a 60s token was "expired at birth", re-minting on
  every call; `_CachedToken.expires_in` (the issued lifetime) exists and is persisted to the
  cache file as `"expires_in"` purely so that clamp survives a restart. Tests that need "stale on
  the very next call" use `expires_in=0` (lifetime 0 → leeway 0 → `now >= expires_at` at any
  later-or-equal instant), not the old `expires_in=1000, refresh_leeway=2000` trick, which the
  clamp neutralises. (11) A refresh response without `refresh_token` keeps the one it was
  refreshed with (RFC 6749 §6 — `_carry_refresh_token`), instead of silently degrading the next
  renewal to a mint. (12) `scope` joined `token_url`/`client_id` in the cache guard and payload.
  (13) `OAuthProvider`'s `asyncio.Lock` is created lazily per running loop (`_get_lock`, keyed
  on `asyncio.get_running_loop()` identity), not in `__init__`: reproduced live on 3.14.7 that
  one provider used across two `asyncio.run()` calls raised `RuntimeError: <asyncio.locks.Lock
  object ...> is bound to a different event loop` from `Lock.acquire` — `asyncio.Lock` binds to
  the first loop it's awaited on, and a module-level provider legitimately outlives an
  `asyncio.run()`. `tests/test_oauth.py::test_provider_can_be_reused_across_event_loops` is a
  plain `def` for exactly this reason. (14) The Basic header is built by lothc
  (`_basic_authorization_header`), not by passing `basic_auth=` to the internal client: RFC 6749
  §2.3.1 wants each half `application/x-www-form-urlencoded`-encoded *before* the `:` join and
  base64 — `quote(..., safe="")`, matching authlib's `encode_client_secret_basic`, NOT
  `quote_plus` (space → `%20`, not `+`). (15) `default_expires_in` covers servers that omit
  `expires_in` (§5.1 only RECOMMENDS it); neither present → `ValueError`, wrapped per (17).
  `TokenResponseTyping.expires_in` widened to `int | None` — a model declaring plain `int` still
  satisfies a read-only property protocol member covariantly, confirmed on all four checkers.
  (16) The refresh→mint fallback is 400-only (`invalid_grant`); a 401/403/429 propagates, and if
  the fallback mint itself fails it's raised `from` the refresh error so both appear in the
  traceback (`_mint_after_rejected_refresh`). (17) `OAuthTokenError` (`.token_url`, original as
  `__cause__`) wraps everything `_renew` raises — the try in `__call__` covers that one line —
  and is the one place lothc wraps a decode library's error: the `bearer_auth` call runs *inside*
  the user's unrelated verb call, so an unwrapped `HTTPResponseError`/`ValidationError` there is
  misattributed to that call (the reviewer's own `if e.status == 404` misread), whereas a
  `response_data_type` error already belongs to the call that raised it. The cache write is
  deliberately outside that try: a disk error is not a token-acquisition failure. (18)
  `client_factory: Callable[[], AbstractAsyncContextManager[HTTPClient]] = HTTPClient.build`
  replaced `timeout`: called with no arguments (possible only because of (14) — the provider no
  longer needs to pass `basic_auth=`), so timeout/proxy/TLS/mTLS for the token endpoint are all a
  `functools.partial(HTTPClient.build, ...)`. Confirmed on all four checkers that the bare
  classmethod and a `partial` of it are both assignable. (19) `_decode_token_response` is gone:
  `TokenResponseTyping` is now a union of two private Protocols that each inherit `_compat.py`'s
  structural `StructTyping`/`BaseModelTyping` plus `_TokenFieldsTyping` (`access_token`,
  `expires_in` only), so `type[TokenResponseTyping]` is itself a valid `response_data_type=` and
  `_post_model` calls `client.post(..., response_data_type=token_response)` directly — zero casts
  on all four checkers with the test suite's real pydantic and msgspec models, and a model
  missing `expires_in` is rejected at the call site by all four. (20) A `token_cache_path` whose
  parent directory doesn't exist is a `FileNotFoundError` at construction
  (`_validate_provider_config`), not a failure on the first write. pylint `max-attributes` went
  13 → 15 for the extra `_default_expires_in`/`_client_factory`/`_lock_loop` attributes. (21)
  `refresh_token` is deliberately NOT part of `_TokenFieldsTyping` — RFC 6749 §5.1 makes it
  OPTIONAL, and the first cut of this protocol required it anyway, forcing every
  `token_response=` model to declare `refresh_token: str | None = None` even for an API that
  never issues one at all. `_token_from_model` reads it with
  `getattr(model, "refresh_token", None)` instead, so a model that omits the field entirely is
  just as valid as one that declares it; `getattr`'s typeshed overload returns `Any | None`,
  which basedpyright's strict mode accepts with no cast and no `reportAny` hit (this project
  doesn't enable that rule).
- **`sse()` never lets the client's total `timeout` touch the stream, and reconnects per the
  WHATWG EventSource model.** Found in real use: every SSE stream died with `HTTPTimeoutError`
  at exactly the client's `timeout` (30s by default) — reqwest's `timeout` runs from connect
  until the body *finishes*, and an SSE body never does. Reproduced live (0.3s timeout, 0.1s
  event spacing: 3 events then `ReadTimeoutError`), and confirmed pyreqwest has no "no timeout"
  per-request override — `RequestBuilder.timeout()` takes only a `timedelta` — but a
  per-request value *does* replace the client's (a 1-year and a 100-year `timedelta` both
  accepted and both let the stream outlive a 0.3s client timeout). So `_sse_stream` passes
  `_sse_default_timeout` (one year, a `ClassVar` on both clients) whenever the caller gives no
  `timeout=`; an explicit `timeout=` bounds one connection attempt. `read_timeout` (new on
  `build()`, plumbed through `_apply_tls_and_pool_config` like `connect_timeout`) is
  reqwest's idle-gap-between-chunks timeout — confirmed live it lets a 0.1s-spaced stream run
  indefinitely at 0.3s and kills a 0.5s-spaced one — and is the right stall detector for SSE.
  It surfaces as `ReadTimeoutError`, a `RequestTimeoutError` subclass (checked via `__mro__`),
  so `_translate_transport_error` already maps it to `HTTPTimeoutError` with no change.
  The retry middleware (`max_retries`) was useless here by construction: it wraps `send()`,
  which returns once headers arrive, so nothing that happens mid-body can ever reach it.
  Reconnect lives in `_sse_stream` itself (both clients), which is now a loop over
  `_sse_connection` (one connection's worth of events) with shared per-call `_SSEStreamState`
  (`last_event_id` buffer, current `retry_delay`, last response `status`). On
  `HTTPTransportError` it sleeps `state.retry_delay` and reconnects with a `Last-Event-ID`
  header, unless `max_reconnects` *consecutive* reconnects have yielded no event (reset to 0 on
  every event — so a flaky-but-alive server is reconnected to indefinitely, a dead one fails
  fast; `None` = unlimited, `0` = raise on first drop). A clean close returns unless
  `reconnect_on_close=True`; a 204 always returns (spec). `_SSEStreamState.status` exists
  purely for that 204 check — on the sync side the response object only ever exists on the
  read-loop's thread (`_drain_stream_chunks`, when `interruptible=True`), so status and
  content-type are captured there via `_SyncSSEResponseCheck`, an adapter that slots into
  `_sync_stream_chunks`'s existing single `check_status` hook rather than widening that
  signature. Inner generators are wrapped in `contextlib.aclosing`/`closing` so a caller's
  `break` closes the live connection right then, not at GC/loop-shutdown time.
  Spec field semantics fixed in the same pass, all shared module-level helpers with doctests:
  `id:` persists across events (an event with no `id:` line inherits the buffer; an explicit
  empty `id:` clears it; a value containing NUL is ignored) — this is what `allow_missing_id`
  now means, "no id ever seen", not "this record lacked one"; `retry:` (digits only, ms) is
  honored and overrides `reconnect_delay` for the rest of the call; data-less records still
  apply their `id:`/`retry:` (the old `_parse_sse_record` returned `None` for them, dropping
  those fields on the floor — my own repro's opening `retry: 100` record was silently
  discarded); all three line terminators (`\r\n`, `\n`, bare `\r`) via `_split_sse_records`,
  which holds back a trailing lone `\r` so a `\r\n` split across two chunks can't fabricate a
  record boundary (the old per-chunk `.replace(b"\r\n", b"\n")` had exactly that bug latent
  in it); a leading UTF-8 BOM is stripped once per connection. Stop conditions: a 2xx (other
  than 204) whose `Content-Type` isn't `text/event-stream` raises `ValueError` (the existing
  precedent for "the stream itself is malformed", cf. the missing-id error) — checked only on
  2xx so `error_for_status=False` against a 500 still just yields nothing as before. This is
  why `tests/test_sse.py`'s truncation tests moved off the generic `/truncated` endpoint
  (`application/octet-stream`) onto `/events-truncated`. Test-server reconnect endpoints
  (`/events-reconnect`, `/events-close-count`) key their per-connection counter on a `key`
  query param (a `uuid4` per test) exactly like `/flaky` does, and simulate a *dropped*
  connection the same way `/truncated` does — chunked transfer encoding with no terminating
  zero-length frame — since a plain server-side close is a *clean* EOF to the client, which is
  a different code path (`reconnect_on_close`) from the transport error a drop produces.
- **`RequestInfo` must be captured *before* `.send()`, not after — a `ConsumedRequest`/
  `SyncConsumedRequest` genuinely can't be read once sent.** Confirmed live: reading `.url`/
  `.method` off the built request *after* calling `.send()` on it raises `RuntimeError: Request
  was already sent` — pyreqwest's own consumed-request type isn't just naming, the object is
  actually unusable afterward. `_send`/`_send_sync` build the request, call `_request_info(built)`
  immediately, *then* `await built.send()` — in that order, not `built.send(), _request_info
  (built)` as a single return-tuple expression (tried first, caught by the real test suite
  immediately: Python evaluates tuple elements left-to-right, so `.send()` had already consumed
  the request by the time `_request_info` ran). `_send`/`_send_sync` return `tuple[RawResponse |
  RawSyncResponse, RequestInfo]` for every caller — originally only the `Result`-returning callers
  used the second element (others discarded it with `_`), but once `HTTPResponseError` also
  gained `.request`, *every* caller needs it (to attach to a raised error even on the plain
  `get`/`post`/etc. path), so the discard case no longer exists at all. **The exact same
  before-not-after discipline applies to `build_streamed()`'s `StreamRequest`**, used by
  `_sse_stream`/`_line_stream`/`_download` — `request_info = _request_info(request)` right after
  `request_builder.build_streamed()`, *before* entering the `async with request as raw_response`/
  `with request as raw_response` block, never after.
- **`error_type` decodes from already-fetched bytes, never the live response object.** By the
  time `_check_status` raises, it has already called `.bytes()` once to populate `body_start` —
  a pyreqwest response body can't be read twice, so `error_type`'s decode helper
  (`_decode_error_body`) takes plain `bytes` and mirrors `_decode_body`'s branch order/reasoning
  exactly, rather than reusing `_decode_body` itself (which reads directly off the response
  object via `.bytes()`/`.json()`). It needed no new overloads at all, unlike
  `response_data_type` — `error_type`'s value only affects what gets attached to
  `HTTPResponseError.parsed_body` on the failure path, never the function's own return type, so
  it's just `type[Data] | None = None` added identically to every existing overload, the same way
  `timeout`/`skip_auth` were. A decode failure against `error_type` (e.g. the body doesn't match
  the model) propagates the decode library's own exception uncaught, matching this file's
  existing rule for `response_data_type` failures — see "Validation errors from the chosen decode
  library are NOT wrapped" below.
- **The `_compat.py` fallback stub classes must be subscriptable, or `import lothc` crashes
  outright when an optional extra is missing.** `_client.py` has many unquoted
  `Decoder[Any]`/`TypeAdapter[Any]` annotations (no `from __future__ import annotations` in that
  file), evaluated eagerly at import time — confirmed live in three clean venvs (msgspec-only,
  pydantic-only, neither) that this raised `TypeError: type 'Decoder'/'TypeAdapter' is not
  subscriptable` on plain `import lothc`, a total regression of the "must work with neither,
  either, or both installed" guarantee. This had gone uncaught because
  `tests/test_compat_fallbacks.py`'s existing fixture deliberately never re-imports `lothc`
  itself (see that file's module docstring) — it only exercises `_compat.py` in isolation. Fixed
  by giving `Struct`/`Decoder`/`BaseModel`/`TypeAdapter`'s fallback stubs a trivial
  `__class_getitem__` returning `cls`, plus a real regression test
  (`test_import_lothc_succeeds_without_msgspec_or_pydantic`) that blocks both imports via
  `builtins.__import__` in a **fresh subprocess** and asserts `import lothc` still succeeds — a
  subprocess, not the existing fixture's in-process module-reload approach, since `lothc` is
  already loaded with the real msgspec/pydantic bound in the test process itself.
  Separately, a real and more serious gap was found and fixed: a strict-mode type checker (e.g.
  basedpyright) run against a consumer's own code in an environment that genuinely lacks
  msgspec/pydantic showed `Unknown` in unrelated public overloads (`sse()`, `get_result()`,
  `download()`, etc.) even when that consumer never touches the missing library — a real
  lothc-caused problem, not something a consumer should have to route around. **First attempt —
  wrapping the `TYPE_CHECKING`-branch import in its own `try/except` — confirmed empirically NOT
  to work**: pyright ignores the `except` branch entirely for typing purposes once
  `TYPE_CHECKING` is `True`, regardless of whether the real package resolves. That's a hard limit
  of *nominal* typing (importing the real class and hoping a fallback catches the failure) — it
  is not a hard limit of typing an optional dependency in general. **Real fix: swap the nominal
  `Struct`/`BaseModel`/`Decoder`/`TypeAdapter` references inside `Data`/`Params`/`Headers`/
  `TypedHeaders`/`JSONPayload`/`response_data_type` for checker-local structural `Protocol`s**
  (`StructTyping`/`BaseModelTyping`/`DecoderTyping`/`TypeAdapterTyping` in `_compat.py`) that
  require zero import of the real packages — `StructTyping` just declares
  `__struct_fields__: ClassVar[tuple[str, ...]]` (msgspec puts this directly on `Struct` itself),
  `BaseModelTyping` declares `model_config: ClassVar[Any]` (same story on pydantic's `BaseModel`),
  and `TypeAdapterTyping`/`DecoderTyping` match on `validate_json`/`decode`'s call shape alone.
  Since these protocols never import anything, they're never `Unknown`, in ANY environment — and
  since a real `Struct`/`BaseModel` subclass structurally satisfies them for free, precision when
  the packages *are* installed is completely unaffected. Confirmed live across all four checkers
  (basedpyright/mypy/ty/zuban), in both a msgspec-absent and a both-installed environment, with a
  real subclass passed as `response_data_type=`: always resolves to its own precise type, never
  `Unknown`. **Runtime `isinstance`/`issubclass`/`case` dispatch must keep using the real nominal
  `Struct`/`BaseModel`/`Decoder`/`TypeAdapter`** — the Protocols are for the five alias
  definitions and `response_data_type`'s parameter type only, never for a runtime check. One real
  side effect from the swap: several places that relied on basedpyright narrowing a `match`
  statement's `case _:` fallback (or an unguarded final branch) down to "not `Struct`/`BaseModel`"
  stopped narrowing correctly, because basedpyright can exclude a nominal class from a plain
  sequential `issubclass`/`isinstance` **if-chain** but NOT from a `match` fallback or an
  unguarded branch, once the union's members are Protocols rather than the nominal classes
  themselves (confirmed by testing each shape directly) — fixed by adding an explicit narrowing
  `issubclass`/`isinstance` guard (mirroring `_decode_body`'s already-correct if-chain) or an
  explicit `cast` at each affected site (`_apply_params`, `_apply_headers`, `_parse_typed_headers`,
  `_decode_json_line`), not by suppressing the resulting error.
- **Never leak the backend's exception types.** pyreqwest's `TransportError`/`RequestTimeoutError`/
  `NetworkError` are caught and translated to `lothc.HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError`
  at every `.send()` call and inside both SSE stream loops. If pyreqwest (or a future alternate
  backend) grows a new exception type that should be treated as a transport failure, translate it in
  `_translate_transport_error`, not at the call site.
  **`pyreqwest.exceptions.RedirectError` (raised past `max_redirects`) is NOT a `TransportError`
  subclass** — verified live via `issubclass()`, it's a sibling under `RequestError`, not a child of
  `TransportError` — so it was leaking raw out of every `.send()`/streaming call site until caught.
  Fixed by widening every one of those 8 call sites (both `_send`/`_send_sync` helpers, plus both
  SSE stream loops, both `_line_stream` loops, and both `download` loops) to catch
  `(PyreqwestTransportError, PyreqwestRedirectError)` and by widening `_translate_transport_error`'s
  own parameter type to the same union. Maps to plain `HTTPTransportError` (the generic base, not a
  new dedicated exception) — it's neither a timeout nor a connection-level network failure, and
  adding a whole new public exception class for one redirect-policy edge case didn't seem justified
  when the existing base already communicates "transport-level failure, no usable response."
  **Same story, found later, for `pyreqwest.exceptions.BuilderError`** (raised by `.build()`/
  `.build_streamed()` itself — e.g. `https_only=True` rejecting a plain-http URL) — confirmed live
  it leaked raw out of `client.get(...)`, since `.build()` was called by each verb *before* handing
  the result to `_send`/`_send_sync`, outside their try/except entirely. `BuilderError` isn't a
  `TransportError` subclass either (`BuilderError -> DetailedPyreqwestError -> PyreqwestError ->
  ValueError`, confirmed via `__mro__`). Fixed properly, not by adding a try/except at each of the
  16 call sites: `_send`/`_send_sync` now take the unbuilt `RequestBuilder`/`SyncRequestBuilder`
  and call `.build()` themselves inside the same try, so every one of their callers just got
  simpler (`_send(request_builder)` instead of `_send(request_builder.build())`); the 6
  `build_streamed()` call sites (`sse`/`stream_get`/`stream_post`/`download`, both clients) moved
  their `.build_streamed()` call inside their existing try block instead. `PyreqwestBuilderError`
  joins the same widened except tuple everywhere and maps to the same plain `HTTPTransportError`,
  for the same reason `RedirectError` does.
- **`lothc.testing` must never expose pyreqwest internals in its public API — no pyreqwest type a
  caller has to import or construct, and no builder pattern (this project doesn't use that pattern
  anywhere else).** Same spirit as "never leak the backend's exception types" above, generalized:
  pyreqwest is `lothc.testing`'s implementation detail too, not something its users should need to
  know exists. Caught in review: `LOTHCMock.match_request_with_response`'s custom-handler escape
  hatch originally required building the response via pyreqwest's own `ResponseBuilder().status(...)
  .body_json(...).build()` (async) / `.build_sync()` (sync) directly in the caller's own handler
  body — a real regression from the rest of this module's design, which already routes `data=`/
  `headers=`/`params=` through lothc's own `Data`/`Headers`/`Params` types on the `add_*_response`
  path. Fixed by adding `MockResponse` (`lothc/testing.py`) — a plain `@dataclass` (`data`/
  `headers`/`status`, same fields and same `_encode_json_payload`/`_encode_headers` encoding
  `add_*_response` already uses), not a builder. A `match_request_with_response` handler now
  returns `MockResponse | None` (`None` = decline, fall through to the next mock) instead of
  pyreqwest's `Response`/`SyncResponse`; `_wrap_custom_handler` adapts it into whichever shape
  pyreqwest's real `Mock.match_request_with_response` actually needs internally, so pyreqwest's
  response types never reach a caller. The async-vs-sync split on the handler itself (`async def`
  for a `HTTPClient` test, plain `def` for `SyncHTTPClient`) is inherent, not a pyreqwest leak —
  the same reason `HTTPClient`/`SyncHTTPClient` are two classes throughout lothc — and is genuinely
  unavoidable: a single `LOTHCMocker`/`ClientMocker` patches both transports, so this module has no
  way to know ahead of registration time which one a given handler will be invoked by; it dispatches
  on `inspect.iscoroutinefunction(handler)`, mirroring the exact constraint pyreqwest's own raw
  `CustomHandler` union already imposes.
  **Second round, same rule, pushed further:** an intermediate fix re-exported pyreqwest's real
  `Request`/`Url` classes from `lothc.testing` (`from lothc.testing import Request`, never
  `from pyreqwest.request import Request`) so a caller's own import statement never named
  `pyreqwest` — rejected as insufficient on review: the *class itself* was still pyreqwest's, a
  re-export is not a wrapper, "never expose pyreqwest internals" means the type too, not just the
  import path. Fixed properly instead: `MockRequest` (`lothc/testing.py`) is a plain, frozen,
  `slots=True` dataclass — `method: str`, `path: str`, `query_string: str`,
  `headers: Mapping[str, str]`, `body: bytes | None` — built by `_mock_request_from` from
  pyreqwest's real `Request` at the one seam a mock rule actually receives one.
  `match_request_with_response`'s handler and `match_request`'s predicate now both take
  `MockRequest`, not `Request`; `get_requests()` (on both `LOTHCMock` and `LOTHCMocker`) returns
  `list[MockRequest]`. `_wrap_custom_handler`/`_wrap_custom_matcher` are the only two places this
  module still touches pyreqwest's `Request` at all, converting it immediately before any
  caller-supplied code runs. `body` is read via `request.body.copy_bytes().to_bytes()` — always
  already-materialized bytes by the time a mock handler runs, never a live stream: confirmed by
  reading pyreqwest's own `ClientMocker._create_middleware`/`_create_sync_middleware` source, both
  of which read any streamed body into bytes *before* handing the request to any mock rule.
  `url=` matching (`mock()`/`get()`/etc.) is narrowed from `Matcher | Url` to `Matcher` (`str |
  re.Pattern[str]`) only — pyreqwest's own `Url`-object alternative is dropped entirely rather than
  wrapped, since a plain `str` already matches the exact URL (confirmed via pyreqwest's own
  `pytest_plugin/types.py`: its `UrlMatcher` is `Matcher | Url` with `Matcher` already including
  `str`), so there's no real capability lost and no second wrapper type needed. **No pyreqwest
  type is left anywhere in `lothc.testing`'s public API after this — not even as a re-export.**
  Two more findings from the same review round, fixed alongside this: (1) `LOTHCMocker._add_response`
  was stringifying `params=` values with plain `str()` for `match_query_param`, which mismatches
  pyreqwest's own query encoding for `bool` (`str(True)` == `"True"` vs pyreqwest's real
  `"true"`, confirmed live against `RequestBuilder.query()`) — first fixed via a dedicated
  `_query_param_str` helper (superseded, see below). (2) `_encode_params`'s plain-`Mapping`
  fallback branch was wrapping the input in `dict(cast(...))`, a copy the pre-refactor code never
  made (pyreqwest's own `.query()` already accepts any `Mapping`, confirmed via its `QueryParams`
  type alias) — fixed by widening `_encode_params`'s return type to `Mapping[str, str | int |
  float | bool]` and returning the fallback branch's input as-is.
  **Third round, same area, from a follow-up review with a stricter read-only constraint (no
  edits/git operations allowed at all this time, after the second round's finder subagents made
  unauthorized edits and a "cleanup" discarded real uncommitted work — see the session's own
  incident, not repeated here in the interest of space):** (1) `_query_param_str` (bullet above)
  hand-reimplemented pyreqwest's query encoding for exactly one known divergence (`bool`) — flagged
  as fragile, since any *future* pyreqwest encoding difference for another type would silently
  reintroduce the same class of bug. Replaced with `_query_param_match_values`, which derives the
  match string by asking pyreqwest's own real encoder (`Url.parse_with_params(...).
  query_dict_multi_value`) rather than reimplementing it — confirmed live this also returns
  lowercase `"true"`/`"false"` for a `bool`, so nothing regressed. (2) That same real-encoder call
  raises pyreqwest's own `ValueError: Invalid query value: None` for a `None` value — which a real
  request already does today (confirmed live via `_apply_params`), but the mock-registration path
  didn't, silently accepting `params={"flag": None}` (not a valid `Params` value per its own type,
  but nothing stops it arriving at runtime) and registering a matcher for the literal string
  `"None"` — a mock a real call could never produce. Switching to `_query_param_match_values` fixed
  this for free, since the same pyreqwest call that derives the encoding is what rejects the
  invalid value. (3) `MockRequest.headers` collapses a genuinely repeated header to its first
  value (`dict(request.headers)`, using `HeaderMap.__getitem__`'s own "first value for key"
  semantics) — flagged as silent data loss versus the real `Request.headers` a caller could
  previously read. Confirmed this exactly matches `lothc`'s own established convention for *every*
  other place it reads real headers (`Result.headers` etc. in `_client.py` all do `dict(raw_response.
  headers)` the same way), so this isn't a new regression pattern introduced by `lothc.testing` —
  documented explicitly in `MockRequest`'s own docstring instead of redesigning it into a
  multi-value shape that nothing else in the library uses. (4) `MockRequest` was `frozen=True`,
  implying immutable/hashable value semantics it couldn't actually deliver: `headers: Mapping[str,
  str]` is backed by a real mutable `dict`, so `frozen=True` still let `.headers[...] = ...` mutate
  in place (frozen only blocks *reassigning* a field, not mutating a mutable field's contents), and
  `hash(...)` still raised — from deep inside dataclass-generated machinery, a confusing failure
  mode for something claiming to be hashable. Dropped `frozen=True` (kept `slots=True`, matching
  `style-guide.md` §7's DTO convention) — a plain mutable dataclass is the honest shape, and
  `hash(MockRequest(...))` now raises a direct, expected `TypeError: unhashable type: 'MockRequest'`
  instead (confirmed live: a non-frozen dataclass with the default `eq=True` already sets
  `__hash__ = None`, Python's own standard "mutable + eq" convention).
- **Status errors are separate from transport errors.** `HTTPResponseError` (4xx/5xx with a body_start
  snippet) is a different failure class from `HTTPTransportError` (never got a response at all) —
  don't unify them.
- **Validation errors from the chosen decode library are NOT wrapped.** A `pydantic.ValidationError`
  or `msgspec.ValidationError` propagates natively — the user opted into that library by choosing
  it as a `response_data_type`, so its own exception is the expected one to see.
- **basedpyright strict mode is the contract.** Every change must pass `task typecheck` with zero
  errors and, ideally, zero new `cast(...)` calls.
- **Overload-pairs over a single generic-with-cast signature.** Every verb has two (or more) `@overload`s
  — one with no `response_data_type` (returns `bytes`), one generic (`response_data_type: type[TData]` → `TData`) — plus
  one real, non-generic implementation whose body returns the plain `Data` union. This is *why* there
  are almost no `cast()` calls anywhere: the implementation never claims to return the type parameter
  `TData` itself, only the overloads do, and pyright doesn't need to re-link a runtime
  `issubclass()`-narrowed value back to a type variable inside the implementation. A single generic
  method with a value-default (`response_data_type: type[T] = SomeDefault`) was tried first and rejected —
  it forces `cast()` at every branch inside the implementation.
- **The `Data`/`JSONPayload`/`Params`/`Headers`/`Form`/`File` type aliases are named at the *instance* level**,
  not the class level — `File` is the tuple a caller passes, not `type[File]`. Keep new aliases
  consistent with that. `JSONPayload` (the `json=` request-body alias, `dict[str, Any] | BaseModel
  | Struct`) was named that way — not `Json` — to avoid a case-only collision with the former
  `JSON` response-decode class (removed, see below): they were unrelated concepts (request input
  vs. response-decode target) that happened to differ only by capitalization, which real code
  review flagged as a genuine readability trap, not just a style nit. `JSONPayload` itself is
  unaffected by that class's removal — it's a plain `dict`/model union, never a `JSON` reference.
- **Bare `dict` is a valid `response_data_type` (and `Data` bound member); a subscripted
  `dict[str, Any]` is still rejected.** There used to be a dedicated `class JSON(dict[str, Any])`
  purely so a caller had a *concrete, already-parametrized* class to pass — bare `dict` was
  explicitly rejected (`_validate_response_data_type` raised `TypeError`) because passing it to a
  single generic `type[TData]` overload resolved to `dict[Unknown, Unknown]` under basedpyright
  strict mode (a generic overload copies the *argument's own* static type; it never fills in type
  arguments from `Data`'s member list). Fixed properly instead of living with that limitation:
  every verb that has a `[TData: Data]`-bound overload also got a second, non-generic, dedicated
  overload with `response_data_type: type[dict[str, Any]]` (hardcoding the return type instead of
  inferring it) placed alongside it — confirmed live via `reveal_type` that `client.get(path,
  response_data_type=dict)` now resolves to `dict[str, Any]`, not `Unknown`. This touched 26
  overload sites (13 verb-groups — `get`/`get_result`/`post`/`post_result`/`put`/`put_result`/
  `patch`/`patch_result`/`delete`/`delete_result`/`sse`/`stream_get`/`stream_post` — × both
  clients). `JSON` was removed entirely once this landed, since a plain `dict` does everything it
  did with no lothc-specific name to learn. **Lesson that generalizes beyond this one case: never
  claim something is "rejected/enforced" from a basedpyright result alone — Python never enforces
  type hints at runtime, so verify the actual runtime behavior, especially for anything that reads
  like a safety/validation guarantee.**
- **`bearer_token` (static) / `bearer_auth` (a callable, resolved fresh on every request) / `basic_auth`
  (a `(username, password)` pair) — not `auth_token`/`auth`.** Renamed deliberately: the first two only
  ever produce a Bearer `Authorization` header via pyreqwest's `.bearer_auth()`, and the old generic
  names hid that. `basic_auth` landed later, precisely named per this note's own earlier warning —
  it calls pyreqwest's separate `.basic_auth()`, never reusing the word "auth" generically. Keep this
  naming precise if a fourth mechanism is ever added.
  `_apply_bearer_auth` (both clients) was renamed to `_apply_auth` when `basic_auth` landed, since it
  now applies whichever of the three mechanisms is configured, not just bearer — a stale name here
  would silently mislead the next person touching auth application.
- **`response_data_type` defaults to `bytes` everywhere** (not a `Response`/`SyncResponse` wrapper — those classes
  were deleted). `sse()` is the deliberate exception: its bare default stays `SSEEvent[str]`, since a stream
  of discrete named records has no single "raw bytes" analogue the way one response body does.
- **`SSEEvent` is generic in both `TData` and `TId` (`SSEEvent[TData, TId = str]`, matching
  `sse()`'s own default of a required plain-`str` id when `id_type` is omitted — the class's own
  default must track whatever `sse()` actually produces by default, not an independently-chosen
  value; these two defaults disagreeing was a real bug caught here, not just a docs error),
  `@dataclass(kw_only=True)`, field order `id`, `event`, `data`** — `response_data_type`
  controls only what `.data` decodes to; `.event`/`.id` are always populated regardless of
  whether a decode target was passed. `.event` is a plain `str`, never `None` — it has a
  dataclass default of `"message"`, matching the SSE spec's own default for the field when
  absent from the wire (`_parse_sse_record` independently applies the same fallback while
  parsing; the dataclass default is just for manual/external construction, every internal call
  site passes `event=` explicitly regardless). `.id` is genuinely `str | None` by default, since
  the SSE spec makes `id:` an optional field a server can choose never to send — this isn't
  overcaution to relax later, it's a real invariant, which is why `TId` exists at all.
  **`id_type: type[TId] = str` + `allow_missing_id: bool = False` are `sse()`'s two independent
  knobs for `.id`'s type and requiredness, respectively.** This went through three designs: two
  separate params (`id_type` for coercion, `require` for a `Literal["event", "id",
  "id-event"] | None` presence check covering both fields — `require`'s `"event"` half never
  affected any type, `.event` is never `None` either way, and was pure orthogonal noise); then a
  version accepting a real union directly (`id_type=int | None`, mirroring how
  `response_data_type=A | B` works for discriminated unions) — this gave `basedpyright` genuinely
  precise inference (it decomposes a union argument against a generic overload into a union of
  results), but empirically broke every *other* type checker: `mypy`/`ty`/`zuban` all hard-error
  on a real `UnionType` *value* being passed where a generic `type[TId]` is declared, since none
  of them have `basedpyright`'s union-decomposition-as-fallback behavior, and widening the
  overload to accept `UnionType` directly to satisfy them made `basedpyright` degrade to
  `Unknown` for that exact call shape — confirmed via a real four-checker comparison, not just
  reasoning about it. The current two-param design was tested the same way and gets full,
  identical precision on all four checkers with zero tradeoff (confirmed via `reveal_type` on all
  four for `id_type` omitted/given crossed with `allow_missing_id` omitted/given) — because
  neither param is ever asked to accept a union *value*, only a plain `type[TId]` and a plain
  `bool`, which every checker already understands identically. `_coerce_sse_id` reflects this:
  `real_type = id_type if id_type is not None else str`, then raise-or-`None` on missing gated by
  `allow_missing_id` alone — no more `isinstance(id_type, UnionType)`/`get_args` inspection
  needed at all, since a union is never passed in the first place now. Don't reintroduce a
  union-accepting `id_type` — it's a strictly worse trade across the four-checker goal than what
  replaced it.
  Originally `sse(response_data_type=...)` returned the decoded payload bare, discarding
  `.event`/`.id` entirely — a real gap caught while writing the docs. Don't reintroduce that: any
  change to SSE parsing must keep decoding scoped to `parsed_record.data`, then build a new
  `SSEEvent(id=event_id, event=parsed_record.event, data=...)`, never yield the decoded value on
  its own.
- **`response_data_type` on `stream_get`/`stream_post` means something different than on every
  other verb** — same parameter name (standardized deliberately for consistency), but on
  `get`/`post`/etc. it decodes the *whole response body* as one value, while on the streaming
  verbs it decodes *each NDJSON line* as a separate value. Keep this per-line-vs-whole-body
  distinction in mind — it's a real tradeoff of the name reuse, not an oversight, and is called
  out explicitly in `docs/streaming.md`.
- **`download(path, dest=None) -> bytes | None` exists because `get()`'s default `bytes` path is
  genuinely expensive for large bodies, verified with a real benchmark (isolated subprocess,
  `ru_maxrss`, 500MB body, single non-concurrent request).** Reading pyreqwest's own Rust source
  (`response/internal/body_reader.rs`, `response/response.rs`) showed `.build().send()` +
  `.bytes()` does **3 full copies** of the body at peak: the `FullyConsumed` read path drains the
  whole response into a `VecDeque<Bytes>` of small chunks *before* the `Response` is even handed
  back to Python, `.bytes()` then copies that into one fresh pre-sized buffer, and lothc's own
  `bytes(await raw_response.bytes())` copies *again* (`pyreqwest.bytes.Bytes` is not a real Python
  `bytes` — confirmed live, `isinstance(b, bytes)` is `False`) — measured ~1540MB peak RSS for a
  500MB body (~3x). `download()` instead uses `build_streamed()` directly and accumulates chunks
  itself into a `bytearray()` via `+= chunk` — no `Content-Length` dependency (verified: peak RSS
  is the same, ~1x payload, whether or not the header is present/pre-sizes the buffer, since
  `bytearray`'s own amortized growth already avoids the extra copies pyreqwest's internal path
  does) — then casts to `bytes` once at the end (measured ~561MB peak, vs ~1540MB). Use `+=`, not
  `.extend()`, to accumulate a `pyreqwest.bytes.Bytes` chunk into a `bytearray`: `+=` (`bytearray.
  __iadd__`) accepts any buffer-protocol object directly, so no per-chunk copy is needed at all,
  while `.extend()` requires something iterable of `int`s, which `Bytes` doesn't implement — it
  needs an explicit `bytes(chunk)` copy first to satisfy that, confirmed live via basedpyright
  (`.extend(chunk)` alone fails with `reportArgumentType`). Passing `dest: Path` skips the in-memory
  buffer
  entirely and streams straight to a file — O(chunk size) memory regardless of body size, measured
  ~1x the OS's own read-buffer, not 1x the body.
  **Deliberately did NOT change `get()`'s default path to this.** For the small JSON bodies this
  library is actually designed around (hundreds of bytes to a few KB), 3x-of-nothing is still
  nothing — the streamed-accumulation path showed no measurable latency difference either way at
  that size in testing, so it's a pure memory-vs-complexity tradeoff, and `get()`/`post()`/etc.
  already have real users depending on their current shape. `download()` is additive, not a
  retrofit — reach for it specifically when fetching something large (a presigned S3 GET URL, a
  big export, etc.), not as a general replacement for `get()`.
- **`.to_bytes()`, never `bytes(the_thing.bytes())`, whenever a `pyreqwest.bytes.Bytes` genuinely
  needs to become a real Python `bytes`.** Both produce the same result (a real copy — `Bytes`
  isn't a `bytes` subclass, see above), but `.to_bytes()` is the conversion method the
  `pyo3_bytes` crate itself provides on the object, so use it instead of routing through the
  generic `bytes(...)` constructor's buffer-protocol dispatch. Applies at every `_check_status`/
  `_decode_body` call site on both clients: `raw_response.bytes().to_bytes()` (sync) /
  `(await raw_response.bytes()).to_bytes()` (async).
- **Don't convert at all when the target library accepts a buffer-protocol object directly.**
  `msgspec.json.decode()` does — so the `Struct` branch decodes straight from `await
  raw_response.bytes()` (or the sync equivalent) with no `.to_bytes()`/`bytes(...)` wrapper at
  all, skipping a full-body copy entirely. `pydantic.BaseModel.model_validate_json()` does
  **not** — it needs a genuine `str`/`bytes`/`bytearray`, so the pydantic branch does need
  `.to_bytes()` first. This is also why the pydantic branch switched from
  `model_validate(await raw_response.json())` to
  `model_validate_json((await raw_response.bytes()).to_bytes())`: the former decodes JSON via
  pyreqwest's own parser into a Python dict, then pydantic validates that dict (an extra
  dict-construction round-trip); the latter lets pydantic-core parse the JSON bytes directly.
  Tracked by `asv_bench/bench_verbs.py`'s `time_get_pydantic`/`peakmem_get_pydantic`.
- **Dropped: `TypedDict`/`typeguard` support.** `response_data_type` used to also accept a
  `TypedDict` class, decode-only, optionally validated at runtime via `typeguard` if installed
  (with its own `_IsTypedDict` Protocol bounding on `__required_keys__: ClassVar[frozenset[str]]`
  to admit a `TypedDict` into the `Data` bound, and a `_validate_typed_dict` helper that filtered
  the dict down to declared keys before validating, since `typeguard.check_type()` rejects any
  undeclared key by default — unlike msgspec `Struct`/pydantic `BaseModel`, which both silently
  ignore unknown fields). Removed entirely, deliberately, not just left unmaintained: pydantic and
  msgspec are the right tools for real validation, and keeping a third, weaker decode path around
  (pure-Python `isinstance` walks, no compiled validator, ~3.4-3.6x slower per a real benchmark —
  see git history for the numbers) added surface area without adding a capability the other two
  didn't already cover better. If this ever needs resurrecting, the old `_IsTypedDict`/
  `_validate_typed_dict` design is preserved in this file's own git history, not reinvented from
  scratch.
- **pyreqwest's `streamed_read_buffer_limit` defaults to 65536 (64KB) and silently defeats
  real-time streaming below that size — `sse`/`stream_get`/`stream_post` must set it to `1`
  explicitly.** Found live: `task example-run`'s SSE section showed one huge first delta (the
  entire stream's real duration) followed by ~25 near-instant deltas, instead of the ~250ms
  spacing the server actually writes at (`tests/_server.py`'s `_write_sse_events`, one
  `time.sleep()` per record). Confirmed via a raw pyreqwest repro (bypassing lothc entirely,
  hand-rolled chunked-transfer-encoding server, no lothc code involved) that this is pyreqwest's
  own behavior, not a lothc bug: its streamed body reader withholds every received byte
  internally until either `streamed_read_buffer_limit` bytes have accumulated or the stream hits
  EOF, at which point everything flushes to Python at once. `RequestBuilder.
  default_streamed_read_buffer_limit()` confirmed live to be exactly `65536` — small, slowly
  trickling payloads (an SSE stream of small records, NDJSON lines) never reach that threshold
  mid-stream, so nothing arrives until the connection closes. Fixed by chaining
  `.streamed_read_buffer_limit(1)` onto the request builder immediately before `.build_streamed()`
  at all 4 real-time streaming call sites (`_sse_stream`/`_line_stream` shared by
  `stream_get`/`stream_post`, both clients) — confirmed via `task example-run` afterward that SSE
  deltas track the server's real per-event pacing. **Deliberately did NOT touch `download()`'s two
  `build_streamed()` call sites** — bulk transfer benefits from the larger default buffer (fewer
  Python/Rust boundary crossings), and has no low-latency-delivery requirement the way
  `sse`/`stream_get`/`stream_post` do.
  **`1` does not mean "read one byte at a time" — verified with a 10MB single-burst benchmark**
  comparing `streamed_read_buffer_limit` at `1` vs `8192` vs `65536` vs `1048576`: chunk count
  (~200) and average chunk size (~50KB) were essentially identical across all four, with the
  bytes/chunk boundaries still governed entirely by whatever the OS/socket naturally delivers per
  read. The parameter only controls whether pyreqwest is willing to hand over already-received
  bytes immediately (`1`) versus holding them back to accumulate more first — it has no effect on
  read granularity itself, so there's no meaningful throughput cost to setting it low.
  `tests/test_sse.py`'s `test_sse_events_arrive_incrementally_not_buffered_until_stream_end` (+
  sync mirror) is the regression test — it measures `time.perf_counter()` deltas between yielded
  SSE events and asserts a majority exceed a small floor (0.3ms), rather than asserting on `max()`
  or the first delta specifically, since either of those flaked (~1-2% of runs) on an occasional
  single slow scheduler wakeup that isn't the bug reappearing. Confirmed both ways: reverting the
  `streamed_read_buffer_limit(1)` fix makes it fail immediately (only 1 of 10 deltas clears the
  floor); 80 consecutive runs of the fixed code passed with zero flakes. `tests/_server.py`'s
  `_write_sse_events` was shrunk from 25 events/0.25s sleeps (6.25s per full-stream test, and this
  endpoint alone accounted for ~77s of the whole ~86s suite) to 10 events/1ms sleeps specifically
  to make this timing assertion affordable to run on every test invocation — `time.perf_counter()`
  was used over `time.monotonic()` since it's the clock Python's own docs recommend specifically
  for measuring short durations/benchmarking (higher guaranteed resolution; `monotonic()` is
  general-purpose and coarser on some platforms), which matters at this test's millisecond scale.
