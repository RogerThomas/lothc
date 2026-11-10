"""The compiled `lothc._native` extension (native/src/): lothc's own HTTP/1.1 transport, hyper's
client driven from the asyncio loop (`Pool`) or the calling thread (`SyncPool`). Used only by
`_NativeTransport`/`_SyncNativeTransport` in `lothc/_transport_native.py`, selected by
`native=True` on either client.

The pools build every result from `glue` (lothc's own classes and helpers), so what a request
returns or raises is exactly what the pyreqwest transport does.
"""

import asyncio
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any, Protocol, final

from ._errors import (
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    HTTPTransportError,
)
from ._headers import CaseInsensitiveDict
from ._response import (
    RequestInfo,
    Response,
)

class _Glue(Protocol):
    """What the pools read off `glue` at construction (`_NativeGlue`)."""

    @property
    def response(self) -> Callable[..., Response[Any, Any]]: ...
    @property
    def request_info(self) -> Callable[[str, str, str, str | None], RequestInfo]: ...
    @property
    def lazy_headers(self) -> Callable[[RawHeaders], CaseInsensitiveDict]: ...
    @property
    def eager_headers(self) -> Callable[[RawHeaders], CaseInsensitiveDict]: ...
    @property
    def status_error(self) -> Callable[..., HTTPResponseError[Any]]: ...
    @property
    def parse_typed_headers(self) -> Callable[..., object]: ...
    @property
    def connection_error(self) -> type[HTTPConnectionError]: ...
    @property
    def timeout_error(self) -> type[HTTPTimeoutError]: ...
    @property
    def transport_error(self) -> type[HTTPTransportError]: ...
    @property
    def builder_error(self) -> Callable[[str], HTTPTransportError]: ...
    @property
    def decode_error(self) -> Callable[[str], HTTPConnectionError]: ...

class ClientBuildError(ValueError):
    """Settings a client can't be built with, raised by the pools' constructors where reqwest's
    `build()` raises its builder error (a TLS version range rustls can't satisfy, a `user_agent`
    that isn't a header value), with reqwest's message. The transports re-raise it as a plain
    `ValueError`, as the pyreqwest one does pyreqwest's `BuilderError`."""

@final
class RawHeaders:
    """Response headers kept in Rust until first read; built by the pools, not constructible."""

    def items(self) -> list[tuple[str, str]]:
        """Every `(name, value)` in arrival order; names lowercase, a repeated name once per
        value. A value that isn't visible ASCII raises `ValueError`."""

    def __len__(self) -> int: ...

@final
class Stream:
    """One streamed request on a `Pool`, from `Pool.stream`: `open` it, then `read` until `None`,
    and `close` it however that ended (closing one mid-body hangs up its connection)."""

    @property
    def request_info(self) -> RequestInfo: ...
    def open(self) -> asyncio.Future[tuple[int, RawHeaders, str]]:
        """Send it: a Future of its final head, `(status, headers, http_version)`, redirects
        followed. Fails as `request`'s Future does; an error status is not raised."""

    def read(self) -> asyncio.Future[bytes | None]:
        """A Future of the body's next chunk (decoded, for a compressed body), `None` at its end;
        one read at a time."""

    def close(self) -> None: ...

@final
class SyncStream:
    """`Stream` for a `SyncPool`: each call blocks, GIL released, under the request's total
    timeout; a signal doesn't interrupt the wait. One thread at a time."""

    @property
    def request_info(self) -> RequestInfo: ...
    def open(self) -> tuple[int, RawHeaders, str]: ...
    def read(self) -> bytes | None: ...
    def close(self) -> None: ...

