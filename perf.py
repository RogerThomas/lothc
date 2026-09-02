#!yeet
"""Performance test comparing HTTP client libraries against the axum JSON server."""

import asyncio
import json
import logging
import resource
import time
import tracemalloc
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args, overload

import aiohttp
import aiosonic
import httpx
import httpx2
import niquests
import requests
from aiosonic.pools import PoolConfig
from msgspec import Struct
from pydantic import BaseModel
from pyreqwest.client import Client as PyreqwestClient
from pyreqwest.client import ClientBuilder
from pyreqwest.client import SyncClient as PyreqwestSyncClient
from pyreqwest.client import SyncClientBuilder as PyreqwestSyncClientBuilder
from rich.console import Console
from rich.measure import Measurement
from rich.table import Table

from lothc import HTTPClient, SyncHTTPClient

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)

console = Console()

type Lib = Literal[
    "httpx",
    "httpx_h2",
    "httpx2",
    "pyreqwest",
    "aiohttp",
    "niquests",
    "aiosonic",
    "requests",
    "lothc",
    "lothc-msgspec",
    "lothc-pydantic",
]

type Tag = Literal["rust", "async", "web", "performance"]

# The two response shapes below both mirror benchmarks/json_server/src/main.rs's actual JSON
# body exactly (nested user/data/pagination included) — the point of the lothc-msgspec/
# lothc-pydantic rows is to measure the cost of a FULLY validated, fully typesafe decode (real
# struct/model construction, real field validation), not just a bare `dict` with no validation
# at all.


class _MsgspecMetadata(Struct):
    last_login: str
    login_count: int
    active: bool


class _MsgspecUser(Struct):
    id: int
    name: str
    email: str
    roles: list[str]
    metadata: _MsgspecMetadata


class _MsgspecNested(Struct):
    depth: int
    values: list[int]


class _MsgspecDataItem(Struct):
    id: int
    title: str
    tags: list[Tag]
    score: float
    nested: _MsgspecNested


class _MsgspecPagination(Struct):
    page: int
    limit: int
    total: int
    has_more: bool


class MsgspecResponse(Struct):
    status: str
    timestamp: str
    user: _MsgspecUser
    data: list[_MsgspecDataItem]
    pagination: _MsgspecPagination


class _PydanticMetadata(BaseModel):
    last_login: str
    login_count: int
    active: bool


class _PydanticUser(BaseModel):
    id: int
    name: str
    email: str
    roles: list[str]
    metadata: _PydanticMetadata


class _PydanticNested(BaseModel):
    depth: int
    values: list[int]


class _PydanticDataItem(BaseModel):
    id: int
    title: str
    tags: list[Tag]
    score: float
    nested: _PydanticNested


class _PydanticPagination(BaseModel):
    page: int
    limit: int
    total: int
    has_more: bool


class PydanticResponse(BaseModel):
    status: str
    timestamp: str
    user: _PydanticUser
    data: list[_PydanticDataItem]
    pagination: _PydanticPagination


@dataclass
class AiosonicClient:
    """Wraps aiosonic.HTTPClient so it exposes the same `get(path)` shape as
    every other client here — aiosonic has no base_url concept of its own."""

    client: aiosonic.HTTPClient
    base_url: str

    async def get(self, path: str) -> aiosonic.HttpResponse:
        return await self.client.get(self.base_url + path)

    async def __aenter__(self) -> "AiosonicClient":
        await self.client.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        # aiosonic.HTTPClient.__aexit__ itself is unannotated upstream (it even
        # carries its own `# type: ignore`) — nothing to type on our side here.
        await self.client.__aexit__(*args)  # pyright: ignore[reportUnknownMemberType]


@dataclass
class RequestsClient:
    """Wraps requests.Session so it exposes the same `get(path)` shape as every other
    client here — requests has no base_url concept of its own."""

    session: requests.Session
    base_url: str

    def get(self, path: str) -> requests.Response:
        return self.session.get(self.base_url + path)

    def __enter__(self) -> "RequestsClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.session.close()


