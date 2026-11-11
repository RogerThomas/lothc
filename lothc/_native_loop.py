"""The async native pool's sockets: connected with the asyncio loop's `sock_connect` and watched
with `loop.add_reader`, on the pool's behalf (`native/src/pool.rs` owns the connections, Python
their sockets). The sync pool connects its own, in Rust.

Each socket is set up as `native/src/connect.rs` describes (reqwest's connector): `resolve=`'s
addresses arrive from the pool, `local_address` is bound before connecting and limits the
addresses tried to its family, and each attempt gets its share of `connect_timeout`. The pool
sets keepalive once a socket is connected, and enforces `connect_timeout` as a whole.
"""

import asyncio
import socket
from dataclasses import dataclass
from dataclasses import field as _dataclass_field
from functools import partial
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any

from ._native import ConnectOptions as _ConnectOptions
from ._native import Pool as _NativePool

type _Address = tuple[int, tuple[Any, ...]]


def _connect_attempts(
    addresses: list[_Address], local: IPv4Address | IPv6Address | None, timeout: float | None
) -> list[tuple[int, tuple[Any, ...], float | None]]:
    """The addresses to try, in order, each with its own connect timeout, as hyper-util's
    `ConnectingTcp` orders them (`connect.rs`'s `attempts`): `local`'s family only, if there is a
    local address, else the first address's family first, then the other; each family's addresses
    share `timeout` equally.

    >>> v4, v6 = (socket.AF_INET, ("127.0.0.1", 80)), (socket.AF_INET6, ("::1", 80))
    >>> [(address[0], share) for _, address, share in _connect_attempts([v6, v4, v6], None, 1.0)]
    [('::1', 0.5), ('::1', 0.5), ('127.0.0.1', 1.0)]
    >>> [address[0] for _, address, _ in _connect_attempts([v6, v4], ip_address("10.0.0.1"), None)]
    ['127.0.0.1']
    """
    if local is not None:
        local_family = socket.AF_INET6 if local.version == 6 else socket.AF_INET
        preferred = [address for address in addresses if address[0] == local_family]
        fallback: list[_Address] = []
    else:
        first = addresses[0][0] if addresses else socket.AF_INET
        preferred = [address for address in addresses if address[0] == first]
        fallback = [address for address in addresses if address[0] != first]
    return [
        (family, sockaddr, None if timeout is None else timeout / len(group))
        for group in (preferred, fallback)
        for family, sockaddr in group
    ]


def _bound_socket(family: int, local: IPv4Address | IPv6Address | None) -> socket.socket:
    """A non-blocking `TCP_NODELAY` socket (reqwest's defaults), bound to `local` (port 0) if it is
    of `family`."""
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)  # noqa: FBT003 — the stdlib's own positional flag
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if local is not None and (local.version == 6) == (family == socket.AF_INET6):
            sock.bind((str(local), 0))
    except BaseException:
        sock.close()
        raise
    return sock


async def _connect_socket(
    event_loop: asyncio.AbstractEventLoop,
    sock: socket.socket,
    address: tuple[Any, ...],
    timeout: float | None,
) -> socket.socket:
    try:
        async with asyncio.timeout(timeout):
            await event_loop.sock_connect(sock, address)
    except BaseException:
        sock.close()
        raise
    return sock


async def _socket_addresses(
    event_loop: asyncio.AbstractEventLoop, host: str, port: int, overrides: list[str] | None
) -> list[_Address]:
    """Where to connect for `host`: `resolve=`'s `overrides` for it, or an IP address as is (no
    resolver round trip), else every address `getaddrinfo` gives, in its order."""
    if overrides is not None:
        return [(_family(ip_address(ip)), (ip, port)) for ip in overrides]
    try:
        address = ip_address(host)
    except ValueError:
        infos = await event_loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [(family, sockaddr) for family, _, _, _, sockaddr in infos]
    return [(_family(address), (host, port))]


def _family(address: IPv4Address | IPv6Address) -> int:
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


async def _first_address(event_loop: asyncio.AbstractEventLoop, host: str) -> str | None:
    """reqwest's local resolution for a `socks4://` or `socks5://` proxy's target: the first
    address `host` resolves to (`None` if none), which goes in the SOCKS request instead of the
    name."""
    try:
        infos = await event_loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as error:
        raise OSError(f"error resolving for socks proxy: {error}") from error
    return next((str(sockaddr[0]) for _, _, _, _, sockaddr in infos), None)


