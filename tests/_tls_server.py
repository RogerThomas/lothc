"""An HTTPS variant of `_server.py`'s stdlib test server, for exercising lothc's TLS settings
(`root_certificates`, `identity_pem`, `min_tls_version`/`max_tls_version`,
`danger_accept_invalid_certs`, `https_only`) against a real handshake instead of plain HTTP.

Same `TestAppHandler`, same endpoints; only the transport differs. The handshake runs in the
per-connection handler thread (`finish_request`), not in `accept()` on the `serve_forever` thread,
so a client that fails or stalls its handshake can never block the server for the next test.
"""

import socket
import ssl
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import trustme
from _server import TestAppHandler


class _TLSServer(ThreadingHTTPServer):
    """A `ThreadingHTTPServer` that wraps each accepted connection in TLS before handling it.

    A failed handshake is the expected outcome of every negative-control test here (an untrusted
    CA, a missing client cert, a TLS version mismatch), so it's dropped silently rather than
    printing a traceback; so is a client disconnect, as in `_server.py`. Any other handler error
    still prints, so a real bug in a route stays visible.
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
            super().finish_request(tls_socket, client_address)

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        # `SSLError` too, not just `ConnectionError`: with TLS 1.3 the server only learns a
        # client cert was rejected (or the client learns the server's was) after the handshake
        # has "completed", so the failure can surface on the handler's first read instead.
        if isinstance(sys.exc_info()[1], (ConnectionError, ssl.SSLError)):
            return
        super().handle_error(request, client_address)


def tls_server_context(ca: trustme.CA) -> ssl.SSLContext:
    """A server-side context presenting a cert for `localhost`/`127.0.0.1` issued by `ca`."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    # Via a temp file rather than trustme's `configure_cert`, whose signature mentions pyOpenSSL
    # (not installed), so it types as partially unknown; stdlib only loads a cert chain from disk.
    with ca.issue_cert("localhost", "127.0.0.1").private_key_and_cert_chain_pem.tempfile() as path:
        context.load_cert_chain(path)
    return context


@contextmanager
def serve_tls(context: ssl.SSLContext) -> Generator[str]:
    """Serve `TestAppHandler` over TLS with `context`, yielding the `https://` base URL.

    `localhost`, not `127.0.0.1`, in the URL: that's the name the certificate is verified against
    here, so hostname verification is exercised too rather than only IP-SAN matching.
    """
    with _TLSServer(context) as server, ThreadPoolExecutor(max_workers=1) as pool:
        # `poll_interval=0.01` for the same reason as `conftest.py`'s plain server: the 0.5s
        # default makes every `shutdown()` wait up to half a second.
        pool.submit(server.serve_forever, 0.01)
        try:
            yield f"https://localhost:{server.server_port}/"
        finally:
            server.shutdown()
