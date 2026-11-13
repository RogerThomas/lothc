"""lothc's exceptions, and building an `HTTPResponseError` (with its `raises=` body)."""

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from types import get_original_bases
from typing import (
    Any,
    TypeIs,
    TypeVar,
    get_args,
    get_origin,
)

from ._decode import _decode_json_line, _Decoder, _decoder_for, _JSONObjectDecoder
from ._headers import CaseInsensitiveDict
from ._response import RequestInfo


class HTTPResponseError[E = object](Exception):
    """Raised when a request gets a 4xx/5xx response (`error_for_status=True`, the default).

    A response WAS received — for a request that never got one, see `HTTPTransportError`.
    `.request` is the request actually sent — the target you asked for, not necessarily the one a
    final response came from if redirects were followed. `.headers` are the error response's own
    (e.g. a `Retry-After` once retries are exhausted, or a request id to quote to support), and
    `.body` is the whole body; `.body_start` is just its first 100 bytes, as used in the message.

    `.error` is the body decoded: as `E` when a subclass pins it
    (`class APIError(HTTPResponseError[NotFound | Conflict]): pass`, passed as `raises=`), else
    as a JSON object (`dict`), else `None` (HTML, text, an array). When a pinned `E` didn't match,
    `.parse_error` holds why (the decode library's own `ValueError`, or a `ValueError` chained to
    whatever else decoding raised, e.g. a `RecursionError` for a body nested too deep to parse).
    `.parse_error` isn't
    carried across pickling, since pydantic's `ValidationError` can't be pickled.
    """

    def __init__(
        self,
        status: int,
        body: bytes,
        request: RequestInfo,
        error: E | dict[str, Any] | None = None,
        *,
        headers: "CaseInsensitiveDict | None" = None,
        parse_error: ValueError | None = None,
    ) -> None:
        self.status = status
        self.parse_error = parse_error
        self.body = body
        self.body_start = body[:100]
        self.request = request
        self.error = error
        self.headers = headers if headers is not None else CaseInsensitiveDict()
        snippet = self.body_start.decode(errors="replace")
        truncation_marker = "…" if len(body) > 100 else ""
        super().__init__(f"Request failed with status {status}: {snippet}{truncation_marker}")

    def __reduce__(self) -> tuple[Any, ...]:
        # Exception's own pickling replays `self.args` — just the message here — into `__init__`,
        # which then fails for want of `body`/`request`, so an error could never cross a process
        # boundary (multiprocessing, a task queue). Rebuild from the real fields instead, as
        # `type(self)` so a `raises=` subclass comes back as itself (pickle finds it by name, so
        # it must be a module-level class).
        return (
            _rebuild_http_response_error,
            (type(self), self.status, self.body, self.request, self.error, self.headers),
        )


def _rebuild_http_response_error(
    cls: type[HTTPResponseError[Any]],
    status: int,
    body: bytes,
    request: RequestInfo,
    error: object,
    headers: "CaseInsensitiveDict",
) -> HTTPResponseError[Any]:
    """Unpickling target for `HTTPResponseError.__reduce__` (keyword-only `headers` rules out
    returning the class itself with positional args)."""
    return cls(status, body, request, error, headers=headers)


class HTTPTransportError(Exception):
    """Raised when a request never got a response at all — pyreqwest's own exception types
    never leak through; they're translated to this (or a subclass) at every call site.
    """


class HTTPConnectionError(HTTPTransportError):
    """The connection was never established, or was lost mid-request."""


class HTTPTimeoutError(HTTPTransportError):
    """A configured timeout elapsed: the total `timeout`, or `connect_timeout`, `read_timeout` or
    `pool_timeout` if set."""


@dataclass(slots=True)
class _ErrorBodyDecoding:
    """How a `raises=` class decodes a 4xx/5xx body. `pinned` is whether it pins an `E` at all,
    since `decoder` is `None` both when it doesn't and when it pins `E = bytes`."""

    pinned: bool
    decoder: _Decoder | None


def _is_response_error_class(value: object) -> TypeIs[type[HTTPResponseError[Any]]]:
    return isinstance(value, type) and issubclass(value, HTTPResponseError)


def _has_free_type_params(form: object) -> bool:
    """Whether `form` is, or still contains, a type parameter nothing bound."""
    if isinstance(form, TypeVar):
        return True
    return get_origin(form) is not None and bool(getattr(form, "__parameters__", ()))


def _bound_type_param(parameter: TypeVar, bindings: Mapping[TypeVar, object]) -> object:
    """What `parameter` stands for: its binding, else its PEP 696 default (checkers read a bare
    `class Base[T = APIError]` as `Base[APIError]`), else itself, still free."""
    if parameter in bindings:
        return bindings[parameter]
    if parameter.has_default():
        # A default may name an earlier parameter (`[T, U = list[T]]`).
        return _bind_type_params(parameter.__default__, bindings)
    return parameter


def _bind_type_params(form: object, bindings: Mapping[TypeVar, object]) -> object:
    """`form` with its type parameters replaced by their bindings, else their defaults
    (`T` -> `int`, `list[T]` -> `list[int]`); anything with neither is left as it was."""
    if isinstance(form, TypeVar):
        return _bound_type_param(form, bindings)
    parameters: tuple[TypeVar, ...] = getattr(form, "__parameters__", ())
    if get_origin(form) is None or not parameters:
        return form
    # A parametrized alias with free parameters (`list[T]`) is subscriptable, which `object`
    # can't say.
    alias: Any = form
    return alias[tuple(_bound_type_param(parameter, bindings) for parameter in parameters)]


