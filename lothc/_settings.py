"""`_TransportSettings`, the validated client settings every transport is built from."""

import os
import sys
from collections.abc import (
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from typing import Literal
from urllib.parse import urlsplit

from ._types import Headers


def _retry_method_set(
    retry_methods: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None,
    default: frozenset[str],
) -> frozenset[str]:
    """`retry_methods` as the upper-case set the middleware compares `request.method` against.

    Upper-cased because pyreqwest reports methods upper-case, so `{"get"}` used to match nothing
    and silently turned retries off. `None` means the idempotent defaults, but an *empty* set now
    means "retry nothing", as written: it used to fall back to the defaults too, via `or`. Not
    typed `Collection[str]`, which a bare `"GET"` satisfies and would split into `{"G", "E", "T"}`.

    >>> sorted(_retry_method_set(["get", "Post"], frozenset({"GET"})))
    ['GET', 'POST']
    >>> _retry_method_set(set(), frozenset({"GET"}))
    frozenset()
    >>> sorted(_retry_method_set(None, frozenset({"GET"})))
    ['GET']
    """
    if retry_methods is None:
        return default
    return frozenset(method.upper() for method in retry_methods)


type TlsVersion = Literal["TLSv1.0", "TLSv1.1", "TLSv1.2", "TLSv1.3"]
"""A TLS version for `min_tls_version`/`max_tls_version`. lothc's own alias rather than
pyreqwest's identical one, so no pyreqwest name appears anywhere in lothc's public signatures."""


def _is_joinable_base_url(base_url: str) -> bool:
    """Whether pyreqwest accepts `base_url`: no query or fragment, and a path that's empty or
    ends in `/`.

    >>> [_is_joinable_base_url(u) for u in ("http://h", "http://h/api/", "http://h/api")]
    [True, True, False]
    >>> _is_joinable_base_url("http://h/api/?x=1")
    False
    """
    split = urlsplit(base_url)
    return not split.query and not split.fragment and (not split.path or split.path.endswith("/"))


@dataclass(slots=True, kw_only=True)
class _TransportSettings:  # pylint: disable=too-many-instance-attributes
    """Everything a client needs to (re)build its pyreqwest client on entry. Held by both clients
    so the configuration lives in one place, `_configure_client_builder`, for both. A plain record
    of the constructor's transport settings, one field each, which is why it's over pylint's
    attribute limit: that rule is aimed at classes with behaviour, and splitting this into
    sub-records would only move the same fields one attribute access further away."""

    base_url: str | None
    default_headers: Headers | None
    user_agent: str | None
    timeout: float | None
    cookie_store: bool
    follow_redirects: bool
    max_redirects: int | None
    proxy: str | None
    no_proxy: list[str] | tuple[str, ...] | None
    max_retries: int
    retry_methods: frozenset[str]
    backoff_base: float
    max_retry_after: float | None
    connect_timeout: float | None
    read_timeout: float | None
    root_certificates: Sequence[bytes] | None
    identity_pem: bytes | None
    min_tls_version: TlsVersion | None
    max_tls_version: TlsVersion | None
    danger_accept_invalid_certs: bool
    https_only: bool
    http2: bool
    resolve: Mapping[str, str] | None
    local_address: str | None
    tcp_keepalive: float | None
    max_connections: int | None
    pool_idle_timeout: float | None
    pool_max_idle_per_host: int | None
    pool_timeout: float | None
    native: bool

    def __post_init__(self) -> None:
        # pyreqwest's own rule, checked here so a bad `base_url` fails at construction rather than
        # on entry: without the trailing slash, a relative path would replace the last segment.
        if self.base_url is not None and not _is_joinable_base_url(self.base_url):
            raise ValueError("base_url must end with a trailing slash '/'")
        # `no_proxy` is an exclusion list on `proxy`; with no proxy it would silently do nothing.
        if self.no_proxy is not None and self.proxy is None:
            raise ValueError("'no_proxy' needs a 'proxy' to exclude hosts from")


@cache
def _default_user_agent() -> str:
    """lothc's own `User-Agent`, so requests don't go out under pyreqwest's name.

    Read lazily and once: package metadata isn't free, and not every import opens a client.
    """
    try:
        return f"python-lothc/{_distribution_version('lothc')}"
    except PackageNotFoundError:  # pragma: no cover — a vendored copy with no installed metadata
        return "python-lothc"


def _uses_native_transport(settings: _TransportSettings) -> bool:
    """Whether a client being constructed gets the native transport: `native=True`, or the
    private, test-only override `LOTHC_TRANSPORT=native`, which wins over the argument. Read once
    per client (never per request), pyreqwest otherwise.

    The override exists so `task test-native` runs every test on the native transport with none
    changed, which is also why it wins: a `bool` argument can't tell an explicit `native=False`
    from the default. Not documented for users. A test that needs pyreqwest under that run must
    unset it. Either way, the native transport is refused on Windows: it waits on raw sockets
    with `poll(2)` and `loop.add_reader`, which asyncio's default Windows loop doesn't have.
    """
    if not settings.native and os.environ.get("LOTHC_TRANSPORT") != "native":
        return False
    if sys.platform == "win32":
        raise NotImplementedError("the native transport (native=True) is not supported on Windows")
    return True
