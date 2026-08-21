# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`lothc` ("Lord Of The Http Clients") is a typed HTTP client library built on [pyreqwest](https://github.com/mostafa-hussein/pyreqwest)
(a Rust-backed HTTP client). It provides a single, consistent typed API surface — `HTTPClient` (async)
and `SyncHTTPClient` (sync) — with first-class, optional support for both **pydantic** and **msgspec** as
decode targets, plus **TypedDict** support (optionally validated at runtime via **typeguard** if installed).

pydantic, msgspec, and typeguard are all optional extras (see `lothc/_compat.py`) — the library must
work with none, either, or all of them installed.

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

### Roadmap — what's done, what's next

Done: query params (typed + raw), per-request headers (typed + raw), timeouts, transport error
wrapping (`HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError`), `put`/`patch`/`delete`/`head`, SSE (with
`TypeAdapter`/`Decoder` support), `stream_get`/`stream_post` (raw chunks by default — unbuffered, safe
for arbitrary binary content; pass `response_data_type` to switch to newline-buffered NDJSON-style typed
decoding instead — the buffering is conditional on that param, not always-on), `download` (see the
large-object note below), the `Data` decode-target
system (`bytes` default, `lothc.JSON`, `TypedDict` + optional `typeguard` validation), a bearer-token
auth provider (static `bearer_token` or a per-request-refreshed `bearer_auth` callable), cookie/session
support (`cookie_store=True`), redirect control (`follow_redirects`/`max_redirects`), proxy config
(`proxy=`), and retries (`max_retries`/`retry_methods`, implemented as a real pyreqwest
`with_middleware` hook — backoff + `Retry-After` honored, defaults to idempotent verbs
`get`/`put`/`delete`/`head`, `post`/`patch` require explicit opt-in via `retry_methods`).

Not done yet:

1. **Typed error bodies** — an `error_type=SomeModel` param (mirrors `response_data_type`) that decodes 4xx/5xx
   bodies onto `HTTPResponseError`, instead of just the raw `body_start` snippet it has today.

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
- `asv_bench/bench_verbs.py`: `get()` against all five decode targets it supports (raw `bytes`,
  `lothc.JSON`, pydantic `BaseModel`, msgspec `Struct`, `TypedDict` — the last exercises typeguard's
  runtime validation when it's installed) plus `post()`, against small, realistic JSON bodies via
  the exact same stdlib server the real pytest suite runs against (`tests/_server.py`, loaded the
  same importlib way `examples/server.py` does — no `sys.path` hack). Deliberately simpler than
  `bench_download.py`: a body this small never meaningfully skews a `peakmem_*` measurement, so
  there's no need for a separate-process server, and no `setup_cache()` since there's no expensive
  fixture to cache. Because pydantic/msgspec/typeguard are optional extras, not lothc's own hard
  dependency, `asv.conf.json`'s `matrix.req` explicitly installs all three into every real isolated
  `asv run`/`asv continuous` environment — confirmed live (the built env is literally named
  `uv-py3.14-msgspec-pydantic-typeguard`) — otherwise those benchmark methods would import-error in
  a from-scratch build.

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
pydantic/msgspec/typeguard imports (`TYPE_CHECKING` block + runtime `try/except ImportError` with stub
fallback classes).

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

- **Never leak the backend's exception types.** pyreqwest's `TransportError`/`RequestTimeoutError`/
  `NetworkError` are caught and translated to `lothc.HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError`
  at every `.send()` call and inside both SSE stream loops. If pyreqwest (or a future alternate
  backend) grows a new exception type that should be treated as a transport failure, translate it in
  `_translate_transport_error`, not at the call site.
- **Status errors are separate from transport errors.** `HTTPResponseError` (4xx/5xx with a body_start
  snippet) is a different failure class from `HTTPTransportError` (never got a response at all) —
  don't unify them.
- **Validation errors from the chosen decode library are NOT wrapped.** A `pydantic.ValidationError`,
  `msgspec.ValidationError`, or `typeguard.TypeCheckError` propagates natively — the user opted into
  that library by choosing it as a `response_data_type`, so its own exception is the expected one to see.
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
- **The `Data`/`Json`/`Params`/`Headers`/`Form`/`File` type aliases are named at the *instance* level**,
  not the class level — `File` is the tuple a caller passes, not `type[File]`. Keep new aliases
  consistent with that (e.g. it's why `JSON` — a real class — reads correctly as `response_data_type: type[JSON]`
  without a category-slip like `type[DataType]` would).
- **Only `lothc.JSON` (or a subclass) and `TypedDict` classes are valid dict-shaped `response_data_type`s** — bare
  `dict` and `dict[str, Any]` are rejected, both statically (not in the `Data` bound) and at runtime
  (`_validate_response_data_type` raises `TypeError` for both). This was almost shipped with only the static
  rejection — a real live bug: bare `dict` silently succeeded, `dict[str, Any]` crashed with an ugly
  `issubclass() arg 1 must be a class` since a subscripted generic isn't a real class. **Lesson that
  generalizes beyond this one bug: never claim something is "rejected/enforced" from a basedpyright
  result alone — Python never enforces type hints at runtime, so verify the actual runtime behavior,
  especially for anything that reads like a safety/validation guarantee.**
  `_IsTypedDict` (a `Protocol` bounding on `__required_keys__: ClassVar[frozenset[str]]`) is how a real
  `TypedDict` is admitted to the `Data` bound while bare `dict` still isn't — every TypedDict class has
  that attribute (set by its metaclass), plain `dict` doesn't.
- **`bearer_token` (static) / `bearer_auth` (a callable, resolved fresh on every request) — not
  `auth_token`/`auth`.** Renamed deliberately: both mechanisms only ever produce a Bearer
  `Authorization` header via pyreqwest's `.bearer_auth()`, and the old generic names hid that. Keep this
  naming precise if Basic auth (or anything else) is ever added — don't let a new mechanism quietly
  reuse the word "auth" generically again.
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
  **`id_type: type[Any] | UnionType | None = str` is `sse()`'s single knob for both
  requiredness and type of `.id`** — this used to be two separate params (`id_type` for
  coercion, `require` for a `Literal["event", "id", "id-event"] | None` presence check covering
  both fields), then briefly a version of `id_type` alone that couldn't express "optional but
  coerced when present" (e.g. `uuid.UUID | None`) at all. Fixed by accepting a real union
  directly: `id_type=int | None` works the same way `response_data_type=A | B` already does for
  discriminated unions — `type[TId]` binds `TId` to whatever's passed, union or not, and
  `_coerce_sse_id` inspects it at runtime via `isinstance(id_type, UnionType)` +
  `get_args(id_type)` to decide (a) whether `NoneType` is a member (→ optional) and (b) the
  non-`None` member to actually coerce to (falls back to `str` if `id_type` was bare `None`).
  `require`'s `"event"` half never affected any type (`.event` is never `None` either way, see
  above) and was pure orthogonal noise — dropped rather than folded in. Don't reintroduce a
  separate requiredness param: bare `str`/a type = required, that same type unioned with `None`
  (or bare `None`) = optional, and that already covers every combination there is.
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