type AnyClient = (
    HTTPClient
    | httpx.AsyncClient
    | httpx2.AsyncClient
    | PyreqwestClient
    | aiohttp.ClientSession
    | niquests.AsyncSession
    | AiosonicClient
)

# aiohttp and aiosonic have no sync client at all — they're async-only libraries — so `sync=True`
# is never valid for them (see the dispatch in `_run_one`). `requests` is the mirror-image case —
# sync-only, no async client — so it's excluded from `_parse_libs` instead (see there).
type AnySyncClient = (
    SyncHTTPClient
    | httpx.Client
    | httpx2.Client
    | niquests.Session
    | PyreqwestSyncClient
    | RequestsClient
)


class Stats(TypedDict):
    lib: Lib
    sync: bool
    total_time: float
    throughput: float
    cpu_time: float
    peak_mem_mb: float
    min: float
    p50: float
    p95: float
    p99: float
    max: float
    mean: float


async def _fetch_one_lothc(client: HTTPClient, path: str) -> None:
    data = await client.get(path, response_data_type=dict)
    assert "status" in data


async def _fetch_one_lothc_msgspec(client: HTTPClient, path: str) -> None:
    data = await client.get(path, response_data_type=MsgspecResponse)
    assert data.status == "success"


async def _fetch_one_lothc_pydantic(client: HTTPClient, path: str) -> None:
    data = await client.get(path, response_data_type=PydanticResponse)
    assert data.status == "success"


def _fetch_one_lothc_sync(client: SyncHTTPClient, path: str) -> None:
    data = client.get(path, response_data_type=dict)
    assert "status" in data


def _fetch_one_lothc_msgspec_sync(client: SyncHTTPClient, path: str) -> None:
    data = client.get(path, response_data_type=MsgspecResponse)
    assert data.status == "success"


def _fetch_one_lothc_pydantic_sync(client: SyncHTTPClient, path: str) -> None:
    data = client.get(path, response_data_type=PydanticResponse)
    assert data.status == "success"


def _fetch_one_httpx_sync(client: httpx.Client, path: str) -> None:
    resp = client.get(path)
    data = resp.json()
    assert "status" in data


def _fetch_one_httpx2_sync(client: httpx2.Client, path: str) -> None:
    resp = client.get(path)
    data = resp.json()
    assert "status" in data


def _fetch_one_niquests_sync(client: niquests.Session, path: str) -> None:
    resp = client.get(path)
    data = resp.json()
    assert "status" in data


def _fetch_one_pyreqwest_sync(client: PyreqwestSyncClient, path: str) -> None:
    resp = client.get(path).build().send()
    data = resp.json()
    assert "status" in data


def _fetch_one_requests_sync(client: RequestsClient, path: str) -> None:
    resp = client.get(path)
    data = resp.json()
    assert "status" in data


async def _fetch_one_httpx(client: httpx.AsyncClient, path: str) -> None:
    resp = await client.get(path)
    data = resp.json()
    assert "status" in data


async def _fetch_one_httpx2(client: httpx2.AsyncClient, path: str) -> None:
    resp = await client.get(path)
    data = resp.json()
    assert "status" in data


async def _fetch_one_pyreqwest(client: PyreqwestClient, path: str) -> None:
    resp = await client.get(path).build().send()
    data = await resp.json()
    assert "status" in data


async def _fetch_one_aiohttp(client: aiohttp.ClientSession, path: str) -> None:
    async with client.get(path) as resp:
        data = await resp.json()
        assert "status" in data


async def _fetch_one_niquests(client: niquests.AsyncSession, path: str) -> None:
    resp = await client.get(path)
    data = resp.json()
    assert "status" in data


async def _fetch_one_aiosonic(client: AiosonicClient, path: str) -> None:
    resp = await client.get(path)
    # aiosonic.HttpResponse.json's `json_decoder` default param is unannotated
    # upstream, so both the member access and its return type are only
    # inferable as Unknown — cast to the real shape rather than leak that in.
    data = cast(
        "dict[str, object]",
        await resp.json(),  # pyright: ignore[reportUnknownMemberType]
    )
    assert "status" in data


