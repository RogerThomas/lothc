"""The async native pool's sockets: connected with the asyncio loop's `sock_connect` and watched
with `loop.add_reader`, on the pool's behalf (`native/src/pool.rs` owns the connections, Python
their sockets). The sync pool connects its own, in Rust.
"""

import asyncio
import socket
from dataclasses import dataclass
from dataclasses import field as _dataclass_field
from ipaddress import ip_address
from typing import Any

from ._native import Pool as _NativePool


async def _connect_socket(
    event_loop: asyncio.AbstractEventLoop, family: int, address: tuple[Any, ...]
) -> socket.socket:
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)  # noqa: FBT003 — the stdlib's own positional flag
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        await event_loop.sock_connect(sock, address)
    except BaseException:
        sock.close()
        raise
    return sock


async def _socket_addresses(
    event_loop: asyncio.AbstractEventLoop, host: str, port: int
) -> list[tuple[int, tuple[Any, ...]]]:
    """Where to connect for `host`: an IP address as is (no resolver round trip), else every
    address `getaddrinfo` gives, in its order."""
    try:
        address = ip_address(host)
    except ValueError:
        infos = await event_loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [(family, sockaddr) for family, _, _, _, sockaddr in infos]
    return [(socket.AF_INET6 if address.version == 6 else socket.AF_INET, (host, port))]


async def _first_address(event_loop: asyncio.AbstractEventLoop, host: str) -> str | None:
    """reqwest's local resolution for a `socks5://` proxy's target: the first address `host`
    resolves to (`None` if none), which goes in the SOCKS request instead of the name."""
    try:
        infos = await event_loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as error:
        raise OSError(f"error resolving for socks proxy: {error}") from error
    return next((str(sockaddr[0]) for _, _, _, _, sockaddr in infos), None)


async def _open_socket(
    event_loop: asyncio.AbstractEventLoop, host: str, port: int
) -> socket.socket:
    """A connected, non-blocking `TCP_NODELAY` socket to `host:port` (reqwest's defaults), trying
    each address in turn."""
    last_error: OSError | None = None
    for family, address in await _socket_addresses(event_loop, host, port):
        try:
            return await _connect_socket(event_loop, family, address)
        except OSError as error:
            last_error = error
    raise last_error if last_error is not None else OSError(f"no addresses found for {host}")


async def _open_connection_socket(
    event_loop: asyncio.AbstractEventLoop, host: str, port: int, resolve: str | None
) -> tuple[socket.socket, str | None]:
    """`_open_socket` to `host:port` (the origin, or its proxy), after resolving `resolve` first
    if asked (a `socks5://` proxy's target, which reqwest resolves before connecting the proxy)."""
    resolved = None if resolve is None else await _first_address(event_loop, resolve)
    return await _open_socket(event_loop, host, port), resolved


@dataclass(slots=True, eq=False)
class _LoopConnections:
    """The sockets behind one async native pool, which owns the connections but not their
    sockets: it asks for one with `spawn` (connected with `loop.sock_connect`, then watched with
    `loop.add_reader`) and hands one back with `close` when it retires the connection."""

    _event_loop: asyncio.AbstractEventLoop
    _pool: _NativePool | None = _dataclass_field(default=None, init=False)
    _sockets: dict[int, socket.socket] = _dataclass_field(default_factory=dict, init=False)
    # Strong references, so a connect in flight isn't garbage-collected.
    _connecting: set[asyncio.Task[None]] = _dataclass_field(default_factory=set, init=False)

    async def _connect(self, conn: int, host: str, port: int, resolve: str | None) -> None:
        try:
            sock, resolved = await _open_connection_socket(self._event_loop, host, port, resolve)
        except OSError as error:
            if self._pool is not None:
                self._pool.connect_failed(conn, str(error))
            return
        pool = self._pool
        if pool is None:  # the client closed while this was connecting
            sock.close()
            return
        self._sockets[conn] = sock
        fd = sock.fileno()
        self._event_loop.add_reader(fd, pool.on_readable, conn)
        pool.connected(conn, fd, resolved)

    def _close(self, conn: int) -> None:
        sock = self._sockets.pop(conn, None)
        if sock is None:
            return
        fd = sock.fileno()
        self._event_loop.remove_reader(fd)
        self._event_loop.remove_writer(fd)
        sock.close()

    def attach(self, pool: _NativePool) -> None:
        self._pool = pool

    def spawn(self, conn: int, host: str, port: int, resolve: str | None) -> None:
        task = self._event_loop.create_task(self._connect(conn, host, port, resolve))
        self._connecting.add(task)
        task.add_done_callback(self._connecting.discard)

    def close(self, conn: int) -> None:
        self._close(conn)

    def close_all(self) -> None:
        self._pool = None
        for task in self._connecting:
            task.cancel()
        for conn in list(self._sockets):
            self._close(conn)
