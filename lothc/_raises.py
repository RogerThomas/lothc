"""Resolving `raises=` (one `HTTPResponseError` class, or a sequence of them) and choosing which
class a 4xx/5xx is raised as, plus the error `Response` a `raise_for_status=False` call returns."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache, cached_property
from types import get_original_bases
from typing import (
    TYPE_CHECKING,
    Any,
    NoReturn,
    TypeIs,
    TypeVar,
    get_args,
    get_origin,
)

from ._decode import _decode_json_line, _Decoder, _decoder_for
from ._errors import HTTPResponseError, RequestInfo, _as_parse_error, _unread_problem
from ._headers import CaseInsensitiveDict
from ._response import Response

# What a transport is given as a call's `raises=`, before it's resolved to a `_RaisesTable`.
type _RaisesArg = type[HTTPResponseError[Any]] | Sequence[type[HTTPResponseError[Any]]]


@dataclass(slots=True)
class _ErrorBodyDecoding:
    """How a `raises=` class decodes a 4xx/5xx body. `pinned` is whether it pins a problem type
    `form` at all, since `decoder` is `None` both when it doesn't and when it pins `bytes`."""

    pinned: bool
    form: object
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
    """`(P,)` for the `P` that `cls` pins on `HTTPResponseError`, or `()` if nothing does.

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
# itself): what this saves is the walk through the class's bases. Unbounded, since the classes
# come from source code, not data. `cache` stores nothing when the call raises, so an unusable
# class (an unbound `P`, a `P` no library can decode) is a `TypeError` on every call: a caller
# bug, never cached and never swallowed.
@cache
def _error_body_decoding(raises: type[HTTPResponseError[Any]]) -> _ErrorBodyDecoding:
    """`raises`'s resolved `P` and its decoder."""
    pinned = _pinned_error_form(raises, {})
    if not pinned:
        return _ErrorBodyDecoding(pinned=False, form=object, decoder=None)
    (form,) = pinned
    if form is object:
        # `HTTPResponseError`'s own default, e.g. a caller's generic base left unbound: any body.
        return _ErrorBodyDecoding(pinned=False, form=object, decoder=None)
    if _has_free_type_params(form):
        raise TypeError(
            f"{raises.__qualname__} leaves HTTPResponseError's problem type unbound ({form!r}),"
            " so there is nothing to decode an error body as. Pin it directly:"
            f" `class {raises.__name__}(HTTPResponseError[YourProblemType]): ...`."
        )
    return _ErrorBodyDecoding(pinned=True, form=form, decoder=_decoder_for(form))


@dataclass(slots=True)
class _Candidate:
    """One `raises=` class, ready to match: the statuses it's for (`None`: every 4xx/5xx) and
    how it decodes a body."""

    cls: type[HTTPResponseError[Any]]
    statuses: frozenset[int] | None
    decoding: _ErrorBodyDecoding


def _unreachable_reason(candidate: _Candidate, earlier: _Candidate) -> str | None:
    """Why `earlier`, listed before `candidate` in one `raises=`, leaves no 4xx/5xx for it to
    match, or `None` if it doesn't."""
    if candidate.cls is earlier.cls:
        return "it's listed twice"
    covered = earlier.statuses is None or (
        candidate.statuses is not None and candidate.statuses <= earlier.statuses
    )
    if not covered:
        return None
    if not earlier.decoding.pinned:
        return (
            f"{earlier.cls.__qualname__}, listed before it, pins no problem type, so takes every"
            " body for every status it's for"
        )
    if candidate.decoding.pinned and candidate.decoding.form == earlier.decoding.form:
        return (
            f"{earlier.cls.__qualname__}, listed before it, pins the same problem type for every"
            " status it's for"
        )
    return None


def _candidates(classes: tuple[object, ...]) -> list[_Candidate]:
    """`classes` as `_Candidate`s, each checked to be reachable: a `TypeError` for a class no
    response could ever be raised as."""
    candidates: list[_Candidate] = []
    for cls in classes:
        if not _is_response_error_class(cls):
            raise TypeError(
                "raises= takes an HTTPResponseError subclass or a sequence of them, got"
                f" {cls!r}. To decode error bodies as a type P, subclass it:"
                " `class APIError(HTTPResponseError[P]): ...`."
            )
        # The statuses `status=` set: package-private, as every `_` name is here.
        statuses = cls._lothc_statuses  # pylint: disable=protected-access
        candidate = _Candidate(cls, statuses, _error_body_decoding(cls))
        for earlier in candidates:
            reason = _unreachable_reason(candidate, earlier)
            if reason is not None:
                raise TypeError(f"raises= can never raise {cls.__qualname__}: {reason}")
        candidates.append(candidate)
    return candidates


