<p align="center">
  <img src="assets/logo.svg" alt="lothc logo" width="220">
</p>

# lothc

**L**ord **O**f **T**he **H**ttp **C**lients — a typed HTTP client for Python, built on
[pyreqwest](https://github.com/mostafa-hussein/pyreqwest) (a Rust-backed HTTP client), with first-class
optional support for **pydantic**, **msgspec**, and **TypedDict** (validated via **typeguard** if
installed) as decode targets.

```python
from lothc import HTTPClient
from pydantic import BaseModel


class Pokemon(BaseModel):
    id: int
    name: str


async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
    pikachu = await client.get("pokemon/pikachu", response_data_type=Pokemon)
    print(pikachu)  # id=25 name='pikachu'
```

## Fast

![HTTP client throughput race — lothc and pyreqwest finish in well under a fifth of a second, other libraries take much longer](assets/perf-race.svg)

Benchmarked with [`perf.py`](perf.py) against a tiny Rust-based static JSON server, 10,000 requests at
concurrency 100 — including lothc's fully-typed decode targets (`response_data_type=` a msgspec
`Struct`, a pydantic `BaseModel`, or a `TypedDict` validated via typeguard), not just raw bytes or
an untyped dict. The object handed back from those runs isn't just parsed JSON — it's a real,
constructed, field-validated instance of your own type, and that validation cost is included in
the numbers, not benchmarked around. Decoding into a real msgspec `Struct` even edged out the
unvalidated dict path in this run; typeguard's pure-Python validation was the one clear exception,
costing a real, visible slowdown. Full numbers: [docs/benchmarks.md](docs/benchmarks.md).

## Highlights

- **First-class DTO validation and transformation, built in.** httpx, aiohttp, niquests, and every
  other general-purpose HTTP client hand you back a response object with a `.json()` that gives
  you, at best, a plain `dict` — decoding that into a real, validated object of your own type is
  something *you* bolt on afterward, because it isn't what those libraries are for. In lothc it's
  not an afterthought: pass `response_data_type` and you get back a real, constructed,
  field-validated pydantic `BaseModel`, msgspec `Struct`, or typeguard-validated `TypedDict`
  directly from the client — one of lothc's biggest offerings over reaching for `requests`/`httpx`
  and validating separately. See [Benchmarks](docs/benchmarks.md) for what that costs (usually
  nothing).
- **`HTTPClient`** (async) and **`SyncHTTPClient`** (sync) — the same typed API, both backed by pyreqwest.
- **Pick your decode target per call** — a pydantic `BaseModel`, a msgspec `Struct`, a `TypedDict`,
  `lothc.JSON`, or raw `bytes` (the default). No cast-laden internals — everything is built on paired
  `@overload`s, so every call site gets a precise static type.
- **All the verbs**: `get`/`get_result`, `post`, `put`, `patch`, `delete`, `head` (headers-only
  `Result`, no body decode).
- **Typed *and* raw query params/headers** — pass a plain `dict`/`Mapping`, or a `BaseModel`/`Struct` for
  free per-field validation and `None`-field omission.
- **SSE support**, with `response_data_type` accepting a class, a pydantic `TypeAdapter`, or a prebuilt msgspec
  `Decoder` — so even discriminated-union event streams decode natively.
- **Streaming (`stream_get`/`stream_post`)** — raw chunks by default (unbuffered, safe for large
  binary bodies), or pass `response_data_type` to switch to newline-buffered, typed NDJSON-style decoding.
- **Retries** — `max_retries`/`retry_methods` on `build()`, implemented as a real pyreqwest
  `with_middleware` hook. Backs off, honors `Retry-After`, and defaults to the idempotent verbs
  (`get`/`put`/`delete`/`head`) — `post`/`patch` need an explicit opt-in via `retry_methods`.
- **Authentication** — a static `bearer_token`, or `bearer_auth` for a token resolved fresh on
  every request (expiring/rotating tokens), plus `arbitrary_headers` for anything else that needs
  to go out on every request (an API key header, etc.).
- **Cookies, redirects, proxy** — `cookie_store=True` for an in-memory cookie jar,
  `follow_redirects`/`max_redirects` for redirect control, `proxy=` for proxying requests.
- **A real transport-error hierarchy** (`HTTPTransportError` / `HTTPTimeoutError` / `HTTPConnectionError`) and a
  status-error type (`HTTPResponseError`) — pyreqwest's own exception types never leak through.
- **pydantic, msgspec, and typeguard are all optional** — the library works with none, either, or all
  three installed.

## Installing

```
pip install lothc[pydantic,msgspec,typeguard]
```

(Any subset of the extras works — the base package alone gets you `bytes`/`JSON`/`TypedDict`-without-validation support.)

## Development

See `CLAUDE.md` and `style-guide.md` for architecture notes and the (non-obvious in places) design
conventions this codebase relies on.

```
task deps-sync    # install
task test         # run tests
task check        # lint + format-check + typecheck (what CI runs)
```