def _pinned_error_form(cls: type, bindings: Mapping[TypeVar, object]) -> tuple[()] | tuple[object]:
    """`(E,)` for the `E` that `cls` pins on `HTTPResponseError`, or `()` if nothing does.

    Walks the bases as written (`types.get_original_bases`, which keeps their type arguments,
    unlike `__mro__`), depth first in declaration order, so a subclass of a subclass finds what
    its parent pinned. Type parameters bound along the way are carried down, so
    `class Base[T](HTTPResponseError[T])` then `class Final(Base[NotFound])` resolves to
    `NotFound`.
    """
    for base in get_original_bases(cls):
        origin = get_origin(base)
        if origin is HTTPResponseError:
            return (_bind_type_params(get_args(base)[0], bindings),)
        if origin is None:
            parent, parent_bindings = base, {}
        else:
            parent = origin
            arguments = (_bind_type_params(argument, bindings) for argument in get_args(base))
            parameters: tuple[TypeVar, ...] = getattr(origin, "__parameters__", ())
            parent_bindings = dict(zip(parameters, arguments, strict=False))
        if _is_response_error_class(parent) and parent is not HTTPResponseError:
            pinned = _pinned_error_form(parent, parent_bindings)
            if pinned:
                return pinned
    return ()


# One per `raises=` class, shared by both clients, like `_decoders` (which caches the decoder
# itself): what this saves is the walk through the class's bases on every error. Unbounded, since
# the classes come from source code, not data. `cache` stores nothing when the call raises, so an
# unusable `raises` (not an `HTTPResponseError` subclass, an unbound `E`, an `E` no library can
# decode) is a `TypeError` on every call: a caller bug, never cached and never swallowed.
@cache
def _error_body_decoding(raises: object) -> _ErrorBodyDecoding:
    """`raises`'s resolved `E` and its decoder. Takes `object`, since nothing has checked yet
    that `raises` really is an `HTTPResponseError` subclass."""
    if not _is_response_error_class(raises):
        raise TypeError(
            f"raises= must be HTTPResponseError or a subclass of it, got {raises!r}. To decode"
            " error bodies as a type E, subclass it: `class APIError(HTTPResponseError[E]): pass`."
        )
    pinned = _pinned_error_form(raises, {})
    if not pinned:
        return _ErrorBodyDecoding(pinned=False, decoder=None)
    (form,) = pinned
    if _has_free_type_params(form):
        raise TypeError(
            f"{raises.__qualname__} leaves HTTPResponseError's error type unbound ({form!r}), so"
            " there is nothing to decode an error body as. Pin it directly:"
            f" `class {raises.__name__}(HTTPResponseError[YourErrorType]): pass`."
        )
    return _ErrorBodyDecoding(pinned=True, decoder=_decoder_for(form))


def _validate_raises(raises: type[HTTPResponseError[Any]] | None) -> None:
    """Resolve a per-call `raises=` before anything is sent, so a bad class is a `TypeError` on
    every call rather than one that only surfaces on a real 4xx/5xx, in place of that error.
    `None` (the client's own, already resolved at construction) costs nothing."""
    if raises is not None:
        _error_body_decoding(raises)


def _json_object_or_none(body: bytes) -> dict[str, Any] | None:
    """`body` as a JSON object, or `None` if it isn't one (HTML, text, an array, `null`, or
    nested too deep to parse)."""
    try:
        return _JSONObjectDecoder().decode(body)
    except (ValueError, RecursionError):  # `RecursionError`: nested too deep for stdlib `json`
        return None


# Whatever the pinned `E` decodes to — genuinely dynamic, can't state it statically.
def _decode_pinned_error(body: bytes, decoder: _Decoder | None) -> Any:  # noqa: ANN401
    return body if decoder is None else _decode_json_line(body, decoder)


def _as_parse_error(error: Exception) -> ValueError:
    """`error` as the `ValueError` `.parse_error` promises: itself if it already is one (every
    decode library's own failure), else a `ValueError` chained to it (`__cause__`), for the
    `RecursionError` a deeply nested body raises and the `TypeError` a pinned `dict` subclass's
    own constructor can raise."""
    if isinstance(error, ValueError):
        return error
    parse_error = ValueError(f"error body could not be decoded: {error!r}")
    parse_error.__cause__ = error
    return parse_error


def _status_error(
    raises: type[HTTPResponseError[Any]],
    status: int,
    body: bytes,
    request_info: RequestInfo,
    headers: CaseInsensitiveDict,
) -> HTTPResponseError[Any]:
    """The `raises` error for a 4xx/5xx whose whole body has already been read.

    Decoding is best-effort, since an error body is where a server is least likely to honour its
    own contract: a body that doesn't match the pinned `E` (a proxy's HTML 502, an error shape
    the union doesn't cover) keeps the decode library's `ValueError` on `.parse_error` and falls
    back to a JSON object, else `None`, rather than replacing the status error with a
    `ValidationError`. A body too deeply nested to parse, or one a pinned `dict` subclass's
    constructor rejects, is treated the same, its error wrapped in a `ValueError`. Nothing pinned:
    a JSON object, else `None`.
    """
    decoding = _error_body_decoding(raises)
    if not decoding.pinned:
        return raises(status, body, request_info, _json_object_or_none(body), headers=headers)
    try:
        error = _decode_pinned_error(body, decoding.decoder)
    # An unusable `E` was already rejected when `raises` was resolved, so anything decoding
    # raises here is the body's fault: too deeply nested (`RecursionError`), or refused by a
    # pinned `dict` subclass's own constructor (`TypeError`).
    except (ValueError, RecursionError, TypeError) as decode_error:
        return raises(
            status,
            body,
            request_info,
            _json_object_or_none(body),
            headers=headers,
            parse_error=_as_parse_error(decode_error),
        )
    return raises(status, body, request_info, error, headers=headers)
