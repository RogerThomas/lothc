<p align="center">
  <img src="assets/logo.svg" alt="lothc logo" width="220">
</p>

# lothc

[![PyPI](https://img.shields.io/pypi/v/lothc)](https://pypi.org/project/lothc/)
[![CI](https://github.com/RogerThomas/lothc/actions/workflows/ci.yml/badge.svg)](https://github.com/RogerThomas/lothc/actions/workflows/ci.yml)
[![codecov](https://img.shields.io/codecov/c/github/RogerThomas/lothc/main)](https://codecov.io/gh/RogerThomas/lothc)
[![Python versions](https://img.shields.io/pypi/pyversions/lothc)](https://pypi.org/project/lothc/)
[![License](https://img.shields.io/pypi/l/lothc)](LICENSE)

**L**ord **O**f **T**he **H**ttp **C**lients — a typed HTTP client for Python, built on
[pyreqwest](https://github.com/mostafa-hussein/pyreqwest) (an awesome Rust-backed HTTP client), with
first-class optional support for **pydantic** and **msgspec** as both decode *and* encode targets.

## Installing

```
uv add 'lothc[pydantic,msgspec]'
```

(Any subset of the extras works — the base package alone gets you `bytes`/`dict` support.)

## Quickstart

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

## Why?

Why was lothc built, and why should you use it?

pyreqwest is a genuinely excellent HTTP client for Python, fast because the heavy lifting happens
in Rust, not pure Python. But its API is a builder pattern (`client.get(path).build()` before you
can even `.send()` it), unfamiliar to anyone coming from `requests`/`httpx`/`niquests`, where a
call is just `client.get(url, params=..., headers=...)`. lothc wraps pyreqwest with exactly that
familiar shape, so you get pyreqwest's speed without giving up the ergonomics Python HTTP users
already expect.

The other reason: Python's most popular third-party HTTP clients (httpx, aiohttp, niquests) all
have first-class JSON support: call `.json()` and get back a `dict`. But in order for a Python
project to be end-to-end type-safe, data crossing I/O boundaries must be validated, and if
invalid, handled accordingly (raise an error) — a plain `dict` does neither. Closing that gap is
exactly what lothc is designed for.

The original idea was to make lothc backend-agnostic: httpx, aiohttp, niquests, and pyreqwest all
interchangeable underneath the same typed lothc interface, so switching backends never meant
switching your call sites. However, in the author's opinion there's currently no compelling
reason not to just use pyreqwest, so pyreqwest is the only backend implemented today, but, if
there's demand for an httpx/aiohttp/niquests backend, the author is happy to consider adding one.

## Fast

![HTTP client throughput race — lothc and pyreqwest finish in well under a fifth of a second, other libraries take much longer](assets/perf-race.svg)

Benchmarked with [`perf.py`](perf.py) against a tiny Rust-based static JSON server, 10,000 requests at
concurrency 100 — including lothc's fully-typed decode targets (`response_data_type=` a msgspec
`Struct` or a pydantic `BaseModel`), not just raw bytes or an untyped dict. The object handed back
from those runs isn't just parsed JSON — it's a real, constructed, field-validated instance of
your own type, and that validation cost is included in the numbers, not benchmarked around.
Decoding into a real msgspec `Struct` even edged out the unvalidated dict path in this run. Full
numbers: [docs/benchmarks.md](docs/benchmarks.md).

## Highlights

Pass `response_data_type` to any lothc call and get back a real, constructed, field-validated
pydantic `BaseModel` or msgspec `Struct`, instead of the plain `dict` `.json()` leaves you to
validate yourself. See [Benchmarks](docs/benchmarks.md) for what that costs (usually nothing).

| | |
|---|---|
| **Two clients, one API** | `HTTPClient` (async) and `SyncHTTPClient` (sync) — identical surface, both backed by pyreqwest. |
| **Every verb** | `get`/`get_result`, `post`, `put`, `patch`, `delete`, `head`, `download` — see [Verbs](docs/verbs.md). |
| **Typed params, headers & forms** | A `BaseModel`/`Struct` for query params or headers (with `None`-field omission), or a real multipart body via `form=`. |
| **Precise static types** | Every verb is paired `@overload`s, not a cast-laden generic — your editor knows the exact return type. |
| **SSE & streaming** | Typed SSE decode (discriminated unions included), plus raw or NDJSON-typed `stream_get`/`stream_post` — see [SSE](docs/sse.md) / [Streaming](docs/streaming.md). |
| **Retries with real backoff** | `max_retries`/`retry_methods`, a genuine pyreqwest middleware hook, `Retry-After`-aware — see [Retries](docs/retries.md). |
| **Authentication** | Static `bearer_token`, a per-request-refreshed `bearer_auth`, or `default_headers` for anything else — see [Authentication](docs/auth.md). |
| **Cookies, redirects, proxy** | `cookie_store`, `follow_redirects`/`max_redirects`, `proxy=` — see [Networking](docs/networking.md). |
| **A real error hierarchy** | `HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError` for no response, `HTTPResponseError` for 4xx/5xx — see [Error handling](docs/errors.md). |
| **Everything optional** | pydantic, msgspec — works with neither, either, or both installed. |

## Development

See `CLAUDE.md` and `style-guide.md` for architecture notes and the (non-obvious in places) design
conventions this codebase relies on.

```
task deps-sync    # install
task test         # run tests
task check        # lint + format-check + typecheck (what CI runs)
```