@final
class Pool:
    """The async pool, bound to the event loop (and thread) it was created on; another thread's
    use is a `RuntimeError`."""

    def __init__(
        self,
        base_url: str | None,
        default_headers: dict[str, str] | None,
        user_agent: str,
        https_only: bool,
        follow_redirects: bool,
        max_redirects: int | None,
        root_certificates: Sequence[bytes],
        identity_pem: bytes | None,
        min_tls_version: str | None,
        max_tls_version: str | None,
        danger_accept_invalid_certs: bool,
        timeout: timedelta | None,
        pool_idle_timeout: timedelta | None,
        pool_max_idle_per_host: int | None,
        proxy: str | None,
        no_proxy: str | None,
        glue: _Glue,
        event_loop: asyncio.AbstractEventLoop,
        spawn_connect: Callable[[int, str, int, str | None], object],
        close_conn: Callable[[int], object],
    ) -> None:
        """`spawn_connect(conn, host, port, resolve)` asks Python to connect a socket for
        connection `conn` (to the origin or its proxy), resolving `resolve` first if it isn't
        `None` (a `socks5://` proxy's target), then `connected` or `connect_failed`;
        `close_conn(conn)` to unregister and close it. `timeout` is the client's, only converted
        (a negative one raises `ValueError`, as pyreqwest's builder does); each request carries
        its own. `pool_idle_timeout`/
        `pool_max_idle_per_host` default to reqwest's 90s and no cap. `proxy`/`no_proxy` are
        `proxy=` and `no_proxy=` (comma-separated); without `proxy`, the environment's proxies
        apply, read now. A `proxy` that isn't a URL raises `ValueError`. With
        `follow_redirects`, a request follows up to `max_redirects` (reqwest's 10 if `None`) as
        reqwest does, within its one timeout; a negative `max_redirects` raises `OverflowError`
        even without, as pyreqwest's builder does."""

    def set_callbacks(
        self,
        on_deadline: Callable[[], object],
        on_writable: Callable[[int], object],
        on_readable: Callable[[int], object],
    ) -> None:
        """Hand the pool its own bound `on_deadline`/`on_writable`/`on_readable`, once."""

    def request(
        self,
        method: str,
        path: str,
        query: object,
        headers: dict[str, str] | None,
        auth: str | None,
        body: object,
        body_kind: int,
        decode: Callable[[bytes], object] | None,
        timeout: float | None,
        raises: type[HTTPResponseError[Any]] | None,
        typed_headers: type | None,
        headers_only: bool,
    ) -> asyncio.Future[Response[Any, Any]]:
        """Start a request; its Future resolves to lothc's `Response` or raises its exception.

        `query` (and a `data=` form `body`) is a `dict` (a `list`/`tuple` value repeats the key)
        or `(name, value)` pairs. `body_kind`: 0 raw `bytes`/`str`, 1 already-encoded JSON, 2 a
        `dict`/`list` to serialize, 3 a form. `raises` is `None` for no status check. Raises
        before any I/O for a request that can't be built (`ValueError`, `HTTPTransportError`).
        """

    def stream(
        self,
        method: str,
        path: str,
        query: object,
        headers: dict[str, str] | None,
        auth: str | None,
        body: object,
        body_kind: int,
        timeout: float | None,
        accept: str | None,
        last_event_id: str | None,
    ) -> Stream:
        """Prepare a streamed request, sending nothing until its `open`: `request`'s arguments
        (failing as it does before any I/O), plus the `accept` and `last-event-id` headers
        `sse()` sets, which go first and last."""

    def connected(self, conn: int, fd: int, resolved: str | None = None) -> None:
        """`conn`'s socket `fd` is connected; `resolved` is the first address of the host
        `spawn_connect` asked to resolve, if any."""
    def connect_failed(self, conn: int, message: str) -> None: ...
    def on_readable(self, conn: int) -> None: ...
    def on_writable(self, conn: int) -> None: ...
    def cancel(self, fut: asyncio.Future[Response[Any, Any]]) -> None:
        """The caller's task was cancelled while awaiting `fut`."""

    def on_deadline(self) -> None: ...
    def close(self) -> None:
        """Fail every pending request with `RuntimeError` and drop every connection."""

@final
class SyncPool:
    """The sync pool, shareable across threads (each request runs on its caller's thread)."""

    def __init__(
        self,
        base_url: str | None,
        default_headers: dict[str, str] | None,
        user_agent: str,
        https_only: bool,
        follow_redirects: bool,
        max_redirects: int | None,
        root_certificates: Sequence[bytes],
        identity_pem: bytes | None,
        min_tls_version: str | None,
        max_tls_version: str | None,
        danger_accept_invalid_certs: bool,
        timeout: timedelta | None,
        pool_idle_timeout: timedelta | None,
        pool_max_idle_per_host: int | None,
        proxy: str | None,
        no_proxy: str | None,
        glue: _Glue,
    ) -> None:
        """As `Pool`, but idle connections expire only when the pool is used: no reaper."""
    def request(
        self,
        method: str,
        path: str,
        query: object,
        headers: dict[str, str] | None,
        auth: str | None,
        body: object,
        body_kind: int,
        decode: Callable[[bytes], object] | None,
        timeout: float | None,
        raises: type[HTTPResponseError[Any]] | None,
        typed_headers: type | None,
        headers_only: bool,
    ) -> Response[Any, Any]:
        """One blocking request, GIL released while it waits: `Pool.request`'s arguments."""

    def stream(
        self,
        method: str,
        path: str,
        query: object,
        headers: dict[str, str] | None,
        auth: str | None,
        body: object,
        body_kind: int,
        timeout: float | None,
        accept: str | None,
        last_event_id: str | None,
    ) -> SyncStream:
        """`Pool.stream`."""

    def close(self) -> None:
        """Fail every request still running (on any thread) with `RuntimeError`, by shutting its
        socket down, and drop every idle connection. A stream whose head has arrived reads on."""
