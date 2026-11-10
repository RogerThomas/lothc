#!yeet
"""The POC behaves like lothc's get()/post(): types, fields, errors, timeouts, wire bytes,
reconnects and cancellation."""

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from msgspec import Struct
from poc_client import PocClient

from lothc import (
    CaseInsensitiveDict,
    HTTPClient,
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    RequestInfo,
    Response,
)


class _Resp(Struct):
    status: str


class _Item(Struct):
    item_id: int
    note: str | None = None


class _Query(Struct):
    page: int
    tag: str | None = None
    tags: list[str] = []


class _Hdrs(Struct):
    x_request_id: str
    x_skip: str | None = None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: bytes, *, close: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Repeat", "a")
        self.send_header("X-Repeat", "b")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/echo"):
            seen = {
                "line": self.requestline,
                "headers": sorted((k.lower(), v) for k, v in self.headers.items()),
                "body": body.decode(),
            }
            self._reply(200, json.dumps(seen).encode())
        elif self.path == "/slow":
            threading.Event().wait(1.0)
            self._reply(200, b'{"status": "success"}')
        elif self.path == "/missing":
            self._reply(404, b'{"detail": "missing"}')
        elif self.path == "/close":
            self._reply(200, b'{"status": "success"}', close=True)
        elif self.path == "/silent-close":
            self._reply(200, b'{"status": "success"}')
            self.close_connection = True  # close without saying so: a stale keep-alive
        else:
            self._reply(200, b'{"status": "success"}')

    do_GET = _handle  # noqa: N815
    do_POST = _handle  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


async def _expect(exc: type[BaseException], coro: Any) -> BaseException:
    try:
        await coro
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


async def _wire_parity(url: str) -> None:
    """Same calls through today's lothc (pyreqwest) and the POC: same bytes on the wire."""
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
        ("get", {}, {}),
        (
            "get",
            {},
            {
                "params": {"a": "x y", "b": [1, 2], "c": True, "d": 1.5, "e": "ü&=*~"},
                "headers": {"X-Custom": "v"},
            },
        ),
        ("get", {}, {"params": {"z": 2}, "headers": {"User-Agent": "mine"}}),
        (
            "get",
            {},
            {"params": {"t": ("a", "b"), "n": -3, "u": 2**63, "big": 2**70, "neg": -(2**70)}},
        ),
        (
            "get",
            {},
            {"params": _Query(page=1, tags=["x", "y"]), "headers": _Hdrs(x_request_id="r")},
        ),
        ("get", {"bearer_token": "token"}, {}),
        ("get", {"bearer_token": "token"}, {"skip_auth": True}),
        ("get", {"basic_auth": ("user", "pa:ss")}, {}),
        ("post", {}, {"json": {"k": [1, 2.5, "s", None, True], "u": "ü"}}),
        ("post", {}, {"json": _Item(item_id=1)}),
        ("post", {}, {"json": [1e20, 0.1, -0.0]}),
    ]
    diffs = 0
    for verb, client_kwargs, call_kwargs in calls:
        params = call_kwargs.get("params")
        path = "/echo?q=1" if isinstance(params, dict) and "z" in params else "/echo"
        async with HTTPClient(base_url=url, **client_kwargs) as lothc:
            want = json.loads((await getattr(lothc, verb)(path, **call_kwargs)).data)
        async with PocClient(url, **client_kwargs) as poc:
            got = json.loads((await getattr(poc, verb)(path, **call_kwargs)).data)
        # The POC doesn't decompress yet, so it doesn't advertise accept-encoding.
        want_headers = [h for h in want["headers"] if h[0] != "accept-encoding"]
        if want["body"] != got["body"] and json.loads(want["body"]) == json.loads(got["body"]):
            # Known: dict/list `json=` floats format differently (pyreqwest `1e+20`, msgspec
            # `1e20`); same JSON value. Compare the rest byte for byte.
            print(f"known float-format difference: {want['body']} vs {got['body']}")
            want_headers = [h for h in want_headers if h[0] != "content-length"]
            got["headers"] = [h for h in got["headers"] if h[0] != "content-length"]
            got["body"] = want["body"]
        if (want["line"], want_headers, want["body"]) != (got["line"], got["headers"], got["body"]):
            diffs += 1
            print(
                f"WIRE DIFF {verb} {client_kwargs} {call_kwargs}\n  lothc: {want}\n  poc:   {got}"
            )
    assert diffs == 0, f"{diffs} wire differences"
    print("wire parity: identical request line, headers (minus accept-encoding) and body")


