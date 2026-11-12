"""Stdlib-only proxies for the proxy tests: an HTTP forward proxy that also tunnels `CONNECT`
(`serve_http_proxy`), the same behind TLS (`serve_https_proxy`, an `https://` proxy), and minimal
SOCKS5 and SOCKS4/4a servers (`serve_socks5_proxy`, `serve_socks4_proxy`). Each records what it
received, so a test can assert that a request really went through it, exactly how it was written,
and with which credentials.

All are function-scoped (one per test, ~1ms to start), so a test reads only its own records.
Records are appended before the proxy answers, so they're complete once the client's call returns.
All connect upstream to the requested port on `127.0.0.1`, whatever host was asked for, since
every test server listens there: a client that resolved `localhost` to `::1`, or asked for a name
that doesn't resolve, still reaches it, and the record keeps what it really asked for. The HTTP
proxy's `upstream_port` sends everything to one port instead, so a request for a port no test
server can bind (80, 443) still reaches one.
"""

import selectors
import socket
import socketserver
import ssl
import threading
import time
from base64 import b64encode
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import override
from urllib.parse import urlsplit

from _server import serve_in_background
from _tls_server import finish_a_failed_handshake


class ProxyBehaviour(Enum):
    """How the HTTP proxy answers a `CONNECT` (forwarded requests are always answered)."""

    TUNNEL = "tunnel"
    # Read the request, then never answer: a stalled proxy, for the deadline tests.
    SILENT = "silent"
    # Read the request, then close the connection without a response.
    HANG_UP = "hang-up"
    # Answer with something that isn't an HTTP response.
    GARBAGE = "garbage"
    # Answer with a response head that never ends.
    ENDLESS_HEADERS = "endless-headers"
    # Answer 200 with 101 headers, one more than the client parses.
    TOO_MANY_HEADERS = "too-many-headers"
    # Tunnel, its 200's head arriving in two parts.
    SPLIT_HEAD = "split-head"


class SocksBehaviour(Enum):
    """How the SOCKS5 server answers a client's greeting."""

    SERVE = "serve"
    # Read the greeting, then never answer.
    SILENT = "silent"
    # Read the greeting, then close the connection without a reply.
    HANG_UP = "hang-up"
    # Reply with a SOCKS4 version byte.
    GARBAGE = "garbage"
    # Select username/password whatever the client offered.
    WRONG_METHOD = "wrong-method"


class Socks4Behaviour(Enum):
    """How the SOCKS4 server answers a request."""

    SERVE = "serve"
    # Read the request, then never answer.
    SILENT = "silent"
    # Read the request, then close the connection without a reply.
    HANG_UP = "hang-up"


@dataclass(slots=True)
class ProxiedRequest:
    """One request as the HTTP proxy received it: the raw request line, every header in arrival
    order with its casing, the body, and the client port of the connection it came on."""

    line: str
    headers: list[tuple[str, str]]
    body: bytes
    client_port: int


@dataclass(slots=True)
class SocksHandshake:
    """One SOCKS5 handshake as the server received it."""

    methods: bytes
    # `(username, password)` when the client authenticated.
    credentials: tuple[str, str] | None
    # SOCKS5's ATYP: 1 an IPv4 address, 3 a domain name, 4 an IPv6 address.
    address_type: int
    host: str
    port: int


@dataclass(slots=True)
class Socks4Request:
    """One SOCKS4 request as the server received it: its bytes as they arrived, and what they
    ask for."""

    raw: bytes
    port: int
    # `DSTIP`: for SOCKS4a, `0.0.0.x` with `x` nonzero, and `domain` the name to resolve.
    ip: str
    user_id: bytes
    domain: str | None


@dataclass(slots=True)
class ProxyTLSHandshake:
    """One TLS handshake as the HTTPS proxy completed it: the name the client asked for (SNI,
    `None` without one), the protocol ALPN settled on (`None` when the client offered none of
    the proxy's), and whether the client presented a certificate."""

    server_name: str | None
    alpn: str | None
    client_certificate: bool


