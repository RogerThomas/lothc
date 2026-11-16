"""lothc's exceptions, and `RequestInfo`, the request a status error (or a `Response`) describes.

`HTTPResponseError` is defined here, before `Response` (`_response.py`) annotates `.error` with it,
since annotations are evaluated at definition on 3.13. Choosing which `raises=` class a 4xx/5xx is
raised as is `_raises.py`'s.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, ClassVar

from ._decode import _JSONObjectDecoder
from ._headers import CaseInsensitiveDict


@dataclass(slots=True)
class RequestInfo:
    """The request actually sent, attached to `Response.request` (every non-streaming verb's
    result) and to `HTTPResponseError.request` — the target you asked for, not necessarily the
    one a final response came from if redirects were followed.
    """

    method: str
    url: str
    path: str
    host: str | None


def _json_object_or_none(body: bytes) -> dict[str, Any] | None:
    """`body` as a JSON object, or `None` if it isn't one (HTML, text, an array, `null`, or
    nested too deep to parse)."""
    try:
        return _JSONObjectDecoder().decode(body)
    except (ValueError, RecursionError):  # `RecursionError`: nested too deep for stdlib `json`
        return None


def _status_set(status: int | Iterable[int], cls: type) -> frozenset[int]:
    """A `status=` class keyword as the statuses it names, each an `int` from 400 to 599."""
    try:
        named = (status,) if isinstance(status, int) else tuple(status)
    except TypeError as error:  # not iterable
        raise TypeError(
            f"{cls.__qualname__}: status= takes an int or an iterable of ints (a tuple, a range),"
            f" got {status!r}"
        ) from error
    statuses: set[int] = set()
    for value in named:
        # The annotation doesn't stop a caller passing anything (`"404"`, `True`).
        if not isinstance(value, int) or isinstance(value, bool):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError(
                f"{cls.__qualname__}: status= takes an int or an iterable of ints (a tuple, a"
                f" range), got {value!r} in {status!r}"
            )
        if not 400 <= value <= 599:
            raise ValueError(
                f"{cls.__qualname__}: status= takes 4xx/5xx statuses (400 to 599), got {value!r}"
            )
        statuses.add(value)
    if not statuses:
        raise ValueError(f"{cls.__qualname__}: status= names no status ({status!r})")
    return frozenset(statuses)


# What `HTTPResponseError.__init__` is given for a `.problem` read from the body only when first
# read (a JSON object, else `None`), for a class that pins no problem type.
_unread_problem: Any = object()


class HTTPResponseError[P = object](Exception):
    """Raised when a request gets a 4xx/5xx response (`raise_for_status=True`, the default; with
    `raise_for_status=False`, the returned `Response`'s `.error`, and raised by reading its
    `.data`).

    A response WAS received — for a request that never got one, see `HTTPTransportError`.
    `.request` is the request actually sent — the target you asked for, not necessarily the one a
    final response came from if redirects were followed. `.headers` are the error response's own
    (e.g. a `Retry-After` once retries are exhausted, or a request id to quote to support), and
    `.body` is the whole body; `.body_start` is just its first 100 bytes, as used in the message.

    Subclass it to have an error body decoded and raised as its own class, and pass the subclasses
    as `raises=`: `class QuotaExceededError(HTTPResponseError[QuotaExceeded], status=429): pass`
    pins the problem type `P` and the statuses it's for (`status=` takes an int or an iterable of
    them, a tuple or a range, inherited by a subclass that doesn't pass its own; without one, a
    class is for every 4xx/5xx). A 4xx/5xx is raised as the first `raises=` class for its status
    whose `P` decodes the body (one that pins no `P` takes any body), else as plain
    `HTTPResponseError`.

    `.problem` is the body as the matched class's `P`; for a class that pins none, or plain
    `HTTPResponseError`, the body as a JSON object (`dict`), else `None` (HTML, text, an array),
    parsed when first read. On plain `HTTPResponseError` after a pinned class failed to decode the
    body, `.parse_error` holds the first such failure (the decode library's own `ValueError`, or a
    `ValueError` chained to whatever else decoding raised, e.g. a `RecursionError` for a body
    nested too deep to parse); otherwise it's `None`. `.parse_error` isn't carried across
    pickling, since pydantic's `ValidationError` can't be pickled.
    """

    # The statuses a `status=` class is for (`None`: every 4xx/5xx), read by `_raises.py`.
    _lothc_statuses: ClassVar[frozenset[int] | None] = None

    def __init_subclass__(
        cls, *, status: int | Iterable[int] | None = None, **kwargs: object
    ) -> None:
        super().__init_subclass__(**kwargs)
        if status is not None:
            cls._lothc_statuses = _status_set(status, cls)

    def __init__(
        self,
        status: int,
        body: bytes,
        request: RequestInfo,
        problem: P,
        *,
        headers: CaseInsensitiveDict | None = None,
        parse_error: ValueError | None = None,
    ) -> None:
        self.status = status
        self.parse_error = parse_error
        self.body = body
        self.body_start = body[:100]
        self.request = request
        # In `__dict__` directly, so `.problem` (a property, which keeps `P` covariant) can parse
        # an unread one on first read with one atomic `setdefault`, safe on a free-threaded build.
        if problem is not _unread_problem:
            self.__dict__["_problem"] = problem
        self.headers = headers if headers is not None else CaseInsensitiveDict()
        snippet = self.body_start.decode(errors="replace")
        truncation_marker = "…" if len(body) > 100 else ""
        super().__init__(f"Request failed with status {status}: {snippet}{truncation_marker}")

    def _read_problem(self) -> P:
        try:
            problem: P = self.__dict__["_problem"]
        except KeyError:
            problem = self.__dict__.setdefault("_problem", _json_object_or_none(self.body))
        return problem

    @property
    def problem(self) -> P:
        """The error body decoded: see the class docstring."""
        return self._read_problem()

    def __reduce__(self) -> tuple[Any, ...]:
        # Exception's own pickling replays `self.args` — just the message here — into `__init__`,
        # which then fails for want of `body`/`request`, so an error could never cross a process
        # boundary (multiprocessing, a task queue). Rebuild from the real fields instead, as
        # `type(self)` so a `raises=` subclass comes back as itself (pickle finds it by name, so
        # it must be a module-level class).
        return (
            _rebuild_http_response_error,
            (type(self), self.status, self.body, self.request, self._read_problem(), self.headers),
        )


def _rebuild_http_response_error(
    cls: type[HTTPResponseError[Any]],
    status: int,
    body: bytes,
    request: RequestInfo,
    problem: object,
    headers: CaseInsensitiveDict,
) -> HTTPResponseError[Any]:
    """Unpickling target for `HTTPResponseError.__reduce__` (keyword-only `headers` rules out
    returning the class itself with positional args)."""
    return cls(status, body, request, problem, headers=headers)


class HTTPTransportError(Exception):
    """Raised when a request never got a response at all — pyreqwest's own exception types
    never leak through; they're translated to this (or a subclass) at every call site.
    """


class HTTPConnectionError(HTTPTransportError):
    """The connection was never established, or was lost mid-request."""


class HTTPTimeoutError(HTTPTransportError):
    """A configured timeout elapsed: the total `timeout`, or `connect_timeout`, `read_timeout` or
    `pool_timeout` if set."""
