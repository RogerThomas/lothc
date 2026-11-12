#!yeet
"""Python twin of the reqwest bench: same pool loop, same JSON-line output."""

import asyncio
import json
import resource
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from msgspec import Struct
from poc_client import POC_BARE, PocClient
from pyreqwest.client import Client, ClientBuilder

from lothc import HTTPClient

type Impl = Literal[
    "pyreqwest-bytes",
    "pyreqwest-json",
    "lothc-bytes",
    "lothc-dict",
    "lothc-msgspec",
    "lothc-msgspec-params",
    "poc-bytes",
    "poc-msgspec",
    "poc-msgspec-params",
]


class _Resp(Struct):
    status: str


async def _pyreqwest_bytes(c: Client) -> None:
    resp = await c.get("/").build().send()
    assert await resp.bytes()


async def _pyreqwest_json(c: Client) -> None:
    resp = await c.get("/").build().send()
    assert "status" in await resp.json()


async def _lothc_bytes(c: HTTPClient) -> None:
    assert (await c.get("/")).data


async def _lothc_dict(c: HTTPClient) -> None:
    assert "status" in (await c.get("/", into=dict)).data


async def _lothc_msgspec(c: HTTPClient) -> None:
    assert (await c.get("/", into=_Resp)).data.status == "success"


async def _poc_bytes(c: PocClient) -> None:
    assert (await c.get("/")).data


async def _poc_msgspec(c: PocClient) -> None:
    resp = await c.get("/", into=_Resp)
    if POC_BARE:
        assert resp.status == "success"
    else:
        assert resp.data.status == "success"


async def _poc_msgspec_params(c: PocClient) -> None:
    resp = await c.get("/", params={"page": 1, "tag": "a"}, headers={"x-id": "1"}, into=_Resp)
    assert resp.data.status == "success"


async def _lothc_msgspec_params(c: HTTPClient) -> None:
    resp = await c.get("/", params={"page": 1, "tag": "a"}, headers={"x-id": "1"}, into=_Resp)
    assert resp.data.status == "success"


async def _worker(
    fetch: Callable[[Any], Awaitable[None]], c: Any, left: list[int], lat: list[float]
) -> None:
    while left[0] > 0:
        left[0] -= 1
        t = time.perf_counter()
        await fetch(c)
        lat.append(time.perf_counter() - t)


async def _pool(
    fetch: Callable[[Any], Awaitable[None]], c: Any, total: int, concurrency: int
) -> list[float]:
    left, lat = [total], []
    await asyncio.gather(*(_worker(fetch, c, left, lat) for _ in range(concurrency)))
    return lat


def _cpu() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


async def _bench(
    impl: Impl, c: Any, url: str, concurrency: int, requests: int, warmup: int, runs: int
) -> None:
    fetch = {
        "pyreqwest-bytes": _pyreqwest_bytes,
        "pyreqwest-json": _pyreqwest_json,
        "lothc-bytes": _lothc_bytes,
        "lothc-dict": _lothc_dict,
        "lothc-msgspec": _lothc_msgspec,
        "poc-bytes": _poc_bytes,
        "poc-msgspec": _poc_msgspec,
        "poc-msgspec-params": _poc_msgspec_params,
        "lothc-msgspec-params": _lothc_msgspec_params,
    }[impl]
    await _pool(fetch, c, warmup, concurrency)
    for run in range(runs):
        cpu0, t0 = _cpu(), time.perf_counter()
        lat = sorted(await _pool(fetch, c, requests, concurrency))
        wall, cpu = time.perf_counter() - t0, _cpu() - cpu0
        pct = lambda p: lat[round((len(lat) - 1) * p)] * 1e6  # noqa: E731
        print(
            json.dumps({
                "impl": impl,
                "concurrency": concurrency,
                "run": run,
                "requests": requests,
                "rps": round(requests / wall),
                "cpu_us_per_req": round(cpu / requests * 1e6, 2),
                "p50_us": round(pct(0.5), 1),
                "p99_us": round(pct(0.99), 1),
                "max_us": round(pct(1.0), 1),
            }),
            flush=True,
        )


async def main(
    impl: Impl,
    *,
    url: str = "http://json-server:3000/",
    concurrency: int = 1,
    requests: int = 20_000,
    warmup: int = 1_000,
    runs: int = 5,
) -> None:
    if impl.startswith("poc"):
        async with PocClient(url) as c:
            await _bench(impl, c, url, concurrency, requests, warmup, runs)
    elif impl.startswith("pyreqwest"):
        async with ClientBuilder().base_url(url).build() as c:
            await _bench(impl, c, url, concurrency, requests, warmup, runs)
    else:
        async with HTTPClient(base_url=url) as c:
            await _bench(impl, c, url, concurrency, requests, warmup, runs)
