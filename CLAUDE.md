# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`lothc` ("Lord Of The Http Clients") is a typed HTTP client library built on [pyreqwest](https://github.com/mostafa-hussein/pyreqwest)
(a Rust-backed HTTP client). It provides a single, consistent typed API surface — `HTTPClient` (async)
and `SyncHTTPClient` (sync) — with first-class, optional support for both **pydantic** and **msgspec** as
decode targets, plus **TypedDict** support (optionally validated at runtime via **typeguard** if installed).

pydantic, msgspec, and typeguard are all optional extras (see `lothc/_compat.py`) — the library must
work with none, either, or all of them installed.

## Testing

- Run the test suite: `task test`
- Run a single test in isolation: `task test-one -- tests/test_foo.py::test_bar`
- Run doctests: `task test-doctests`

All test tasks accept `PYTEST_PROFILE=agent|dev` (default `dev`). Use `PYTEST_PROFILE=agent` for concise
agent-consumable output; re-run a failing test with `PYTEST_PROFILE=dev` for full detail when debugging.

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

## Manual example / smoke-testing

There's no automated integration suite yet — verification against a real server has so far been done
manually against a small FastAPI app:

```
task example-server   # starts a FastAPI test app on 127.0.0.1:8701 (separate terminal)
task example-run       # runs examples/run_http.py against it, showcasing every feature
```

`examples/test_server.py` is a minimal FastAPI app (JSON endpoints, an SSE stream, a slow endpoint, a
500-raising endpoint) used purely for manual verification — extend it when adding features that need a
live round trip to prove out (new verbs, retries, streaming, etc.).

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

`get`, `get_result`, `post`, `put`, `patch`, `sse` — each is a set of `@overload`s plus one real
implementation. See style guide for *why* overloads are used instead of a single generic signature.

### Read style-guide.md before making code changes

Always read `./style-guide.md` before touching `lothc/_client.py` — the conventions there
(overload-pairs-over-casts, the type alias vocabulary, naming rules) are load-bearing; deviating from
them silently reintroduces bugs this project has already paid to fix once (see git history / the
project's design log for the war stories).

Before writing any code, tell the user that you've read this file AND read and fully understand
`./style-guide.md`, and are about to proceed with code changes.

## Development notes

- **Never leak the backend's exception types.** pyreqwest's `TransportError`/`RequestTimeoutError`/
  `NetworkError` are caught and translated to `lothc.TransportError`/`TimeoutError`/`ConnectionError`
  at every `.send()` call and inside both SSE stream loops. If pyreqwest (or a future alternate
  backend) grows a new exception type that should be treated as a transport failure, translate it in
  `_translate_transport_error`, not at the call site.
- **Status errors are separate from transport errors.** `ResponseError` (4xx/5xx with a body_start
  snippet) is a different failure class from `TransportError` (never got a response at all) —
  don't unify them.
- **Validation errors from the chosen decode library are NOT wrapped.** A `pydantic.ValidationError`,
  `msgspec.ValidationError`, or `typeguard.TypeCheckError` propagates natively — the user opted into
  that library by choosing it as a `data_type`, so its own exception is the expected one to see.
- **basedpyright strict mode is the contract.** Every change must pass `task typecheck` with zero
  errors and, ideally, zero new `cast(...)` calls — see style guide for how to avoid them.
- Check `git log` / the roadmap discussion history for *why* before assuming a design choice is
  arbitrary — several non-obvious decisions here (e.g. `data_type` defaulting to `bytes`, `dict`/
  `dict[str, Any]` being rejected while `JSON`/`TypedDict` are accepted) were arrived at after a wrong
  first attempt was caught and corrected. Don't silently revert them.
