"""Loop-native POC client with lothc's `get()`/`post()` signatures and return types.

Everything a caller sees (`Response`, `RequestInfo`, `_LazyHeaders`, the error classes,
`into=` resolution, `raises=` validation, params/headers/json encoding, auth) is lothc's own
code; only the transport underneath is new (`_poc.Pool`, hyper on the asyncio loop).
"""

import asyncio
import base64
import os
import socket
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

import msgspec
from msgspec import Struct
from pydantic import BaseModel, TypeAdapter

from lothc import HTTPConnectionError, HTTPResponseError, HTTPTimeoutError, RequestInfo, Response
from lothc._decode import (
    _decoder_for,
    _decoders,
)
from lothc._encode import (
    _encode_headers,
    _encode_params,
    _pydantic_by_alias,
    _query_pairs,
)
from lothc._errors import (
    _status_error,
    _validate_raises,
)
from lothc._headers import _LazyHeaders
from lothc._settings import _default_user_agent

POC_BARE = os.environ.get("POC_BARE") == "1"  # read once: per request it cost ~0.5us


def _json_body(payload: Any) -> bytes:
    if isinstance(payload, BaseModel):
        return payload.model_dump_json(by_alias=_pydantic_by_alias(payload)).encode()
    return msgspec.json.encode(payload)


class PocClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float | None = 30.0,
        bearer_token: str | None = None,
        bearer_auth: Callable[[], Any] | None = None,
        basic_auth: tuple[str, str] | None = None,
        root_certificates: Sequence[bytes] | None = None,
        danger_accept_invalid_certs: bool = False,
    ) -> None:
        parts = urlsplit(base_url)
        self._https = parts.scheme == "https"
        self._root_certificates = list(root_certificates or ())
        self._danger_accept_invalid_certs = danger_accept_invalid_certs
        self._addr = (parts.hostname or "", parts.port or (443 if self._https else 80))
        self._host = parts.netloc
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._timeout = None if os.environ.get("POC_NO_TIMEOUT") == "1" else timeout
        self._raises: type[HTTPResponseError[Any]] = HTTPResponseError
        self._bearer_auth = bearer_auth
        if bearer_token is not None:
            self._static_auth: str | None = f"Bearer {bearer_token}"
        elif basic_auth is not None:
            self._static_auth = (
                "Basic " + base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
            )
        else:
            self._static_auth = None
        # `into` -> the callable handed to Rust, for forms lothc's own `_decoders` caches.
        self._decode_cache: dict[Any, Callable[[bytes], Any] | None] = {None: None, bytes: None}

    async def __aenter__(self) -> "PocClient":
        from _poc import Pool  # noqa: PLC0415  (only in the poc image)

        self._loop = asyncio.get_running_loop()
        self._socks: dict[int, socket.socket] = {}
        self._pool = Pool(
            self._host,
            self._origin,
            _default_user_agent(),
            self._loop,
            self._spawn_connect,
            self._close_conn,
            Response,
            RequestInfo,
            _LazyHeaders,
            _status_error,
            HTTPConnectionError,
            HTTPTimeoutError,
            self._https,
            self._root_certificates,
            self._danger_accept_invalid_certs,
        )
        self._pool.set_deadline_callback(self._pool.on_deadline)
        self._pool.set_bare(POC_BARE)
        return self

    async def __aexit__(self, *args: object) -> None:
        for conn in list(self._socks):
            self._close_conn(conn)

    def _spawn_connect(self, conn: int) -> None:
        self._loop.create_task(self._connect(conn))

    async def _connect(self, conn: int) -> None:
        sock = socket.socket()
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            await self._loop.sock_connect(sock, self._addr)
        except OSError as error:
            sock.close()
            self._pool.connect_failed(conn, str(error))
            return
        self._socks[conn] = sock
        self._loop.add_reader(sock.fileno(), self._pool.on_readable, conn)
        self._pool.connected(conn, sock.fileno())

    def _close_conn(self, conn: int) -> None:
        sock = self._socks.pop(conn, None)
        if sock is not None:
            self._loop.remove_reader(sock.fileno())
            sock.close()

    def _resolve_decode(self, into: Any) -> Callable[[bytes], Any] | None:
        decoder = _decoder_for(into)
        if decoder is None:
            decode = None
        elif isinstance(decoder, TypeAdapter):
            decode = decoder.validate_json
        else:
            decode = decoder.decode
        try:
            if _decoders.get(into) is decoder:
                self._decode_cache[into] = decode
        except TypeError:
            pass  # unhashable form: lothc rebuilds it per call, so do we
        return decode

    async def _auth(self, skip_auth: bool) -> str | None:
        if skip_auth:
            return None
        if self._bearer_auth is not None:
            return f"Bearer {await self._bearer_auth()}"
        return self._static_auth

    def _start(
        self,
        method: str,
        path: str,
        params: Any,
        headers: Any,
        auth: str | None,
        timeout: float | None,
        json: Any,
        into: Any,
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> "asyncio.Future[Response[Any]]":
        if raises is not None:
            _validate_raises(raises)
        try:
            decode = self._decode_cache[into]
        except (KeyError, TypeError):
            decode = self._resolve_decode(into)
        error_class = (raises if raises is not None else self._raises) if error_for_status else None
        return self._pool.request(
            method,
            path,
            # A plain dict goes to Rust as is (~0.8us of lothc's Python encoding skipped); a
            # model/Struct still goes through lothc's encoders, so aliases/None-dropping match.
            params
            if params is None or type(params) is dict
            else _query_pairs(_encode_params(params)),
            headers if headers is None or type(headers) is dict else _encode_headers(headers),
            auth,
            None if json is None else _json_body(json),
            json is not None,
            decode,
            timeout if timeout is not None else self._timeout,
            error_class,
        )

    async def get(
        self,
        path: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: Any = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any]:
        auth = (
            self._static_auth
            if self._bearer_auth is None and not skip_auth
            else await self._auth(skip_auth)
        )
        fut = self._start(
            "GET", path, params, headers, auth, timeout, None, into, error_for_status, raises
        )
        try:
            return await fut
        except asyncio.CancelledError:
            self._pool.cancel(fut)
            raise

    async def post(
        self,
        path: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: Any = None,
        into: Any = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any]:
        """`json=` only in the POC (lothc's `data=`/`form=`/`content=` not wired up)."""
        auth = (
            self._static_auth
            if self._bearer_auth is None and not skip_auth
            else await self._auth(skip_auth)
        )
        fut = self._start(
            "POST", path, params, headers, auth, timeout, json, into, error_for_status, raises
        )
        try:
            return await fut
        except asyncio.CancelledError:
            self._pool.cancel(fut)
            raise


__all__ = ["POC_BARE", "PocClient", "Struct"]


class SyncPocClient:
    """The sync spike: lothc's `SyncHTTPClient.get`/`post` shape over `_poc.SyncPool`."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float | None = 30.0,
        bearer_token: str | None = None,
        bearer_auth: Callable[[], str] | None = None,
        basic_auth: tuple[str, str] | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        self._parts = parts
        self._timeout = timeout
        self._raises: type[HTTPResponseError[Any]] = HTTPResponseError
        self._bearer_auth = bearer_auth
        if bearer_token is not None:
            self._static_auth: str | None = f"Bearer {bearer_token}"
        elif basic_auth is not None:
            self._static_auth = (
                "Basic " + base64.b64encode(f"{basic_auth[0]}:{basic_auth[1]}".encode()).decode()
            )
        else:
            self._static_auth = None
        self._decode_cache: dict[Any, Callable[[bytes], Any] | None] = {None: None, bytes: None}

    def __enter__(self) -> "SyncPocClient":
        from _poc import SyncPool  # noqa: PLC0415

        parts = self._parts
        self._pool = SyncPool(
            parts.hostname or "",
            parts.port or 80,
            f"{parts.scheme}://{parts.netloc}",
            _default_user_agent(),
            Response,
            RequestInfo,
            _LazyHeaders,
            _status_error,
            HTTPConnectionError,
            HTTPTimeoutError,
        )
        return self

    def __exit__(self, *args: object) -> None:
        del self._pool

    _resolve_decode = PocClient._resolve_decode

    def _send(
        self,
        method: str,
        path: str,
        params: Any,
        headers: Any,
        timeout: float | None,
        skip_auth: bool,
        json: Any,
        into: Any,
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> Response[Any]:
        if raises is not None:
            _validate_raises(raises)
        try:
            decode = self._decode_cache[into]
        except (KeyError, TypeError):
            decode = self._resolve_decode(into)
        if skip_auth:
            auth = None
        elif self._bearer_auth is not None:
            auth = f"Bearer {self._bearer_auth()}"
        else:
            auth = self._static_auth
        return self._pool.request(
            method,
            path,
            params
            if params is None or type(params) is dict
            else _query_pairs(_encode_params(params)),
            headers if headers is None or type(headers) is dict else _encode_headers(headers),
            auth,
            None if json is None else _json_body(json),
            json is not None,
            decode,
            timeout if timeout is not None else self._timeout,
            (raises if raises is not None else self._raises) if error_for_status else None,
        )

    def get(
        self,
        path: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: Any = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any]:
        return self._send(
            "GET", path, params, headers, timeout, skip_auth, None, into, error_for_status, raises
        )

    def post(
        self,
        path: str,
        *,
        params: Any = None,
        headers: Any = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: Any = None,
        into: Any = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any]:
        return self._send(
            "POST", path, params, headers, timeout, skip_auth, json, into, error_for_status, raises
        )


class LoopThreadSyncClient:
    """Option 1: sync calls run on the async POC in a private uvloop thread
    (`run_coroutine_threadsafe(...).result()`), one engine for both."""

    def __init__(self, base_url: str, **kwargs: Any) -> None:
        self._client = PocClient(base_url, **kwargs)

    def __enter__(self) -> "LoopThreadSyncClient":
        import threading  # noqa: PLC0415

        import uvloop  # noqa: PLC0415

        self._loop = uvloop.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._client.__aenter__(), self._loop).result()
        return self

    def __exit__(self, *args: object) -> None:
        asyncio.run_coroutine_threadsafe(self._client.__aexit__(), self._loop).result()
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

    def get(self, path: str, **kwargs: Any) -> Response[Any]:
        return asyncio.run_coroutine_threadsafe(
            self._client.get(path, **kwargs), self._loop
        ).result()