@dataclass(slots=True, eq=False)
class _RaisesTable:
    """A resolved `raises=`: for each status some class names, the classes that may match it, in
    list order (a class with no `status=` is in every entry); `default` for any other status.
    `key` is the class, or tuple of classes, it was resolved from: what it pickles as, so an
    error `Response` holding one stays picklable (decoders aren't)."""

    key: type | tuple[object, ...]
    by_status: dict[int, tuple[_Candidate, ...]]
    default: tuple[_Candidate, ...]

    def __reduce__(self) -> tuple[Any, ...]:
        return (_resolve_raises, (self.key,))


def _build_raises_table(key: type | tuple[object, ...]) -> _RaisesTable:
    candidates = _candidates((key,) if isinstance(key, type) else key)
    statuses = {status for c in candidates if c.statuses is not None for status in c.statuses}
    by_status = {
        status: tuple(c for c in candidates if c.statuses is None or status in c.statuses)
        for status in statuses
    }
    default = tuple(c for c in candidates if c.statuses is None)
    return _RaisesTable(key, by_status, default)


# Every distinct `raises=` resolved so far, keyed by the class, or by a sequence's classes as a
# tuple (a list isn't hashable, and a tuple of classes hashes by identity, cheaply). Bounded like
# `_decoders`: past the bound a new value is resolved on every call instead.
_raises_tables: dict[type | tuple[object, ...], _RaisesTable] = {}
_raises_tables_max_size = 4096


def _is_sequence(value: object) -> TypeIs[Sequence[object]]:
    # A list or tuple first: checking against the `Sequence` ABC costs ~150ns a call.
    return isinstance(value, (list, tuple)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    )


def _raises_key(raises: object) -> type | tuple[object, ...]:
    """The cache key `raises` resolves under: the class itself, or a sequence's classes as a
    tuple (so a later change to the caller's list can't change a resolved table)."""
    if isinstance(raises, type):
        return raises
    if _is_sequence(raises):
        return tuple(raises)
    raise TypeError(
        f"raises= takes an HTTPResponseError subclass or a sequence of them, got {raises!r}"
    )


def _resolve_raises(raises: object) -> _RaisesTable:
    """`raises=` as a `_RaisesTable`, cached; a value no table can be built from is a
    `TypeError`, never cached. Takes `object`, since nothing has checked it yet."""
    key = _raises_key(raises)
    try:
        return _raises_tables[key]
    except (KeyError, TypeError):  # not resolved yet, or an unhashable entry `_candidates` rejects
        pass
    table = _build_raises_table(key)
    if len(_raises_tables) < _raises_tables_max_size:
        _raises_tables[key] = table
    return table


def _validate_raises(raises: _RaisesArg | None) -> None:
    """Resolve a per-call `raises=` before anything is sent, so a bad value is a `TypeError` on
    every call rather than one that only surfaces on a real 4xx/5xx, in place of that error.
    `None` (the client's own, already resolved at construction) costs nothing."""
    if raises is not None:
        _resolve_raises(raises)


def _call_raises(raises: _RaisesArg | None, client_raises: _RaisesTable) -> _RaisesTable:
    """A call's resolved `raises=`: its own, which replaces the client's, else the client's."""
    return client_raises if raises is None else _resolve_raises(raises)


# Whatever the pinned `P` decodes to — genuinely dynamic, can't state it statically.
def _decode_pinned_error(body: bytes, decoder: _Decoder | None) -> Any:  # noqa: ANN401
    return body if decoder is None else _decode_json_line(body, decoder)


# The matched `P` value, or `_unread_problem` — genuinely dynamic, can't state it statically.
def _matched_error(
    table: _RaisesTable, status: int, body: bytes
) -> tuple[type[HTTPResponseError[Any]], Any, ValueError | None]:
    """The class a 4xx/5xx is raised as, its `.problem`, and its `.parse_error`: the first
    candidate for `status` whose pinned `P` decodes `body` (one that pins none takes any body,
    its `.problem` parsed on first read), else plain `HTTPResponseError` with the first pinned
    candidate's decode failure.

    Decoding is best-effort, since an error body is where a server is least likely to honour its
    own contract: a body no pinned `P` matches (a proxy's HTML 502, a shape no class covers)
    falls through rather than replacing the status error with a `ValidationError`. A body too
    deeply nested to parse, or one a pinned `dict` subclass's constructor rejects, is treated the
    same, its error wrapped in a `ValueError`.
    """
    first_failure: ValueError | None = None
    for candidate in table.by_status.get(status, table.default):
        decoding = candidate.decoding
        if not decoding.pinned:
            return candidate.cls, _unread_problem, None
        try:
            problem = _decode_pinned_error(body, decoding.decoder)
        # An unusable `P` was already rejected when `raises` was resolved, so anything decoding
        # raises here is the body's fault: too deeply nested (`RecursionError`), or refused by a
        # pinned `dict` subclass's own constructor (`TypeError`).
        except (ValueError, RecursionError, TypeError) as decode_error:
            if first_failure is None:
                first_failure = _as_parse_error(decode_error)
            continue
        return candidate.cls, problem, None
    return HTTPResponseError, _unread_problem, first_failure


