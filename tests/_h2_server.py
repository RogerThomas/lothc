"""An HTTPS test server that speaks HTTP/2 to a client that negotiates it (ALPN `h2`) and falls back
to `_server.py`'s HTTP/1.1 handler for one that doesn't, so a test can tell which protocol the
`http2=` setting actually got.

The HTTP/2 side answers every request with a small JSON body naming the request's method and
path; that's all a negotiation test needs. It uses the `h2` package (a dev dependency), the same
sans-IO protocol library the `perf` comparison clients build on.
"""

import json
import socket
import ssl
import sys
from collections.abc import Generator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

from _server import TestAppHandler, serve_in_background
from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import ConnectionTerminated, DataReceived, RequestReceived, StreamEnded


def _header_text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _answer(connection: H2Connection, stream_id: int, request: dict[str, str]) -> None:
    body = json.dumps({"method": request[":method"], "path": request[":path"]}).encode()
    connection.send_headers(
        stream_id,
        [
            (":status", "200"),
            ("content-type", "application/json"),
            ("content-length", str(len(body))),
        ],
    )
    connection.send_data(stream_id, body, end_stream=True)


def _serve_http2(tls_socket: ssl.SSLSocket) -> None:
    """Answer every request on one HTTP/2 connection until the client closes it."""
    connection = H2Connection(H2Configuration(client_side=False))
    connection.initiate_connection()
    tls_socket.sendall(connection.data_to_send())
    requests: dict[int, dict[str, str]] = {}
    while data := tls_socket.recv(65535):
        for event in connection.receive_data(data):
            if isinstance(event, RequestReceived):
                requests[event.stream_id] = {
                    _header_text(name): _header_text(value) for name, value in event.headers or ()
                }
            elif isinstance(event, DataReceived):
                connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
            elif isinstance(event, StreamEnded):
                _answer(connection, event.stream_id, requests.pop(event.stream_id))
            elif isinstance(event, ConnectionTerminated):
                return
        tls_socket.sendall(connection.data_to_send())


class _HTTP2Server(ThreadingHTTPServer):
    """Wraps each accepted connection in TLS, then serves HTTP/2 if ALPN chose `h2`, else hands
    the connection to `TestAppHandler` as plain HTTP/1.1.

    As in `_tls_server.py`, the handshake runs on the per-connection handler thread, and a client
    disconnect or failed handshake is dropped silently.
    """

    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(("127.0.0.1", 0), TestAppHandler)
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
            if tls_socket.selected_alpn_protocol() == "h2":
                _serve_http2(tls_socket)
            else:
                super().finish_request(tls_socket, client_address)

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)


@contextmanager
def serve_http2(context: ssl.SSLContext) -> Generator[str]:
    """Serve over TLS with `context`, offering `h2` and `http/1.1` via ALPN; yields the base URL."""
    context.set_alpn_protocols(["h2", "http/1.1"])
    with serve_in_background(_HTTP2Server(context)) as server:
        yield f"https://localhost:{server.server_port}/"
