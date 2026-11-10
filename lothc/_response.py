"""`Response` and `RequestInfo`, what every non-streaming verb returns."""

from collections.abc import Mapping
from dataclasses import dataclass

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
    """

    data: TData
    status: int
    headers: CaseInsensitiveDict
    typed_headers: THeaders
    request: RequestInfo
    http_version: str
    elapsed: float
