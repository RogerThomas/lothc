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

No git repo exists yet (deliberately — not created yet). No persistent memory has accumulated for
this project either, since it only just moved here from being prototyped inside another repo — this
file is currently the *only* thing that survives between sessions, so keep it up to date as the
source of truth on decisions and remaining work, until real project memory builds up.

`version = "0.0.0"` in `pyproject.toml` is a static placeholder. This project uses **CalVer** (e.g.
`2026.8.18`, no "v" prefix — same scheme as the `yeetr` project), derived from the git tag via
`hatch-vcs` once a repo exists — a release is just a pushed tag, nothing to bump by hand. The exact
change needed (uncomment `hatch-vcs`, add `dynamic = ["version"]`) is commented directly above
`[tool.hatch.build.targets.wheel]` in `pyproject.toml`.

### Roadmap — what's done, what's next

Done: query params (typed + raw), per-request headers (typed + raw), timeouts, transport error
wrapping (`TransportError`/`TimeoutError`/`ConnectionError`), `put`/`patch`, SSE (with
`TypeAdapter`/`Decoder` support), the `Data` decode-target system (`bytes` default, `lothc.JSON`,
`TypedDict` + optional `typeguard` validation), and a bearer-token auth provider (static
`bearer_token` or a per-request-refreshed `bearer_auth` callable).

Not done yet, in rough priority order:

1. **Retries** — backoff on `TransportError`/`ConnectionError` and 5xx/429, honoring `Retry-After`,
   for idempotent verbs (`get`, `put`, `delete`, `head` — NOT blind `post`/`patch` unless opted in).
2. **Typed error bodies** — an `error_type=SomeModel` param (mirrors `data_type`) that decodes 4xx/5xx
   bodies onto `ResponseError`, instead of just the raw `body_start` snippet it has today.
3. **Streaming downloads** — a `stream()` method yielding raw chunks for large bodies; reuse the
   `_sse_stream` plumbing minus the SSE record-parsing.
4. **`delete`/`head` verbs** — `delete` is a thin wrapper over `_send_with_body` with no body params;
   `head` returns a headers-only `Result` (no body to decode).

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
manually against a small jero app:

```
task example-server   # starts a jero test app on 127.0.0.1:8701 (separate terminal)
task example-run       # runs examples/run_http.py against it, showcasing every feature
```

`examples/server.py` is a minimal jero app (JSON endpoints, an SSE stream, a slow endpoint, a
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
them silently reintroduces bugs this project has already paid to fix once. There's no git history or
project memory to check yet (see Status above) — the list below **is** the design log until one builds up.

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
  errors and, ideally, zero new `cast(...)` calls.
- **Overload-pairs over a single generic-with-cast signature.** Every verb has two (or more) `@overload`s
  — one with no `data_type` (returns `bytes`), one generic (`data_type: type[TData]` → `TData`) — plus
  one real, non-generic implementation whose body returns the plain `Data` union. This is *why* there
  are almost no `cast()` calls anywhere: the implementation never claims to return the type parameter
  `TData` itself, only the overloads do, and pyright doesn't need to re-link a runtime
  `issubclass()`-narrowed value back to a type variable inside the implementation. A single generic
  method with a value-default (`data_type: type[T] = SomeDefault`) was tried first and rejected —
  it forces `cast()` at every branch inside the implementation.
- **The `Data`/`Json`/`Params`/`Headers`/`Form`/`File` type aliases are named at the *instance* level**,
  not the class level — `File` is the tuple a caller passes, not `type[File]`. Keep new aliases
  consistent with that (e.g. it's why `JSON` — a real class — reads correctly as `data_type: type[JSON]`
  without a category-slip like `type[DataType]` would).
- **Only `lothc.JSON` (or a subclass) and `TypedDict` classes are valid dict-shaped `data_type`s** — bare
  `dict` and `dict[str, Any]` are rejected, both statically (not in the `Data` bound) and at runtime
  (`_validate_data_type` raises `TypeError` for both). This was almost shipped with only the static
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
- **`data_type` defaults to `bytes` everywhere** (not a `Response`/`SyncResponse` wrapper — those classes
  were deleted). `sse()` is the deliberate exception: its bare default stays `SSEEvent`, since a stream
  of discrete named records has no single "raw bytes" analogue the way one response body does.
