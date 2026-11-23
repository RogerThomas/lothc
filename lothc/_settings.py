"""`_TransportSettings`, the validated client settings every transport is built from,
`RetryAttempt`, what a `retry=` rule among them is asked about, and `RequestHook`, what a
`request_hook=` yields for each attempt."""

import inspect
import os
import sys
from collections.abc import (
    Callable,
    Collection,
    Mapping,
    Sequence,
)
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from typing import Literal
from urllib.parse import urlsplit

from ._errors import HTTPResponseError, HTTPTransportError, RequestInfo
from ._types import Headers


@dataclass(slots=True)
class RetryAttempt:
    """A failed attempt a client's `retry=` rule is asked about: one lothc would retry, since the
    call has retries left, its request can be sent again, and the failure is a `retry_statuses`
    response or a transport error a retry can fix.

    `attempt` is which retry this would be (0 for the first). `error` is the failure: for a
    status, the `HTTPResponseError` the call would raise for that response (the matching
    `raises=` class, its `.problem` decoded); otherwise the `HTTPTransportError`. `delay` is the
    wait lothc would take on its own (the response's `Retry-After`, else the backoff), `method`
    the upper-case HTTP method, and `elapsed` the seconds since the call started.
    """

    attempt: int
    error: HTTPResponseError | HTTPTransportError
    delay: float
    method: str
    elapsed: float


# A client's `retry=`: the seconds to wait before the next attempt, or `None` to stop retrying.
type _RetryRule = Callable[[RetryAttempt], float | None]


def _validate_retry_rule(rule: _RetryRule | None) -> None:
    """Refuse a `retry=` that isn't a plain callable, an `async` one included: the rule is called
    the same way by both clients, and nothing would await it.

    >>> async def rule(attempt: RetryAttempt) -> float | None: ...
    >>> _validate_retry_rule(rule)
    Traceback (most recent call last):
    ...
    TypeError: 'retry' takes a plain function, not an async one: rule
    """
    if rule is None:
        return
    if not callable(rule):
        raise TypeError(f"'retry' takes a function of a RetryAttempt, not {rule!r}")
    if inspect.iscoroutinefunction(rule) or inspect.iscoroutinefunction(type(rule).__call__):
        name = getattr(rule, "__qualname__", repr(rule))
        raise TypeError(f"'retry' takes a plain function, not an async one: {name}")


@dataclass(slots=True)
class RequestHook:
    """One attempt at a request, as a client's `request_hook=` yields it: `headers` are added to
    that attempt (empty at first; each replaces a header of the same name the request already
    has), and `status` is the response's, set by lothc once a response arrives (`None` until
    then, and if none does)."""

    headers: dict[str, str] = field(default_factory=dict[str, str])
    status: int | None = None


# A client's `request_hook=`: entered once per attempt with the request it's about to send.
type _RequestHookFactory = Callable[[RequestInfo], AbstractContextManager[RequestHook]]


def _validate_request_hook(hook: _RequestHookFactory | None) -> None:
    """Refuse a `request_hook=` that isn't a plain callable, an `async` one (an
    `@asynccontextmanager` included) too: both clients enter it as a `with` block would.

    >>> from contextlib import asynccontextmanager
    >>> @asynccontextmanager
    ... async def hook(request: RequestInfo): yield RequestHook()
    >>> _validate_request_hook(hook)
    Traceback (most recent call last):
    ...
    TypeError: 'request_hook' takes a plain function, not an async one: hook
    """
    if hook is None:
        return
    if not callable(hook):
        raise TypeError(f"'request_hook' takes a function of a RequestInfo, not {hook!r}")
    unwrapped = inspect.unwrap(hook)
    if (
        inspect.iscoroutinefunction(unwrapped)
        or inspect.isasyncgenfunction(unwrapped)
        or inspect.iscoroutinefunction(type(hook).__call__)
    ):
        name = getattr(hook, "__qualname__", repr(hook))
        raise TypeError(f"'request_hook' takes a plain function, not an async one: {name}")


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