@overload
def _build_client(
    lib: Literal["lothc", "lothc-msgspec", "lothc-pydantic"],
    url: str,
    concurrency: int,
) -> AbstractAsyncContextManager[HTTPClient]: ...
@overload
def _build_client(
    lib: Literal["httpx"], url: str, concurrency: int
) -> AbstractAsyncContextManager[httpx.AsyncClient]: ...
@overload
def _build_client(
    lib: Literal["httpx_h2"], url: str, concurrency: int
) -> AbstractAsyncContextManager[httpx.AsyncClient]: ...
@overload
def _build_client(
    lib: Literal["httpx2"], url: str, concurrency: int
) -> AbstractAsyncContextManager[httpx2.AsyncClient]: ...
@overload
def _build_client(
    lib: Literal["pyreqwest"], url: str, concurrency: int
) -> AbstractAsyncContextManager[PyreqwestClient]: ...
@overload
def _build_client(
    lib: Literal["aiohttp"], url: str, concurrency: int
) -> AbstractAsyncContextManager[aiohttp.ClientSession]: ...
@overload
def _build_client(
    lib: Literal["niquests"], url: str, concurrency: int
) -> AbstractAsyncContextManager[niquests.AsyncSession]: ...
@overload
def _build_client(
    lib: Literal["aiosonic"], url: str, concurrency: int
) -> AbstractAsyncContextManager[AiosonicClient]: ...
def _build_client(lib: Lib, url: str, concurrency: int) -> AbstractAsyncContextManager[AnyClient]:
    """Return an async context manager yielding a ready-to-use client for `lib`."""
    if lib in ("lothc", "lothc-msgspec", "lothc-pydantic"):
        return HTTPClient.build(base_url=url)

    if lib == "httpx":
        return httpx.AsyncClient(base_url=url)

    if lib == "httpx_h2":
        # NB: over plain HTTP (no TLS/ALPN, no h2c) this negotiates down to
        # HTTP/1.1 anyway — see the caveat printed in the results table.
        return httpx.AsyncClient(base_url=url, http2=True)

    if lib == "httpx2":
        return httpx2.AsyncClient(base_url=url)

    if lib == "pyreqwest":
        return ClientBuilder().base_url(url).build()

    if lib == "aiohttp":
        return aiohttp.ClientSession(base_url=url)

    if lib == "niquests":
        return niquests.AsyncSession(base_url=url)

    if lib == "aiosonic":
        # aiosonic's default pool size (30) is well under typical benchmark
        # concurrency, forcing it to keep opening fresh connections instead of
        # reusing pooled ones — at high total_requests that exhausts the
        # container's local ephemeral port range (OSError: Cannot assign
        # requested address). Size the pool to the concurrency under test,
        # matching what httpx/aiohttp already default to (100).
        connector = aiosonic.TCPConnector(pool_configs={":default": PoolConfig(size=concurrency)})
        return AiosonicClient(aiosonic.HTTPClient(connector), url)

    raise ValueError(f"Unknown lib: {lib}")


@overload
def _build_sync_client(
    lib: Literal["lothc", "lothc-msgspec", "lothc-pydantic"], url: str
) -> AbstractContextManager[SyncHTTPClient]: ...
@overload
def _build_sync_client(lib: Literal["httpx"], url: str) -> AbstractContextManager[httpx.Client]: ...
@overload
def _build_sync_client(
    lib: Literal["httpx2"], url: str
) -> AbstractContextManager[httpx2.Client]: ...
@overload
def _build_sync_client(
    lib: Literal["niquests"], url: str
) -> AbstractContextManager[niquests.Session]: ...
@overload
def _build_sync_client(
    lib: Literal["pyreqwest"], url: str
) -> AbstractContextManager[PyreqwestSyncClient]: ...
@overload
def _build_sync_client(
    lib: Literal["requests"], url: str
) -> AbstractContextManager[RequestsClient]: ...
def _build_sync_client(lib: Lib, url: str) -> AbstractContextManager[AnySyncClient]:
    """Return a sync context manager yielding a ready-to-use blocking client for `lib`. Same lib
    names as `_build_client` (its async counterpart) — sync-vs-async is conveyed by which of the
    two you call, not by the name."""
    if lib in ("lothc", "lothc-msgspec", "lothc-pydantic"):
        return SyncHTTPClient.build(base_url=url)

    if lib == "httpx":
        return httpx.Client(base_url=url)

    if lib == "httpx2":
        return httpx2.Client(base_url=url)

    if lib == "niquests":
        return niquests.Session(base_url=url)

    if lib == "pyreqwest":
        return PyreqwestSyncClientBuilder().base_url(url).build()

    if lib == "requests":
        return RequestsClient(requests.Session(), url)

    raise ValueError(f"{lib!r} has no sync client")


