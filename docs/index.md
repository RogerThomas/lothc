---
icon: lucide/rocket
---

<p align="center">
  <img src="assets/logo-light.svg" alt="lothc logo" width="300" class="logo-light-only">
  <img src="assets/logo-dark.svg" alt="lothc logo" width="300" class="logo-dark-only">
</p>

# lothc

**L**ord **O**f **T**he **H**ttp **C**lients — a fast, Rust-backed, typed HTTP client for Python,
with first-class optional support for **pydantic** and **msgspec** as both decode *and* encode
targets.

## Installing

```
uv add 'lothc[pydantic,msgspec]'
```

Any subset of the extras works — the base package alone gets you `bytes`/`dict` support.

Wheels cover Linux (glibc and musl) and macOS on x86_64 and Arm, and Windows on x86_64, for
CPython 3.13 to 3.15 and free-threaded 3.14t and 3.15t; anywhere else, installing builds from
source, which needs a Rust toolchain. Windows uses the pyreqwest transport, not the
[native one](networking.md#native-transport).

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
        async with HTTPClient(base_url="https://pokeapi.co/api/v2/") as client:
            response = await client.get("pokemon/pikachu", into=Pokemon)
            print(response.status, response.data)  # 200 Pokemon(id=25, name='pikachu')


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
        async with HTTPClient(base_url="https://pokeapi.co/api/v2/") as client:
            response = await client.get("pokemon/pikachu", into=Pokemon)
            print(response.status, response.data)  # 200 id=25 name='pikachu'


    asyncio.run(main())
    ```

=== "dict"

    ```python {data-uv-extra=""}
    import asyncio
    from lothc import HTTPClient


    async def main() -> None:
        async with HTTPClient(base_url="https://pokeapi.co/api/v2/") as client:
            response = await client.get("pokemon/pikachu", into=dict)
            print(response.status, response.data)  # 200 {'id': 25, 'name': 'pikachu', ...}


    asyncio.run(main())
    ```

    !!! warning

        `into=dict` is parsed JSON, unvalidated.

`SyncHTTPClient` mirrors every method in these docs one-for-one — swap `async with` for `with`,
drop the `await`s, and everything still applies.

## Why?

[pyreqwest](https://github.com/MarkusSintonen/pyreqwest) is a genuinely great Rust-backed HTTP
client, and the inspiration for lothc. Its API is a builder (`client.get(url).build()` before you
can `.send()`), so lothc gives it the `requests`/`httpx` call shape, and adds its own native
transport, faster still. pyreqwest stays lothc's transport on Windows, for HTTP/2 and with
`native="off"`.

Where other clients' `.json()` returns an unvalidated `dict`, lothc decodes straight into a
validated pydantic or msgspec type.

## Performance

On Linux and macOS, lothc's [native transport](networking.md#native-transport) (the default there)
runs async `get(into=Struct)` at about **2.2x bare pyreqwest**, validation included.

![HTTP client throughput race — lothc's native transport finishes first, at more than twice pyreqwest's throughput, and other libraries take much longer](assets/perf-race.svg){: .perf-race-img }

See [Benchmarks](benchmarks.md) for the full numbers.

## Highlights

<div class="grid cards" markdown>

-   **Type-safe I/O boundaries**

    `into` takes a pydantic `BaseModel`, a msgspec `Struct`, plain `dict`, raw `bytes` (the
    default), or any type built from them (`list[Item]`, `Item | None`), and hands back a
    validated instance instead of a `dict` to check yourself. Every verb is paired `@overload`s,
    so every call site gets a precise static type.

-   **Typed request bodies too**

    `json=` isn't limited to a plain `dict` — pass a pydantic `BaseModel` or msgspec `Struct`
    instance directly and it's encoded for you. Pydantic and msgspec are decode *and* encode
    targets in lothc, not just decode.

-   **Every verb**

    `get`, `post`, `put`, `patch`, `delete`, `head`, `options`, `download` (each returning a `Response`: `.data`, `.status`, `.headers`) — plus `sse`,
    `stream_get`, and `stream_post` for streaming responses. See [Verbs](verbs.md).

-   **Streaming, both ways**

    Raw chunks by default (unbuffered, safe for large binary bodies), or switch to newline-
    buffered, typed NDJSON-style decoding. See [Streaming](streaming.md).

-   **SSE support**

    Spec-compliant `sse()`: automatic reconnect with `Last-Event-ID`, the server's `retry:`
    honored, and each event decoded into your type, discriminated unions included. See
    [SSE](sse.md).

-   **Retries with real backoff**

    Exponential backoff that honors `Retry-After`, on the idempotent verbs by default. See
    [Retries](retries.md).

-   **Authentication**

    A static `bearer_token`, `bearer_auth` for a token resolved fresh on every request, or
    `basic_auth`; `OAuthProvider` handles OAuth 2 client credentials (with refresh and an optional
    on-disk cache). Plus `default_headers` for anything else that needs to go out on every request.
    See [Authentication](auth.md).

-   **Cookies, redirects, proxy**

    An in-memory cookie jar, redirect control, proxying and TLS. See [Networking](networking.md).

-   **A real error hierarchy**

    `HTTPTransportError`/`HTTPTimeoutError`/`HTTPConnectionError` for failures with no response,
    and a separate `HTTPResponseError` for 4xx/5xx. See [Error handling](errors.md).

</div>
