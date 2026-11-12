#!yeet
"""Sync spike: SyncPocClient behaves like lothc's SyncHTTPClient."""

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from correctness import _Handler, _Item, _Query, _Resp
from http.server import ThreadingHTTPServer
from poc_client import SyncPocClient

from lothc import (
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    RequestInfo,
    Response,
    SyncHTTPClient,
)


def _raises(exc: type[BaseException], fn, *args, **kwargs) -> BaseException:  # noqa: ANN001
    try:
        fn(*args, **kwargs)
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/"
    calls = [
        ("get", {}, {"params": {"a": "x y", "b": [1, 2], "c": True}, "headers": {"X-Custom": "v"}}),
        ("get", {"bearer_token": "token"}, {"params": _Query(page=1, tags=["x"])}),
        ("post", {"basic_auth": ("user", "pass")}, {"json": _Item(item_id=1)}),
    ]
    for verb, ckw, kw in calls:
        with SyncHTTPClient(base_url=url, **ckw) as lothc:
            want = json.loads(getattr(lothc, verb)("/echo", **kw).data)
        with SyncPocClient(url, **ckw) as poc:
            got = json.loads(getattr(poc, verb)("/echo", **kw).data)
        want["headers"] = [h for h in want["headers"] if h[0] != "accept-encoding"]
        assert want == got, (want, got)
    print("sync wire parity: identical")
    with SyncPocClient(url) as client:
        resp = client.get("/item", params={"page": 2}, into=_Resp)
        assert type(resp) is Response and resp.data == _Resp("success") and resp.status == 200
        assert resp.request == RequestInfo("GET", f"{url}item?page=2", "/item", "127.0.0.1")
        assert resp.headers.get_all("x-repeat") == ["a", "b"]
        e = _raises(HTTPResponseError, client.get, "/missing", into=_Resp)
        assert e.status == 404 and e.error == {"detail": "missing"}
        _raises(HTTPTimeoutError, client.get, "/slow", timeout=0.1)
        for path in ("/close", "/silent-close"):
            for _ in range(20):
                assert client.get(path, into=_Resp).data.status == "success"
                time.sleep(
                    0.005 if path == "/silent-close" else 0
                )  # as the async test: let the FIN land
        # Threads sharing one client (the GIL is released while waiting on the socket).
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(lambda _: client.get("/", into=_Resp).data.status, range(400)))
        assert results == ["success"] * 400
        # While one thread waits on /slow, others still get through (GIL released).
        slow = threading.Thread(target=client.get, args=("/slow",))
        slow.start()
        assert client.get("/", into=_Resp).data.status == "success" and slow.is_alive()
        slow.join()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    with SyncPocClient(f"http://127.0.0.1:{port}/") as client:
        _raises(HTTPConnectionError, client.get, "/")
    print("sync correctness: all checks passed")