async def _fetch_one[ClientT](
    fetcher: Callable[[ClientT, str], Awaitable[None]], client: ClientT
) -> float:
    """Fetch once and return elapsed time."""
    start = time.perf_counter()
    await fetcher(client, "/")
    return time.perf_counter() - start


async def _pool_worker[ClientT](
    fetcher: Callable[[ClientT, str], Awaitable[None]],
    client: ClientT,
    queue: asyncio.Queue[None],
    timings: list[float],
) -> None:
    while not queue.empty():
        queue.get_nowait()
        timings.append(await _fetch_one(fetcher, client))


async def _run_pool[ClientT](
    fetcher: Callable[[ClientT, str], Awaitable[None]],
    client: ClientT,
    total_requests: int,
    concurrency: int,
) -> list[float]:
    """Run `concurrency` workers that each grab the next request as soon as
    their previous one completes — no waiting on batch stragglers."""
    queue: asyncio.Queue[None] = asyncio.Queue()
    for _ in range(total_requests):
        queue.put_nowait(None)

    timings: list[float] = []
    await asyncio.gather(
        *(_pool_worker(fetcher, client, queue, timings) for _ in range(concurrency))
    )
    return timings


async def _time_run[ClientT](
    fetcher: Callable[[ClientT, str], Awaitable[None]],
    client: ClientT,
    total_requests: int,
    concurrency: int,
    warmup: int,
) -> tuple[list[float], float, float]:
    """Time the real (untimed-warmup-excluded) pass. CPU time is measured here
    too — `getrusage` is a cheap syscall snapshot, not per-allocation tracking,
    so it doesn't skew throughput/latency the way memory profiling would."""
    if warmup:
        await _run_pool(fetcher, client, warmup, concurrency)
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    start_time = time.perf_counter()
    timings = await _run_pool(fetcher, client, total_requests, concurrency)
    total_time = time.perf_counter() - start_time
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    cpu_time = (cpu_after.ru_utime + cpu_after.ru_stime) - (
        cpu_before.ru_utime + cpu_before.ru_stime
    )
    return timings, total_time, cpu_time


async def _measure_peak_memory[ClientT](
    fetcher: Callable[[ClientT, str], Awaitable[None]],
    client: ClientT,
    total_requests: int,
    concurrency: int,
) -> float:
    """Re-run the same workload with tracemalloc active and return peak traced
    Python-level allocation in MB. Deliberately a separate pass — tracemalloc
    hooks every allocation, which would otherwise skew the timed pass above.
    Only tracks CPython object allocations, so Rust/C-side heap use (e.g.
    pyreqwest's) isn't reflected — this measures Python-side wrapper overhead,
    not total process memory."""
    tracemalloc.start()
    try:
        await _run_pool(fetcher, client, total_requests, concurrency)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak / (1024 * 1024)


def _fetch_one_sync[ClientT](fetcher: Callable[[ClientT, str], None], client: ClientT) -> float:
    """Fetch once (sync, no event loop involved) and return elapsed time."""
    start = time.perf_counter()
    fetcher(client, "/")
    return time.perf_counter() - start


def _run_sequential_sync[ClientT](
    fetcher: Callable[[ClientT, str], None], client: ClientT, total_requests: int
) -> list[float]:
    """Run `total_requests` calls back-to-back, one at a time — no pool, no concurrency."""
    return [_fetch_one_sync(fetcher, client) for _ in range(total_requests)]


