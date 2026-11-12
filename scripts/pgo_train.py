#!yeet
"""The PGO training workload for `lothc._native`, which `scripts/build_wheel.sh` runs on an
instrumented wheel: the paths requests take through the native transport, async and sync, over
HTTP and HTTPS, against `tests/_server.py`'s server. Error paths too, since PGO makes whatever
training never ran slower.

`certs` is the directory `scripts/pgo_certs.py` wrote; `requests` scales the hot path, which
dominates the profile as it dominates real use.
"""

import asyncio
import importlib.util
import socket
import ssl
import sys
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import cast
from uuid import uuid4

from msgspec import Struct

from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    HTTPTransportError,
    SyncHTTPClient,
)


class _Item(Struct):
    id: int
    name: str


@dataclass(slots=True)
class _Targets:
    http: str
    https: str
    ca_pem: bytes
    scratch: Path
    # How often each path off the hot one runs: enough that none is left untrained.
    rounds: int
    # `/large`'s body (a multiple of its 64KB writes): big enough to spend its time reading.
    large: int = 16 * 1024 * 1024
    # `/large` closes its connection without saying so, and a request reusing it would race
    # that close, so its requests say so instead.
    close: dict[str, str] = field(default_factory=lambda: {"Connection": "close"})
    # Nothing listens on port 1, so connecting is refused.
    refused: str = "http://127.0.0.1:1/"


class _TLSServer(ThreadingHTTPServer):
    """`tests/_tls_server.py`'s server, which can't be imported here: it imports trustme. The
    handshake runs in each connection's handler thread, and one that fails (the untrusted-CA
    requests below) is dropped silently."""

    def __init__(self, handler: type[BaseHTTPRequestHandler], context: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self._context = context

    def finish_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError(f"Expected a stream socket, got {type(request)!r}")
        try:
            tls_socket = self._context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, ConnectionError):
            return
        with tls_socket:
            super().finish_request(tls_socket, client_address)

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)


def _test_server_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_server", Path(__file__).parent.parent / "tests" / "_server.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("tests/_server.py not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _serve(certs: Path, rounds: int) -> Generator[_Targets]:
    module = _test_server_module()
    handler = cast(type[BaseHTTPRequestHandler], module.TestAppHandler)
    # Without it, Linux holds each response body back ~40ms, until the client's delayed ACK of
    # the headers (Nagle's algorithm): 25 requests a second. Real servers set it too.
    handler.disable_nagle_algorithm = True
    make_server = cast(Callable[[], ThreadingHTTPServer], module.make_server)
    serve_in_background = cast(
        Callable[[ThreadingHTTPServer], AbstractContextManager[ThreadingHTTPServer]],
        module.serve_in_background,
    )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certs / "server.pem")
    with (
        serve_in_background(make_server()) as http,
        serve_in_background(_TLSServer(handler, context)) as https,
        TemporaryDirectory() as scratch,
    ):
        yield _Targets(
            f"http://127.0.0.1:{http.server_port}/",
            # The name the certificate is verified against, as in the tests.
            f"https://localhost:{https.server_port}/",
            (certs / "ca.pem").read_bytes(),
            Path(scratch),
            rounds,
        )


def _key() -> str:
    """A fresh key for the server's per-key state (`/flaky`'s attempt counter and the like)."""
    return str(uuid4())


async def _async_requests(client: HTTPClient, count: int) -> None:
    for i in range(count):
        item = (await client.get(f"items/{i}", into=_Item)).data
        _ = await client.get("items", params={"q": item.name, "page": i}, into=dict)
        _ = await client.post("items", json={"id": i, "name": item.name}, into=_Item)
        response = await client.get("echo-headers", headers={"x-request": item.name}, into=dict)
        _ = response.headers["content-type"]