async def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    await _wire_parity(url)
    async with PocClient(url) as client:
        resp = await client.get("/item", params={"page": 2}, into=_Resp)
        assert type(resp) is Response and resp.data == _Resp("success"), resp
        assert resp.status == 200 and resp.http_version == "HTTP/1.1" and resp.elapsed > 0
        assert resp.request == RequestInfo("GET", f"{url}item?page=2", "/item", "127.0.0.1"), (
            resp.request
        )
        assert resp.headers["Content-Type"] == "application/json"
        assert resp.headers.get_all("x-repeat") == ["a", "b"]
        assert type(resp.headers) is CaseInsensitiveDict
        assert (await client.get("/")).data == b'{"status": "success"}'
        e = await _expect(HTTPResponseError, client.get("/missing", into=_Resp))
        assert e.status == 404 and e.error == {"detail": "missing"} and e.request.path == "/missing"
        assert (await client.get("/missing", error_for_status=False)).status == 404
        await _expect(HTTPTimeoutError, client.get("/slow", timeout=0.1))
        assert (await asyncio.gather(*(client.get("/", into=_Resp) for _ in range(50))))[
            -1
        ].data.status == "success"
        # A short deadline arriving after a long one must re-arm the shared timer earlier.
        t0 = time.monotonic()
        long, short = await asyncio.gather(
            client.get("/slow", timeout=5.0),
            client.get("/slow", timeout=0.1),
            return_exceptions=True,
        )
        assert isinstance(long, Response) and isinstance(short, HTTPTimeoutError), (long, short)
        assert time.monotonic() - t0 < 1.5
        # Server closes after each response, said (Connection: close) and unsaid (stale keep-alive).
        for path in ("/close", "/silent-close"):
            for _ in range(20):
                assert (await client.get(path, into=_Resp)).data.status == "success"
                await asyncio.sleep(0.005 if path == "/silent-close" else 0)
        # Caller cancellation, in flight and while queued; the pool keeps working.
        await _expect(TimeoutError, asyncio.wait_for(client.get("/slow"), 0.1))
        assert (await client.get("/", into=_Resp)).data.status == "success"
    async with PocClient(url) as client:
        busy = asyncio.ensure_future(client.get("/slow"))
        await asyncio.sleep(0.05)
        # Only one connection is open and busy, so this one queues; cancel it there.
        await _expect(TimeoutError, asyncio.wait_for(client.get("/slow"), 0.05))
        assert (await busy).status == 200
    # Timing out while still queued for a connection: a connect that never completes.
    async with PocClient(url) as client:
        t0 = time.monotonic()
        busy, queued = await asyncio.gather(
            client.get("/slow", timeout=5.0),
            client.get("/slow", timeout=0.1),
            return_exceptions=True,
        )
        assert isinstance(busy, Response) and isinstance(queued, HTTPTimeoutError), (busy, queued)
    # Nothing listening: HTTPConnectionError, as from lothc today.
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    async with PocClient(f"http://127.0.0.1:{port}/") as client:
        await _expect(HTTPConnectionError, client.get("/"))
    # A server that closes mid-request: HTTPConnectionError.
    bad = socket.socket()
    bad.bind(("127.0.0.1", 0))
    bad.listen()
    threading.Thread(target=lambda: bad.accept()[0].close(), daemon=True).start()
    async with PocClient(f"http://127.0.0.1:{bad.getsockname()[1]}/") as client:
        await _expect(HTTPConnectionError, client.get("/"))
    print("correctness: all checks passed")