def _time_run_sequential_sync[ClientT](
    fetcher: Callable[[ClientT, str], None],
    client: ClientT,
    total_requests: int,
    warmup: int,
) -> tuple[list[float], float, float]:
    if warmup:
        _run_sequential_sync(fetcher, client, warmup)
    cpu_before = resource.getrusage(resource.RUSAGE_SELF)
    start_time = time.perf_counter()
    timings = _run_sequential_sync(fetcher, client, total_requests)
    total_time = time.perf_counter() - start_time
    cpu_after = resource.getrusage(resource.RUSAGE_SELF)
    cpu_time = (cpu_after.ru_utime + cpu_after.ru_stime) - (
        cpu_before.ru_utime + cpu_before.ru_stime
    )
    return timings, total_time, cpu_time


def _measure_peak_memory_sequential_sync[ClientT](
    fetcher: Callable[[ClientT, str], None], client: ClientT, total_requests: int
) -> float:
    tracemalloc.start()
    try:
        _run_sequential_sync(fetcher, client, total_requests)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak / (1024 * 1024)


def _parse_libs(libs: str) -> list[Lib]:
    """Restricted to the libs that actually have an async client — see `_build_client`.
    `requests` is sync-only (no async client at all — see `RequestsClient`/`_build_sync_client`)
    and would otherwise slip through as a valid `Lib` value, only to fail confusingly inside
    `_build_client`'s own fallback `raise`."""
    valid: tuple[Lib, ...] = tuple(
        lib for lib in cast("tuple[Lib, ...]", get_args(Lib.__value__)) if lib != "requests"
    )
    result: list[Lib] = []
    for name in libs.split():
        if name not in valid:
            raise ValueError(f"Unknown lib: {name!r} (expected one of {valid})")
        result.append(name)
    return result


def _parse_sync_libs(libs: str) -> list[Lib]:
    """Like `_parse_libs`, but restricted to the libs that actually have a sync client — see
    `_build_sync_client`. aiohttp/aiosonic/httpx_h2/lothc's decode-target variants otherwise being
    valid `Lib` values would let them slip through `_parse_libs`'s wider check."""
    valid: tuple[Lib, ...] = (
        "httpx",
        "httpx2",
        "niquests",
        "pyreqwest",
        "requests",
        "lothc",
        "lothc-msgspec",
        "lothc-pydantic",
    )
    result: list[Lib] = []
    for name in libs.split():
        if name not in valid:
            raise ValueError(f"Unknown sync-capable lib: {name!r} (expected one of {valid})")
        result.append(name)
    return result


async def single_call(
    url: str = "http://127.0.0.1:3000",
    *,
    libs: str = (
        "httpx httpx2 pyreqwest aiohttp niquests aiosonic lothc lothc-msgspec lothc-pydantic"
    ),
    concurrency: int = 1,
    total_requests: int = 1000,
    warmup: int = 100,
) -> None:
    """Async sequential: concurrency fixed at 1 against each of `libs`'s ASYNC clients — one
    non-concurrent request at a time, so each timing is the raw wall time (t2-t1) of a single
    request in isolation, rather than a pooled-concurrency throughput figure. See `main` for the
    pooled-concurrency comparison, `single_call_sync` for the blocking-sync-client equivalent.
    `concurrency` is accepted but ignored — always sequential — purely so `task perf-all` can pass
    one shared --concurrency flag across all three tests without erroring on the two that don't
    use it."""
    _ = concurrency  # CLI-parity-only param, see docstring — intentionally unused
    run_id = str(int(time.time() * 1000))
    console.print(
        f"[bold]Running[/bold] {total_requests} sequential requests (plus {warmup} warm-up) "
        f"against [bold yellow]{url}[/bold yellow] [dim](run {run_id})[/dim]\n"
    )

    results = [await _run_one(lib, url, 1, total_requests, warmup) for lib in _parse_libs(libs)]

    _print_results_table(results, title="Single-Call Comparison — Async Sequential")

    out_path = _write_results_json(
        results, run_id, url, 1, total_requests, warmup, prefix="single-call-"
    )
    console.print(f"\n[dim]Results written to {out_path}[/dim]")


