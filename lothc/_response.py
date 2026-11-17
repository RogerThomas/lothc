"""`Response`, what every non-streaming verb returns."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from ._compat import (
    BaseModel,
    Struct,
    msgspec,
)
from ._errors import HTTPResponseError, RequestInfo
from ._headers import CaseInsensitiveDict
from ._types import TypedHeaders


def _parse_typed_headers(
    headers: Mapping[str, str], response_headers_type: type[TypedHeaders]
) -> TypedHeaders:
    normalized = {name.lower().replace("-", "_"): value for name, value in headers.items()}
    if issubclass(response_headers_type, Struct):
        return msgspec.convert(normalized, type=response_headers_type, strict=False)
    # Statically, `TypedHeaders`'s remaining member here is always BaseModel — but unlike a
    # sequential `issubclass` chain (see `_decode_body`), basedpyright doesn't narrow past
    # `StructTyping` from the check above alone, so this explicit check is still required to
    # resolve `.model_validate` at all, not just for runtime-reachability.
    if issubclass(response_headers_type, BaseModel):
        return response_headers_type.model_validate(normalized)
    raise TypeError(f"Unsupported response_headers_type: {response_headers_type!r}")


@dataclass
class Response[TData, THeaders: TypedHeaders | None = None]:
    """What `get`/`post`/`put`/`patch`/`delete`/`head`/`options`/`download` return: the decoded body
    (`.data`) alongside the status and headers.

    `.typed_headers` is `None` unless `response_headers_type` was passed to the call that
    produced this. `.request` is the request actually sent — the target you asked for, not
    necessarily the one a final response came from if redirects were followed.
    `.http_version` is the protocol the response came over (e.g. `"HTTP/1.1"`), and `.elapsed`
    the seconds from sending the request to having the whole response, including any retries.

    With `raise_for_status=False` (the client's or the call's), a 4xx/5xx comes back as a
    `Response` instead of raising: `.error` is the `HTTPResponseError` the call would otherwise
    have raised, built on first read and the same object on every read after, and reading
    `.data` or `.typed_headers` raises an error of the same class and contents (a new one on each
    read). Every other response has `.error` `None`.
    """

    data: TData
    status: int
    headers: CaseInsensitiveDict
    typed_headers: THeaders
    request: RequestInfo
    http_version: str
    elapsed: float
    # A class-level default `__init__` never sets (`init=False` on a class without slots), so
    # building a `Response`, the native side's positional call included, costs what it did
    # before it existed, and an instance is no bigger. Plain `HTTPResponseError` (its `P` is
    # `object`): `.problem` is read-only, so `P` is covariant and a `match` or `isinstance` over
    # the caller's own subclasses narrows to each, `.problem` exactly typed.
    error: HTTPResponseError | None = field(default=None, init=False, repr=False, compare=False)