class _HTTPProxyServer(ThreadingHTTPServer):
    def __init__(
        self,
        credentials: tuple[str, str] | None,
        connect_status: int,
        behaviour: ProxyBehaviour,
        upstream_port: int | None,
        hold_later_tunnels: threading.Event | None,
        answer_at_most: int | None = None,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _HTTPProxyHandler)
        self.expected_authorization = (
            None
            if credentials is None
            else "Basic " + b64encode(":".join(credentials).encode()).decode()
        )
        self.connect_status = connect_status
        self.behaviour = behaviour
        self.upstream_port = upstream_port
        self.hold_later_tunnels = hold_later_tunnels
        self.answer_at_most = answer_at_most
        self.requests: list[ProxiedRequest] = []
        self.answered_tunnels: list[int] = []
        self.tunnels = 0
        self.tunnels_lock = threading.Lock()

    @override
    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        pass  # a client that hangs up mid-tunnel is expected; nothing worth a traceback here


def _remember_server_name(
    names: threading.local, _connection: object, server_name: str | None, _context: object
) -> None:
    """`sni_callback`: the name a handshake asked for, kept for the thread doing it."""
    names.server_name = server_name


class _HTTPSProxyServer(_HTTPProxyServer):
    """`_HTTPProxyServer` behind TLS: each connection's handshake runs on its handler thread, as
    `_tls_server.py`'s does, and is recorded once it completes. A failed one is dropped silently,
    since it's what the negative controls expect."""

    def __init__(
        self,
        context: ssl.SSLContext,
        credentials: tuple[str, str] | None,
        connect_status: int,
        behaviour: ProxyBehaviour,
        upstream_port: int | None,
        *,
        stalled_handshake: bytes | None,
    ) -> None:
        super().__init__(credentials, connect_status, behaviour, upstream_port, None)
        self.context = context
        self.stalled_handshake = stalled_handshake
        self.tls_handshakes: list[ProxyTLSHandshake] = []
        self._names = threading.local()
        context.set_alpn_protocols(["http/1.1"])
        context.sni_callback = partial(_remember_server_name, self._names)

    @override
    def finish_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        if not isinstance(request, socket.socket):
            raise TypeError(f"Expected a stream socket, got {type(request)!r}")
        if self.stalled_handshake is not None:
            request.sendall(self.stalled_handshake)
            _drain(request)
            return
        # As `_tls_server.py`'s, so a failed handshake's alert reaches the client.
        tls_socket = self.context.wrap_socket(
            request, server_side=True, do_handshake_on_connect=False
        )
        with tls_socket:
            try:
                tls_socket.do_handshake()
            except (ssl.SSLError, ConnectionError):
                finish_a_failed_handshake(tls_socket)
                return
            self.tls_handshakes.append(
                ProxyTLSHandshake(
                    self._names.server_name,
                    tls_socket.selected_alpn_protocol(),
                    tls_socket.getpeercert(binary_form=True) is not None,
                )
            )
            super().finish_request(tls_socket, client_address)


class _SocksServer(ThreadingHTTPServer):
    def __init__(
        self,
        credentials: tuple[str, str] | None,
        behaviour: SocksBehaviour,
        port: int,
        connect_reply: list[bytes] | None,
    ) -> None:
        super().__init__(("127.0.0.1", port), _SocksHandler)
        self.credentials = credentials
        self.behaviour = behaviour
        self.connect_reply = connect_reply
        self.handshakes: list[SocksHandshake] = []

    @override
    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        pass  # as `_HTTPProxyServer`


class _Socks4Server(ThreadingHTTPServer):
    def __init__(
        self, behaviour: Socks4Behaviour, port: int, connect_reply: list[bytes] | None
    ) -> None:
        super().__init__(("127.0.0.1", port), _Socks4Handler)
        self.behaviour = behaviour
        self.connect_reply = connect_reply
        self.requests: list[Socks4Request] = []

    @override
    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        pass  # as `_HTTPProxyServer`


@dataclass(slots=True)
class HTTPProxy:
    url: str
    requests: list[ProxiedRequest] = field(default_factory=list[ProxiedRequest])
    # An `https://` proxy's TLS handshakes; always empty for an `http://` one.
    tls_handshakes: list[ProxyTLSHandshake] = field(default_factory=list[ProxyTLSHandshake])
    # The client port of each `CONNECT` answered with a 200, once it has been.
    answered_tunnels: list[int] = field(default_factory=list[int])