async def single_call_sync(
    url: str = "http://127.0.0.1:3000",
    *,
    libs: str = ("httpx httpx2 niquests requests pyreqwest lothc lothc-msgspec lothc-pydantic"),
    concurrency: int = 1,
    total_requests: int = 1000,
    warmup: int = 100,
) -> None:
    """Sync sequential: one non-concurrent request at a time against each of `libs`'s blocking SYNC
    clients — no event loop involved. aiohttp and aiosonic have no sync client so aren't included.
    Same lib names as `perf`/`single_call` (e.g. "niquests", never "niquests-sync") — this
    test/table is what marks them as the sync variant. See `single_call` for the async-sequential
    equivalent, `main` for the pooled-concurrency comparison. `concurrency` is accepted but
    ignored — always sequential — purely so `task perf-all` can pass one shared --concurrency flag
    across all three tests without erroring on the two that don't use it."""
    _ = concurrency  # CLI-parity-only param, see docstring — intentionally unused
    run_id = str(int(time.time() * 1000))
    console.print(
        f"[bold]Running[/bold] {total_requests} sequential requests (plus {warmup} warm-up) "
        f"against [bold yellow]{url}[/bold yellow] [dim](run {run_id})[/dim]\n"
    )

    results = [
        await _run_one(lib, url, 1, total_requests, warmup, sync=True)
        for lib in _parse_sync_libs(libs)
    ]

    _print_results_table(results, title="Single-Call Comparison — Sync Sequential")

    out_path = _write_results_json(
        results, run_id, url, 1, total_requests, warmup, prefix="single-call-sync-"
    )
    console.print(f"\n[dim]Results written to {out_path}[/dim]")


def _run_sync_lib(
    lib: Lib, url: str, total_requests: int, warmup: int
) -> tuple[list[float], float, float, float]:
    """The sync half of `_run_one`'s dispatch, split out to keep both under the project's mccabe
    complexity ceiling — see `_run_one` for why sync-vs-async is a flag, not a separate lib name.
    Returns (timings, total_time, cpu_time, peak_mem_mb)."""
    if lib in ("lothc", "lothc-msgspec", "lothc-pydantic"):
        fetcher = {
            "lothc": _fetch_one_lothc_sync,
            "lothc-msgspec": _fetch_one_lothc_msgspec_sync,
            "lothc-pydantic": _fetch_one_lothc_pydantic_sync,
        }[lib]
        with _build_sync_client(lib, url) as client:
            timings, total_time, cpu_time = _time_run_sequential_sync(
                fetcher, client, total_requests, warmup
            )
            peak_mem_mb = _measure_peak_memory_sequential_sync(fetcher, client, total_requests)
        return timings, total_time, cpu_time, peak_mem_mb

    if lib == "httpx":
        with _build_sync_client(lib, url) as client:
            timings, total_time, cpu_time = _time_run_sequential_sync(
                _fetch_one_httpx_sync, client, total_requests, warmup
            )
            peak_mem_mb = _measure_peak_memory_sequential_sync(
                _fetch_one_httpx_sync, client, total_requests
            )
        return timings, total_time, cpu_time, peak_mem_mb

    if lib == "httpx2":
        with _build_sync_client(lib, url) as client:
            timings, total_time, cpu_time = _time_run_sequential_sync(
                _fetch_one_httpx2_sync, client, total_requests, warmup
            )
            peak_mem_mb = _measure_peak_memory_sequential_sync(
                _fetch_one_httpx2_sync, client, total_requests
            )
        return timings, total_time, cpu_time, peak_mem_mb

    if lib == "niquests":
        with _build_sync_client(lib, url) as client:
            timings, total_time, cpu_time = _time_run_sequential_sync(
                _fetch_one_niquests_sync, client, total_requests, warmup
            )
            peak_mem_mb = _measure_peak_memory_sequential_sync(
                _fetch_one_niquests_sync, client, total_requests
            )
        return timings, total_time, cpu_time, peak_mem_mb

    if lib == "pyreqwest":
        with _build_sync_client(lib, url) as client:
            timings, total_time, cpu_time = _time_run_sequential_sync(
                _fetch_one_pyreqwest_sync, client, total_requests, warmup
            )
            peak_mem_mb = _measure_peak_memory_sequential_sync(
                _fetch_one_pyreqwest_sync, client, total_requests
            )
        return timings, total_time, cpu_time, peak_mem_mb

    if lib == "requests":
        with _build_sync_client(lib, url) as client:
            timings, total_time, cpu_time = _time_run_sequential_sync(
                _fetch_one_requests_sync, client, total_requests, warmup
            )
            peak_mem_mb = _measure_peak_memory_sequential_sync(
                _fetch_one_requests_sync, client, total_requests
            )
        return timings, total_time, cpu_time, peak_mem_mb

    raise ValueError(f"{lib!r} has no sync client (aiohttp/aiosonic/httpx_h2 are async-only)")


