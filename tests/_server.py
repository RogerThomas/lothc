"""A minimal stdlib-only HTTP test server — no ASGI framework, no jero.

Mirrors the handful of endpoints `examples/server.py` (the jero app used for
manual smoke-testing) exposes, purely for the automated test suite: `/items`
(GET/POST/PUT/PATCH/DELETE/HEAD), `/echo-headers`, `/slow`, `/events` (SSE),
`/boom`, plus cookie/redirect/retry scenarios used by their own tests.
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import sleep
from typing import ClassVar, cast
from urllib.parse import parse_qs, urlparse

_not_found_body = {
    "type": "not-found",
    "title": "Not found",
    "status": 404,
}
_server_error_body = {
    "type": "internal-server-error",
    "title": "Internal server error",
    "status": 500,
}


class TestAppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Per-key hit counters for the flaky/retry-after endpoints below — shared
    # across request threads for the lifetime of the test server.
    _counters: ClassVar[dict[str, int]] = {}

    def _write_json(
        self, status: int, payload: object, extra_headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else {}

    def _bump_counter(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def _write_sse_events(self, query: str) -> None:
        omit_id = parse_qs(query).get("omit", [""])[0] == "id"
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for index in range(25):
            data = json.dumps({"msg": f"hello {index}", "now": index * 100})
            record = "event: tick\n"
            if not omit_id:
                record += f"id: {index}\n"
            record += f"data: {data}\n\n"
            self.wfile.write(record.encode())
            self.wfile.flush()

    def _handle_read_item(self, item_id: str) -> None:
        self._write_json(200, {"id": int(item_id), "name": f"item-{item_id}"})

    def _handle_search_items(self, query: str) -> None:
        params = parse_qs(query)
        q = params["q"][0]
        page = int(params.get("page", ["1"])[0])
        self._write_json(200, {"q": q, "page": page, "items": [{"id": page, "name": f"{q}-match"}]})

    def _handle_set_cookie(self) -> None:
        self._write_json(200, {"ok": True}, {"Set-Cookie": "session=abc123; Path=/"})

    def _handle_read_cookie(self) -> None:
        self._write_json(200, {"cookie": self.headers.get("Cookie")})

    def _handle_redirect(self) -> None:
        self.send_response(302)
        self.send_header("Location", "/items/7")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_flaky(self, query: str) -> None:
        params = parse_qs(query)
        key = params["key"][0]
        fail_times = int(params.get("fail_times", ["0"])[0])
        attempt = self._bump_counter(key)
        if attempt <= fail_times:
            self._write_json(
                503, {"type": "service-unavailable", "title": "Service unavailable", "status": 503}
            )
        else:
            self._write_json(200, {"attempts": attempt})

    def _handle_retry_after(self, query: str) -> None:
        params = parse_qs(query)
        key = params["key"][0]
        attempt = self._bump_counter(key)
        if attempt == 1:
            self._write_json(429, {"type": "too-many-requests"}, {"Retry-After": "0"})
        else:
            self._write_json(200, {"attempts": attempt})

    def _handle_ndjson(self, query: str) -> None:
        params = parse_qs(query)
        count = int(params.get("count", ["5"])[0])
        body = "\n".join(json.dumps({"i": i}) for i in range(count)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_ndjson_echo(self) -> None:
        posted = self._read_json_body()
        count = cast(int, posted["n"])
        body = ("\n".join(json.dumps({"i": i}) for i in range(count)) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_binary(self) -> None:
        body = b"AAA\nBBB\x00\nCCC"
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_connection_flaky(self, query: str) -> None:
        params = parse_qs(query)
        key = params["key"][0]
        fail_times = int(params.get("fail_times", ["0"])[0])
        attempt = self._bump_counter(key)
        if attempt <= fail_times:
            self.close_connection = True  # abruptly drop the connection, no response at all
            return
        self._write_json(200, {"attempts": attempt})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/slow":
            sleep(3)
            self._write_json(200, {"finally": True})
        elif parsed.path == "/echo-headers":
            headers = [{"name": name, "value": value} for name, value in self.headers.items()]
            self._write_json(200, {"headers": headers})
        elif parsed.path == "/items":
            self._handle_search_items(parsed.query)
        elif parsed.path.startswith("/items/"):
            self._handle_read_item(parsed.path.removeprefix("/items/"))
        elif parsed.path == "/events":
            self._write_sse_events(parsed.query)
        elif parsed.path == "/boom":
            self._write_json(500, _server_error_body)
        elif parsed.path == "/set-cookie":
            self._handle_set_cookie()
        elif parsed.path == "/read-cookie":
            self._handle_read_cookie()
        elif parsed.path == "/redirect":
            self._handle_redirect()
        elif parsed.path == "/flaky":
            self._handle_flaky(parsed.query)
        elif parsed.path == "/retry-after":
            self._handle_retry_after(parsed.query)
        elif parsed.path == "/connection-flaky":
            self._handle_connection_flaky(parsed.query)
        elif parsed.path == "/ndjson":
            self._handle_ndjson(parsed.query)
        elif parsed.path == "/binary":
            self._handle_binary()
        else:
            self._write_json(404, _not_found_body)

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/items/"):
            self.send_response(200)
            self.send_header("X-Item-Exists", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/items":
            self._write_json(200, self._read_json_body())
        elif parsed.path == "/flaky":
            self._handle_flaky(parsed.query)
        elif parsed.path == "/ndjson-echo":
            self._handle_ndjson_echo()
        else:
            self._write_json(404, _not_found_body)

    def do_PUT(self) -> None:
        if self.path.startswith("/items/"):
            item_id = int(self.path.removeprefix("/items/"))
            body = self._read_json_body()
            self._write_json(200, {"id": item_id, "name": body["name"]})
        else:
            self._write_json(404, _not_found_body)

    def do_PATCH(self) -> None:
        if self.path.startswith("/items/"):
            item_id = int(self.path.removeprefix("/items/"))
            body = self._read_json_body()
            self._write_json(200, {"id": item_id, "name": body["name"]})
        else:
            self._write_json(404, _not_found_body)

    def do_DELETE(self) -> None:
        if self.path.startswith("/items/"):
            item_id = int(self.path.removeprefix("/items/"))
            self._write_json(200, {"id": item_id, "deleted": True})
        else:
            self._write_json(404, _not_found_body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own signature
        pass  # silence default per-request stderr logging


def make_server() -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", 0), TestAppHandler)
