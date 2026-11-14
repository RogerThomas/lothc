"""`Response` and `RequestInfo`, what every non-streaming verb returns."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from ._compat import (
    BaseModel,
    Struct,
    msgspec,
)
from ._headers import CaseInsensitiveDict
from ._types import TypedHeaders


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
    `Response` instead of raising: reading its `.data` or `.typed_headers` raises the
    `HTTPResponseError` the call would otherwise have raised (a new one on each read),
    `.raw_error` is the error body, and `.error` is that body decoded as
    `HTTPResponseError.error` decodes it (the `raises=` class's pinned type, else a JSON object
    as a `dict`, else `None`), decoded on first read. Every other response has `.error` and
    `.raw_error` `None`.
    """

    data: TData
    status: int
    headers: CaseInsensitiveDict
    typed_headers: THeaders
    request: RequestInfo
    http_version: str
    elapsed: float
    # Class-level defaults `__init__` never sets (`init=False` on a class without slots), so
    # building a `Response`, the native side's positional call included, costs what it did
    # before these existed, and an instance is no bigger. `object`, since `Response` has no type
    # parameter for the `raises=` class's error type: a `match` over the caller's own error
    # types, or `isinstance`, narrows it without an unknown type under any of the four checkers
    # (`Any` made zuban flag a value a mapping pattern captured as returning `Any`).
    error: object = field(default=None, init=False, repr=False, compare=False)
    raw_error: bytes | None = field(default=None, init=False, repr=False, compare=False)