@dataclass(slots=True)
class SocksProxy:
    # `host:port`, for the test to put a scheme (`socks5://`/`socks5h://`) and credentials on.
    address: str
    handshakes: list[SocksHandshake] = field(default_factory=list[SocksHandshake])


@dataclass(slots=True)
class Socks4Proxy:
    # `host:port`, for the test to put a scheme (`socks4://`/`socks4a://`) and credentials on.
    address: str
    requests: list[Socks4Request] = field(default_factory=list[Socks4Request])


def _received(source: socket.socket) -> bytes:
    """What `source` has to read now. From a TLS socket, the rest of the record too: bytes it has
    already decrypted are invisible to `select`."""
    chunk = source.recv(65536)
    while chunk and isinstance(source, ssl.SSLSocket) and source.pending():
        chunk += source.recv(source.pending())
    return chunk


def _pipe(client: socket.socket, upstream: socket.socket) -> None:
    """Copy bytes both ways until both sides have closed (each EOF is passed on as a half-close,
    except to a TLS socket, whose `shutdown` drops TLS: the pipe ends there instead). Without
    Nagle either way (the handlers disable it on the client's side), as the test server, so a
    piece passed on isn't held for the receiver's delayed ACK of the last."""
    upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    with selectors.DefaultSelector() as selector:
        selector.register(client, selectors.EVENT_READ, (client, upstream))
        selector.register(upstream, selectors.EVENT_READ, (upstream, client))
        open_sides = 2
        while open_sides:
            for key, _ in selector.select():
                source, destination = key.data
                try:
                    chunk = _received(source)
                except OSError:
                    return
                if chunk:
                    destination.sendall(chunk)
                    continue
                if isinstance(destination, ssl.SSLSocket):
                    return
                selector.unregister(source)
                open_sides -= 1
                with suppress(OSError):
                    destination.shutdown(socket.SHUT_WR)


def _drain(connection: socket.socket) -> None:
    """Read and drop everything until the client goes away."""
    while connection.recv(1024):
        pass


