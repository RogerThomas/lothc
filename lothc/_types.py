"""The public type aliases for request inputs and auth, plus the re-authentication protocols."""

from collections.abc import (
    Awaitable,
    Callable,
    Mapping,
)
from io import BufferedIOBase
from pathlib import Path
from typing import (
    Any,
    Protocol,
    runtime_checkable,
)

from ._compat import (
    BaseModelTyping,
    StructTyping,
)

# `Data`/`TypedHeaders`/`JSONPayload`/`Params`/`Headers` below deliberately use `BaseModelTyping`/
# `StructTyping` (checker-local Protocols), not the real `BaseModel`/`Struct` — see
# `_compat.py`'s `StructTyping` docstring for why: it keeps these aliases fully typed even when
# msgspec/pydantic aren't resolvable to whatever type checker is running, with no precision loss
# when they are. `isinstance`/`issubclass`/`case` checks elsewhere in this file must still use the
# real `BaseModel`/`Struct` imported above — only these five alias definitions changed.
type Data = BaseModelTyping | StructTyping | bytes | dict[str, Any]
type TypedHeaders = BaseModelTyping | StructTyping
# `list[Any]` because a top-level JSON array is a perfectly valid body (bulk endpoints take one)
# and was already sent correctly at runtime; only the type rejected it.
type JSONPayload = dict[str, Any] | list[Any] | BaseModelTyping | StructTyping

# A caller never constructs these three directly — they just write a plain tuple literal, and
# _apply_form_value/_apply_sync_form_value tell them apart by exact arity + element type. Keep
# them private; File (below) is the only name a caller-facing type hint ever needs.
type _FormFileNoContentType = tuple[str, bytes | Path | BufferedIOBase]
type _FormFileWithContentType = tuple[str, bytes | Path | BufferedIOBase, str]
type _FormFileFromPathOverrideContentType = tuple[Path, str]
type File = (
    _FormFileNoContentType
    | _FormFileWithContentType
    | _FormFileFromPathOverrideContentType
    | Path
    | BufferedIOBase
)
# A tuple value in Form (below) means "repeat this part name once per element" — never
# "JSON-encode this tuple," which is what list[Any] means instead. Keeping the repeat marker as
# `tuple` and the JSON-array marker as `list` is what makes the two unambiguous.
# Every element must be the same *kind* of value, which is why this is a union of four homogeneous
# tuples rather than one `tuple[_FormValue, ...]`: the latter also admits `(b"...", "text/plain")`,
# which reads like a file paired with its content-type but has no filename to be one (only `Path`
# carries its own, hence File's `tuple[Path, str]`) — so it would quietly go out as a binary part
# plus a second text part reading "text/plain". Spelled this way it's a type error instead, and
# `_check_form_repeat` enforces the same rule at runtime for callers who bypass the checker.
type _FormRepeat = (
    tuple[str | int | float, ...]
    | tuple[bytes, ...]
    | tuple[File, ...]
    | tuple[list[Any] | JSONPayload, ...]
)
type _FormValue = str | int | float | bytes | list[Any] | JSONPayload | File
type Form = dict[str, _FormValue | _FormRepeat]
# A list[...]/tuple[...] value in Params (unlike Form's list-vs-tuple split, above) means "repeat
# this query key once per element, verbatim" — e.g. {"tag": ["a", "b"]} sends ?tag=a&tag=b. Both
# spellings are accepted, unlike Form, because Params has no existing meaning for a bare list the
# way Form does (there a bare list[Any] already means "JSON-encode this value", which is why Form
# needs list vs tuple to mean two different things) — there's nothing to disambiguate here, so
# both are just "a sequence of values for this key." Deliberately NOT `Sequence[...]` — `str`
# itself is a `Sequence[str]`, so an `isinstance` check against `Sequence` would silently explode
# a scalar string value into one query param per character; `list | tuple` has no such trap.
# lothc has no concept of what an element "means" (it's never itself a key=value pair to lothc);
# it's just another scalar occurrence of that key, exactly as the caller wrote it.
type _QueryValue = (
    str | int | float | bool | list[str | int | float | bool] | tuple[str | int | float | bool, ...]
)
type Params = Mapping[str, _QueryValue] | BaseModelTyping | StructTyping
type Headers = Mapping[str, str] | BaseModelTyping | StructTyping
type AuthProvider = Callable[[], Awaitable[str]]
type SyncAuthProvider = Callable[[], str]


@runtime_checkable
class _InvalidatableAuth(Protocol):
    """A `bearer_auth` callable that can also discard its current token, as `OAuthProvider` does.

    The client knows nothing about OAuth: any `bearer_auth` with this method gets the one-shot
    re-authentication in `_ReauthMiddleware`, which is the whole contract.
    """

    def __call__(self) -> Awaitable[str]: ...
    def invalidate(self, stale_access_token: str | None = None) -> None: ...


@runtime_checkable
class _SyncInvalidatableAuth(Protocol):
    """Sync mirror of `_InvalidatableAuth`, for `SyncOAuthProvider` and friends."""

    def __call__(self) -> str: ...
    def invalidate(self, stale_access_token: str | None = None) -> None: ...
