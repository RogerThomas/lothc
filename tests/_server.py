"""A minimal stdlib-only HTTP test server — no ASGI framework, no jero.

`examples/server.py` (used for manual smoke-testing) reuses this same handler on a fixed port —
this module is the one canonical server implementation for both. Endpoints: `/items`
(GET/POST/PUT/PATCH/DELETE/HEAD), `/echo-headers`, `/slow`, `/events` (SSE), `/boom`, `/upload`
(multipart), plus cookie/redirect/retry/streaming scenarios used by their own tests.
"""

import email.policy
import json
from email.parser import BytesParser
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

    def _handle_echo_body(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        self._write_json(
            200,
            {
                "body": raw_body.decode(),
                "content_type": self.headers.get("Content-Type"),
            },
        )

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

    def _handle_redirect_loop(self) -> None:
        self.send_response(302)
        self.send_header("Location", "/redirect-loop")
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

    def _handle_upload(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length)
        # `email` parses multipart/form-data correctly since it's a MIME multipart subset —
        # confirmed against a real body lothc's own form= produces. It just wants the
        # Content-Type as a message header, so prepend it ourselves.
        header_bytes = f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode()
        message = BytesParser(policy=email.policy.default).parsebytes(header_bytes + raw_body)

        fields: dict[str, str] = {}
        files: list[dict[str, object]] = []
        for part in message.iter_parts():
            name = cast(str, part.get_param("name", header="content-disposition"))
            filename = part.get_filename()
            payload = cast(bytes, part.get_payload(decode=True))
            if filename is None:
                fields[name] = payload.decode()
            else:
                files.append({"name": name, "filename": filename, "size": len(payload)})
        self._write_json(200, {"fields": fields, "files": files})

    def _write_sse_weird(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        # A comment-only record (no `data:` line) parses to `None` and must be skipped, and a
        # record with an unrecognized field must be ignored while still keeping its data line.
        self.wfile.write(b": just a comment\n\n")
        self.wfile.write(b"foo: bar\ndata: hello\n\n")
        self.wfile.flush()

    def _handle_retry_after_custom(self, query: str) -> None:
        params = parse_qs(query)
        key = params["key"][0]
        value = params["value"][0]
        attempt = self._bump_counter(key)
        if attempt == 1:
            self._write_json(429, {"type": "too-many-requests"}, {"Retry-After": value})
        else:
            self._write_json(200, {"attempts": attempt})

    def _handle_ndjson_with_blank_line(self, query: str) -> None:
        params = parse_qs(query)
        count = int(params.get("count", ["3"])[0])
        lines = [json.dumps({"i": i}) for i in range(count)]
        body = ("\n\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_truncated(self) -> None:
        # No terminating zero-length chunk is ever sent — a genuinely truncated
        # chunked body, which surfaces as a real transport error to the client,
        # unlike a clean connection close (which just marks a normal stream end).
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        body = b"partial-data"
        self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
        self.wfile.flush()

    def _handle_connection_flaky(self, query: str) -> None:
        params = parse_qs(query)
        key = params["key"][0]
        fail_times = int(params.get("fail_times", ["0"])[0])
        attempt = self._bump_counter(key)
        if attempt <= fail_times:
            self.close_connection = True  # abruptly drop the connection, no response at all
            return
        self._write_json(200, {"attempts": attempt})

    def _handle_slow(self) -> None:
        sleep(3)
        self._write_json(200, {"finally": True})

    def _handle_echo_headers(self) -> None:
        headers = [{"name": name, "value": value} for name, value in self.headers.items()]
        self._write_json(200, {"headers": headers})

    def _handle_boom(self) -> None:
        self._write_json(500, _server_error_body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/items":
            self._handle_search_items(parsed.query)
            return
        if parsed.path.startswith("/items/"):
            self._handle_read_item(parsed.path.removeprefix("/items/"))
            return
        query_routes = {
            "/events": self._write_sse_events,
            "/flaky": self._handle_flaky,
            "/retry-after": self._handle_retry_after,
            "/retry-after-custom": self._handle_retry_after_custom,
            "/connection-flaky": self._handle_connection_flaky,
            "/ndjson": self._handle_ndjson,
            "/ndjson-blank-line": self._handle_ndjson_with_blank_line,
        }
        if parsed.path in query_routes:
            query_routes[parsed.path](parsed.query)
            return
        plain_routes = {
            "/slow": self._handle_slow,
            "/echo-headers": self._handle_echo_headers,
            "/events-weird": self._write_sse_weird,
            "/boom": self._handle_boom,
            "/set-cookie": self._handle_set_cookie,
            "/read-cookie": self._handle_read_cookie,
            "/redirect": self._handle_redirect,
            "/redirect-loop": self._handle_redirect_loop,
            "/binary": self._handle_binary,
            "/truncated": self._handle_truncated,
        }
        route = plain_routes.get(parsed.path)
        if route is None:
            self._write_json(404, _not_found_body)
        else:
            route()

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
        elif parsed.path == "/upload":
            self._handle_upload()
        elif parsed.path == "/echo-body":
            self._handle_echo_body()
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