@dataclass(slots=True)
class _Target:
    """Where one connection's socket connects (the origin, or its proxy), and how."""

    host: str
    port: int
    # `resolve=`'s addresses for `host`, if it has any.
    overrides: list[str] | None
    local: IPv4Address | IPv6Address | None
    connect_timeout: float | None


async def _open_socket(event_loop: asyncio.AbstractEventLoop, target: _Target) -> socket.socket:
    """A connected socket to `target`, trying each address in turn. A socket that can't be set up
    (its bind fails) ends it at once, as in hyper-util; a failed connect moves on to the next
    address, and the first failure is the one raised."""
    addresses = await _socket_addresses(event_loop, target.host, target.port, target.overrides)
    first_error: OSError | None = None
    for family, address, timeout in _connect_attempts(
        addresses, target.local, target.connect_timeout
    ):
        sock = _bound_socket(family, target.local)
        try:
            return await _connect_socket(event_loop, sock, address, timeout)
        except OSError as error:
            first_error = first_error or error
    raise first_error or OSError("tcp connect error: Network unreachable")


async def _open_connection_socket(
    event_loop: asyncio.AbstractEventLoop, target: _Target, resolve: str | None
) -> tuple[socket.socket, str | None]:
    """`_open_socket` to `target` (the origin, or its proxy), after resolving `resolve` first if
    asked (a `socks4://` or `socks5://` proxy's target, which reqwest resolves before connecting
    the proxy)."""
    resolved = None if resolve is None else await _first_address(event_loop, resolve)
    return await _open_socket(event_loop, target), resolved


def _failure_message(error: OSError) -> str:
    """What a failed connect reports: `asyncio.timeout`'s `TimeoutError` has no message."""
    return str(error) or "tcp connect error: operation timed out"


@dataclass(slots=True, eq=False)
class _LoopConnections:
    """The sockets behind one async native pool, which owns the connections but not their
    sockets: it asks for one with `spawn` (connected with `loop.sock_connect`, then watched with
    `loop.add_reader`) and hands one back with `close` when it retires the connection, or gives
    up on its connect (`connect_timeout`)."""

    _event_loop: asyncio.AbstractEventLoop
    _options: _ConnectOptions
    _pool: _NativePool | None = _dataclass_field(default=None, init=False)
    _local: IPv4Address | IPv6Address | None = _dataclass_field(default=None, init=False)
    _sockets: dict[int, socket.socket] = _dataclass_field(default_factory=dict, init=False)
    # Strong references, so a connect in flight isn't garbage-collected; by connection, so the
    # pool giving up on one can cancel it.
    _connecting: dict[int, asyncio.Task[None]] = _dataclass_field(default_factory=dict, init=False)

    async def _connect(self, conn: int, target: _Target, resolve: str | None) -> None:
        try:
            sock, resolved = await _open_connection_socket(self._event_loop, target, resolve)
        except OSError as error:
            if self._pool is not None:
                self._pool.connect_failed(
                    conn, _failure_message(error), isinstance(error, TimeoutError)
                )
            return
        pool = self._pool
        if pool is None:  # the client closed while this was connecting
            sock.close()
            return
        self._sockets[conn] = sock
        fd = sock.fileno()
        self._event_loop.add_reader(fd, pool.on_readable, conn)
        pool.connected(conn, fd, resolved)

    def _forget(self, conn: int, task: asyncio.Task[None]) -> None:
        if self._connecting.get(conn) is task:
            del self._connecting[conn]

    def _close(self, conn: int) -> None:
        task = self._connecting.pop(conn, None)
        if task is not None:
            task.cancel()
        sock = self._sockets.pop(conn, None)
        if sock is None:
            return
        fd = sock.fileno()
        self._event_loop.remove_reader(fd)
        self._event_loop.remove_writer(fd)
        sock.close()

    def attach(self, pool: _NativePool) -> None:
        self._pool = pool
        # Parsed only now, after the pool has checked it as pyreqwest does.
        local_address = self._options.local_address
        self._local = None if local_address is None else ip_address(local_address)

    def spawn(
        self, conn: int, host: str, port: int, resolve: str | None, overrides: list[str] | None
    ) -> None:
        target = _Target(host, port, overrides, self._local, self._options.connect_timeout)
        task = self._event_loop.create_task(self._connect(conn, target, resolve))
        self._connecting[conn] = task
        task.add_done_callback(partial(self._forget, conn))

    def close(self, conn: int) -> None:
        self._close(conn)

    def stop(self) -> None:
        """The pool has closed: connect nothing more. It has closed every socket already but
        those of streams still reading their bodies, which it closes as each one ends."""
        self._pool = None
        for task in self._connecting.values():
            task.cancel()