async def _async_concurrent_requests(client: HTTPClient, count: int, tasks: int = 8) -> None:
    await asyncio.gather(*(_async_requests(client, count // tasks) for _ in range(tasks)))


async def _async_other_requests(client: HTTPClient, targets: _Targets) -> None:
    for _ in range(targets.rounds):
        for codec in ("gzip", "deflate", "br", "zstd", "gzip-large"):
            _ = await client.get("compressed", params={"codec": codec}, into=dict)
        _ = await client.put("items/1", json={"name": "name"}, into=dict)
        _ = await client.patch("items/1", json={"name": "name"}, into=dict)
        _ = await client.delete("items/1", into=dict)
        _ = await client.head("items/1")
        _ = await client.options("items")
        _ = await client.post("echo-body", data={"a": "1", "b": ["2", "3"]}, into=dict)
        _ = await client.post("echo-body", content=b"content", into=str)
        _ = await client.post("upload", form={"field": "value", "file": ("a.txt", b"a")})
        _ = await client.get("redirect", into=_Item)
        _ = await client.get("connection-close")
        _ = await client.get("boom", error_for_status=False)


async def _async_events(client: HTTPClient, targets: _Targets) -> None:
    """Over HTTP only: the test server ends a TLS connection without a close_notify, which an
    event stream (ended by the close) rightly reads as cut off, and reconnects 3s later."""
    for _ in range(targets.rounds):
        _ = [event async for event in client.sse("events", params={"count": 20, "interval": 0})]


async def _async_streaming(client: HTTPClient, targets: _Targets) -> None:
    large = {"key": _key(), "size": targets.large}
    close = targets.close
    for _ in range(targets.rounds):
        _ = [line async for line in client.stream_get("ndjson", params={"count": 100}, into=dict)]
    for _ in range(2):
        _ = [chunk async for chunk in client.stream_get("large", params=large, headers=close)]
        _ = await client.download("large", targets.scratch / "a", params=large, headers=close)
        _ = await client.download("large", params=large, headers=close)


async def _async_errors(client: HTTPClient, targets: _Targets) -> None:
    for _ in range(targets.rounds):
        for path in ("boom", "unauthorized", "missing"):
            with suppress(HTTPResponseError):
                _ = await client.get(path, into=dict)
        with suppress(ValueError):
            _ = await client.get("invalid-json", into=dict)
        with suppress(HTTPTimeoutError):
            _ = await client.get("slow", params={"seconds": 0.05}, timeout=0.005)
        with suppress(HTTPConnectionError):
            _ = await client.get(targets.refused)
        # `silent-close` closes its connection unannounced, so the next request on it may find
        # it closed or race the close.
        for path in ("truncated", "redirect-loop", "silent-close", "items/1"):
            with suppress(HTTPTransportError):
                _ = await client.get(path)


async def _async_retries(targets: _Targets) -> None:
    async with HTTPClient(
        base_url=targets.http, max_retries=2, backoff_base=0.001, native="on"
    ) as client:
        for _ in range(targets.rounds):
            for path in ("flaky", "connection-flaky", "body-flaky"):
                _ = await client.get(path, params={"key": _key(), "fail_times": 1}, into=dict)
            _ = await client.get("retry-after", params={"key": _key()}, into=dict)


async def _async_limits(targets: _Targets) -> None:
    """A connection limit callers queue for, and a cookie jar."""
    async with HTTPClient(
        base_url=targets.http, max_connections=2, cookie_store=True, native="on"
    ) as client:
        await _async_concurrent_requests(client, targets.rounds * 8)
        for _ in range(targets.rounds):
            _ = await client.get("set-cookie")
            _ = await client.get("read-cookie", into=dict)


async def _async_stalls(targets: _Targets) -> None:
    """An idle limit that gives up on a stream gone quiet."""
    async with HTTPClient(base_url=targets.http, read_timeout=0.01, native="on") as client:
        for _ in range(targets.rounds):
            with suppress(HTTPTimeoutError):
                stream = client.stream_get("stall-after-first-chunk", params={"seconds": 0.05})
                _ = [chunk async for chunk in stream]


async def _async_untrusted(targets: _Targets) -> None:
    async with HTTPClient(base_url=targets.https, native="on") as client:
        for _ in range(targets.rounds):
            with suppress(HTTPConnectionError):
                _ = await client.get("items/1")


async def _train_async(targets: _Targets, requests: int) -> None:
    async with HTTPClient(base_url=targets.http, native="on") as client:
        await _async_concurrent_requests(client, requests)
        await _async_requests(client, requests // 8)
        await _async_other_requests(client, targets)
        await _async_streaming(client, targets)
        await _async_events(client, targets)
        await _async_errors(client, targets)
    async with HTTPClient(
        base_url=targets.https,
        root_certificates=[targets.ca_pem],
        native="on",
    ) as client:
        await _async_concurrent_requests(client, requests // 4)
        await _async_other_requests(client, targets)
        await _async_streaming(client, targets)
    await _async_retries(targets)
    await _async_limits(targets)
    await _async_stalls(targets)
    await _async_untrusted(targets)


def _sync_requests(client: SyncHTTPClient, count: int) -> None:
    for i in range(count):
        item = client.get(f"items/{i}", into=_Item).data
        _ = client.get("items", params={"q": item.name, "page": i}, into=dict)
        _ = client.post("items", json={"id": i, "name": item.name}, into=_Item)
        response = client.get("echo-headers", headers={"x-request": item.name}, into=dict)
        _ = response.headers["content-type"]


def _sync_concurrent_requests(client: SyncHTTPClient, count: int, threads: int = 4) -> None:
    with ThreadPoolExecutor(max_workers=threads) as pool:
        _ = list(pool.map(partial(_sync_requests, client), [count // threads] * threads))


def _sync_other_requests(client: SyncHTTPClient, targets: _Targets) -> None:
    for _ in range(targets.rounds):
        for codec in ("gzip", "deflate", "br", "zstd", "gzip-large"):
            _ = client.get("compressed", params={"codec": codec}, into=dict)
        _ = client.put("items/1", json={"name": "name"}, into=dict)
        _ = client.patch("items/1", json={"name": "name"}, into=dict)
        _ = client.delete("items/1", into=dict)
        _ = client.head("items/1")
        _ = client.options("items")
        _ = client.post("echo-body", data={"a": "1", "b": ["2", "3"]}, into=dict)
        _ = client.post("echo-body", content=b"content", into=str)
        _ = client.post("upload", form={"field": "value", "file": ("a.txt", b"a")})
        _ = client.get("redirect", into=_Item)
        _ = client.get("connection-close")
        _ = client.get("boom", error_for_status=False)


def _sync_events(client: SyncHTTPClient, targets: _Targets) -> None:
    """Over HTTP only, as `_async_events`."""
    for _ in range(targets.rounds):
        _ = list(client.sse("events", params={"count": 20, "interval": 0}))


def _sync_streaming(client: SyncHTTPClient, targets: _Targets) -> None:
    large = {"key": _key(), "size": targets.large}
    close = targets.close
    for _ in range(targets.rounds):
        _ = list(client.stream_get("ndjson", params={"count": 100}, into=dict))
    for _ in range(2):
        _ = list(client.stream_get("large", params=large, headers=close))
        _ = client.download("large", targets.scratch / "s", params=large, headers=close)
        _ = client.download("large", params=large, headers=close)


def _sync_errors(client: SyncHTTPClient, targets: _Targets) -> None:
    for _ in range(targets.rounds):
        for path in ("boom", "unauthorized", "missing"):
            with suppress(HTTPResponseError):
                _ = client.get(path, into=dict)
        with suppress(ValueError):
            _ = client.get("invalid-json", into=dict)
        with suppress(HTTPTimeoutError):
            _ = client.get("slow", params={"seconds": 0.05}, timeout=0.005)
        with suppress(HTTPConnectionError):
            _ = client.get(targets.refused)
        for path in ("truncated", "redirect-loop", "silent-close", "items/1"):
            with suppress(HTTPTransportError):
                _ = client.get(path)


def _sync_retries(targets: _Targets) -> None:
    with SyncHTTPClient(
        base_url=targets.http, max_retries=2, backoff_base=0.001, native="on"
    ) as client:
        for _ in range(targets.rounds):
            for path in ("flaky", "connection-flaky", "body-flaky"):
                _ = client.get(path, params={"key": _key(), "fail_times": 1}, into=dict)
            _ = client.get("retry-after", params={"key": _key()}, into=dict)


def _sync_limits(targets: _Targets) -> None:
    """A connection limit callers queue for, and a cookie jar."""
    with SyncHTTPClient(
        base_url=targets.http, max_connections=2, cookie_store=True, native="on"
    ) as client:
        _sync_concurrent_requests(client, targets.rounds * 8)
        for _ in range(targets.rounds):
            _ = client.get("set-cookie")
            _ = client.get("read-cookie", into=dict)


def _sync_stalls(targets: _Targets) -> None:
    """An idle limit that gives up on a stream gone quiet."""
    with SyncHTTPClient(base_url=targets.http, read_timeout=0.01, native="on") as client:
        for _ in range(targets.rounds):
            with suppress(HTTPTimeoutError):
                _ = list(client.stream_get("stall-after-first-chunk", params={"seconds": 0.05}))


def _sync_untrusted(targets: _Targets) -> None:
    with SyncHTTPClient(base_url=targets.https, native="on") as client:
        for _ in range(targets.rounds):
            with suppress(HTTPConnectionError):
                _ = client.get("items/1")


def _train_sync(targets: _Targets, requests: int) -> None:
    with SyncHTTPClient(base_url=targets.http, native="on") as client:
        _sync_concurrent_requests(client, requests)
        _sync_requests(client, requests // 8)
        _sync_other_requests(client, targets)
        _sync_streaming(client, targets)
        _sync_events(client, targets)
        _sync_errors(client, targets)
    with SyncHTTPClient(
        base_url=targets.https,
        root_certificates=[targets.ca_pem],
        native="on",
    ) as client:
        _sync_concurrent_requests(client, requests // 4)
        _sync_other_requests(client, targets)
        _sync_streaming(client, targets)
    _sync_retries(targets)
    _sync_limits(targets)
    _sync_stalls(targets)
    _sync_untrusted(targets)


async def main(*, certs: Path, requests: int = 5000) -> None:
    with _serve(certs, max(requests // 100, 5)) as targets:
        await _train_async(targets, requests)
        # On a thread of its own, as a sync caller has no loop to run on.
        await asyncio.to_thread(_train_sync, targets, requests)