def _status_error(
    raises: _RaisesTable,
    status: int,
    body: bytes,
    request_info: RequestInfo,
    headers: CaseInsensitiveDict,
) -> HTTPResponseError[Any]:
    """The error a 4xx/5xx whose whole body has already been read is raised as (see
    `_matched_error`)."""
    cls, problem, parse_error = _matched_error(raises, status, body)
    return cls(status, body, request_info, problem, headers=headers, parse_error=parse_error)


class _ErrorResponse(Response[Any, Any]):
    """What a non-streaming verb returns for a 4xx/5xx under `raise_for_status=False` (see
    `Response`), from the parts a raising call builds its `HTTPResponseError` from: the class is
    matched only on first need, `.error` is built on first read and kept, and `.data`/
    `.typed_headers` raise a new error on each read (one shared instance grew its traceback on
    every read, and mixed tracebacks across threads on a free-threaded build).

    Not a dataclass: `Response`'s generated `__init__`, `__repr__` and `__eq__` would set or read
    `.data`. Its own read none of it, and pickling (`__getstate__`) leaves out the match and the
    kept `.error`, which may hold a pydantic `ValidationError` that can't be pickled; they're
    rebuilt on first read. The properties are hidden from type checkers, since ty and zuban
    reject a read-only property over a writable dataclass field: statically this is just a
    `Response`.
    """

    def __init__(  # pylint: disable=super-init-not-called
        self,
        raises: _RaisesTable,
        status: int,
        headers: CaseInsensitiveDict,
        request: RequestInfo,
        http_version: str,
        elapsed: float,
        body: bytes,
        error: HTTPResponseError[Any] | None = None,
    ) -> None:
        # No `super().__init__()`: it would set `.data` and `.typed_headers`, which raise here.
        self._raises = raises
        self._body = body
        self.status = status
        self.headers = headers
        self.request = request
        self.http_version = http_version
        self.elapsed = elapsed
        # The error already built for this response (the one a `retry=` rule was shown), kept
        # as `.error`, so the rule and the caller see one object.
        if error is not None:
            self.__dict__["_error"] = error

    @cached_property
    def _match(self) -> tuple[type[HTTPResponseError[Any]], Any, ValueError | None]:
        return _matched_error(self._raises, self.status, self._body)

    def _new_status_error(self) -> HTTPResponseError[Any]:
        cls, problem, parse_error = self._match
        return cls(
            self.status,
            self._body,
            self.request,
            problem,
            headers=self.headers,
            parse_error=parse_error,
        )

    def _kept_error(self) -> HTTPResponseError[Any]:
        # `setdefault`, one atomic step, so threads racing the first read all get one error.
        try:
            error: HTTPResponseError[Any] = self.__dict__["_error"]
        except KeyError:
            error = self.__dict__.setdefault("_error", self._new_status_error())
        return error

    def _raise_status_error(self) -> NoReturn:
        raise self._new_status_error()

    if not TYPE_CHECKING:

        @property
        def data(self) -> NoReturn:
            self._raise_status_error()

        @property
        def typed_headers(self) -> NoReturn:
            self._raise_status_error()

        @property
        def error(self) -> HTTPResponseError[Any]:
            return self._kept_error()

    def __getstate__(self) -> dict[str, object]:
        state = dict(self.__dict__)
        state.pop("_match", None)
        state.pop("_error", None)
        return state

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ErrorResponse):
            return NotImplemented
        return (
            self._raises.key,
            self.status,
            self.headers,
            self.request,
            self.http_version,
            self.elapsed,
            self._body,
        ) == (
            other._raises.key,
            other.status,
            other.headers,
            other.request,
            other.http_version,
            other.elapsed,
            other._body,
        )

    def __repr__(self) -> str:
        return (
            f"Response(status={self.status!r}, headers={self.headers!r},"
            f" request={self.request!r}, http_version={self.http_version!r},"
            f" elapsed={self.elapsed!r}, body={self._body!r})"
        )
