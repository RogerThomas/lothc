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

A git repo now exists (`main`, no tags pushed yet). A real test suite exists under `tests/` (see
Testing below) and a benchmark suite under `benchmarks/` — this file is no longer the only thing
that survives between sessions, but it's still the source of truth on decisions and remaining work,
so keep it up to date.

`version = "0.0.0"` in `pyproject.toml` is a static placeholder. This project uses **CalVer** (e.g.
`2026.8.18`, no "v" prefix — same scheme as the `yeetr` project), derived from the git tag via
`hatch-vcs` once the first tag is pushed — a release is just a pushed tag, nothing to bump by hand.
The exact change needed (uncomment `hatch-vcs`, add `dynamic = ["version"]`) is commented directly
above `[tool.hatch.build.targets.wheel]` in `pyproject.toml`.

Not yet done, outside the roadmap below: no `LICENSE`, no CI (`.github/workflows`).

### Roadmap — what's done, what's next

Done: query params (typed + raw), per-request headers (typed + raw), timeouts, transport error
wrapping (`TransportError`/`TimeoutError`/`ConnectionError`), `put`/`patch`/`delete`/`head`, SSE (with
`TypeAdapter`/`Decoder` support), `stream_get`/`stream_post` (raw chunks by default — unbuffered, safe
for arbitrary binary content; pass `response_data_type` to switch to newline-buffered NDJSON-style typed
decoding instead — the buffering is conditional on that param, not always-on), the `Data` decode-target
system (`bytes` default, `lothc.JSON`, `TypedDict` + optional `typeguard` validation), a bearer-token
auth provider (static `bearer_token` or a per-request-refreshed `bearer_auth` callable), cookie/session
support (`cookie_store=True`), redirect control (`follow_redirects`/`max_redirects`), proxy config
(`proxy=`), and retries (`max_retries`/`retry_methods`, implemented as a real pyreqwest
`with_middleware` hook — backoff + `Retry-After` honored, defaults to idempotent verbs
`get`/`put`/`delete`/`head`, `post`/`patch` require explicit opt-in via `retry_methods`).

Not done yet:

1. **Typed error bodies** — an `error_type=SomeModel` param (mirrors `response_data_type`) that decodes 4xx/5xx
   bodies onto `ResponseError`, instead of just the raw `body_start` snippet it has today.

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
- Install the git pre-commit hook: `task precommit-install` (`prek` — a fast Rust drop-in for
  `pre-commit` that reads the same `.pre-commit-config.yaml` — is a `uv`-managed dev dependency,
  no separate install needed)
- Run all pre-commit hooks against the whole repo: `task precommit`

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

`get`, `get_result`, `post`, `put`, `patch`, `delete`, `head`, `sse`, `stream_get`, `stream_post` —
each is a set of `@overload`s plus one real implementation. See style guide for *why* overloads are
used instead of a single generic signature.

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
