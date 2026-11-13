#!yeet
"""Sync throughput: lothc's SyncHTTPClient vs the sync spike, `threads` sharing one client."""

import json
import resource
import threading
import time
from typing import Any, Literal

from msgspec import Struct
from poc_client import LoopThreadSyncClient, SyncPocClient

from lothc import SyncHTTPClient


class _Resp(Struct):
    status: str


def _cpu() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


def _run(client: Any, total: int, threads: int) -> list[float]:
    left, lat, lock = [total], [], threading.Lock()

    def work() -> None:
        while True:
            with lock:
                if left[0] <= 0:
                    return
                left[0] -= 1
            t = time.perf_counter()
            assert client.get("/", into=_Resp).data.status == "success"
            lat.append(time.perf_counter() - t)

    workers = [threading.Thread(target=work) for _ in range(threads)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return lat


def main(
    impl: Literal["lothc-sync", "poc-sync", "poc-loopthread"],
    *,
    url: str = "http://json-server:3000/",
    threads: int = 1,
    requests: int = 20_000,
    warmup: int = 1_000,
    runs: int = 3,
) -> None:
    if impl == "lothc-sync":
        client_cm: Any = SyncHTTPClient(base_url=url)
    elif impl == "poc-sync":
        client_cm = SyncPocClient(url)
    else:
        client_cm = LoopThreadSyncClient(url)
    with client_cm as client:
        _run(client, warmup, threads)
        for run in range(runs):
            cpu0, t0 = _cpu(), time.perf_counter()
            lat = sorted(_run(client, requests, threads))
            wall, cpu = time.perf_counter() - t0, _cpu() - cpu0
            print(
                json.dumps({
                    "impl": impl,
                    "concurrency": threads,
                    "run": run,
                    "requests": requests,
                    "rps": round(requests / wall),
                    "cpu_us_per_req": round(cpu / requests * 1e6, 2),
                    "p50_us": round(lat[len(lat) // 2] * 1e6, 1),
                    "p99_us": round(lat[int(len(lat) * 0.99)] * 1e6, 1),
                }),
                flush=True,
            )
