"""Temporary CI diagnostic (to be removed): sleep accuracy, connect latency and /events gaps."""

import socket
import sys
import time

sys.path.insert(0, "tests")
from _server import TestAppHandler, make_server, serve_in_background  # noqa: E402

from lothc import SyncHTTPClient  # noqa: E402


def _summary(samples: list[float]) -> str:
    samples = sorted(samples)
    return f"median {samples[len(samples) // 2]:.4f} max {samples[-1]:.4f} n {len(samples)}"


def _sleeps() -> None:
    samples = []
    for _ in range(30):
        started = time.perf_counter()
        time.sleep(0.02)
        samples.append(time.perf_counter() - started)
    print(f"sleep(0.02): {_summary(samples)}")


def _connects(port: int) -> None:
    samples = []
    for _ in range(50):
        started = time.perf_counter()
        with socket.create_connection(("127.0.0.1", port)):
            samples.append(time.perf_counter() - started)
    print(f"connect: {_summary(samples)}")


def _raw_gaps(port: int, label: str) -> None:
    sock = socket.create_connection(("127.0.0.1", port))
    sock.settimeout(3)
    sock.sendall(b"GET /events?interval=0.02&count=12 HTTP/1.1\r\nHost: x\r\n\r\n")
    last = time.perf_counter()
    gaps = []
    while data := sock.recv(65536):
        now = time.perf_counter()
        gaps.append(now - last)
        last = now
    sock.close()
    print(f"{label} raw gaps: {_summary(gaps[1:])}")


def _client_gaps(port: int, label: str) -> None:
    with SyncHTTPClient(base_url=f"http://127.0.0.1:{port}/", timeout=5) as client:
        last = time.perf_counter()
        gaps = []
        for _ in client.stream_get("events", params={"interval": 0.02, "count": 12}):
            now = time.perf_counter()
            gaps.append(now - last)
            last = now
    print(f"{label} pyreqwest gaps: {_summary(gaps[1:])}")


_sleeps()
for nodelay in (False, True, False, True):
    TestAppHandler.disable_nagle_algorithm = nodelay
    with serve_in_background(make_server()) as server:
        if not nodelay:
            _connects(server.server_port)
        _raw_gaps(server.server_port, f"nodelay={nodelay}")
        _client_gaps(server.server_port, f"nodelay={nodelay}")