def _read_exactly(stream: socketserver.StreamRequestHandler, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.rfile.read(size - len(data))
        if not chunk:
            raise ConnectionError("the client closed mid-message")
        data += chunk
    return data


class _HTTPProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Unbuffered, so nothing the client sends after a `CONNECT` is stranded in a read buffer
    # instead of reaching the tunnel.
    rbufsize = 0
    # As `TestAppHandler`: a forwarded response's head and body are separate writes, and the
    # body otherwise waited 40ms on Linux for the client's delayed ACK of the head.
    disable_nagle_algorithm = True

    def _server(self) -> _HTTPProxyServer:
        server = self.server
        if not isinstance(server, _HTTPProxyServer):
            raise TypeError(f"Expected the proxy server, got {type(server)!r}")
        return server

    def _record(self, body: bytes) -> None:
        _, client_port = self.client_address
        self._server().requests.append(
            ProxiedRequest(self.requestline, list(self.headers.items()), body, client_port)
        )

    def _authorized(self) -> bool:
        expected = self._server().expected_authorization
        if expected is None or self.headers.get("Proxy-Authorization") == expected:
            return True
        self.send_response_only(407)
        self.send_header("Proxy-Authenticate", 'Basic realm="proxy"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def _forward(self) -> None:
        body = _read_exactly(self, int(self.headers.get("Content-Length", "0")))
        self._record(body)
        if not self._authorized():
            return
        target = urlsplit(self.path)
        upstream = HTTPConnection(
            "127.0.0.1", self._server().upstream_port or target.port or 80, timeout=10
        )
        try:
            upstream.putrequest(
                self.command,
                (target.path or "/") + (f"?{target.query}" if target.query else ""),
                skip_host=True,
                skip_accept_encoding=True,
            )
            for name, value in self.headers.items():
                if name.lower() not in {"proxy-authorization", "proxy-connection", "connection"}:
                    upstream.putheader(name, value)
            upstream.endheaders(body or None)
            response = upstream.getresponse()
            data = response.read()
            headers = response.getheaders()
        finally:
            upstream.close()
        self.send_response_only(response.status, response.reason)
        for name, value in headers:
            if name.lower() not in {"connection", "transfer-encoding", "content-length"}:
                self.send_header(name, value)
        if self.command == "HEAD":
            self.send_header("Content-Length", response.getheader("Content-Length", "0"))
        else:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _wait_if_held(self) -> bool:
        """With `hold_later_tunnels`, a `CONNECT` after the first waits for it to be set. Whether
        to answer it at all: not one past `answer_at_most`."""
        server = self._server()
        with server.tunnels_lock:
            server.tunnels += 1
            tunnel = server.tunnels
        if tunnel > 1 and server.hold_later_tunnels is not None:
            server.hold_later_tunnels.wait(timeout=10)
        return server.answer_at_most is None or tunnel <= server.answer_at_most

    def _refuse(self, status: int) -> None:
        self.send_response_only(status)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True

    def do_CONNECT(self) -> None:
        self._record(b"")
        server = self._server()
        self.close_connection = True
        if server.behaviour is ProxyBehaviour.SILENT:
            _drain(self.connection)
            return
        if server.behaviour is ProxyBehaviour.HANG_UP:
            return
        if server.behaviour is ProxyBehaviour.GARBAGE:
            self.wfile.write(b"garbage\r\n\r\n")
            return
        if server.behaviour is ProxyBehaviour.ENDLESS_HEADERS:
            self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Long: " + b"x" * 10_000)
            _drain(self.connection)
            return
        if server.behaviour is ProxyBehaviour.TOO_MANY_HEADERS:
            self.wfile.write(b"HTTP/1.1 200 OK\r\n" + b"X-Header: x\r\n" * 101 + b"\r\n")
            return
        if not self._authorized():
            return
        if server.connect_status != 200:
            self._refuse(server.connect_status)
            return
        if not self._wait_if_held():
            _drain(self.connection)
            return
        _, _, port = self.path.rpartition(":")
        upstream = socket.create_connection(
            ("127.0.0.1", server.upstream_port or int(port)), timeout=10
        )
        upstream.settimeout(None)
        if server.behaviour is ProxyBehaviour.SPLIT_HEAD:
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n")
            time.sleep(0.02)  # so the rest arrives in a read of its own
            self.wfile.write(b"\r\n")
        else:
            self.send_response_only(200, "Connection established")
            self.end_headers()
        _, client_port = self.client_address
        server.answered_tunnels.append(client_port)
        with upstream:
            _pipe(self.connection, upstream)

    def do_GET(self) -> None:
        self._forward()

    def do_POST(self) -> None:
        self._forward()

    def do_PUT(self) -> None:
        self._forward()

    def do_PATCH(self) -> None:
        self._forward()

    def do_DELETE(self) -> None:
        self._forward()

    def do_HEAD(self) -> None:
        self._forward()

    def do_OPTIONS(self) -> None:
        self._forward()

    @override
    def log_message(self, format: str, *args: object) -> None:
        pass


class _SocksHandler(socketserver.StreamRequestHandler):
    """RFC 1928 SOCKS5 (CONNECT only), with RFC 1929 username/password auth when the server has
    credentials: it picks username/password if it has credentials, no-auth otherwise."""

    rbufsize = 0
    disable_nagle_algorithm = True  # as `_HTTPProxyHandler`

    def _server(self) -> _SocksServer:
        server = self.server
        if not isinstance(server, _SocksServer):
            raise TypeError(f"Expected the SOCKS server, got {type(server)!r}")
        return server

    def _authenticate(self) -> tuple[str, str]:
        _version, username_length = _read_exactly(self, 2)
        username = _read_exactly(self, username_length).decode()
        (password_length,) = _read_exactly(self, 1)
        password = _read_exactly(self, password_length).decode()
        return username, password

    def _read_address(self) -> tuple[int, str, int]:
        _version, _command, _reserved, address_type = _read_exactly(self, 4)
        if address_type == 1:
            host = socket.inet_ntop(socket.AF_INET, _read_exactly(self, 4))
        elif address_type == 4:
            host = socket.inet_ntop(socket.AF_INET6, _read_exactly(self, 16))
        else:
            (length,) = _read_exactly(self, 1)
            host = _read_exactly(self, length).decode()
        port = int.from_bytes(_read_exactly(self, 2))
        return address_type, host, port

    @override
    def handle(self) -> None:
        server = self._server()
        _version, method_count = _read_exactly(self, 2)
        methods = _read_exactly(self, method_count)
        if server.behaviour is SocksBehaviour.SILENT:
            _drain(self.connection)
            return
        if server.behaviour is SocksBehaviour.HANG_UP:
            return
        if server.behaviour is SocksBehaviour.GARBAGE:
            self.wfile.write(b"\x04\x00")
            return
        method = 0x00 if server.credentials is None else 0x02
        if server.behaviour is SocksBehaviour.WRONG_METHOD:
            self.wfile.write(b"\x05\x02")
            return
        if method not in methods:
            self.wfile.write(b"\x05\xff")
            return
        self.wfile.write(bytes([0x05, method]))
        credentials = None if method == 0x00 else self._authenticate()
        if credentials is not None:
            accepted = credentials == server.credentials
            self.wfile.write(b"\x01\x00" if accepted else b"\x01\x01")
            if not accepted:
                server.handshakes.append(SocksHandshake(methods, credentials, 0, "", 0))
                return
        address_type, host, port = self._read_address()
        server.handshakes.append(SocksHandshake(methods, credentials, address_type, host, port))
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=10)
        except OSError:
            self.wfile.write(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")  # connection refused
            return
        upstream.settimeout(None)
        for index, part in enumerate(
            server.connect_reply or [b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"]
        ):
            if index:
                time.sleep(0.02)  # so each part arrives in a read of its own
            self.wfile.write(part)
        with upstream:
            _pipe(self.connection, upstream)


class _Socks4Handler(socketserver.StreamRequestHandler):
    """SOCKS4 `CONNECT`, with SOCKS4a's domain names: a request whose `DSTIP` is `0.0.0.x` (`x`
    nonzero) names the host after its user ID."""

    rbufsize = 0
    disable_nagle_algorithm = True  # as `_HTTPProxyHandler`

    def _server(self) -> _Socks4Server:
        server = self.server
        if not isinstance(server, _Socks4Server):
            raise TypeError(f"Expected the SOCKS4 server, got {type(server)!r}")
        return server

    def _read_terminated(self) -> bytes:
        """A NUL-terminated field, its NUL included."""
        data = _read_exactly(self, 1)
        while not data.endswith(b"\x00"):
            data += _read_exactly(self, 1)
        return data

    def _read_request(self) -> Socks4Request:
        head = _read_exactly(self, 8)
        user_id = self._read_terminated()
        address = head[4:8]
        is_4a = address[:3] == b"\x00\x00\x00" and address[3] != 0
        domain = self._read_terminated() if is_4a else b""
        return Socks4Request(
            head + user_id + domain,
            int.from_bytes(head[2:4]),
            socket.inet_ntop(socket.AF_INET, address),
            user_id[:-1],
            domain[:-1].decode() if is_4a else None,
        )

    @override
    def handle(self) -> None:
        server = self._server()
        request = self._read_request()
        server.requests.append(request)
        if server.behaviour is Socks4Behaviour.SILENT:
            _drain(self.connection)
            return
        if server.behaviour is Socks4Behaviour.HANG_UP:
            return
        try:
            upstream = socket.create_connection(("127.0.0.1", request.port), timeout=10)
        except OSError:
            self.wfile.write(b"\x00\x5b" + bytes(6))  # request rejected or failed
            return
        upstream.settimeout(None)
        for index, part in enumerate(server.connect_reply or [b"\x00\x5a" + bytes(6)]):
            if index:
                time.sleep(0.02)  # so each part arrives in a read of its own
            self.wfile.write(part)
        with upstream:
            _pipe(self.connection, upstream)


@contextmanager
def serve_http_proxy(
    *,
    credentials: tuple[str, str] | None = None,
    connect_status: int = 200,
    behaviour: ProxyBehaviour = ProxyBehaviour.TUNNEL,
    upstream_port: int | None = None,
    hold_later_tunnels: threading.Event | None = None,
    answer_at_most: int | None = None,
) -> Generator[HTTPProxy]:
    """An HTTP proxy on `127.0.0.1` for the block: it forwards absolute-form requests and tunnels
    `CONNECT`, after checking `Proxy-Authorization` against `credentials` if given (a 407
    otherwise), and answers a `CONNECT` with `connect_status` (anything but 200 refuses it).

    `upstream_port` replaces the port of every request and tunnel. `hold_later_tunnels` keeps
    every `CONNECT` but the first unanswered until it is set (recorded on arrival as usual), so a
    second connection stays mid-handshake for as long as a test needs. A `CONNECT` past
    `answer_at_most` is recorded but never answered, so no request can be carried on it."""
    server = _HTTPProxyServer(
        credentials, connect_status, behaviour, upstream_port, hold_later_tunnels, answer_at_most
    )
    with serve_in_background(server):
        yield HTTPProxy(
            f"http://127.0.0.1:{server.server_port}",
            server.requests,
            answered_tunnels=server.answered_tunnels,
        )


@contextmanager
def serve_https_proxy(
    context: ssl.SSLContext,
    *,
    host: str = "localhost",
    stalled_handshake: bytes | None = None,
    credentials: tuple[str, str] | None = None,
    connect_status: int = 200,
    behaviour: ProxyBehaviour = ProxyBehaviour.TUNNEL,
    upstream_port: int | None = None,
) -> Generator[HTTPProxy]:
    """`serve_http_proxy`'s proxy behind TLS with `context` (an `https://` proxy), its URL naming
    it as `host`. Each completed handshake is recorded (`tls_handshakes`), with ALPN offering
    `http/1.1` (this sets `context`'s ALPN protocols and SNI callback). With
    `stalled_handshake`, it accepts connections but never finishes a TLS handshake: it writes
    those bytes (`b""` for none), then reads and drops whatever arrives."""
    server = _HTTPSProxyServer(
        context,
        credentials,
        connect_status,
        behaviour,
        upstream_port,
        stalled_handshake=stalled_handshake,
    )
    with serve_in_background(server):
        yield HTTPProxy(
            f"https://{host}:{server.server_port}",
            server.requests,
            server.tls_handshakes,
            server.answered_tunnels,
        )


@contextmanager
def serve_socks4_proxy(
    *,
    behaviour: Socks4Behaviour = Socks4Behaviour.SERVE,
    port: int = 0,
    connect_reply: list[bytes] | None = None,
) -> Generator[Socks4Proxy]:
    """A SOCKS4/4a proxy on `127.0.0.1` (on `port`, any free one by default) for the block,
    answering as `behaviour` says. `connect_reply` replaces its reply to a `CONNECT` it could
    carry out (granted, bound to `0.0.0.0:0`), written in those parts 20ms apart; it still pipes
    the connection through whatever the reply says."""
    server = _Socks4Server(behaviour, port, connect_reply)
    with serve_in_background(server):
        yield Socks4Proxy(f"127.0.0.1:{server.server_port}", server.requests)


@contextmanager
def serve_socks5_proxy(
    *,
    credentials: tuple[str, str] | None = None,
    behaviour: SocksBehaviour = SocksBehaviour.SERVE,
    port: int = 0,
    connect_reply: list[bytes] | None = None,
) -> Generator[SocksProxy]:
    """A SOCKS5 proxy on `127.0.0.1` (on `port`, any free one by default) for the block,
    requiring `credentials` if given, answering as `behaviour` says. `connect_reply` replaces its
    reply to a `CONNECT` it could carry out (a success, bound to `0.0.0.0:0`), written in those
    parts 20ms apart; it still pipes the connection through whatever the reply says."""
    server = _SocksServer(credentials, behaviour, port, connect_reply)
    with serve_in_background(server):
        yield SocksProxy(f"127.0.0.1:{server.server_port}", server.handshakes)