async def _run_one(
    lib: Lib, url: str, concurrency: int, total_requests: int, warmup: int, *, sync: bool = False
) -> Stats:
    """Run the perf test once for a single library and return its stats. `sync=True` uses `lib`'s
    blocking sync client instead of its async one (aiohttp/aiosonic have none — see
    `_build_sync_client`) — always sequential regardless of `concurrency` (only meaningful via
    `single_call_sync`). The lib name is the same either way (e.g. "niquests", never "niquests-
    sync") since sync-vs-async is conveyed by which test/table this is, not by the name."""
    with console.status(f"[bold green]Running {lib}{' (sync)' if sync else ''}..."):
        if sync:
            timings, total_time, cpu_time, peak_mem_mb = _run_sync_lib(
                lib, url, total_requests, warmup
            )
        elif lib == "lothc":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_lothc, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_lothc, client, total_requests, concurrency
                )
        elif lib == "lothc-msgspec":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_lothc_msgspec, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_lothc_msgspec, client, total_requests, concurrency
                )
        elif lib == "lothc-pydantic":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_lothc_pydantic, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_lothc_pydantic, client, total_requests, concurrency
                )
        elif lib == "httpx" or lib == "httpx_h2":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_httpx, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_httpx, client, total_requests, concurrency
                )
        elif lib == "httpx2":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_httpx2, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_httpx2, client, total_requests, concurrency
                )
        elif lib == "pyreqwest":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_pyreqwest, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_pyreqwest, client, total_requests, concurrency
                )
        elif lib == "aiohttp":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_aiohttp, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_aiohttp, client, total_requests, concurrency
                )
        elif lib == "aiosonic":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_aiosonic, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_aiosonic, client, total_requests, concurrency
                )
        elif lib == "requests":
            # `requests` is sync-only — no async client at all (see `RequestsClient`) — so it
            # must never reach here in practice (`_parse_libs` excludes it); this branch exists
            # only so the type checker can narrow the trailing `else` back down to "niquests"
            # instead of "niquests" | "requests".
            raise ValueError(f"{lib!r} has no async client (it's sync-only, see _parse_libs)")
        else:
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_niquests, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_niquests, client, total_requests, concurrency
                )

    timings = sorted(timings)
    return {
        "lib": lib,
        "sync": sync,
        "total_time": total_time,
        "throughput": total_requests / total_time,
        "cpu_time": cpu_time,
        "peak_mem_mb": peak_mem_mb,
        "min": timings[0],
        "p50": timings[len(timings) // 2],
        "p95": timings[int(len(timings) * 0.95)],
        "p99": timings[int(len(timings) * 0.99)],
        "max": timings[-1],
        "mean": sum(timings) / len(timings),
    }


def _print_table_unwrapped(console: Console, table: Table) -> None:
    """Print `table` at its natural width so no cell is ever wrapped or ellipsised.

    rich sizes a table to the console it's printed on and, when that's too narrow, either
    wraps cell text onto extra lines or (with `no_wrap`) truncates it with an ellipsis — both
    make a numeric results table unreadable. Non-TTY runs (e.g. `docker compose run`, CI logs)
    are the worst case: rich assumes 80 columns there. Measuring against an effectively
    unbounded width and printing through a console at least that wide sidesteps both.
    """
    natural_width = Measurement.get(console, console.options.update_width(10_000), table).maximum
    original_width = console.width
    console.width = max(original_width, natural_width)
    try:
        console.print(table)
    finally:
        console.width = original_width


def _print_results_table(results: list[Stats], *, title: str = "Perf Comparison") -> None:
    ordered = sorted(results, key=lambda r: r["throughput"])
    slowest = ordered[0]["throughput"]

    table = Table(title=title)
    table.add_column("Library", style="cyan", no_wrap=True)
    table.add_column("Total Time (s)", justify="right", no_wrap=True)
    table.add_column("Throughput (req/s)", justify="right", style="bold green", no_wrap=True)
    table.add_column("Relative (x)", justify="right", style="bold yellow", no_wrap=True)
    table.add_column("CPU (s)", justify="right", style="blue", no_wrap=True)
    table.add_column("Peak Py Mem (MB)", justify="right", style="blue", no_wrap=True)
    table.add_column("Min (ms)", justify="right", no_wrap=True)
    table.add_column("P50 (ms)", justify="right", no_wrap=True)
    table.add_column("P95 (ms)", justify="right", no_wrap=True)
    table.add_column("P99 (ms)", justify="right", no_wrap=True)
    table.add_column("Max (ms)", justify="right", no_wrap=True)
    table.add_column("Mean (ms)", justify="right", style="magenta", no_wrap=True)

    for r in ordered:
        table.add_row(
            r["lib"],
            f"{r['total_time']:.2f}",
            f"{r['throughput']:,.0f}",
            f"{r['throughput'] / slowest:.2f}",
            f"{r['cpu_time']:.2f}",
            f"{r['peak_mem_mb']:.2f}",
            f"{r['min'] * 1000:.2f}",
            f"{r['p50'] * 1000:.2f}",
            f"{r['p95'] * 1000:.2f}",
            f"{r['p99'] * 1000:.2f}",
            f"{r['max'] * 1000:.2f}",
            f"{r['mean'] * 1000:.2f}",
        )

    _print_table_unwrapped(console, table)
    if any(r["lib"] == "httpx_h2" for r in results):
        console.print(
            "[dim]Note: httpx_h2 negotiates HTTP/2 only over TLS/ALPN or h2c — "
            "against this plain-HTTP axum server it silently falls back to "
            "HTTP/1.1, so its numbers should match plain httpx.[/dim]"
        )


def _write_results_json(
    results: list[Stats],
    run_id: str,
    url: str,
    concurrency: int,
    total_requests: int,
    warmup: int,
    *,
    prefix: str = "",
) -> Path:
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{prefix}{run_id}.json"
    payload = {
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(run_id) / 1000)),
        "url": url,
        "concurrency": concurrency,
        "total_requests": total_requests,
        "warmup": warmup,
        "results": results,
    }
    out_path.write_text(json.dumps(payload, indent=2))
    return out_path


async def main(
    url: str = "http://127.0.0.1:3000",
    *,
    libs: str = (
        "httpx httpx2 pyreqwest aiohttp niquests aiosonic lothc lothc-msgspec lothc-pydantic"
    ),
    concurrency: int = 50,
    total_requests: int = 1000,
    warmup: int = 100,
) -> None:
    """Run perf test with specified concurrency against each of `libs`, sequentially."""
    run_id = str(int(time.time() * 1000))
    console.print(
        f"[bold]Running[/bold] {total_requests} requests (plus {warmup} warm-up) with "
        f"[bold cyan]concurrency={concurrency}[/bold cyan] against "
        f"[bold yellow]{url}[/bold yellow] [dim](run {run_id})[/dim]\n"
    )

    results = [
        await _run_one(lib, url, concurrency, total_requests, warmup) for lib in _parse_libs(libs)
    ]

    _print_results_table(results)

    out_path = _write_results_json(results, run_id, url, concurrency, total_requests, warmup)
    console.print(f"\n[dim]Results written to {out_path}[/dim]")