def _retry_status_set(
    retry_statuses: Collection[int] | None, default: frozenset[int]
) -> frozenset[int]:
    """`retry_statuses` as the set the retry loop checks a status against, refused at
    construction unless each one is a 4xx/5xx status, an `int` (not a `bool`). `None` means the
    defaults; an empty collection retries no status, while a transport failure is still retried.
    A bare `int` (`retry_statuses=503`) is a `TypeError`, not one status.

    >>> sorted(_retry_status_set((503, 429), frozenset({500})))
    [429, 503]
    >>> len(_retry_status_set(range(500, 600), frozenset()))
    100
    >>> _retry_status_set((), frozenset({500}))
    frozenset()
    >>> _retry_status_set([302], frozenset())
    Traceback (most recent call last):
    ...
    ValueError: 'retry_statuses' takes 4xx/5xx statuses (400 to 599), not 302
    """
    if retry_statuses is None:
        return default
    if isinstance(retry_statuses, int):
        raise TypeError(f"'retry_statuses' takes a collection of statuses, not {retry_statuses!r}")
    for status in retry_statuses:
        if isinstance(status, bool) or not isinstance(status, int):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(f"'retry_statuses' takes int statuses, not {status!r}")
        if not 400 <= status <= 599:
            raise ValueError(f"'retry_statuses' takes 4xx/5xx statuses (400 to 599), not {status}")
    return frozenset(retry_statuses)


def _validate_max_retries(max_retries: int) -> None:
    """Refuse a `max_retries` (the client's, or one call's) that isn't a count: a non-`int`
    (a `bool` included) is a `TypeError`, a negative one a `ValueError`.

    >>> _validate_max_retries(-1)
    Traceback (most recent call last):
    ...
    ValueError: 'max_retries' must be 0 or more, not -1
    """
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError(f"'max_retries' must be an int, not {max_retries!r}")
    if max_retries < 0:
        raise ValueError(f"'max_retries' must be 0 or more, not {max_retries}")


type TlsVersion = Literal["TLSv1.0", "TLSv1.1", "TLSv1.2", "TLSv1.3"]
"""A TLS version for `min_tls_version`/`max_tls_version`. lothc's own alias rather than
pyreqwest's identical one, so no pyreqwest name appears anywhere in lothc's public signatures."""

type NativeMode = Literal["auto", "off", "on"]
"""Which transport a client uses: `"auto"`, lothc's native transport where it runs (Linux, macOS)
and supports the settings (not `http2=True`), pyreqwest otherwise; `"off"`, always pyreqwest;
`"on"`, always native, refusing Windows and `http2=True` with `NotImplementedError`."""


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
    retry_statuses: frozenset[int]
    retry_rule: _RetryRule | None
    request_hook: _RequestHookFactory | None
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
    native: NativeMode

    def __post_init__(self) -> None:
        # pyreqwest's own rule, checked here so a bad `base_url` fails at construction rather than
        # on entry: without the trailing slash, a relative path would replace the last segment.
        if self.base_url is not None and not _is_joinable_base_url(self.base_url):
            raise ValueError("base_url must end with a trailing slash '/'")
        # `no_proxy` is an exclusion list on `proxy`; with no proxy it would silently do nothing.
        if self.no_proxy is not None and self.proxy is None:
            raise ValueError("'no_proxy' needs a 'proxy' to exclude hosts from")
        if self.native not in ("auto", "off", "on"):
            raise ValueError(f"'native' must be 'auto', 'off' or 'on', not {self.native!r}")
        _validate_max_retries(self.max_retries)
        _validate_retry_rule(self.retry_rule)
        _validate_request_hook(self.request_hook)


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
    """Whether a client being constructed gets the native transport, per its `native=` mode (see
    `NativeMode`), unless the private, test-only override `LOTHC_TRANSPORT` is `native` (as if
    `"on"`) or `pyreqwest` (as if `"off"`), which wins over the argument. Read once per client
    (never per request); any other override value is ignored.

    The override exists so `task test-native`/`task test-pyreqwest` run every test on one
    transport with none changed, which is why it wins. Not documented for users. A test that
    picks a transport itself must unset it. Native is never used on Windows: it waits on raw
    sockets with `poll(2)` and `loop.add_reader`, which asyncio's default Windows loop doesn't
    have, so `"auto"` falls back there and `"on"` raises.
    """
    mode = settings.native
    match os.environ.get("LOTHC_TRANSPORT"):
        case "native":
            mode = "on"
        case "pyreqwest":
            mode = "off"
        case _:
            pass
    if mode == "off":
        return False
    if sys.platform == "win32":
        if mode == "auto":
            return False
        raise NotImplementedError('the native transport (native="on") is not supported on Windows')
    # `"auto"` never raises for a supported configuration, so it leaves HTTP/2 to pyreqwest.
    return mode == "on" or not settings.http2
