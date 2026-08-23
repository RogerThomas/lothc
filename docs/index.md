---
icon: lucide/rocket
---

<p align="center">
  <img src="assets/logo-light.svg" alt="lothc logo" width="300" class="logo-light-only">
  <img src="assets/logo-dark.svg" alt="lothc logo" width="300" class="logo-dark-only">
</p>

# lothc

**L**ord **O**f **T**he **H**ttp **C**lients — a typed HTTP client for Python, built on
[pyreqwest](https://github.com/mostafa-hussein/pyreqwest) (an awesome Rust-backed HTTP client),
with first-class optional support for **pydantic** and **msgspec** as both decode *and* encode
targets, plus **TypedDict** (validated via **typeguard** if installed) as a decode target.

## Installing

```
uv add 'lothc[pydantic,msgspec,typeguard]'
```

Any subset of the extras works — the base package alone gets you `bytes`/`JSON`/`TypedDict`-without-validation
support.

## Quickstart

Every snippet below is a complete, runnable script — hit the extra clipboard icon in its
top-right corner to copy a one-liner that runs it via `uv run`, no local install needed.

=== "msgspec"

    ```python {data-uv-extra="msgspec"}
    import asyncio
    from lothc import HTTPClient
    from msgspec import Struct


    class Pokemon(Struct):
        id: int
        name: str


    async def main() -> None:
        async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
            pikachu = await client.get("pokemon/pikachu", response_data_type=Pokemon)
            print(pikachu)  # Pokemon(id=25, name='pikachu')


    asyncio.run(main())
    ```

=== "pydantic"

    ```python {data-uv-extra="pydantic"}
    import asyncio
    from lothc import HTTPClient
    from pydantic import BaseModel


    class Pokemon(BaseModel):
        id: int
        name: str


    async def main() -> None:
        async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
            pikachu = await client.get("pokemon/pikachu", response_data_type=Pokemon)
            print(pikachu)  # id=25 name='pikachu'


    asyncio.run(main())
    ```

=== "TypedDict"

    ```python {data-uv-extra="typeguard"}
    import asyncio
    from lothc import HTTPClient
    from typing import TypedDict


    class Pokemon(TypedDict):
        id: int
        name: str


    async def main() -> None:
        async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
            pikachu = await client.get("pokemon/pikachu", response_data_type=Pokemon)
            print(pikachu)  # {'id': 25, 'name': 'pikachu'}


    asyncio.run(main())
    ```

    !!! warning

        Install the `typeguard` extra to get the `TypedDict` fields validated at runtime —
        without it, the dict is returned as-is, matching the type hint on trust alone. If
        typeguard isn't installed, a warning is raised on first use; set
        `LOTHC_SUPPRESS_TYPEGUARD_WARNING=1` to silence it. The `uv run` copy button above
        installs it, so the copied command runs fully validated. Only the fields you declare are
        checked — a response with extra, undeclared fields (like the real PokéAPI response
        above) still validates fine, and every field it returns, declared or not, comes back in
        the result.

=== "JSON"

    ```python {data-uv-extra=""}
    import asyncio
    from lothc import JSON, HTTPClient


    async def main() -> None:
        async with HTTPClient.build(base_url="https://pokeapi.co/api/v2/") as client:
            pikachu = await client.get("pokemon/pikachu", response_data_type=JSON)
            print(pikachu)  # {'id': 25, 'name': 'pikachu', ...}


    asyncio.run(main())
    ```

    !!! warning

        `JSON` is a plain `dict[str, Any]` subclass — no schema, no extra dependency, just parsed
        JSON. There's no validation at all: the shape is fully trusted, on your say-so alone.

`SyncHTTPClient` mirrors every method in these docs one-for-one — swap `async with` for `with`,
drop the `await`s, and everything still applies.

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

## Performance

lothc is built on top of the awesome [pyreqwest](https://github.com/mostafa-hussein/pyreqwest)
package — a Rust-based HTTP client for Python. lothc then adds some nice abstractions on top of that
(discussed in the other sections of these docs): typed decode targets, retries, SSE, streaming,
and so on. Because of that, there's a bit of overhead compared to using pyreqwest directly —
however, this is almost negligible, as can be seen below: the heavy lifting still happens in
Rust, so lothc stays far closer to pyreqwest's throughput than to any pure-Python HTTP library's.

![HTTP client throughput race — lothc and pyreqwest finish in well under a fifth of a second, other libraries take much longer](assets/perf-race.svg){: .perf-race-img }

Benchmarked with `perf.py` against a tiny Rust-based static JSON server, 10,000 requests at
concurrency 100 — including lothc's fully-typed decode targets (`response_data_type=` a msgspec
`Struct`, a pydantic `BaseModel`, or a `TypedDict` validated via typeguard), not just raw bytes or
an untyped dict. The object handed back from those runs isn't just parsed JSON — it's a real,
constructed, field-validated instance of your own type, and that validation cost is included in
the numbers, not benchmarked around. Decoding into a real msgspec `Struct` even edged out the
unvalidated dict path in this run; typeguard's pure-Python validation was the one clear exception,
costing a real, visible slowdown. See [Benchmarks](benchmarks.md) for the full numbers behind the
chart above.

## Highlights

<div class="grid cards" markdown>

-   **Type-safe I/O boundaries, not bolted on**

    `response_data_type` gets you a real, constructed, field-validated pydantic `BaseModel`,
    msgspec `Struct`, or typeguard-validated `TypedDict` straight from the client, instead of the
    plain `dict` a `.json()` call leaves you to validate yourself. See
    [Benchmarks](benchmarks.md) for what that actually costs (usually nothing).

-   **Typed decode targets**

    Pick a pydantic `BaseModel`, a msgspec `Struct`, a `TypedDict`, `lothc.JSON`, or raw `bytes`
    (the default) per call. No cast-laden internals — every verb is built from paired
    `@overload`s, so every call site gets a precise static type.

-   **Typed request bodies too**

    `json=` isn't limited to a plain `dict` — pass a pydantic `BaseModel` or msgspec `Struct`
    instance directly and it's encoded for you. Pydantic and msgspec are decode *and* encode
    targets in lothc, not just decode.

-   **Every verb**

    `get`, `get_result`, `post`, `put`, `patch`, `delete`, `head`, `download` — plus `sse`,
    `stream_get`, and `stream_post` for streaming responses. See [Verbs](verbs.md).

-   **Typed *and* raw params/headers**

    Pass a plain `dict`/`Mapping`, or a `BaseModel`/`Struct` for free per-field validation and
    `None`-field omission.

-   **Streaming, both ways**

    Raw chunks by default (unbuffered, safe for large binary bodies), or switch to newline-
    buffered, typed NDJSON-style decoding. See [Streaming](streaming.md).

-   **SSE support**

    Decode targets accept a class, a pydantic `TypeAdapter`, or a prebuilt msgspec `Decoder` — so
    even discriminated-union event streams decode natively. See [SSE](sse.md).

-   **Retries with real backoff**

    Implemented as a real pyreqwest `with_middleware` hook. Honors `Retry-After`, defaults to the
    idempotent verbs. See [Retries](retries.md).

-   **Authentication**

    A static `bearer_token`, or `bearer_auth` for a token resolved fresh on every request —
    plus `default_headers` for anything else that needs to go out on every request. See
    [Authentication](auth.md).

-   **Cookies, redirects, proxy**

    An in-memory cookie jar, redirect control, and proxying. See
    [Cookies, redirects & proxy](networking.md).

-   **A real error hierarchy**

    `HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError` for failures with no response,
    and a separate `HTTPResponseError` for 4xx/5xx. See [Error handling](errors.md).

</div>

pydantic, msgspec, and typeguard are all optional — the library works with none, either, or all
three installed.
