#!yeet
"""TLS spike: the POC over HTTPS behaves like lothc today (same failures, same messages)."""

import asyncio
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import trustme
from msgspec import Struct
from poc_client import PocClient

from lothc import HTTPClient, HTTPConnectionError


class _Resp(Struct):
    status: str


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = b'{"status": "success"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _handle  # noqa: N815
    do_POST = _handle  # noqa: N815

    def log_message(self, *args: object) -> None:
        pass


class _TLSServer(ThreadingHTTPServer):
    """Handshake on the handler thread, as tests/_tls_server.py does."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self._context = context
        super().__init__(("127.0.0.1", 0), _Handler)

    def finish_request(self, request, client_address) -> None:  # noqa: ANN001
        try:
            tls_socket = self._context.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        super().finish_request(tls_socket, client_address)


async def _message(client_cls: type, url: str, **kwargs: object) -> str:
    async with (
        client_cls(url, **kwargs)
        if client_cls is PocClient
        else client_cls(base_url=url, **kwargs) as client
    ):
        try:
            await client.get("/")
        except HTTPConnectionError as e:
            return str(e)
    raise AssertionError("no HTTPConnectionError")


async def main() -> None:
    ca = trustme.CA()
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1").configure_cert(context)
    server = _TLSServer(context)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"https://127.0.0.1:{server.server_port}/"
    ca_pem = ca.cert_pem.bytes()

    async with PocClient(url, root_certificates=[ca_pem]) as client:
        for _ in range(20):  # keep-alive over one TLS session
            assert (await client.get("/", into=_Resp)).data.status == "success"
        assert (await client.post("/", json={"k": 1}, into=_Resp)).data.status == "success"
        got = await asyncio.gather(*(client.get("/", into=_Resp) for _ in range(30)))
        assert all(r.data.status == "success" for r in got)
    async with PocClient(url, danger_accept_invalid_certs=True) as client:
        assert (await client.get("/", into=_Resp)).data.status == "success"

    for name, kwargs in (
        ("untrusted CA", {}),
        ("other CA", {"root_certificates": [trustme.CA().cert_pem.bytes()]}),
    ):
        lothc_msg = await _message(HTTPClient, url, **kwargs)
        poc_msg = await _message(PocClient, url, **kwargs)
        assert "UnknownIssuer" in lothc_msg and "UnknownIssuer" in poc_msg, (lothc_msg, poc_msg)
        print(
            f"{name}: both HTTPConnectionError with UnknownIssuer\n  lothc: {lothc_msg}\n  poc:   {poc_msg}"
        )
    # Wrong host name: the cert is for 127.0.0.1, connect as localhost.
    other = f"https://localhost:{server.server_port}/"
    lothc_msg = await _message(HTTPClient, other, root_certificates=[ca_pem])
    poc_msg = await _message(PocClient, other, root_certificates=[ca_pem])
    print(f"wrong name:\n  lothc: {lothc_msg}\n  poc:   {poc_msg}")
    assert "not valid for name" in lothc_msg and "not valid for name" in poc_msg
    print("tls: all checks passed")
