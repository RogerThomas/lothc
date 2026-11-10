# CLAUDE.md

`lothc` ("Lord Of The Http Clients") is a typed HTTP client on
[pyreqwest](https://github.com/mostafa-hussein/pyreqwest): `HTTPClient` (async) and
`SyncHTTPClient` (sync), with pydantic and msgspec as optional extras (`lothc/_compat.py`) for
decoding and encoding. lothc must work with neither, either or both installed.

Design decisions and their reasons are records in `adrs/` (index: `adrs/README.md`). Read the
relevant one before changing the behaviour it covers, and add a new record for a new decision.
This file keeps only what every session needs: no narrative, and nothing the code, a docstring or
a test already says.

## Commands

- Dependencies: `task deps-sync` / `task deps-upgrade`
- Lint / format / type-check: `task lint` / `task format` / `task typecheck`; everything CI
  runs: `task check`
- Tests: `task test`, one test: `task test-one -- tests/test_foo.py::test_bar`, doctests:
  `task test-doctests`. Pass `PYTEST_PROFILE=agent` for concise output; re-run a failure with
  `PYTEST_PROFILE=dev`.
- Pre-commit (`prek`, a drop-in for `pre-commit`): `task precommit-install`, `task precommit`
- Benchmarks: `task bench-check` (working tree, before/after via `-- --against <hash>`),
  `task asv-*` (across git history), `task bench-codspeed` (the CI CodSpeed suite)

> [!CAUTION]
> `task perf`/`task perf-build` start Docker containers and run for minutes: only when the user
> asks for a benchmark. `task example-run` needs `task example-server` already running on
> `127.0.0.1:8701`.

## Releasing and CI

- **Versions come from the git tag** (`hatch-vcs`), SemVer because other packages depend on
  lothc. Pre-1.0, `feat` bumps the patch and a breaking change the minor.
- **release-please cuts releases**, so commit messages on `main` must be Conventional Commits.
  Tags are the bare version (no `v`, no component), which hatch-vcs reads. Never add an
  `## [Unreleased]` section to `CHANGELOG.md`.
- **Publishing is dispatched** (`gh workflow run release.yml --ref <tag>`), since a release made
  with `GITHUB_TOKEN` triggers no workflow. `release.yml` stays top-level for PyPI's Trusted
  Publisher, runs all of CI against the tag, and refuses a non-tag ref.
- **CI:** 3.13/3.14/3.15 plus free-threaded 3.14t/3.15t with `PYTHON_GIL=0` (lothc shares
  state across threads, and users share a client across threads), and `test-no-extras` with
  pydantic/msgspec uninstalled. The `perf` group's clients stay out of `dev`: one has no
  free-threaded wheel.
- **README.md uses absolute URLs only**, since it's the PyPI description.
- **The sdist has an explicit `include` allowlist**; hatchling's default shipped the whole repo.
- **TODO (GitHub UI):** add a tag-pattern protection rule to the `pypi` environment.

## Scope

- **Not done yet:** `sse_post` (deferred by the user).
- **Only if someone asks:** request/response hooks, a generic `request()`, streaming uploads,
  status/headers on the streaming verbs, cookie-jar access.
- **Deliberately not exposed:** per-algorithm compression toggles, `interface`, `tcp_nodelay`,
  `referer`, CRLs, a final URL on `Response` (pyreqwest has none).

## Architecture

- **`lothc/_client.py` holds everything** until it earns a split; `lothc/_oauth.py` is separate
  because it's built *on* the client (a short-lived `HTTPClient` per token request, plugged into
  `bearer_auth`).
- **pyreqwest stays** (2026-10-04): a direct reqwest binding gained only 5-12% and would mean
  per-platform wheels plus reimplementing sync, streaming, multipart, TLS and the mocker.
- **The two clients are near-mirrors**; implement every feature on both.
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
  Wording differs between macOS and Linux, so check a new pin on Linux.
- **Mutation runs use `python -B`**: a same-size edit restored within a second leaves a stale
  `.pyc`.
- **Speed (~20s):** test servers poll at 0.01s; scale a timing pair down together, never a lone
  margin; `backoff_base=0.001` where only *that* a retry happened matters; a test below a
  default asserts a bracket, not a floor. Rejected: pytest-xdist.
- Prefer doctests for small algorithmic functions.

## Pitfalls

Each of these was a real bug or a lost hour once.

- **`_compat.py`'s fallback stubs must be subscriptable**: annotations are evaluated at import, so
  `import lothc` crashes without an extra otherwise.
- **On 3.13, define a class before an annotation uses it**; don't quote the annotation.
- **In the `form=` `match`, `case list()` comes before the `File` patterns**: a sequence pattern
  matches a list too, so `["a.png", b"..."]` was read as a file.
- **`_LazyHeaders` holds pyreqwest's `HeaderMap`, not the response**: a read response caches its
  body (40 held 10MB responses: ~470MB).
- **`.to_bytes()`, never `bytes(x.bytes())`**, and don't convert at all when the decoder takes a
  buffer (msgspec does; pydantic needs `bytes`).
- **Keep `_send_method` one async layer deep**: each extra layer is a frame on every resume.
- **The async idle limit wraps each chunk read, never a `yield`**, or a slow consumer looks like
  a stalled server.
- **The mocker validates before registering** (pyreqwest registers a mock as soon as the call
  returns) **and applies `data=` before `headers=`** (`.body_json()` appends a Content-Type).
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
