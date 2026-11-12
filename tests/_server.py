"""A minimal stdlib-only HTTP test server — no ASGI framework, no jero.

`examples/server.py` (used for manual smoke-testing) reuses this same handler on a fixed port —
this module is the one canonical server implementation for both. Endpoints: `/items`
(GET/POST/PUT/PATCH/DELETE/HEAD), `/echo-headers`, `/echo-query`, `/echo-request` (the whole
request as it arrived, any verb with a body), `/slow`, `/peer` (which connection served it),
`/events` (SSE),
`/boom`, `/upload` (multipart), `/oauth/token*` (OAuth 2 token endpoints), plus
cookie/redirect/retry/streaming/connection-close scenarios used by their own tests.
"""

import contextlib
import email.policy
import gzip
import json
import socket
import sys
import time
import zlib
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
_not_authorized_body = {
    "type": "not-authorized",
    "title": "Not authorized",
    "status": 401,
}
_server_error_body = {
    "type": "internal-server-error",
    "title": "Internal server error",
    "status": 500,
}
# `/compressed`'s br and zstd bodies, precomputed (no brotli in the stdlib, and
# `compression.zstd` is 3.14+): `compressed_payload(name)` for each, then two zstd frames.
_precompressed = {
    "br-large": b"b\x8c\x13\x83#\xc1\x8e\xb5X\xc7\xe0\xc81B\xa5\xb7\x08\x88\xd8\xded\xd1\xe9"
    b"\xab\x169p\xc8\x01\xfb\x81Z\x10I@\x89S\xb5@D_\xd80\x02\x94\xdc\xc1UY-\xb0\xd1\xe5'\x01`\x03",
    "zstd-large": b'(\xb5/\xfd`v\xc2u\x01\x00$\x02{"encoding": "zstd-large", "text"}\x02\x00C\xc3'
    b"\x16\x1d \xe5\xf9\x04",
    "br": b"\x1bm\x00\xf8\x1d\x07v,<\xc6\xe2p\xd6\xa8E\xc4\xe4\xf6&\x8bN_\x80\xc0\x06\x1c"
    b"\xb8\x04\x14\x04\x08\x03\xd3\xcd\xc0\x034\x00\xa3[\x864\x84Gh",
    "zstd": b'(\xb5/\xfd \xc0E\x01\x00\xe0{"encoding": "zstd", "text"}\x03\x00\x95\x1d\xc1M'
    b"\x0b\x1cb\x8e\x01",
    "zstd-frames": b"(\xb5/\xfd \x079\x00\x00frame-1(\xb5/\xfd \x079\x00\x00frame-2",
}


def compressed_payload(codec: str) -> bytes:
    """What `/compressed?codec=` decompresses to: 40-65KB for a `-large` codec, many times what
    a decoder writes per read, a few hundred bytes for the others."""
    repeat = 5000 if codec.endswith("-large") else 40
    return json.dumps({"encoding": codec, "text": codec * repeat}).encode()


def _with_gzip_flags(member: bytes, flags: int, fields: bytes) -> bytes:
    # A gzip member's 10-byte header given `flags`, followed by the fields they announce.
    return member[:3] + bytes([flags]) + member[4:10] + fields + member[10:]


def _compressed_body(codec: str) -> bytes:
    payload = compressed_payload(codec)
    match codec:
        case "gzip" | "gzip-large":
            return gzip.compress(payload, mtime=0)
        case "gzip-named":
            # FEXTRA, FNAME and FCOMMENT.
            fields = b"\x05\x00extra" + b"name\x00" + b"comment\x00"
            return _with_gzip_flags(gzip.compress(payload, mtime=0), 0x1C, fields)
        case "deflate" | "deflate-large":
            return zlib.compress(payload)
        case "deflate-raw":
            # Deflate without zlib's 2-byte header and 4-byte checksum.
            return zlib.compress(payload)[2:-4]
        case _:
            return _precompressed[codec]


def _flip(body: bytes, index: int) -> bytes:
    return body[:index] + bytes([body[index] ^ 1]) + body[index + 1 :]


def _compressed_shape(complete: bytes, shape: str) -> tuple[list[bytes], int | None]:
    # The writes `/compressed` makes for `shape=`, and its Content-Length (None: chunked).
    match shape:
        case "empty":
            return [], 0
        case "trailing":
            return [complete + b"trailing"], len(complete) + 8
        case "trailing-later":
            return [complete, b"trailing"], len(complete) + 8
        case "truncated":
            return [complete[:-4]], len(complete) - 4
        case "cut-off":
            return [complete[:-4]], len(complete)
        case "bad-crc":
            return [_flip(complete, len(complete) - 8)], len(complete)
        case "bad-length":
            return [_flip(complete, len(complete) - 4)], len(complete)
        case "byte-chunks":
            return [b"1\r\n%c\r\n" % byte for byte in complete] + [b"0\r\n\r\n"], None
        case _:
            return [complete], len(complete)


class TestAppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Per-key hit counters for the flaky/retry-after endpoints below — shared
    # across request threads for the lifetime of the test server.
    _counters: ClassVar[dict[str, int]] = {}
    # Per-key record of every request the /oauth/token* endpoints received (content type,
    # Authorization header, parsed body), read back via GET /oauth/token-requests so tests can
    # assert the wire shape of a token request through a public endpoint, never via provider
    # internals.
    _token_requests: ClassVar[dict[str, list[dict[str, object]]]] = {}
    # Per-key client ports of the `/peer?key=` connections that have since closed, recorded when
    # a connection's request loop ends (`handle`) and read back via GET /peer-closed, so a test
    # can see a client close an idle connection without sending anything on it.
    _closed_peers: ClassVar[dict[str, list[int]]] = {}
    # The key this connection's last `/peer?key=` request gave (a handler serves one connection).
    _peer_key: str | None = None

    def _client_port(self) -> int:
        _, port = self.client_address
        return port

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
                # Every value, not just the first: a duplicated Content-Type is exactly the kind
                # of bug `.get` would hide.
                "content_types": self.headers.get_all("Content-Type") or [],
            },
        )

    def _bump_counter(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    def _start_sse_response(self, *, chunked: bool = False) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _write_sse_chunk(self, record: str) -> None:
        # One chunked-transfer frame per record; never sending the terminating zero-length
        # frame afterwards (see `_handle_truncated`) is what turns a plain close into a real
        # transport error on the client, which is what an SSE reconnect test needs.
        body = record.encode()
        self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
        self.wfile.flush()

    def _write_sse_events(self, query: str) -> None:
        params = parse_qs(query)
        omit_id = params.get("omit", [""])[0] == "id"
        # Small but real by default, so a test can assert events arrive incrementally rather
        # than all at once (see test_sse_events_arrive_incrementally in test_sse.py).
        interval = float(params.get("interval", ["0.001"])[0])
        count = int(params.get("count", ["10"])[0])
        self._start_sse_response()
        # A client that gives up mid-stream (e.g. its `read_timeout` fired) makes the next write
        # here fail — that's the client's business, not a server error worth a traceback.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            for index in range(count):
                data = json.dumps({"msg": f"hello {index}", "now": index * 100})
                record = "event: tick\n"
                if not omit_id:
                    record += f"id: {index}\n"
                record += f"data: {data}\n\n"
                self.wfile.write(record.encode())
                self.wfile.flush()
                time.sleep(interval)

    def _write_sse_truncated(self) -> None:
        self._start_sse_response(chunked=True)
        self._write_sse_chunk("id: 0\ndata: first\n\n")

    def _write_sse_reconnect(self, query: str) -> None:
        # First connection: a `retry:` hint, two events, then the connection dies mid-stream.
        # Every later connection: two more events, then a clean close. Every event's data echoes
        # each `Last-Event-ID` value its connection carried (a list, so a duplicate shows).
        attempt = self._bump_counter(parse_qs(query)["key"][0])
        data = json.dumps({"last": self.headers.get_all("Last-Event-ID", [])})
        if attempt == 1:
            self._start_sse_response(chunked=True)
            self._write_sse_chunk("retry: 20\n\n")
            for index in range(2):
                self._write_sse_chunk(f"id: {index}\ndata: {data}\n\n")
            return
        self._start_sse_response()
        for index in range(2, 4):
            self.wfile.write(f"id: {index}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _write_sse_close_count(self, query: str) -> None:
        # Exercises `reconnect_on_close`: the first `events` connections each send exactly one
        # event (its id is the connection number, its data echoes `Last-Event-ID`) then close
        # cleanly; every connection after that closes cleanly with no events at all.
        params = parse_qs(query)
        attempt = self._bump_counter(params["key"][0])
        events = int(params.get("events", ["1"])[0])
        last_event_id = self.headers.get("Last-Event-ID")
        self._start_sse_response()
        if attempt <= events:
            record = f"id: {attempt}\ndata: {json.dumps({'last': last_event_id})}\n\n"
            self.wfile.write(record.encode())
            self.wfile.flush()

    def _write_sse_no_content(self) -> None:
        self.close_connection = True
        self.send_response(204)
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_sse_wrong_content_type(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"data: not-a-stream\n\n")
        self.wfile.flush()

    def _write_sse_spec_edge_cases(self) -> None:
        # Everything the WHATWG event-stream format allows that a naive parser gets wrong: a
        # leading UTF-8 BOM, bare-CR line terminators, a data-less record whose `id:` must still
        # persist onto the next event, and an explicit empty `id:` that resets that buffer.
        self._start_sse_response()
        self.wfile.write(b"\xef\xbb\xbf")
        self.wfile.write(b"id: 7\r\r")
        self.wfile.write(b"data: a\r\n\r\n")
        self.wfile.write(b"data: b\n\n")
        self.wfile.write(b"id\ndata: c\n\n")
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

    def _handle_multi_set_cookie(self) -> None:
        # Sent header-by-header rather than through _write_json's `extra_headers` dict, which
        # can't express the same field name twice — which is the whole point here.
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "session=abc; Path=/")
        self.send_header("Set-Cookie", "csrf=xyz; Path=/")
        self.send_header("Set-Cookie", "theme=dark; Path=/")
        self.end_headers()
        self.wfile.write(body)

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

    @staticmethod
    def _ndjson_line(i: int, shape: str) -> str:
        # `array`: every line `[i]`; `mixed`: objects on even lines, arrays on odd ones (for
        # union `into=` forms); anything else: every line `{"i": i}`.
        if shape == "array" or (shape == "mixed" and i % 2):
            return json.dumps([i])
        return json.dumps({"i": i})

    def _handle_ndjson(self, query: str) -> None:
        params = parse_qs(query)
        count = int(params.get("count", ["5"])[0])
        shape = params.get("shape", ["object"])[0]
        body = "\n".join(self._ndjson_line(i, shape) for i in range(count)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_ndjson_echo(self) -> None:
        posted = self._read_json_body()
        count = cast(int, posted["n"])
        shape = cast(str, posted.get("shape", "object"))
        body = ("\n".join(self._ndjson_line(i, shape) for i in range(count)) + "\n").encode()
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

    def _handle_invalid_utf8(self) -> None:
        body = b"\xff\xfe"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
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
        # `parts` additionally records every part in wire order (unlike `fields`, which
        # collapses repeated names to their last value) plus each part's own Content-Type
        # header — `.get(...)` (not `.get_content_type()`, which defaults to "text/plain"
        # even when no header was sent) so a mime-inference test can tell "no header sent"
        # apart from "server default".
        parts: list[dict[str, object]] = []
        for part in message.iter_parts():
            name = cast(str, part.get_param("name", header="content-disposition"))
            filename = part.get_filename()
            payload = cast(bytes, part.get_payload(decode=True))
            content_type = part.get("Content-Type")
            parts.append({
                "name": name,
                "filename": filename,
                "content_type": content_type,
                "text": payload.decode(errors="replace"),
            })
            if filename is None:
                fields[name] = payload.decode()
            else:
                files.append({"name": name, "filename": filename, "size": len(payload)})
        self._write_json(200, {"fields": fields, "files": files, "parts": parts})

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
        # `newline=crlf` sends CRLF line endings, whose blank lines arrive as a bare `\r`.
        newline = "\r\n" if params.get("newline", ["lf"])[0] == "crlf" else "\n"
        lines = [json.dumps({"i": i}) for i in range(count)]
        body = ((newline * 2).join(lines) + newline).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_sse_byte_by_byte(self) -> None:
        # Each record boundary is split *inside* itself, across separate reads: `\n` | `\n`,
        # `\r` | `\r`, and `\r\n\r` | `\n`. The pause is what makes those real chunk boundaries;
        # written back to back, TCP coalesces them into one read and the split never happens.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in (b"data: a\n", b"\n", b"data: b\r", b"\r", b"data: c\r\n\r", b"\n"):
            self.wfile.write(piece)
            self.wfile.flush()
            time.sleep(0.01)

    def _write_sse_lenient_cases(self) -> None:
        # An empty `event:` field (dispatched as "message" per spec, not "") and invalid UTF-8
        # (decoded with replacement per spec, not raised).
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"event:\ndata: first\n\ndata: bad-\xff-byte\n\n")
        self.wfile.flush()

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

    def _handle_truncated_content_length(self) -> None:
        # Promises 100 bytes, sends 8, then closes. Unlike `_handle_truncated` (chunked), this is
        # what a server or pod dying mid-response usually looks like, and pyreqwest reports it
        # through a different exception (`DecodeError`), so it needs its own route to be tested.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b'{"id": 1')
        self.wfile.flush()

    def _handle_truncated_after_first_chunk(self) -> None:
        # Like `_handle_truncated_content_length`, but a real first chunk arrives on its own
        # before the connection dies, so a client is genuinely mid-body when it fails. The
        # 8-byte version fails before any body chunk is handed over, which never exercises what
        # a half-written download leaves behind.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(1024 * 1024))
        self.end_headers()
        self.wfile.write(b"x" * 64 * 1024)
        self.wfile.flush()
        time.sleep(0.02)

    def _handle_stall_after_first_chunk(self, query: str) -> None:
        # One chunk, then silence for `seconds` (the connection stays open), then close. A client
        # whose idle limit is shorter must give up during the silence rather than wait it out.
        seconds = float(parse_qs(query).get("seconds", ["1"])[0])
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(b"5\r\nfirst\r\n")
        self.wfile.flush()
        time.sleep(seconds)

    def _handle_endless(self, query: str) -> None:
        # Streams chunks until the client hangs up, then counts the hang-up (per `key`), so a
        # test can assert that closing a stream early really releases its connection.
        key = parse_qs(query)["key"][0]
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                self.wfile.write(b"5\r\nchunk\r\n")
                self.wfile.flush()
                time.sleep(0.005)
        except ConnectionError:
            self._bump_counter(f"{key}-hangups")

    def _handle_hits(self, query: str) -> None:
        # Reads back any per-key counter, for routes whose own response can't carry it.
        key = parse_qs(query)["key"][0]
        self._write_json(200, {"hits": self._counters.get(key, 0)})

    def _handle_rejects_first_token(self) -> None:
        # Stands in for an API whose first-issued token was revoked before it expired: the token
        # endpoint issues `token-1`, `token-2`, ..., and this rejects `token-1` with a 401.
        # Drain any request body first, so a kept-alive connection's next request isn't misread.
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        authorization = self.headers.get("Authorization")
        if authorization == "Bearer token-1":
            self._write_json(401, {"error": "invalid_token"})
        else:
            self._write_json(200, {"authorization": authorization})

    def _handle_always_unauthorized(self, query: str) -> None:
        # Rejects every token, and says how many times it's been asked (per `key`), so a test can
        # assert a request was retried exactly once rather than looping.
        hits = self._bump_counter(parse_qs(query)["key"][0])
        self._write_json(401, {"hits": hits})

    def _handle_invalid_json_error(self) -> None:
        # What a proxy in front of a JSON API sends when the API is down: an HTML 502.
        body = b"<html><body>502 Bad Gateway</body></html>"
        self.send_response(502)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_deeply_nested_error(self) -> None:
        # Nested far past any JSON parser's recursion limit.
        body = b"[" * 200000
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_crlf_text(self) -> None:
        # A CRLF line, an LF line, a blank line, then a final line with no terminator.
        body = b"a\r\nb\n\nc"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_json_literal(self, query: str) -> None:
        # A top-level JSON value that isn't an object, sent verbatim (`value=` is raw JSON).
        body = parse_qs(query)["value"][0].encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_json_array(self) -> None:
        self._write_json(200, [{"id": 1, "name": "one"}, {"id": 2, "name": "two"}])

    def _handle_invalid_json(self) -> None:
        body = b"{not json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
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

    def _handle_body_flaky(self, query: str) -> None:
        # Like `/connection-flaky`, but the first `fail_times` responses die partway through the
        # body (fewer bytes than `Content-Length` promised) rather than before any response.
        params = parse_qs(query)
        attempt = self._bump_counter(params["key"][0])
        if attempt > int(params["fail_times"][0]):
            self._write_json(200, {"attempts": attempt})
            return
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b'{"attempts": ')
        self.wfile.flush()

    def _handle_corrupt_gzip(self, query: str) -> None:
        # A complete response whose body can never be decompressed, however often it's fetched.
        # At least gzip's 10-byte header: a shorter body ends the decoder early, which pyreqwest
        # reports as a connection error rather than a decode one.
        self._bump_counter(parse_qs(query)["key"][0])
        body = b"this-is-not-gzip"
        self.send_response(200)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_compressed(self, query: str) -> None:
        # A `codec=` body (see `_compressed_body`) sent as `Content-Encoding: <coding>` (default
        # the codec up to its first `-`; a repeated `coding=` sends one header each), shaped by
        # `shape=` (see `_compressed_shape`): `trailing`
        # adds bytes after the stream in the same write, `trailing-later` in a later one,
        # `truncated` cuts the stream short with an exact Content-Length, `cut-off` closes the
        # connection short of it, `byte-chunks` sends one byte per chunk. `extra=1` sends two
        # more headers after Content-Length.
        params = parse_qs(query)
        codec = params.get("codec", ["gzip"])[0]
        status = int(params.get("status", ["200"])[0])
        writes, length = _compressed_shape(
            _compressed_body(codec), params.get("shape", ["complete"])[0]
        )
        self.close_connection = params.get("shape") == ["cut-off"]
        self.send_response(status)
        for coding in params.get("coding", [codec.split("-")[0]]):
            self.send_header("Content-Encoding", coding)
        if length is None:
            self.send_header("Transfer-Encoding", "chunked")
        elif status != 204:
            self.send_header("Content-Length", str(length))
        if "extra" in params:
            self.send_header("X-1", "1")
            self.send_header("X-2", "2")
        self.end_headers()
        if self.command == "HEAD":
            return
        for write in writes:
            self.wfile.write(write)
            self.wfile.flush()
            if len(writes) == 2:
                # `trailing-later`: the trailing bytes arrive in a later read.
                sleep(0.02)

    def _handle_connection_close(self) -> None:
        # Announces the close, so the client must open a new connection for its next request.
        self.close_connection = True
        self._write_json(200, {"closed": "announced"}, {"Connection": "close"})

    def _handle_silent_close(self) -> None:
        # Closes after responding without saying so, as a server dropping an idle keep-alive
        # connection does: a client reusing that connection unchecked writes into a dead socket.
        self._write_json(200, {"closed": "silently"})
        self.close_connection = True

    def _handle_slow(self, query: str) -> None:
        # `seconds` lets each caller buy exactly the delay its own assertion needs, rather than
        # one shared number sized for the strictest case. A test that only needs the client to
        # time out never waits this out at all, so it can ask for a comfortably long sleep; the
        # two "a longer per-call timeout overrides the client's" tests deliberately run this to
        # completion and pay every millisecond, so they ask for a short one.
        sleep(float(parse_qs(query).get("seconds", ["0.5"])[0]))
        self._write_json(200, {"finally": True})

    def _handle_peer(self, query: str) -> None:
        # Which TCP connection served this request (its client port), for the connection-reuse
        # tests. `seconds` holds the answer back, so concurrent requests finish in a known order.
        params = parse_qs(query)
        sleep(float(params.get("seconds", ["0"])[0]))
        if "key" in params:
            self._peer_key = params["key"][0]
        self._write_json(200, {"port": self._client_port()})

    def _handle_peer_closed(self, query: str) -> None:
        key = parse_qs(query)["key"][0]
        self._write_json(200, {"ports": self._closed_peers.get(key, [])})

    def _handle_echo_headers(self) -> None:
        headers = [{"name": name, "value": value} for name, value in self.headers.items()]
        self._write_json(200, {"headers": headers})

    def _handle_echo_query(self, query: str) -> None:
        # parse_qs already returns every value as a list (even a single-value key), so a
        # genuinely repeated key round-trips as a multi-element list with no extra handling.
        self._write_json(200, {"query": parse_qs(query)})

    def _handle_echo_request(self) -> None:
        # The request as it arrived, for the wire-format tests: the raw request line, every
        # header in arrival order with its casing, and the body as latin-1 (one character per
        # byte, so any body round-trips through JSON unchanged).
        length = int(self.headers.get("Content-Length", "0"))
        self._write_json(
            200,
            {
                "line": self.requestline,
                "headers": [[name, value] for name, value in self.headers.items()],
                "body": self.rfile.read(length).decode("latin-1"),
            },
        )

    def _handle_boom(self) -> None:
        self._write_json(500, _server_error_body)

    def _handle_unauthorized(self) -> None:
        self._write_json(401, _not_authorized_body)

    def _handle_boom_large(self) -> None:
        # Longer than `HTTPResponseError.body_start`'s 100 bytes, with a header worth quoting.
        self._write_json(500, {"detail": "x" * 300}, {"X-Request-Id": "request-id"})

    # Routes for the behaviour-contract tests (retry statuses and methods, redirect rules, SSE
    # field parsing, ...). Each one answers whichever method it's called with, through
    # `_handle_contract_route` at the top of every `do_*` below.

    # Per-key record of every request body `/status-then-ok` received, oldest first.
    _received_bodies: ClassVar[dict[str, list[str]]] = {}

    def _drain_body(self) -> bytes:
        # Read any request body, so a kept-alive connection's next request isn't misread.
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _write_bodyless(self, status: int, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _handle_status_then_ok(self, query: str) -> None:
        # The key's first `fail_times` requests get `status` (503 by default), later ones a 200
        # with the attempt number and every body received for the key so far. A HEAD answer has
        # no body, so a HEAD test reads the attempt count back through `/hits`.
        params = parse_qs(query)
        key = params["key"][0]
        self._received_bodies.setdefault(key, []).append(self._drain_body().decode())
        attempt = self._bump_counter(key)
        failed = attempt <= int(params.get("fail_times", ["0"])[0])
        status = int(params.get("status", ["503"])[0]) if failed else 200
        if self.command == "HEAD":
            self._write_bodyless(status)
        elif failed:
            self._write_json(status, {"attempt": attempt})
        else:
            self._write_json(200, {"attempts": attempt, "bodies": self._received_bodies[key]})

    def _handle_redirect_to(self, query: str) -> None:
        # A `status` redirect (302 by default) to `to`, which may be another host.
        # `cookie=name=value` also sets that cookie on the redirect response itself.
        params = parse_qs(query)
        self._drain_body()
        headers = {"Location": params["to"][0]}
        if "cookie" in params:
            headers["Set-Cookie"] = f"{params['cookie'][0]}; Path=/"
        self._write_bodyless(int(params.get("status", ["302"])[0]), headers)

    def _handle_redirect_chain(self, query: str) -> None:
        # `hops` redirects in a row, then a 200.
        hops = int(parse_qs(query)["hops"][0])
        if hops == 0:
            self._write_json(200, {"hops": "done"})
        else:
            self._write_bodyless(302, {"Location": f"/redirect-chain?hops={hops - 1}"})

    def _handle_counted_redirect_loop(self, query: str) -> None:
        # `/redirect-loop`, counting every request it gets (per `key`, read back via `/hits`).
        self._bump_counter(parse_qs(query)["key"][0])
        self._write_bodyless(302, {"Location": f"/redirect-loop-counted?{query}"})

    def _handle_echo_method(self) -> None:
        # What arrived, so a redirect test can see whether the method and body survived a hop.
        body = self._drain_body()
        self._write_json(
            200,
            {
                "method": self.command,
                "body": body.decode(),
                "content_type": self.headers.get("Content-Type"),
            },
        )

    def _write_sse_fields(self) -> None:
        # The field rules a naive event-stream parser gets wrong, one record each: a multi-line
        # `data`, no space and two spaces after the colon, a bare `data` line, a non-numeric
        # `retry`, an `id` containing NUL, an id-only record, and a final record with no blank
        # line after it, which the spec discards at EOF.
        self._start_sse_response()
        self.wfile.write(
            b"data: line-1\ndata: line-2\n\n"
            b"data:no-space\n\n"
            b"data:  two-spaces\n\n"
            b"data\n\n"
            b"retry: soon\ndata: bad-retry\n\n"
            b"id: a\x00b\ndata: nul-id\n\n"
            b"id: 5\n\n"
            b"data: after-id\n\n"
            b"data: unterminated\n"
        )
        self.wfile.flush()

    def _write_sse_echo_accept(self) -> None:
        # One event whose data is every `Accept` header value the request carried.
        self._start_sse_response()
        self.wfile.write(f"data: {json.dumps(self.headers.get_all('Accept') or [])}\n\n".encode())
        self.wfile.flush()

    def _write_sse_drops(self, query: str) -> None:
        # The key's first `drops` connections break mid-stream (a chunked body never terminated),
        # each after one event if `with_event=1`, else after only a comment; the next connection
        # sends `done` and ends cleanly.
        params = parse_qs(query)
        attempt = self._bump_counter(params["key"][0])
        self._start_sse_response(chunked=True)
        if attempt > int(params["drops"][0]):
            self._write_sse_chunk("data: done\n\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        elif params.get("with_event", ["0"])[0] == "1":
            self._write_sse_chunk(f"id: {attempt}\ndata: dropped-{attempt}\n\n")
        else:
            self._write_sse_chunk(": dropped\n\n")

    def _write_sse_then_status(self, query: str) -> None:
        # The key's first connection: one event, then the connection breaks. Every later one is
        # refused with `status`.
        params = parse_qs(query)
        if self._bump_counter(params["key"][0]) == 1:
            self._start_sse_response(chunked=True)
            self._write_sse_chunk("id: 1\ndata: first\n\n")
        else:
            self._write_json(int(params["status"][0]), {"refused": True})

    def _handle_oauth_token_broken(self, query: str) -> None:
        # A token endpoint answering 200 with something that isn't a usable token: `shape=not-json`
        # (an HTML page) or `shape=no-access-token` (JSON without `access_token`).
        params = parse_qs(query)
        self._drain_body()
        self._record_token_request(params["key"][0], None)
        if params["shape"][0] == "no-access-token":
            self._write_json(200, {"token_type": "Bearer", "expires_in": 3600})
            return
        body = b"<html>not a token</html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_contract_route(self) -> bool:
        """Serve one of the routes above, whatever the method; `False` if it isn't one of them."""
        parsed = urlparse(self.path)
        query_routes = {
            "/status-then-ok": self._handle_status_then_ok,
            "/redirect-to": self._handle_redirect_to,
            "/redirect-chain": self._handle_redirect_chain,
            "/redirect-loop-counted": self._handle_counted_redirect_loop,
            "/events-drops": self._write_sse_drops,
            "/events-then-status": self._write_sse_then_status,
            "/oauth/token-broken": self._handle_oauth_token_broken,
        }
        plain_routes = {
            "/echo-method": self._handle_echo_method,
            "/events-fields": self._write_sse_fields,
            "/events-echo-accept": self._write_sse_echo_accept,
        }
        if parsed.path in query_routes:
            query_routes[parsed.path](parsed.query)
            return True
        if parsed.path in plain_routes:
            plain_routes[parsed.path]()
            return True
        return False

    def _record_token_request(self, key: str, body: object) -> None:
        self._token_requests.setdefault(key, []).append({
            "content_type": self.headers.get("Content-Type"),
            "authorization": self.headers.get("Authorization"),
            "headers": {name.lower(): value for name, value in self.headers.items()},
            "body": body,
        })

    @staticmethod
    def _refresh_failure_status(params: dict[str, list[str]]) -> int | None:
        # `refresh_status=<int>` fails a refresh with that status; `refresh_fails=1` is the
        # RFC's own `400 invalid_grant`. Neither → the refresh succeeds.
        if "refresh_status" in params:
            return int(params["refresh_status"][0])
        if params.get("refresh_fails", ["0"])[0] == "1":
            return 400
        return None

    def _handle_oauth_token(self, query: str) -> None:
        # RFC 6749 token endpoint: form-encoded body, standard JSON response. Credentials are
        # deliberately NOT validated here — tests assert the recorded header/body instead.
        params = parse_qs(query)
        key = params["key"][0]
        length = int(self.headers.get("Content-Length", "0"))
        form = {
            name: values[0] for name, values in parse_qs(self.rfile.read(length).decode()).items()
        }
        self._record_token_request(key, form)
        attempt = self._bump_counter(key)
        is_refresh = form["grant_type"] == "refresh_token"
        refresh_failure_status = self._refresh_failure_status(params)
        if is_refresh and refresh_failure_status is not None:
            self._write_json(refresh_failure_status, {"error": "invalid_grant"})
            return
        # `later_mint_status=<int>`: every mint after the key's first request fails with that
        # status, e.g. client credentials revoked after the first token was issued.
        if not is_refresh and attempt > 1 and "later_mint_status" in params:
            self._write_json(int(params["later_mint_status"][0]), {"error": "invalid_client"})
            return
        payload: dict[str, object] = {"access_token": f"token-{attempt}", "token_type": "Bearer"}
        if params.get("omit_expires_in", ["0"])[0] != "1":
            payload["expires_in"] = int(params.get("expires_in", ["3600"])[0])
        # `refresh_omits_token=1`: the refresh response has no `refresh_token` key at all (RFC
        # 6749 §6 allows this — the old one stays valid); a mint still includes one.
        omits_refresh_token = is_refresh and params.get("refresh_omits_token", ["0"])[0] == "1"
        if params.get("with_refresh", ["0"])[0] == "1" and not omits_refresh_token:
            payload["refresh_token"] = f"refresh-{attempt}"
        self._write_json(200, payload)

    def _handle_oauth_token_custom(self, query: str) -> None:
        # A non-RFC token endpoint: JSON body in, renamed camelCase fields out.
        params = parse_qs(query)
        key = params["key"][0]
        self._record_token_request(key, self._read_json_body())
        attempt = self._bump_counter(key)
        payload: dict[str, object] = {"accessToken": f"token-{attempt}"}
        if params.get("omit_expires_in", ["0"])[0] != "1":
            payload["expiresInSeconds"] = int(params.get("expires_in", ["3600"])[0])
        if params.get("with_refresh", ["0"])[0] == "1":
            payload["refreshToken"] = f"refresh-{attempt}"
        self._write_json(200, payload)

    def _handle_oauth_token_boom(self, query: str) -> None:
        self._record_token_request(parse_qs(query)["key"][0], None)
        self._write_json(500, _server_error_body)

    def _handle_oauth_token_requests(self, query: str) -> None:
        key = parse_qs(query)["key"][0]
        self._write_json(200, {"requests": self._token_requests.get(key, [])})

    def handle(self) -> None:
        try:
            super().handle()
        finally:
            if self._peer_key is not None:
                self._closed_peers.setdefault(self._peer_key, []).append(self._client_port())

    def do_GET(self) -> None:
        if self._handle_contract_route():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/items":
            self._handle_search_items(parsed.query)
            return
        if parsed.path.startswith("/items/"):
            self._handle_read_item(parsed.path.removeprefix("/items/"))
            return
        query_routes = {
            "/events": self._write_sse_events,
            "/events-reconnect": self._write_sse_reconnect,
            "/events-close-count": self._write_sse_close_count,
            "/flaky": self._handle_flaky,
            "/retry-after": self._handle_retry_after,
            "/retry-after-custom": self._handle_retry_after_custom,
            "/connection-flaky": self._handle_connection_flaky,
            "/ndjson": self._handle_ndjson,
            "/ndjson-blank-line": self._handle_ndjson_with_blank_line,
            "/oauth/token-requests": self._handle_oauth_token_requests,
            "/echo-query": self._handle_echo_query,
            "/slow": self._handle_slow,
            "/peer": self._handle_peer,
            "/peer-closed": self._handle_peer_closed,
            "/endless": self._handle_endless,
            "/hits": self._handle_hits,
            "/body-flaky": self._handle_body_flaky,
            "/corrupt-gzip": self._handle_corrupt_gzip,
            "/compressed": self._handle_compressed,
            "/always-unauthorized": self._handle_always_unauthorized,
            "/json-literal": self._handle_json_literal,
            "/stall-after-first-chunk": self._handle_stall_after_first_chunk,
        }
        if parsed.path in query_routes:
            query_routes[parsed.path](parsed.query)
            return
        plain_routes = {
            "/echo-headers": self._handle_echo_headers,
            "/echo-request": self._handle_echo_request,
            "/events-weird": self._write_sse_weird,
            "/events-byte-by-byte": self._write_sse_byte_by_byte,
            "/events-lenient": self._write_sse_lenient_cases,
            "/events-truncated": self._write_sse_truncated,
            "/events-no-content": self._write_sse_no_content,
            "/events-wrong-content-type": self._write_sse_wrong_content_type,
            "/events-spec-edge-cases": self._write_sse_spec_edge_cases,
            "/boom": self._handle_boom,
            "/unauthorized": self._handle_unauthorized,
            "/boom-large": self._handle_boom_large,
            "/set-cookie": self._handle_set_cookie,
            "/multi-set-cookie": self._handle_multi_set_cookie,
            "/read-cookie": self._handle_read_cookie,
            "/redirect": self._handle_redirect,
            "/redirect-loop": self._handle_redirect_loop,
            "/binary": self._handle_binary,
            "/invalid-utf8": self._handle_invalid_utf8,
            "/truncated": self._handle_truncated,
            "/truncated-content-length": self._handle_truncated_content_length,
            "/truncated-after-first-chunk": self._handle_truncated_after_first_chunk,
            "/invalid-json": self._handle_invalid_json,
            "/invalid-json-error": self._handle_invalid_json_error,
            "/rejects-first-token": self._handle_rejects_first_token,
            "/json-array": self._handle_json_array,
            "/deeply-nested-error": self._handle_deeply_nested_error,
            "/crlf-text": self._handle_crlf_text,
            "/connection-close": self._handle_connection_close,
            "/silent-close": self._handle_silent_close,
        }
        route = plain_routes.get(parsed.path)
        if route is None:
            self._write_json(404, _not_found_body)
        else:
            route()

    def do_HEAD(self) -> None:
        if self._handle_contract_route():
            return
        parsed = urlparse(self.path)
        if parsed.path.startswith("/items/"):
            self.send_response(200)
            self.send_header("X-Item-Exists", "true")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif parsed.path == "/compressed":
            self._handle_compressed(parsed.query)
        elif parsed.path == "/slow":
            # GET's `/slow` delay with no body, so HEAD's own `.elapsed` can be bracketed too.
            sleep(float(parse_qs(parsed.query).get("seconds", ["0.5"])[0]))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_OPTIONS(self) -> None:
        if self._handle_contract_route():
            return
        # A CORS preflight answer: 204 echoing the request's `Origin`, or 404 for `/missing`.
        parsed = urlparse(self.path)
        if parsed.path == "/flaky":
            self._handle_flaky(parsed.query)
            return
        if parsed.path == "/missing":
            self.send_response(404)
        else:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self._handle_contract_route():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/items":
            self._write_json(200, self._read_json_body())
        elif parsed.path == "/flaky":
            self._handle_flaky(parsed.query)
        elif parsed.path == "/body-flaky":
            self._handle_body_flaky(parsed.query)
        elif parsed.path == "/endless":
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self._handle_endless(parsed.query)
        elif parsed.path == "/ndjson-echo":
            self._handle_ndjson_echo()
        elif parsed.path == "/upload":
            self._handle_upload()
        elif parsed.path == "/echo-body":
            self._handle_echo_body()
        elif parsed.path == "/echo-request":
            self._handle_echo_request()
        elif parsed.path == "/rejects-first-token":
            self._handle_rejects_first_token()
        elif parsed.path == "/oauth/token":
            self._handle_oauth_token(parsed.query)
        elif parsed.path == "/oauth/token-custom":
            self._handle_oauth_token_custom(parsed.query)
        elif parsed.path == "/oauth/token-boom":
            self._handle_oauth_token_boom(parsed.query)
        else:
            self._write_json(404, _not_found_body)

    def do_PUT(self) -> None:
        if self._handle_contract_route():
            return
        if urlparse(self.path).path == "/echo-request":
            self._handle_echo_request()
        elif self.path.startswith("/items/"):
            item_id = int(self.path.removeprefix("/items/"))
            body = self._read_json_body()
            self._write_json(200, {"id": item_id, "name": body["name"]})
        else:
            self._write_json(404, _not_found_body)

    def do_PATCH(self) -> None:
        if self._handle_contract_route():
            return
        if urlparse(self.path).path == "/echo-request":
            self._handle_echo_request()
        elif self.path.startswith("/items/"):
            item_id = int(self.path.removeprefix("/items/"))
            body = self._read_json_body()
            self._write_json(200, {"id": item_id, "name": body["name"]})
        else:
            self._write_json(404, _not_found_body)

    def do_DELETE(self) -> None:
        if self._handle_contract_route():
            return
        if self.path.startswith("/items/"):
            item_id = int(self.path.removeprefix("/items/"))
            self._write_json(200, {"id": item_id, "deleted": True})
        elif self.path == "/echo-body":
            self._handle_echo_body()
        elif urlparse(self.path).path == "/echo-request":
            self._handle_echo_request()
        else:
            self._write_json(404, _not_found_body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own signature
        pass  # silence default per-request stderr logging


class _QuietOnDisconnectServer(ThreadingHTTPServer):
    """Doesn't print a traceback when a client goes away before the server has finished answering.

    Several tests deliberately stop reading early or give up on a stalled response, and the
    stdlib server otherwise prints "Exception occurred during processing of request" plus a
    `BrokenPipeError` traceback to stderr, sometimes after pytest's own summary. Only a client
    disconnect (`ConnectionError`) is silenced; any other handler error still prints, so a real
    bug in a test route stays visible.
    """

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


def make_server() -> ThreadingHTTPServer:
    return _QuietOnDisconnectServer(("127.0.0.1", 0), TestAppHandler)


@contextmanager
def serve_in_background(server: ThreadingHTTPServer) -> Generator[ThreadingHTTPServer]:
    """Run `server` on a background thread for the duration of the block, then shut it down."""
    with server, ThreadPoolExecutor(max_workers=1) as pool:
        # `poll_interval=0.01`, not the 0.5s default: `shutdown()` blocks until
        # `serve_forever`'s loop next wakes up, so the default makes every teardown pay up
        # to half a second of pure waiting (measured).
        pool.submit(server.serve_forever, 0.01)
        try:
            yield server
        finally:
            server.shutdown()
