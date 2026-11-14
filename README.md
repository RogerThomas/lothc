<p align="center">
  <img src="https://raw.githubusercontent.com/RogerThomas/lothc/main/assets/logo.svg" alt="lothc logo" width="220">
</p>

# lothc

[![PyPI](https://img.shields.io/pypi/v/lothc)](https://pypi.org/project/lothc/)
[![CI](https://github.com/RogerThomas/lothc/actions/workflows/ci.yml/badge.svg)](https://github.com/RogerThomas/lothc/actions/workflows/ci.yml)
[![CodSpeed](https://img.shields.io/endpoint?url=https://codspeed.io/badge.json)](https://app.codspeed.io/RogerThomas/lothc?utm_source=badge)
[![codecov](https://img.shields.io/codecov/c/github/RogerThomas/lothc/main)](https://codecov.io/gh/RogerThomas/lothc)
[![Python versions](https://img.shields.io/pypi/pyversions/lothc)](https://pypi.org/project/lothc/)
[![License](https://img.shields.io/pypi/l/lothc)](https://github.com/RogerThomas/lothc/blob/main/LICENSE)

**L**ord **O**f **T**he **H**ttp **C**lients — a fast, Rust-backed, typed HTTP client for Python,
with first-class optional support for **pydantic** and **msgspec** as both decode *and* encode
targets.

## Installing

```
uv add 'lothc[pydantic,msgspec]'
```

(Any subset of the extras works — the base package alone gets you `bytes`/`dict` support.)

## Quickstart

```python
import asyncio
from lothc import HTTPClient
from pydantic import BaseModel


class Pokemon(BaseModel):
    id: int
    name: str


async def main() -> None:
    async with HTTPClient(base_url="https://pokeapi.co/api/v2/") as client:
        response = await client.get("pokemon/pikachu", into=Pokemon)
        print(response.status, response.data)  # 200 id=25 name='pikachu'


asyncio.run(main())
```

## Why?

[pyreqwest](https://github.com/MarkusSintonen/pyreqwest) is a genuinely great Rust-backed HTTP
client, and the inspiration for lothc. Its API is a builder (`client.get(url).build()` before you
can `.send()`), so lothc gives it the `requests`/`httpx` call shape, and adds its own native
transport, faster still. pyreqwest stays lothc's transport on Windows, for HTTP/2 and with
`native="off"`.

Where other clients' `.json()` returns an unvalidated `dict`, lothc decodes straight into a
validated pydantic or msgspec type.

## Fast

On Linux and macOS, lothc's own native transport (the default) runs async `get(into=Struct)` at
about **2.2x bare pyreqwest** under load: 97,501 requests a second, each decoded into a validated
msgspec `Struct`. On its pyreqwest transport (Windows, `http2=True`, or `native="off"`), lothc
stays close to pyreqwest's own throughput.

![HTTP client throughput race — lothc's native transport finishes first, at more than twice pyreqwest's throughput, and other libraries take much longer](https://raw.githubusercontent.com/RogerThomas/lothc/main/assets/perf-race.svg)

50,000 requests at concurrency 100 on an AWS `c8a.xlarge`, validation included.
[Full numbers](https://rogerthomas.github.io/lothc/benchmarks/).

## Highlights

| | |
|---|---|
| **Typed decoding** | `into=` any type expression (`Pokemon`, `list[Pokemon]`, ...) gives back a validated pydantic `BaseModel` or msgspec `Struct`, not a `dict`. |
| **Two clients, one API** | `HTTPClient` (async) and `SyncHTTPClient` (sync) — identical surface. |
| **Every verb** | `get`, `post`, `put`, `patch`, `delete`, `head`, `options`, `download` (each returning a `Response`: `.data`, `.status`, `.headers`) — see [Verbs](https://rogerthomas.github.io/lothc/verbs/). |
| **Typed params, headers & forms** | A `BaseModel`/`Struct` for query params or headers (with `None`-field omission), or a body sent urlencoded (`data=`) or as a real multipart form (`form=`). |
| **Precise static types** | Every verb is paired `@overload`s, not a cast-laden generic — your editor knows the exact return type. |
| **SSE & streaming** | Spec-compliant SSE — automatic reconnect with `Last-Event-ID`, server `retry:` honored, typed decode (discriminated unions included) — plus raw or NDJSON-typed `stream_get`/`stream_post` — see [SSE](https://rogerthomas.github.io/lothc/sse/) / [Streaming](https://rogerthomas.github.io/lothc/streaming/). |
| **Retries with real backoff** | `max_retries`/`retry_methods`, `Retry-After`-aware — see [Retries](https://rogerthomas.github.io/lothc/retries/). |
| **Authentication** | Static `bearer_token`, a per-request-refreshed `bearer_auth`, `basic_auth`, or `default_headers` for anything else — plus a ready-made OAuth 2 client-credentials provider (`OAuthProvider`: mint, leeway-based renewal, refresh-then-mint fallback, optional on-disk token cache) — see [Authentication](https://rogerthomas.github.io/lothc/auth/). |
| **Cookies, redirects, proxy** | `cookie_store`, `follow_redirects`/`max_redirects`, `proxy=` — see [Networking](https://rogerthomas.github.io/lothc/networking/). |
| **A real error hierarchy** | `HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError` for no response, `HTTPResponseError` for 4xx/5xx (or `raise_for_status=False` to get those back as a `Response`) — see [Error handling](https://rogerthomas.github.io/lothc/errors/). |

## Development

See [Development](https://rogerthomas.github.io/lothc/development/).

## License

lothc is [MIT](https://github.com/RogerThomas/lothc/blob/main/LICENSE). It's built on
[pyreqwest](https://github.com/MarkusSintonen/pyreqwest) (Apache-2.0), which is built on the Rust
crate [reqwest](https://github.com/seanmonstar/reqwest) (MIT or Apache-2.0). Thanks to both.
