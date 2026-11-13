#!yeet
"""Directional check, not a benchmark: CPU per request for lothc's own `get(into=Struct)` on the
pyreqwest transport vs `LOTHC_TRANSPORT=native`, at a fixed concurrency, runs interleaved.

Needs `benchmarks/json_server` listening (`cargo run --release` there, port 3000). CPU is
`getrusage(RUSAGE_SELF)` user+system, so pyreqwest's tokio threads are counted too.
"""

import asyncio
import os
import resource
import time
from typing import Literal

from msgspec import Struct

from lothc import HTTPClient

type Transport = Literal["pyreqwest", "native"]


class _Resp(Struct):
    status: str


def _cpu_seconds() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


async def _worker(client: HTTPClient, count: int) -> None:
    for _ in range(count):
        assert (await client.get("/", into=_Resp)).data.status == "success"


async def _measure(
    transport: Transport, url: str, requests: int, concurrency: int
) -> tuple[float, float]:
    """(CPU us per request, requests per second) for one run."""
    if transport == "native":
        os.environ["LOTHC_TRANSPORT"] = "native"
    else:
        os.environ.pop("LOTHC_TRANSPORT", None)
    client = HTTPClient(base_url=url)
    async with client:
        await asyncio.gather(*(_worker(client, 50) for _ in range(concurrency)))  # warm up
        per_worker = requests // concurrency
        cpu, wall = _cpu_seconds(), time.perf_counter()
        await asyncio.gather(*(_worker(client, per_worker) for _ in range(concurrency)))
        cpu, wall = _cpu_seconds() - cpu, time.perf_counter() - wall
    done = per_worker * concurrency
    return cpu / done * 1e6, done / wall


async def main(
    *,
    url: str = "http://127.0.0.1:3000/",
    requests: int = 20_000,
    concurrency: int = 10,
    runs: int = 3,
) -> None:
    results: dict[Transport, list[tuple[float, float]]] = {"pyreqwest": [], "native": []}
    for run in range(runs):
        for transport in ("pyreqwest", "native"):
            cpu_us, rate = await _measure(transport, url, requests, concurrency)
            results[transport].append((cpu_us, rate))
            print(f"run {run + 1} {transport:9} {cpu_us:6.1f} us CPU/request {rate:8.0f} req/s")
    for transport, samples in results.items():
        cpu = sorted(sample[0] for sample in samples)[len(samples) // 2]
        rate = sorted(sample[1] for sample in samples)[len(samples) // 2]
        print(f"median {transport:9} {cpu:6.1f} us CPU/request {rate:8.0f} req/s")
