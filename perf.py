#!yeet
"""Performance test comparing HTTP client libraries against the axum JSON server."""

import asyncio
import json
import logging
import resource
import time
import tracemalloc
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, cast, get_args, overload

import aiohttp
import aiosonic
import httpx
import httpx2
import niquests
from aiosonic.pools import PoolConfig
from msgspec import Struct
from pydantic import BaseModel
from pyreqwest.client import Client as PyreqwestClient
from pyreqwest.client import ClientBuilder
from rich.console import Console
from rich.table import Table

from lothc import JSON, HTTPClient

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
    "lothc",
    "lothc-msgspec",
    "lothc-pydantic",
    "lothc-typeguard",
]

type Tag = Literal["rust", "async", "web", "performance"]

# The three response shapes below all mirror benchmarks/json_server/src/main.rs's actual JSON
# body exactly (nested user/data/pagination included) — the point of the lothc-msgspec/
# lothc-pydantic/lothc-typeguard rows is to measure the cost of a FULLY validated, fully
# typesafe decode (real struct/model construction, real field validation, for msgspec/pydantic/
# typeguard respectively), not just a bare `JSON` dict wrapper with no validation at all.


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


class _TypedDictMetadata(TypedDict):
    last_login: str
    login_count: int
    active: bool


class _TypedDictUser(TypedDict):
    id: int
    name: str
    email: str
    roles: list[str]
    metadata: _TypedDictMetadata


class _TypedDictNested(TypedDict):
    depth: int
    values: list[int]


class _TypedDictDataItem(TypedDict):
    id: int
    title: str
    tags: list[Tag]
    score: float
    nested: _TypedDictNested


class _TypedDictPagination(TypedDict):
    page: int
    limit: int
    total: int
    has_more: bool


class TypeguardResponse(TypedDict):
    status: str
    timestamp: str
    user: _TypedDictUser
    data: list[_TypedDictDataItem]
    pagination: _TypedDictPagination


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


type AnyClient = (
    HTTPClient
    | httpx.AsyncClient
    | httpx2.AsyncClient
    | PyreqwestClient
    | aiohttp.ClientSession
    | niquests.AsyncSession
    | AiosonicClient
)


class Stats(TypedDict):
    lib: Lib
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
    data = await client.get(path, response_data_type=JSON)
    assert "status" in data


async def _fetch_one_lothc_msgspec(client: HTTPClient, path: str) -> None:
    data = await client.get(path, response_data_type=MsgspecResponse)
    assert data.status == "success"


async def _fetch_one_lothc_pydantic(client: HTTPClient, path: str) -> None:
    data = await client.get(path, response_data_type=PydanticResponse)
    assert data.status == "success"


async def _fetch_one_lothc_typeguard(client: HTTPClient, path: str) -> None:
    data = await client.get(path, response_data_type=TypeguardResponse)
    assert data["status"] == "success"


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
    lib: Literal["lothc", "lothc-msgspec", "lothc-pydantic", "lothc-typeguard"],
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
    if lib in ("lothc", "lothc-msgspec", "lothc-pydantic", "lothc-typeguard"):
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


def _parse_libs(libs: str) -> list[Lib]:
    valid: tuple[Lib, ...] = get_args(Lib.__value__)
    result: list[Lib] = []
    for name in libs.split():
        if name not in valid:
            raise ValueError(f"Unknown lib: {name!r} (expected one of {valid})")
        result.append(name)
    return result


async def _run_one(lib: Lib, url: str, concurrency: int, total_requests: int, warmup: int) -> Stats:
    """Run the perf test once for a single library and return its stats."""
    with console.status(f"[bold green]Running {lib}..."):
        if lib == "lothc":
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
        elif lib == "lothc-typeguard":
            async with _build_client(lib, url, concurrency) as client:
                timings, total_time, cpu_time = await _time_run(
                    _fetch_one_lothc_typeguard, client, total_requests, concurrency, warmup
                )
                peak_mem_mb = await _measure_peak_memory(
                    _fetch_one_lothc_typeguard, client, total_requests, concurrency
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


def _print_results_table(results: list[Stats]) -> None:
    ordered = sorted(results, key=lambda r: r["throughput"])
    slowest = ordered[0]["throughput"]

    table = Table(title="Perf Comparison")
    table.add_column("Library", style="cyan")
    table.add_column("Total Time", justify="right")
    table.add_column("Throughput", justify="right", style="bold green")
    table.add_column("Relative", justify="right", style="bold yellow")
    table.add_column("CPU", justify="right", style="blue")
    table.add_column("Peak Py Mem", justify="right", style="blue")
    table.add_column("Min", justify="right")
    table.add_column("P50", justify="right")
    table.add_column("P95", justify="right")
    table.add_column("P99", justify="right")
    table.add_column("Max", justify="right")
    table.add_column("Mean", justify="right", style="magenta")

    for r in ordered:
        table.add_row(
            r["lib"],
            f"{r['total_time']:.3f}s",
            f"{r['throughput']:.1f} req/s",
            f"x{r['throughput'] / slowest:.1f}",
            f"{r['cpu_time']:.3f}s",
            f"{r['peak_mem_mb']:.2f}MB",
            f"{r['min'] * 1000:.3f}ms",
            f"{r['p50'] * 1000:.3f}ms",
            f"{r['p95'] * 1000:.3f}ms",
            f"{r['p99'] * 1000:.3f}ms",
            f"{r['max'] * 1000:.3f}ms",
            f"{r['mean'] * 1000:.3f}ms",
        )

    console.print(table)
    if any(r["lib"] == "httpx_h2" for r in results):
        console.print(
            "[dim]Note: httpx_h2 negotiates HTTP/2 only over TLS/ALPN or h2c — "
            "against this plain-HTTP axum server it silently falls back to "
            "HTTP/1.1, so its numbers should match plain httpx.[/dim]"
        )


def _write_results_json(
    results: list[Stats], run_id: str, url: str, concurrency: int, total_requests: int, warmup: int
) -> Path:
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{run_id}.json"
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
        "httpx httpx2 pyreqwest aiohttp niquests aiosonic lothc "
        "lothc-msgspec lothc-pydantic lothc-typeguard"
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
