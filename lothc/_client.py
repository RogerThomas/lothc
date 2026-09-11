import asyncio
import codecs
import queue
import threading
import time
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import aclosing, asynccontextmanager, closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from io import BufferedIOBase
from json import dumps as _json_dumps
from json import loads as _json_loads
from mimetypes import guess_type as _guess_mime_type
from pathlib import Path
from typing import Any, ClassVar, Literal, Self, cast, overload

from pyreqwest.client import BaseClientBuilder, Client, ClientBuilder, SyncClient, SyncClientBuilder
from pyreqwest.client.types import TlsVersion
from pyreqwest.exceptions import BuilderError as PyreqwestBuilderError
from pyreqwest.exceptions import NetworkError as PyreqwestNetworkError
from pyreqwest.exceptions import RedirectError as PyreqwestRedirectError
from pyreqwest.exceptions import RequestTimeoutError as PyreqwestRequestTimeoutError
from pyreqwest.exceptions import TransportError as PyreqwestTransportError
from pyreqwest.middleware import Next, SyncNext
from pyreqwest.multipart import FormBuilder, PartBuilder
from pyreqwest.proxy import ProxyBuilder
from pyreqwest.request import (
    BaseRequestBuilder,
    Request,
    RequestBuilder,
    SyncRequestBuilder,
    SyncStreamRequest,
)
from pyreqwest.response import Response as RawResponse
from pyreqwest.response import SyncResponse as RawSyncResponse

from ._compat import (
    BaseModel,
    BaseModelTyping,
    Decoder,
    DecoderTyping,
    Struct,
    StructTyping,
    TypeAdapter,
    TypeAdapterTyping,
    msgspec,
)

# `Data`/`TypedHeaders`/`JSONPayload`/`Params`/`Headers` below deliberately use `BaseModelTyping`/
# `StructTyping` (checker-local Protocols), not the real `BaseModel`/`Struct` — see
# `_compat.py`'s `StructTyping` docstring for why: it keeps these aliases fully typed even when
# msgspec/pydantic aren't resolvable to whatever type checker is running, with no precision loss
# when they are. `isinstance`/`issubclass`/`case` checks elsewhere in this file must still use the
# real `BaseModel`/`Struct` imported above — only these five alias definitions changed.
type Data = BaseModelTyping | StructTyping | bytes | dict[str, Any]
type TypedHeaders = BaseModelTyping | StructTyping
type JSONPayload = dict[str, Any] | BaseModelTyping | StructTyping

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
# A tuple[_FormValue, ...] value in Form (below) means "repeat this part name once per element" —
# never "JSON-encode this tuple," which is what list[Any] means instead. Keeping the repeat
# marker as `tuple` and the JSON-array marker as `list` is what makes the two unambiguous.
type _FormValue = str | int | bytes | list[Any] | JSONPayload | File
type Form = dict[str, _FormValue | tuple[_FormValue, ...]]
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


@dataclass(slots=True)
class RequestInfo:
    """The request actually sent, attached to `Result.request`/`head`'s result and to
    `HTTPResponseError.request` — the target you asked for, not necessarily the one a final
    response came from if redirects were followed.
    """

    method: str
    url: str
    path: str
    host: str | None


class HTTPResponseError(Exception):
    """Raised when a request gets a 4xx/5xx response (`error_for_status=True`, the default).

    A response WAS received — for a request that never got one, see `HTTPTransportError`.
    `.request` is the request actually sent — the target you asked for, not necessarily the one a
    final response came from if redirects were followed.
    """

    def __init__(
        self, status: int, body: bytes, request: RequestInfo, parsed_body: Data | None = None
    ) -> None:
        self.status = status
        self.body_start = body[:100]
        self.request = request
        self.parsed_body = parsed_body
        snippet = self.body_start.decode(errors="replace")
        truncation_marker = "…" if len(body) > 100 else ""
        super().__init__(f"Request failed with status {status}: {snippet}{truncation_marker}")


class HTTPTransportError(Exception):
    """Raised when a request never got a response at all — pyreqwest's own exception types
    never leak through; they're translated to this (or a subclass) at every call site.
    """


class HTTPConnectionError(HTTPTransportError):
    """The connection was never established, or was lost mid-request."""


class HTTPTimeoutError(HTTPTransportError):
    """The request exceeded its configured `timeout`."""


def _translate_transport_error(
    error: PyreqwestTransportError | PyreqwestRedirectError | PyreqwestBuilderError,
) -> HTTPTransportError:
    if isinstance(error, PyreqwestRequestTimeoutError):
        return HTTPTimeoutError(str(error))
    if isinstance(error, PyreqwestNetworkError):
        return HTTPConnectionError(str(error))
    if isinstance(error, (PyreqwestRedirectError, PyreqwestBuilderError)):
        return HTTPTransportError(str(error))
    # Every real pyreqwest TransportError subclass (verified against the installed version)
    # descends from either RequestTimeoutError or NetworkError, both handled above — this is a
    # forward-compatible fallback for a future pyreqwest exception type, not reachable today.
    return HTTPTransportError(str(error))  # pragma: no cover


def _request_info(request: Request) -> RequestInfo:
    url = request.url
    return RequestInfo(method=request.method, url=str(url), path=url.path, host=url.host_str)


async def _send(request_builder: RequestBuilder) -> tuple[RawResponse, RequestInfo]:
    # `.build()` (not just `.send()`) can raise — e.g. `BuilderError` when `https_only=True`
    # rejects a plain-http URL — so it must be inside this same try, not called by the caller
    # beforehand, or that exception type leaks past this translation layer entirely.
    try:
        built = request_builder.build()
        request_info = _request_info(built)
        return await built.send(), request_info
    except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
        raise _translate_transport_error(error) from error


def _send_sync(request_builder: SyncRequestBuilder) -> tuple[RawSyncResponse, RequestInfo]:
    try:
        built = request_builder.build()
        request_info = _request_info(built)
        return built.send(), request_info
    except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
        raise _translate_transport_error(error) from error


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    if value.isdigit():
        return float(value)
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (parsed - datetime.now(UTC)).total_seconds())


def _backoff_delay(attempt: int, backoff_base: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return backoff_base * (2**attempt)


@dataclass
class _RetryMiddleware:
    _retryable_statuses: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    max_retries: int
    retry_methods: frozenset[str]
    backoff_base: float = 0.1

    async def __call__(self, request: Request, next: Next) -> RawResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        if request.method not in self.retry_methods:
            return await next.run(request)
        for attempt in range(self.max_retries + 1):
            try:
                response = await next.run(request.copy())
            except PyreqwestTransportError:
                if attempt == self.max_retries:
                    raise
                await asyncio.sleep(_backoff_delay(attempt, self.backoff_base, retry_after=None))
                continue
            if attempt == self.max_retries or response.status not in self._retryable_statuses:
                return response
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            await asyncio.sleep(_backoff_delay(attempt, self.backoff_base, retry_after))
        # range(max_retries+1) is never empty, so this is never actually reached.
        raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class _SyncRetryMiddleware:
    _retryable_statuses: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    max_retries: int
    retry_methods: frozenset[str]
    backoff_base: float = 0.1

    def __call__(self, request: Request, next: SyncNext) -> RawSyncResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        if request.method not in self.retry_methods:
            return next.run(request)
        for attempt in range(self.max_retries + 1):
            try:
                response = next.run(request.copy())
            except PyreqwestTransportError:
                if attempt == self.max_retries:
                    raise
                time.sleep(_backoff_delay(attempt, self.backoff_base, retry_after=None))
                continue
            if attempt == self.max_retries or response.status not in self._retryable_statuses:
                return response
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            time.sleep(_backoff_delay(attempt, self.backoff_base, retry_after))
        # range(max_retries+1) is never empty, so this is never actually reached.
        raise AssertionError("unreachable")  # pragma: no cover


def _guess_form_part_mime(
    filename: str | None, *, infer_mime_type_from_file_extension: bool
) -> str | None:
    if not infer_mime_type_from_file_extension or filename is None:
        return None
    return _guess_mime_type(filename)[0]


# Shared by both _apply_form_value and _apply_sync_form_value — encoding a value that's already
# known to be JSON-shaped needs no I/O, so unlike file-content reading there's no async/sync split.
def _encode_json_form_part(value: list[Any] | JSONPayload) -> bytes:
    if isinstance(value, BaseModel):
        return _json_dumps(value.model_dump(mode="json")).encode()
    if isinstance(value, Struct):
        return msgspec.json.encode(value)
    return _json_dumps(value).encode()


# `explicit_mime` always wins over inference; `filename` is only applied when given, since the
# Path-override shape (a bare Path's own auto-derived filename, kept as-is) has no filename to
# pass here at all. Collapsing this into one helper is what keeps _apply_form_value's/
# _apply_sync_form_value's own branches under the complexity linter's threshold.
def _finish_file_part(
    part: PartBuilder,
    filename: str | None,
    explicit_mime: str | None,
    *,
    infer_mime_type_from_file_extension: bool,
) -> PartBuilder:
    if filename is not None:
        part = part.file_name(filename)
    mime = explicit_mime or _guess_form_part_mime(
        filename, infer_mime_type_from_file_extension=infer_mime_type_from_file_extension
    )
    return part if mime is None else part.mime(mime)


def _buffered_io_filename(value: BufferedIOBase) -> str | None:
    raw_name = getattr(value, "name", None)
    return Path(raw_name).name if isinstance(raw_name, str) else None


async def _build_file_part(content: bytes | Path | BufferedIOBase) -> PartBuilder:
    if isinstance(content, Path):
        return await PartBuilder.from_file(content)
    if isinstance(content, BufferedIOBase):
        return PartBuilder.from_bytes(content.read())
    return PartBuilder.from_bytes(content)


# `value` is typed `object`, not `Form`'s value union — nothing at runtime stops a caller
# bypassing the type checker, so the `case _` fallback below must stay reachable rather than
# basedpyright proving it `Never` from a narrower declared type (same reasoning as
# `_validate_response_data_type`'s `object` parameter).
async def _apply_form_value(  # pylint: disable=too-many-return-statements
    form_builder: FormBuilder,
    name: str,
    value: object,
    *,
    infer_mime_type_from_file_extension: bool,
) -> FormBuilder:
    match value:
        case str():
            return form_builder.text(name, value)
        case int():
            return form_builder.text(name, str(value))
        case bytes():
            return form_builder.part(name, PartBuilder.from_bytes(value))
        case (str() as filename, bytes() | Path() | BufferedIOBase() as content):
            part = _finish_file_part(
                await _build_file_part(content),
                filename,
                None,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case (str() as filename, bytes() | Path() | BufferedIOBase() as content, str() as mime):
            part = _finish_file_part(
                await _build_file_part(content),
                filename,
                mime,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case (Path() as content_path, str() as mime):
            part = _finish_file_part(
                await _build_file_part(content_path),
                None,
                mime,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case Path():
            part = _finish_file_part(
                await _build_file_part(value),
                value.name,
                None,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case BufferedIOBase():
            part = _finish_file_part(
                await _build_file_part(value),
                _buffered_io_filename(value),
                None,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case list():
            body = _encode_json_form_part(cast(list[Any], value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
        case dict() | BaseModel() | Struct():
            body = _encode_json_form_part(cast("JSONPayload", value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
        case tuple():
            for item in cast("tuple[object, ...]", value):
                form_builder = await _apply_form_value(
                    form_builder,
                    name,
                    item,
                    infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
                )
            return form_builder
        case _:
            raise TypeError(
                f"Unsupported form value for {name!r}: {value!r} ({type(value).__name__})"
            )


async def _build_form(form: Form, *, infer_mime_type_from_file_extension: bool) -> FormBuilder:
    form_builder = FormBuilder()
    for name, value in form.items():
        form_builder = await _apply_form_value(
            form_builder,
            name,
            value,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
    return form_builder


@dataclass(kw_only=True)
class SSEEvent[TData, TId = str]:
    """One Server-Sent Event, as yielded by `sse()`.

    `.event` is always a plain `str` (defaults to `"message"` per the SSE spec when the wire
    omits it). `.id`'s type/requiredness is controlled by `sse()`'s `id_type` argument. `.data`
    is decoded per `sse()`'s `response_data_type`, raw `str` by default.
    """

    id: TId
    event: str = "message"
    data: TData


@dataclass(slots=True)
class _SSERecord:
    """One parsed SSE record (the lines between two blank lines), before dispatch.

    `data` is `None` when the record carried no `data:` line at all — such a record dispatches no
    event, but its `id:`/`retry:` fields still take effect (WHATWG "process the field" rules).
    `id` is `None` when there was no `id:` line; an explicit empty `id:` is `""` (it resets the
    stream's last-event-id buffer). `retry_ms` is `None` unless a well-formed `retry:` was seen.
    """

    data: str | None
    event: str
    id: str | None
    retry_ms: int | None


@dataclass(slots=True)
class _SSEStreamState:
    """Per-`sse()`-call state that has to survive across reconnects.

    `retry_delay` starts at the caller's `reconnect_delay` and is overwritten whenever the server
    sends a `retry:` field. `last_event_id` is the spec's last-event-id buffer: it persists
    across events (an event with no `id:` line inherits it) and is sent back as
    `Last-Event-ID` on every reconnect. `status` is the most recent connection's response
    status, so the reconnect loop can tell a 204 ("stop, don't reconnect") from any other
    clean close.
    """

    retry_delay: float
    last_event_id: str | None = None
    status: int | None = None


def _parse_sse_record(record: str) -> _SSERecord:
    r"""Parse one blank-line-delimited SSE record per the WHATWG event-stream field rules.

    >>> _parse_sse_record("event: tick\nid: 7\ndata: a\ndata: b")
    _SSERecord(data='a\nb', event='tick', id='7', retry_ms=None)
    >>> _parse_sse_record(": comment only\nretry: 250")
    _SSERecord(data=None, event='message', id=None, retry_ms=250)
    >>> _parse_sse_record("retry: soon\nid: bad\x00null\nid:")
    _SSERecord(data=None, event='message', id='', retry_ms=None)
    >>> _parse_sse_record("data")
    _SSERecord(data='', event='message', id=None, retry_ms=None)
    """
    data_lines: list[str] = []
    event = "message"
    event_id: str | None = None
    retry_ms: int | None = None
    for line in record.split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event = value
        elif field == "id" and "\x00" not in value:
            event_id = value
        elif field == "retry" and value.isascii() and value.isdigit():
            retry_ms = int(value)
    data = "\n".join(data_lines) if data_lines else None
    return _SSERecord(data=data, event=event, id=event_id, retry_ms=retry_ms)


def _split_sse_records(buffer: bytes) -> tuple[list[bytes], bytes]:
    r"""Split off every complete record in `buffer`, returning them plus the unconsumed tail.

    Normalizes all three line terminators the spec allows (`\r\n`, `\n`, `\r`) to `\n` first,
    so a record boundary is always exactly `\n\n` afterwards. A trailing lone `\r` is held back
    unconsumed rather than normalized — it may be the first half of a `\r\n` whose `\n` hasn't
    arrived yet, and normalizing it now would fabricate a bogus record boundary when it does.

    >>> _split_sse_records(b"data: a\n\ndata: b\r\n\r\ndata: c")
    ([b'data: a', b'data: b'], b'data: c')
    >>> _split_sse_records(b"data: a\r\rdata: b\r")
    ([b'data: a'], b'data: b\r')
    >>> _split_sse_records(b"data: b\r\n\r\n")
    ([b'data: b'], b'')
    >>> _split_sse_records(b"")
    ([], b'')
    """
    pending_cr = b"\r" if buffer.endswith(b"\r") else b""
    normalized = buffer.removesuffix(b"\r").replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    *records, tail = normalized.split(b"\n\n")
    return records, tail + pending_cr


def _coerce_sse_id(raw_id: str | None, id_type: type[Any] | None, *, allow_missing_id: bool) -> Any:  # noqa: ANN401 — pylint: disable=line-too-long
    real_type = id_type if id_type is not None else str
    if raw_id is None:
        if allow_missing_id:
            return None
        raise ValueError(f"SSE event missing required 'id' field (id_type={real_type!r})")
    return raw_id if real_type is str else real_type(raw_id)


def _verify_sse_response(state: _SSEStreamState, status: int, content_type: str | None) -> None:
    """Record the connection's status and enforce the spec's "must be text/event-stream" rule.

    Only a 2xx that isn't a 204 is held to that rule: a 204 is the spec's explicit "stop
    reconnecting" signal and has no body to type, and a 4xx/5xx here means the caller passed
    `error_for_status=False` and will simply get no events from a body that isn't a stream.
    """
    state.status = status
    if status == 204 or not 200 <= status < 300:
        return
    media_type = (content_type or "").partition(";")[0].strip().lower()
    if media_type != "text/event-stream":
        raise ValueError(
            f"SSE response has Content-Type {content_type!r}, expected 'text/event-stream'"
        )


@dataclass(slots=True)
class _SyncSSEResponseCheck:
    """Adapter so the sync chunk iterators' single `check_status` hook also runs
    `_verify_sse_response` — the response object only ever exists on whichever thread owns the
    read loop (see `_drain_stream_chunks`), so status/content-type have to be captured there."""

    _check_status: Callable[[RawSyncResponse, type[Data] | None, RequestInfo], None] | None
    _state: _SSEStreamState

    def __call__(
        self,
        raw_response: RawSyncResponse,
        error_type: type[Data] | None,
        request_info: RequestInfo,
    ) -> None:
        if self._check_status is not None:
            self._check_status(raw_response, error_type, request_info)
        content_type = raw_response.headers.get("content-type")
        _verify_sse_response(self._state, raw_response.status, content_type)


def _validate_response_data_type(response_data_type: object) -> None:
    if not isinstance(response_data_type, type):
        raise TypeError(f"response_data_type must be a class, got {response_data_type!r}")


def _decode_error_body(body: bytes, error_type: type[Data]) -> Data:
    """Decode an error response's body for `HTTPResponseError.parsed_body`. Takes plain bytes,
    not a live response object — by the time this runs, `.bytes()` has already been consumed once
    to populate `body_start`, and a pyreqwest response body can't be read twice."""
    _validate_response_data_type(error_type)
    if issubclass(error_type, bytes):
        return body
    if issubclass(error_type, dict):
        parsed = cast(dict[str, Any], _json_loads(body))
        return error_type(parsed)
    if issubclass(error_type, Struct):
        return msgspec.json.decode(body, type=error_type)
    # Statically, `Data`'s remaining member here is always BaseModel (basedpyright flags the
    # check itself as unnecessary) — but `error_type` is only actually validated to be *some*
    # class by `_validate_response_data_type`, not proven to be a `Data` member at runtime, so an
    # uncovered class must still fall through to the raise below.
    if issubclass(error_type, BaseModel):  # pyright: ignore[reportUnnecessaryIsInstance] — pylint: disable=line-too-long
        return error_type.model_validate_json(body)
    raise TypeError(f"Unsupported error_type: {error_type!r}")


# Return type is whatever response_data_type is — genuinely dynamic, can't state it statically.
def _decode_json_line(
    data: str, response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any]
) -> Any:  # noqa: ANN401
    if isinstance(response_data_type, TypeAdapter):
        return response_data_type.validate_json(data)
    if isinstance(response_data_type, Decoder):
        return response_data_type.decode(data)
    # basedpyright can't narrow past `TypeAdapterTyping`/`DecoderTyping` (structural Protocols —
    # see _compat.py's `StructTyping` docstring) via the two `isinstance` checks above the way it
    # can narrow a same-kind `issubclass` chain, so it still sees them as possible here even
    # though both are already excluded at runtime.
    response_data_type = cast("type[Any]", response_data_type)
    _validate_response_data_type(response_data_type)
    if issubclass(response_data_type, dict):
        dict_type = cast("type[dict[str, Any]]", response_data_type)
        parsed = cast(dict[str, Any], _json_loads(data))
        return dict_type(parsed)
    if issubclass(response_data_type, Struct):
        return msgspec.json.decode(data.encode(), type=response_data_type)
    if issubclass(response_data_type, BaseModel):
        return response_data_type.model_validate_json(data)
    raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")


def _dispatch_sse_record(
    record: bytes,
    state: _SSEStreamState,
    response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
    id_type: type[Any] | None,
    *,
    allow_missing_id: bool,
) -> SSEEvent[Any, Any] | None:
    """Apply one record's `retry:`/`id:` fields to `state`, then build its event (if it has data).

    `.id` comes from the *persisted* last-event-id buffer, not this record alone — per spec an
    event with no `id:` line carries the most recent id seen on the stream, and the buffer is
    only cleared by an explicit empty `id:` line.
    """
    parsed_record = _parse_sse_record(record.decode())
    if parsed_record.retry_ms is not None:
        state.retry_delay = parsed_record.retry_ms / 1000
    if parsed_record.id is not None:
        state.last_event_id = parsed_record.id or None
    if parsed_record.data is None:
        return None
    event_id = _coerce_sse_id(state.last_event_id, id_type, allow_missing_id=allow_missing_id)
    if response_data_type is None:
        return SSEEvent(id=event_id, event=parsed_record.event, data=parsed_record.data)
    decoded = _decode_json_line(parsed_record.data, response_data_type)
    return SSEEvent(id=event_id, event=parsed_record.event, data=decoded)


def _sse_reconnect_budget_exhausted(
    max_reconnects: int | None, consecutive_reconnects: int
) -> bool:
    return max_reconnects is not None and consecutive_reconnects >= max_reconnects


def _build_sync_file_part(content: bytes | Path | BufferedIOBase) -> PartBuilder:
    if isinstance(content, Path):
        return PartBuilder.from_sync_file(content)
    if isinstance(content, BufferedIOBase):
        return PartBuilder.from_bytes(content.read())
    return PartBuilder.from_bytes(content)


# See _apply_form_value's comment above _build_form — same reasoning, sync mirror.
def _apply_sync_form_value(  # pylint: disable=too-many-return-statements
    form_builder: FormBuilder,
    name: str,
    value: object,
    *,
    infer_mime_type_from_file_extension: bool,
) -> FormBuilder:
    match value:
        case str():
            return form_builder.text(name, value)
        case int():
            return form_builder.text(name, str(value))
        case bytes():
            return form_builder.part(name, PartBuilder.from_bytes(value))
        case (str() as filename, bytes() | Path() | BufferedIOBase() as content):
            part = _finish_file_part(
                _build_sync_file_part(content),
                filename,
                None,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case (str() as filename, bytes() | Path() | BufferedIOBase() as content, str() as mime):
            part = _finish_file_part(
                _build_sync_file_part(content),
                filename,
                mime,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case (Path() as content_path, str() as mime):
            part = _finish_file_part(
                _build_sync_file_part(content_path),
                None,
                mime,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case Path():
            part = _finish_file_part(
                _build_sync_file_part(value),
                value.name,
                None,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case BufferedIOBase():
            part = _finish_file_part(
                _build_sync_file_part(value),
                _buffered_io_filename(value),
                None,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
            return form_builder.part(name, part)
        case list():
            body = _encode_json_form_part(cast(list[Any], value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
        case dict() | BaseModel() | Struct():
            body = _encode_json_form_part(cast("JSONPayload", value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
        case tuple():
            for item in cast("tuple[object, ...]", value):
                form_builder = _apply_sync_form_value(
                    form_builder,
                    name,
                    item,
                    infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
                )
            return form_builder
        case _:
            raise TypeError(
                f"Unsupported form value for {name!r}: {value!r} ({type(value).__name__})"
            )


def _build_sync_form(form: Form, *, infer_mime_type_from_file_extension: bool) -> FormBuilder:
    form_builder = FormBuilder()
    for name, value in form.items():
        form_builder = _apply_sync_form_value(
            form_builder,
            name,
            value,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
    return form_builder


def _encode_params(params: Params) -> Mapping[str, _QueryValue]:
    """Turns any `Params` (`Mapping[str, _QueryValue]`, pydantic `BaseModel`, or msgspec `Struct`)
    into a plain `Mapping` the way a real request's query string would be encoded (non-`None`
    values only) — shared by `_apply_params` and `lothc.testing`'s mock query matching. A
    plain-`Mapping` input is returned as-is (no copy) — nothing here mutates it, and both real
    callers either flatten it into a fresh structure of their own (`_apply_params`'s
    `_query_pairs`) or hand it straight to pyreqwest's own `Url.parse_with_params`
    (`lothc.testing`'s `_query_param_match_values`), so there's nothing to gain from forcing a
    fresh `dict` here too."""
    match params:
        case BaseModel():
            return {
                name: value
                for name, value in params.model_dump(mode="json").items()
                if value is not None
            }
        case Struct():
            builtins = cast(dict[str, Any], msgspec.to_builtins(params))
            return {name: value for name, value in builtins.items() if value is not None}
        case _:
            # basedpyright can't narrow a `match` fallback past `BaseModelTyping`/`StructTyping`
            # (structural Protocols — see _compat.py's `StructTyping` docstring) the way it can
            # narrow a sequential `issubclass` chain, so it still sees them as possible here even
            # though the two `case` patterns above already excluded any real BaseModel/Struct.
            return cast("Mapping[str, _QueryValue]", params)


def _query_pairs(params: Mapping[str, _QueryValue]) -> list[tuple[str, str | int | float | bool]]:
    """Expands an `_encode_params`-normalized `Params` mapping into the flat list-of-pairs shape
    pyreqwest's `RequestBuilder.query()` actually needs to produce a repeated query key on the
    wire — confirmed live that `.query()` raises `pyreqwest.exceptions.BuilderError: unsupported
    value` if a `Mapping`'s own value is itself a `list`/`tuple`, even though
    `Url.parse_with_params` (used by `lothc.testing`'s own `_query_param_match_values` instead)
    happily accepts that exact shape (either spelling) and expands it correctly — a genuine
    inconsistency between the two pyreqwest APIs, not something to build on. A flat
    `Sequence[tuple[str, QueryPrimitive]]` works for `.query()`, so that's the shape used here. A
    `list`/`tuple` value means "repeat this query param once per element" — see `Params`'s own
    doc comment above."""
    pairs: list[tuple[str, str | int | float | bool]] = []
    for name, value in params.items():
        if isinstance(value, list | tuple):
            pairs.extend((name, item) for item in value)
        else:
            pairs.append((name, value))
    return pairs


def _apply_params[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, params: Params | None
) -> TBuilder:
    if params is None:
        return request_builder
    return request_builder.query(_query_pairs(_encode_params(params)))


def _encode_headers(headers: Headers) -> dict[str, str]:
    """Turns any `Headers` (`Mapping[str, str]`, pydantic `BaseModel`, or msgspec `Struct`) into a
    plain `dict[str, str]` the way a real request's headers would be encoded (`_` -> `-`,
    non-`None` values only) — shared by `_apply_headers` and `lothc.testing`'s mock-response
    header encoding. Deliberately NOT symmetric with `_encode_params`'s no-copy fallback above,
    despite looking like the same situation: `_encode_params`'s result is always used once,
    immediately, then discarded (`_apply_params`/`_query_param_match_values`), so returning the
    caller's own `Mapping` unchanged is safe. `_encode_headers`'s result is not always used that
    way — `lothc.testing`'s `LOTHCMock.with_headers` stores it (`_last_encoded_headers`) to
    re-apply later from `with_data`, so an uncopied fallback would alias the caller's own mutable
    `dict` and let a mutation between `.with_headers(...)` and `.with_data(...)` silently change
    the mocked response's headers (confirmed live — a no-copy version of this fallback introduced
    exactly that bug before being caught by review). Always copy here, even though
    `_apply_headers`'s own use of the result doesn't itself need one."""
    match headers:
        case BaseModel():
            dumped = headers.model_dump(mode="json")
        case Struct():
            dumped = cast(dict[str, Any], msgspec.to_builtins(headers))
        case _:
            # Same basedpyright match-fallback narrowing gap as `_apply_params` above.
            return dict(cast("Mapping[str, str]", headers))
    return {
        name.replace("_", "-"): str(value) for name, value in dumped.items() if value is not None
    }


def _apply_headers[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, headers: Headers | None
) -> TBuilder:
    if headers is None:
        return request_builder
    return request_builder.headers(_encode_headers(headers))


def _apply_timeout[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, timeout: float | None
) -> TBuilder:
    if timeout is None:
        return request_builder
    return request_builder.timeout(timedelta(seconds=timeout))


def _prepare[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder,
    params: Params | None,
    headers: Headers | None,
    timeout: float | None,
) -> TBuilder:
    prepared = _apply_headers(_apply_params(request_builder, params), headers)
    return _apply_timeout(prepared, timeout)


def _apply_tls_and_pool_config[TBuilder: BaseClientBuilder](
    client_builder: TBuilder,
    *,
    connect_timeout: float | None,
    read_timeout: float | None,
    root_certificates: Sequence[bytes] | None,
    identity_pem: bytes | None,
    min_tls_version: TlsVersion | None,
    max_tls_version: TlsVersion | None,
    danger_accept_invalid_certs: bool,
    https_only: bool,
    max_connections: int | None,
    pool_idle_timeout: float | None,
    pool_max_idle_per_host: int | None,
    pool_timeout: float | None,
) -> TBuilder:
    """Shared by `HTTPClient.build`/`SyncHTTPClient.build` — `ClientBuilder`/`SyncClientBuilder`
    both inherit these methods from the same `BaseClientBuilder`, so one function configures
    either."""
    if connect_timeout is not None:
        client_builder = client_builder.connect_timeout(timedelta(seconds=connect_timeout))
    if read_timeout is not None:
        client_builder = client_builder.read_timeout(timedelta(seconds=read_timeout))
    if root_certificates is not None:
        for certificate in root_certificates:
            client_builder = client_builder.add_root_certificate_pem(certificate)
    if identity_pem is not None:
        client_builder = client_builder.identity_pem(identity_pem)
    if min_tls_version is not None:
        client_builder = client_builder.min_tls_version(min_tls_version)
    if max_tls_version is not None:
        client_builder = client_builder.max_tls_version(max_tls_version)
    client_builder = client_builder.danger_accept_invalid_certs(danger_accept_invalid_certs)
    client_builder = client_builder.https_only(https_only)
    if max_connections is not None:
        client_builder = client_builder.max_connections(max_connections)
    if pool_idle_timeout is not None:
        client_builder = client_builder.pool_idle_timeout(timedelta(seconds=pool_idle_timeout))
    if pool_max_idle_per_host is not None:
        client_builder = client_builder.pool_max_idle_per_host(pool_max_idle_per_host)
    if pool_timeout is not None:
        client_builder = client_builder.pool_timeout(timedelta(seconds=pool_timeout))
    return client_builder


def _encode_json_payload(payload: JSONPayload) -> Any:  # noqa: ANN401
    """Turns any `JSONPayload` (`dict`, pydantic `BaseModel`, or msgspec `Struct`) into a plain
    JSON-able value the way a real request body would be encoded — shared by `_attach_body`/
    `_attach_body_sync` and `lothc.testing`'s mock-response encoding, so both stay in sync."""
    if isinstance(payload, BaseModel):
        return payload.model_dump(mode="json")
    if isinstance(payload, Struct):
        return msgspec.to_builtins(payload)
    return payload


async def _attach_body[TBuilder: BaseRequestBuilder](  # pylint: disable=too-many-return-statements
    request_builder: TBuilder,
    json: JSONPayload | None,
    form: Form | None,
    content: str | bytes | None,
    *,
    infer_mime_type_from_file_extension: bool,
) -> TBuilder:
    provided_bodies = [body for body in (json, form, content) if body is not None]
    if len(provided_bodies) > 1:
        raise ValueError("Provide at most one of 'json', 'form' or 'content'")
    if json is not None:
        return request_builder.body_json(_encode_json_payload(json))
    if form is not None:
        return request_builder.multipart(
            await _build_form(
                form, infer_mime_type_from_file_extension=infer_mime_type_from_file_extension
            )
        )
    if isinstance(content, str):
        return request_builder.body_text(content)
    if content is not None:
        return request_builder.body_bytes(content)
    return request_builder


def _attach_body_sync[TBuilder: BaseRequestBuilder](  # pylint: disable=too-many-return-statements
    request_builder: TBuilder,
    json: JSONPayload | None,
    form: Form | None,
    content: str | bytes | None,
    *,
    infer_mime_type_from_file_extension: bool,
) -> TBuilder:
    provided_bodies = [body for body in (json, form, content) if body is not None]
    if len(provided_bodies) > 1:
        raise ValueError("Provide at most one of 'json', 'form' or 'content'")
    if json is not None:
        return request_builder.body_json(_encode_json_payload(json))
    if form is not None:
        return request_builder.multipart(
            _build_sync_form(
                form, infer_mime_type_from_file_extension=infer_mime_type_from_file_extension
            )
        )
    if isinstance(content, str):
        return request_builder.body_text(content)
    if content is not None:
        return request_builder.body_bytes(content)
    return request_builder


# Discriminated union of what a worker thread hands back to the polling consumer thread via
# the Queue in `_interruptible_chunk_iter` — a decoded chunk, EOF, or a forwarded exception.
type _StreamQueueItem = (
    tuple[Literal["chunk"], bytes]
    | tuple[Literal["eof"], None]
    | tuple[Literal["error"], Exception]
)


def _drain_stream_chunks(
    request: SyncStreamRequest,
    check_status: Callable[[RawSyncResponse, type[Data] | None, RequestInfo], None] | None,
    error_type: type[Data] | None,
    request_info: RequestInfo,
    item_queue: queue.Queue[_StreamQueueItem],
) -> None:
    """Worker-thread target for `_interruptible_chunk_iter` — owns the entire
    `with request as raw_response:` lifecycle itself (entry, status check, read loop, and its
    own eventual exit), never the calling thread. Confirmed via spike: dropping/exiting a
    streamed response does NOT cancel an in-flight `read_chunk()` on another thread — it
    blocks the calling thread until that read resolves. So the thread that does the blocking
    reads must also be the one that enters and exits this context manager, sequentially, or
    cleanup itself becomes the new hang.
    """
    try:
        with request as raw_response:
            if check_status is not None:
                check_status(raw_response, error_type, request_info)
            while True:
                chunk = raw_response.body_reader.read_chunk()
                if chunk is None:
                    item_queue.put(("eof", None))
                    return
                item_queue.put(("chunk", bytes(chunk)))
    except Exception as error:  # noqa: BLE001 — forwarded to the consumer thread below, not swallowed — pylint: disable=broad-except
        item_queue.put(("error", error))


def _interruptible_chunk_iter(
    request: SyncStreamRequest,
    check_status: Callable[[RawSyncResponse, type[Data] | None, RequestInfo], None] | None,
    error_type: type[Data] | None,
    request_info: RequestInfo,
    *,
    poll_timeout: float = 0.2,
) -> Iterator[bytes]:
    """Yield a streamed response's chunks with Ctrl-C-interruptible waits between them.

    `read_chunk()` blocks the calling thread with no way to interrupt it from Python — CPython
    only converts SIGINT into `KeyboardInterrupt` on the main thread while it's executing
    Python bytecode. Running the read loop on a daemon worker thread (via
    `_drain_stream_chunks`) and having the main thread only ever do timed `Queue.get()` calls
    keeps the main thread in interruptible Python bytecode the whole time, so Ctrl-C fires
    within roughly `poll_timeout`.

    Abandoning iteration (an early `break`, or the generator being garbage-collected) leaves
    the worker thread parked in `read_chunk()` — there is no cancellation path, see
    `_drain_stream_chunks`'s docstring. This is a deliberate, documented leak: one thread and
    one open socket per abandonment, reclaimed only when the peer closes the connection, a
    timeout fires, or the process exits.
    """
    item_queue: queue.Queue[_StreamQueueItem] = queue.Queue()
    worker = threading.Thread(
        target=_drain_stream_chunks,
        args=(request, check_status, error_type, request_info, item_queue),
        daemon=True,
    )
    worker.start()
    while True:
        try:
            item = item_queue.get(timeout=poll_timeout)
        except queue.Empty:
            continue
        match item:
            case ("chunk", bytes_chunk):
                yield bytes_chunk
            case ("eof", None):
                return
            case ("error", error):
                raise error.with_traceback(error.__traceback__)


def _sync_stream_chunks(
    request: SyncStreamRequest,
    check_status: Callable[[RawSyncResponse, type[Data] | None, RequestInfo], None] | None,
    error_type: type[Data] | None,
    request_info: RequestInfo,
    *,
    interruptible: bool,
) -> Iterator[bytes]:
    if interruptible:
        yield from _interruptible_chunk_iter(request, check_status, error_type, request_info)
        return
    with request as raw_response:
        if check_status is not None:
            check_status(raw_response, error_type, request_info)
        while True:
            chunk = raw_response.body_reader.read_chunk()
            if chunk is None:
                return
            yield bytes(chunk)


def _parse_typed_headers(
    headers: dict[str, str], response_headers_type: type[TypedHeaders]
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
class Result[TData, THeaders: TypedHeaders | None = None]:
    """The decoded body alongside status/headers, as returned by `get_result()`/`head()`.

    `.typed_headers` is `None` unless `response_headers_type` was passed to the call that
    produced this. `.request` is the request actually sent — the target you asked for, not
    necessarily the one a final response came from if redirects were followed.
    """

    data: TData
    status: int
    headers: dict[str, str]
    typed_headers: THeaders
    request: RequestInfo


@dataclass
class HTTPClient:
    """Async typed HTTP client, built on pyreqwest. Construct via `HTTPClient.build(...)`.

    See `SyncHTTPClient` for the sync mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({"GET", "PUT", "DELETE", "HEAD"})
    # `sse()` swaps the client's total `timeout` for this per-request one whenever the caller
    # doesn't pass their own: pyreqwest has no "no timeout" override, and a total timeout on an
    # open-ended stream just kills every healthy stream at the 30s mark. A year is "never".
    _sse_default_timeout: ClassVar[float] = 365 * 24 * 60 * 60

    _client: Client
    _bearer_token: str | None = None
    _bearer_auth: AuthProvider | None = None
    _basic_auth: tuple[str, str | None] | None = None

    @classmethod
    @asynccontextmanager
    async def build(
        cls,
        *,
        base_url: str | None = None,
        bearer_token: str | None = None,
        bearer_auth: AuthProvider | None = None,
        basic_auth: tuple[str, str | None] | None = None,
        default_headers: dict[str, str] | None = None,
        timeout: float | None = 30.0,
        cookie_store: bool = False,
        follow_redirects: bool = True,
        max_redirects: int | None = None,
        proxy: str | None = None,
        max_retries: int = 0,
        retry_methods: frozenset[str] | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        root_certificates: Sequence[bytes] | None = None,
        identity_pem: bytes | None = None,
        min_tls_version: TlsVersion | None = None,
        max_tls_version: TlsVersion | None = None,
        danger_accept_invalid_certs: bool = False,
        https_only: bool = False,
        max_connections: int | None = None,
        pool_idle_timeout: float | None = None,
        pool_max_idle_per_host: int | None = None,
        pool_timeout: float | None = None,
    ) -> AsyncGenerator[Self]:
        """Build an `HTTPClient` as an async context manager.

        `bearer_token` is a static token; `bearer_auth` is an async callable resolved fresh on
        every request; `basic_auth` is a `(username, password)` pair — provide at most one of the
        three. `max_retries` enables a real retry middleware (backoff, `Retry-After`-aware); with
        no `retry_methods`, only the idempotent verbs (`GET`/`PUT`/`DELETE`/`HEAD`) retry.

        `connect_timeout` bounds only the TCP connect phase (separate from `timeout`, which
        covers the whole request); `read_timeout` bounds the idle gap between two consecutive
        body chunks (the right knob for a long-lived `sse()` stream, which `timeout` never
        applies to). `root_certificates` (each a PEM-encoded cert) and
        `identity_pem` (a PEM bundle containing both a client cert and its private key) cover
        trusting a custom/internal CA and mTLS respectively. `danger_accept_invalid_certs`
        disables certificate validation entirely — insecure, for local/test use only.
        `max_connections`/`pool_idle_timeout`/`pool_max_idle_per_host`/`pool_timeout` tune the
        underlying connection pool; leave them `None` to keep pyreqwest's own defaults.
        """
        if sum(value is not None for value in (bearer_token, bearer_auth, basic_auth)) > 1:
            raise ValueError(
                "Provide at most one of 'bearer_token', 'bearer_auth', or 'basic_auth'"
            )
        pyreqwest_client_builder = ClientBuilder()
        if timeout is not None:
            pyreqwest_client_builder = pyreqwest_client_builder.timeout(timedelta(seconds=timeout))
        if default_headers:
            pyreqwest_client_builder = pyreqwest_client_builder.default_headers(default_headers)
        if base_url:
            pyreqwest_client_builder = pyreqwest_client_builder.base_url(base_url)
        pyreqwest_client_builder = pyreqwest_client_builder.default_cookie_store(cookie_store)
        pyreqwest_client_builder = pyreqwest_client_builder.follow_redirects(follow_redirects)
        if max_redirects is not None:
            pyreqwest_client_builder = pyreqwest_client_builder.max_redirects(max_redirects)
        if proxy is not None:
            pyreqwest_client_builder = pyreqwest_client_builder.proxy(ProxyBuilder.all(proxy))
        if max_retries > 0:
            middleware = _RetryMiddleware(max_retries, retry_methods or cls._default_retry_methods)
            pyreqwest_client_builder = pyreqwest_client_builder.with_middleware(middleware)
        pyreqwest_client_builder = _apply_tls_and_pool_config(
            pyreqwest_client_builder,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            root_certificates=root_certificates,
            identity_pem=identity_pem,
            min_tls_version=min_tls_version,
            max_tls_version=max_tls_version,
            danger_accept_invalid_certs=danger_accept_invalid_certs,
            https_only=https_only,
            max_connections=max_connections,
            pool_idle_timeout=pool_idle_timeout,
            pool_max_idle_per_host=pool_max_idle_per_host,
            pool_timeout=pool_timeout,
        )
        async with pyreqwest_client_builder.build() as client:
            yield cls(client, bearer_token, bearer_auth, basic_auth)

    async def _apply_auth(
        self, request_builder: RequestBuilder, *, skip_auth: bool
    ) -> RequestBuilder:
        if skip_auth:
            return request_builder
        if self._bearer_auth is not None:
            return request_builder.bearer_auth(await self._bearer_auth())
        if self._bearer_token is not None:
            return request_builder.bearer_auth(self._bearer_token)
        if self._basic_auth is not None:
            username, password = self._basic_auth
            return request_builder.basic_auth(username, password)
        return request_builder

    async def _prepare_request(
        self,
        request_builder: RequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
    ) -> RequestBuilder:
        request_builder = await self._apply_auth(request_builder, skip_auth=skip_auth)
        return _prepare(request_builder, params, headers, timeout)

    async def _check_status(
        self, raw_response: RawResponse, error_type: type[Data] | None, request_info: RequestInfo
    ) -> None:
        if raw_response.status < 400:
            return
        body = (await raw_response.bytes()).to_bytes()
        parsed_body = _decode_error_body(body, error_type) if error_type is not None else None
        raise HTTPResponseError(raw_response.status, body, request_info, parsed_body)

    async def _decode_body(self, raw_response: RawResponse, response_data_type: type[Data]) -> Data:
        _validate_response_data_type(response_data_type)
        if issubclass(response_data_type, bytes):
            return (await raw_response.bytes()).to_bytes()
        if issubclass(response_data_type, dict):
            parsed = cast(dict[str, Any], await raw_response.json())
            return response_data_type(parsed)
        if issubclass(response_data_type, Struct):
            return msgspec.json.decode(await raw_response.bytes(), type=response_data_type)
        # Statically, `Data`'s remaining member here is always BaseModel (basedpyright flags the
        # check itself as unnecessary) — but `response_data_type` is only actually validated to
        # be *some* class by `_validate_response_data_type`, not proven to be a `Data` member at
        # runtime, so an uncovered class must still fall through to the raise below.
        if issubclass(response_data_type, BaseModel):  # pyright: ignore[reportUnnecessaryIsInstance] — pylint: disable=line-too-long
            return response_data_type.model_validate_json((await raw_response.bytes()).to_bytes())
        raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")

    async def _parse(
        self,
        raw_response: RawResponse,
        response_data_type: type[Data],
        request_info: RequestInfo,
        *,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Data:
        if error_for_status:
            await self._check_status(raw_response, error_type, request_info)
        return await self._decode_body(raw_response, response_data_type)

    @overload
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    async def get[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """GET `path` and decode the body as `response_data_type` (raw `bytes` by default).

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = await self._prepare_request(
            self._client.get(path), params, headers, timeout, skip_auth=skip_auth
        )
        raw_response, request_info = await _send(request_builder)
        return await self._parse(
            raw_response,
            response_data_type,
            request_info,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    async def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    async def get_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    async def get_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    async def get_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    async def get_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    async def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `get`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = await self._prepare_request(
            self._client.get(path), params, headers, timeout, skip_auth=skip_auth
        )
        raw_response, request_info = await _send(request_builder)
        if error_for_status:
            await self._check_status(raw_response, error_type, request_info)
        data = await self._decode_body(raw_response, response_data_type)
        headers = dict(raw_response.headers)
        if response_headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(headers, response_headers_type)
        return Result(data, raw_response.status, headers, typed_headers, request_info)

    async def _sse_connection(
        self,
        request_builder: RequestBuilder,
        state: _SSEStreamState,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        id_type: type[Any] | None,
        *,
        allow_missing_id: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> AsyncGenerator[SSEEvent[Any, Any]]:
        """One connection's worth of an SSE stream: yields its events, returns on clean EOF."""
        try:
            # pyreqwest's default streamed_read_buffer_limit is 64KB — it withholds received
            # bytes internally until that fills or the stream ends, which for an SSE stream (each
            # record a few dozen bytes) means every event arrives in one burst at stream end
            # instead of as it happens. 1 forces it to hand over whatever it has as soon as it has
            # it, restoring real-time delivery — SSE's entire point.
            request = request_builder.streamed_read_buffer_limit(1).build_streamed()
            request_info = _request_info(request)
            async with request as raw_response:
                if error_for_status:
                    await self._check_status(raw_response, error_type, request_info)
                _verify_sse_response(
                    state, raw_response.status, raw_response.headers.get("content-type")
                )
                buffer = b""
                strip_bom = True
                while True:
                    chunk = await raw_response.body_reader.read_chunk()
                    if chunk is None:
                        return
                    buffer += chunk
                    if strip_bom and len(buffer) >= len(codecs.BOM_UTF8):
                        buffer = buffer.removeprefix(codecs.BOM_UTF8)
                        strip_bom = False
                    records, buffer = _split_sse_records(buffer)
                    for record in records:
                        event = _dispatch_sse_record(
                            record,
                            state,
                            response_data_type,
                            id_type,
                            allow_missing_id=allow_missing_id,
                        )
                        if event is not None:
                            yield event
        except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    async def _sse_stream(  # pylint: disable=too-many-locals
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        id_type: type[Any] | None,
        *,
        skip_auth: bool,
        allow_missing_id: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
    ) -> AsyncIterator[SSEEvent[Any, Any]]:
        state = _SSEStreamState(retry_delay=reconnect_delay)
        consecutive_reconnects = 0
        effective_timeout = timeout if timeout is not None else self._sse_default_timeout
        while True:
            request_builder = self._client.get(path).header("accept", "text/event-stream")
            request_builder = await self._prepare_request(
                request_builder, params, headers, effective_timeout, skip_auth=skip_auth
            )
            if state.last_event_id is not None:
                request_builder = request_builder.header("last-event-id", state.last_event_id)
            connection = self._sse_connection(
                request_builder,
                state,
                response_data_type,
                id_type,
                allow_missing_id=allow_missing_id,
                error_for_status=error_for_status,
                error_type=error_type,
            )
            try:
                # `aclosing` so an early `break` by the caller closes this connection right
                # here, not whenever the event loop's async-generator finalizer gets around to it.
                async with aclosing(connection) as events:
                    async for event in events:
                        consecutive_reconnects = 0
                        yield event
            except HTTPTransportError:
                if _sse_reconnect_budget_exhausted(max_reconnects, consecutive_reconnects):
                    raise
            else:
                # A clean close: a 204 is the spec's explicit "stop, don't reconnect"; any other
                # clean close ends the stream too unless the caller opted into browser-style
                # reconnect-on-close — which still respects the reconnect budget.
                if state.status == 204 or not reconnect_on_close:
                    return
                if _sse_reconnect_budget_exhausted(max_reconnects, consecutive_reconnects):
                    return
            consecutive_reconnects += 1
            await asyncio.sleep(state.retry_delay)

    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[str, str]]: ...
    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[str, str | None]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        id_type: type[TId],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[str, TId]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        id_type: type[TId],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[str, TId | None]]: ...
    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[dict[str, Any], str]]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[TData, str]]: ...
    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[dict[str, Any], str | None]]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[TData, str | None]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        id_type: type[TId],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[dict[str, Any], TId]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        id_type: type[TId],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[dict[str, Any], TId | None]]: ...
    @overload
    def sse[TData, TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        id_type: type[TId],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[TData, TId]]: ...
    @overload
    def sse[TData, TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        id_type: type[TId],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[TData, TId | None]]: ...
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        id_type: type[Any] | None = None,
        allow_missing_id: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncIterator[SSEEvent[Any, Any]]:
        """Open `path` as a Server-Sent Events stream, yielding one `SSEEvent` per event.

        `response_data_type` decodes `.data` (a class, `TypeAdapter`, or msgspec `Decoder`);
        `.event`/`.id` are always populated regardless. `id_type` controls what `.id` becomes
        (defaults to `str`); `allow_missing_id` controls whether a missing `id` field raises
        (the default) or becomes `None`.

        The client's `timeout` never applies here — an SSE stream is open-ended, and a total
        request timeout would kill every healthy stream on schedule. `timeout` bounds one
        connection attempt only; use `read_timeout` on `build()` to detect a stalled stream.
        `skip_auth` omits the `Authorization` header (and skips invoking `bearer_auth`) for this
        call; `error_type` decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead
        of leaving it `None`.

        A dropped connection (transport error or timeout) reconnects automatically, after
        `reconnect_delay` seconds — or the server's own `retry:` value once it has sent one —
        with a `Last-Event-ID` header carrying the last id seen, so a spec-compliant server
        resumes where it left off. `max_reconnects` caps *consecutive* reconnects that yield no
        event (`None` for unlimited, `0` to disable — the error is raised instead). A clean close
        ends the stream, unless `reconnect_on_close=True` (browser `EventSource` behavior); a 204
        always ends it.
        """
        return self._sse_stream(
            path,
            params,
            headers,
            timeout,
            response_data_type,
            id_type,
            skip_auth=skip_auth,
            allow_missing_id=allow_missing_id,
            error_for_status=error_for_status,
            error_type=error_type,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
            reconnect_on_close=reconnect_on_close,
        )

    async def _line_stream(
        self,
        request_builder: RequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> AsyncIterator[Any]:
        request_builder = await self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = await _attach_body(
            request_builder,
            json,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        try:
            # See the matching comment in `_sse_stream` — same 64KB-default buffering issue,
            # same fix. `stream_get`/`stream_post` promise unbuffered/NDJSON-as-it-arrives
            # delivery, which the pyreqwest default silently defeats otherwise.
            request = request_builder.streamed_read_buffer_limit(1).build_streamed()
            request_info = _request_info(request)
            async with request as raw_response:
                if error_for_status:
                    await self._check_status(raw_response, error_type, request_info)
                if response_data_type is None:
                    while True:
                        chunk = await raw_response.body_reader.read_chunk()
                        if chunk is None:
                            return
                        yield bytes(chunk)
                buffer = b""
                while True:
                    chunk = await raw_response.body_reader.read_chunk()
                    if chunk is None:
                        break
                    buffer += bytes(chunk)
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line:
                            yield _decode_json_line(line.decode(), response_data_type)
                if buffer.strip():
                    yield _decode_json_line(buffer.decode(), response_data_type)
        except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    @overload
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[bytes]: ...
    @overload
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...
    @overload
    def stream_get[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TLine] | TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[TLine]: ...
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[Any]:
        """Stream GET `path`'s response as raw `bytes` chunks (unbuffered, safe for binary).

        Pass `response_data_type` to switch to newline-buffered NDJSON-style decoding instead —
        each complete line is parsed and decoded as its own value.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._line_stream(
            self._client.get(path),
            params,
            headers,
            timeout,
            None,
            None,
            None,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[bytes]: ...
    @overload
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[dict[str, Any]]: ...
    @overload
    def stream_post[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TLine] | TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[TLine]: ...
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncIterator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`form`/`content` options as
        `post` (at most one), same raw-bytes-by-default / NDJSON-via-`response_data_type` split.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._line_stream(
            self._client.post(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    async def _download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> bytes | None:
        request_builder = await self._prepare_request(
            self._client.get(path), params, headers, timeout, skip_auth=skip_auth
        )
        try:
            request = request_builder.build_streamed()
            request_info = _request_info(request)
            async with request as raw_response:
                if error_for_status:
                    await self._check_status(raw_response, error_type, request_info)
                if dest is None:
                    buffer = bytearray()
                    while True:
                        chunk = await raw_response.body_reader.read_chunk()
                        if chunk is None:
                            return bytes(buffer)
                        buffer += chunk
                with dest.open("wb") as file:
                    while True:
                        chunk = await raw_response.body_reader.read_chunk()
                        if chunk is None:
                            return None
                        file.write(chunk)
        except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    @overload
    async def download(
        self,
        path: str,
        dest: None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    async def download(
        self,
        path: str,
        dest: Path,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> None: ...
    async def download(
        self,
        path: str,
        dest: Path | None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes | None:
        """Download `path`'s response body, optimized for large objects (e.g. a presigned GET
        URL for a multi-GB file) — much lower peak memory than `get()` for big bodies.

        With no `dest`, streams the body into one pre-grown buffer and returns `bytes` — still
        O(body size) memory, but roughly a third of what `get()` uses (pyreqwest's own
        `.bytes()` does two extra full copies internally). Pass `dest` to stream straight to a
        file instead — memory then stays O(chunk size) regardless of how large the body is.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._download(
            path,
            dest,
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    async def _send_with_body(
        self,
        request_builder: RequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data],
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Data:
        request_builder = await self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = await _attach_body(
            request_builder,
            json,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        raw_response, request_info = await _send(request_builder)
        return await self._parse(
            raw_response,
            response_data_type,
            request_info,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    async def _send_with_body_result(
        self,
        request_builder: RequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data],
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Result[Any, Any]:
        request_builder = await self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = await _attach_body(
            request_builder,
            json,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        raw_response, request_info = await _send(request_builder)
        if error_for_status:
            await self._check_status(raw_response, error_type, request_info)
        data = await self._decode_body(raw_response, response_data_type)
        response_headers = dict(raw_response.headers)
        if response_headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(response_headers, response_headers_type)
        return Result(data, raw_response.status, response_headers, typed_headers, request_info)

    @overload
    async def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    async def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    async def post[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    async def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """POST to `path` with at most one of `json`/`form`/`content` (raises `ValueError` if
        more than one is given) and decode the response as `response_data_type`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body(
            self._client.post(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def post_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    async def post_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    async def post_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    async def post_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    async def post_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    async def post_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    async def post_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `post`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body_result(
            self._client.post(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    async def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    async def put[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    async def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """PUT to `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body(
            self._client.put(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def put_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    async def put_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    async def put_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    async def put_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    async def put_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    async def put_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    async def put_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `put`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body_result(
            self._client.put(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    async def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    async def patch[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    async def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """PATCH `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body(
            self._client.patch(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def patch_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    async def patch_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    async def patch_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    async def patch_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    async def patch_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    async def patch_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    async def patch_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `patch`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body_result(
            self._client.patch(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    async def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    async def delete[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    async def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """DELETE `path` and decode the response as `response_data_type`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body(
            self._client.delete(path),
            params,
            headers,
            timeout,
            None,
            None,
            None,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def delete_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    async def delete_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    async def delete_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    async def delete_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    async def delete_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    async def delete_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    async def delete_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `delete`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_with_body_result(
            self._client.delete(path),
            params,
            headers,
            timeout,
            None,
            None,
            None,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    async def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[None]: ...
    @overload
    async def head[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[None, THeaders]: ...
    async def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass
        `response_headers_type` to get the response headers parsed into `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = await self._prepare_request(
            self._client.head(path), params, headers, timeout, skip_auth=skip_auth
        )
        raw_response, request_info = await _send(request_builder)
        if error_for_status:
            await self._check_status(raw_response, error_type, request_info)
        response_headers = dict(raw_response.headers)
        typed_headers = (
            None
            if response_headers_type is None
            else _parse_typed_headers(response_headers, response_headers_type)
        )
        return Result(None, raw_response.status, response_headers, typed_headers, request_info)


@dataclass
class SyncHTTPClient:
    """Sync typed HTTP client, built on pyreqwest. Construct via `SyncHTTPClient.build(...)`.

    See `HTTPClient` for the async mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({"GET", "PUT", "DELETE", "HEAD"})
    # `sse()` swaps the client's total `timeout` for this per-request one whenever the caller
    # doesn't pass their own: pyreqwest has no "no timeout" override, and a total timeout on an
    # open-ended stream just kills every healthy stream at the 30s mark. A year is "never".
    _sse_default_timeout: ClassVar[float] = 365 * 24 * 60 * 60

    _client: SyncClient
    _bearer_token: str | None = None
    _bearer_auth: SyncAuthProvider | None = None
    _basic_auth: tuple[str, str | None] | None = None

    @classmethod
    @contextmanager
    def build(
        cls,
        *,
        base_url: str | None = None,
        bearer_token: str | None = None,
        bearer_auth: SyncAuthProvider | None = None,
        basic_auth: tuple[str, str | None] | None = None,
        default_headers: dict[str, str] | None = None,
        timeout: float | None = 30.0,
        cookie_store: bool = False,
        follow_redirects: bool = True,
        max_redirects: int | None = None,
        proxy: str | None = None,
        max_retries: int = 0,
        retry_methods: frozenset[str] | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        root_certificates: Sequence[bytes] | None = None,
        identity_pem: bytes | None = None,
        min_tls_version: TlsVersion | None = None,
        max_tls_version: TlsVersion | None = None,
        danger_accept_invalid_certs: bool = False,
        https_only: bool = False,
        max_connections: int | None = None,
        pool_idle_timeout: float | None = None,
        pool_max_idle_per_host: int | None = None,
        pool_timeout: float | None = None,
    ) -> Generator[Self]:
        """Build a `SyncHTTPClient` as a context manager.

        `bearer_token` is a static token; `bearer_auth` is a callable resolved fresh on every
        request; `basic_auth` is a `(username, password)` pair — provide at most one of the
        three. `max_retries` enables a real retry middleware (backoff, `Retry-After`-aware); with
        no `retry_methods`, only the idempotent verbs (`GET`/`PUT`/`DELETE`/`HEAD`) retry.

        `connect_timeout` bounds only the TCP connect phase (separate from `timeout`, which
        covers the whole request); `read_timeout` bounds the idle gap between two consecutive
        body chunks (the right knob for a long-lived `sse()` stream, which `timeout` never
        applies to). `root_certificates` (each a PEM-encoded cert) and
        `identity_pem` (a PEM bundle containing both a client cert and its private key) cover
        trusting a custom/internal CA and mTLS respectively. `danger_accept_invalid_certs`
        disables certificate validation entirely — insecure, for local/test use only.
        `max_connections`/`pool_idle_timeout`/`pool_max_idle_per_host`/`pool_timeout` tune the
        underlying connection pool; leave them `None` to keep pyreqwest's own defaults.
        """
        if sum(value is not None for value in (bearer_token, bearer_auth, basic_auth)) > 1:
            raise ValueError(
                "Provide at most one of 'bearer_token', 'bearer_auth', or 'basic_auth'"
            )
        sync_client_builder = SyncClientBuilder()
        if timeout is not None:
            sync_client_builder = sync_client_builder.timeout(timedelta(seconds=timeout))
        if default_headers:
            sync_client_builder = sync_client_builder.default_headers(default_headers)
        if base_url:
            sync_client_builder = sync_client_builder.base_url(base_url)
        sync_client_builder = sync_client_builder.default_cookie_store(cookie_store)
        sync_client_builder = sync_client_builder.follow_redirects(follow_redirects)
        if max_redirects is not None:
            sync_client_builder = sync_client_builder.max_redirects(max_redirects)
        if proxy is not None:
            sync_client_builder = sync_client_builder.proxy(ProxyBuilder.all(proxy))
        if max_retries > 0:
            retry_methods = retry_methods or cls._default_retry_methods
            middleware = _SyncRetryMiddleware(max_retries, retry_methods)
            sync_client_builder = sync_client_builder.with_middleware(middleware)
        sync_client_builder = _apply_tls_and_pool_config(
            sync_client_builder,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            root_certificates=root_certificates,
            identity_pem=identity_pem,
            min_tls_version=min_tls_version,
            max_tls_version=max_tls_version,
            danger_accept_invalid_certs=danger_accept_invalid_certs,
            https_only=https_only,
            max_connections=max_connections,
            pool_idle_timeout=pool_idle_timeout,
            pool_max_idle_per_host=pool_max_idle_per_host,
            pool_timeout=pool_timeout,
        )
        with sync_client_builder.build() as client:
            yield cls(client, bearer_token, bearer_auth, basic_auth)

    def _apply_auth(
        self, request_builder: SyncRequestBuilder, *, skip_auth: bool
    ) -> SyncRequestBuilder:
        if skip_auth:
            return request_builder
        if self._bearer_auth is not None:
            return request_builder.bearer_auth(self._bearer_auth())
        if self._bearer_token is not None:
            return request_builder.bearer_auth(self._bearer_token)
        if self._basic_auth is not None:
            username, password = self._basic_auth
            return request_builder.basic_auth(username, password)
        return request_builder

    def _prepare_request(
        self,
        request_builder: SyncRequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
    ) -> SyncRequestBuilder:
        request_builder = self._apply_auth(request_builder, skip_auth=skip_auth)
        return _prepare(request_builder, params, headers, timeout)

    def _check_status(
        self,
        raw_response: RawSyncResponse,
        error_type: type[Data] | None,
        request_info: RequestInfo,
    ) -> None:
        if raw_response.status < 400:
            return
        body = raw_response.bytes().to_bytes()
        parsed_body = _decode_error_body(body, error_type) if error_type is not None else None
        raise HTTPResponseError(raw_response.status, body, request_info, parsed_body)

    def _decode_body(self, raw_response: RawSyncResponse, response_data_type: type[Data]) -> Data:
        _validate_response_data_type(response_data_type)
        if issubclass(response_data_type, bytes):
            return raw_response.bytes().to_bytes()
        if issubclass(response_data_type, dict):
            parsed = cast(dict[str, Any], raw_response.json())
            return response_data_type(parsed)
        if issubclass(response_data_type, Struct):
            return msgspec.json.decode(raw_response.bytes(), type=response_data_type)
        # See the async `_decode_body`'s comment above the equivalent check — same reasoning.
        if issubclass(response_data_type, BaseModel):  # pyright: ignore[reportUnnecessaryIsInstance] — pylint: disable=line-too-long
            return response_data_type.model_validate_json(raw_response.bytes().to_bytes())
        raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")

    def _parse(
        self,
        raw_response: RawSyncResponse,
        response_data_type: type[Data],
        request_info: RequestInfo,
        *,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Data:
        if error_for_status:
            self._check_status(raw_response, error_type, request_info)
        return self._decode_body(raw_response, response_data_type)

    @overload
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    def get[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """GET `path` and decode the body as `response_data_type` (raw `bytes` by default).

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = self._prepare_request(
            self._client.get(path), params, headers, timeout, skip_auth=skip_auth
        )
        raw_response, request_info = _send_sync(request_builder)
        return self._parse(
            raw_response,
            response_data_type,
            request_info,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    def get_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    def get_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    def get_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    def get_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `get`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = self._prepare_request(
            self._client.get(path), params, headers, timeout, skip_auth=skip_auth
        )
        raw_response, request_info = _send_sync(request_builder)
        if error_for_status:
            self._check_status(raw_response, error_type, request_info)
        data = self._decode_body(raw_response, response_data_type)
        headers = dict(raw_response.headers)
        if response_headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(headers, response_headers_type)
        return Result(data, raw_response.status, headers, typed_headers, request_info)

    def _sse_connection(
        self,
        request_builder: SyncRequestBuilder,
        state: _SSEStreamState,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        id_type: type[Any] | None,
        *,
        allow_missing_id: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
        interruptible: bool,
    ) -> Generator[SSEEvent[Any, Any]]:
        """One connection's worth of an SSE stream: yields its events, returns on clean EOF."""
        try:
            # See the matching comment in `HTTPClient._sse_connection` — pyreqwest's default
            # streamed_read_buffer_limit (64KB) withholds received bytes until that fills or the
            # stream ends, so an SSE stream of small records arrives in one burst at stream end
            # instead of as it happens. 1 forces it to hand over whatever it has immediately.
            request = request_builder.streamed_read_buffer_limit(1).build_streamed()
            request_info = _request_info(request)
            response_check = _SyncSSEResponseCheck(
                self._check_status if error_for_status else None, state
            )
            buffer = b""
            strip_bom = True
            for chunk in _sync_stream_chunks(
                request, response_check, error_type, request_info, interruptible=interruptible
            ):
                buffer += chunk
                if strip_bom and len(buffer) >= len(codecs.BOM_UTF8):
                    buffer = buffer.removeprefix(codecs.BOM_UTF8)
                    strip_bom = False
                records, buffer = _split_sse_records(buffer)
                for record in records:
                    event = _dispatch_sse_record(
                        record,
                        state,
                        response_data_type,
                        id_type,
                        allow_missing_id=allow_missing_id,
                    )
                    if event is not None:
                        yield event
        except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    def _sse_stream(  # pylint: disable=too-many-locals
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        id_type: type[Any] | None,
        *,
        skip_auth: bool,
        allow_missing_id: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
        interruptible: bool,
    ) -> Iterator[SSEEvent[Any, Any]]:
        state = _SSEStreamState(retry_delay=reconnect_delay)
        consecutive_reconnects = 0
        effective_timeout = timeout if timeout is not None else self._sse_default_timeout
        while True:
            request_builder = self._client.get(path).header("accept", "text/event-stream")
            request_builder = self._prepare_request(
                request_builder, params, headers, effective_timeout, skip_auth=skip_auth
            )
            if state.last_event_id is not None:
                request_builder = request_builder.header("last-event-id", state.last_event_id)
            connection = self._sse_connection(
                request_builder,
                state,
                response_data_type,
                id_type,
                allow_missing_id=allow_missing_id,
                error_for_status=error_for_status,
                error_type=error_type,
                interruptible=interruptible,
            )
            try:
                # `closing` so an early `break` by the caller closes this connection right here,
                # not whenever the generator happens to be garbage-collected.
                with closing(connection) as events:
                    for event in events:
                        consecutive_reconnects = 0
                        yield event
            except HTTPTransportError:
                if _sse_reconnect_budget_exhausted(max_reconnects, consecutive_reconnects):
                    raise
            else:
                # See the matching comment in `HTTPClient._sse_stream`.
                if state.status == 204 or not reconnect_on_close:
                    return
                if _sse_reconnect_budget_exhausted(max_reconnects, consecutive_reconnects):
                    return
            consecutive_reconnects += 1
            time.sleep(state.retry_delay)

    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[str, str]]: ...
    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[str, str | None]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        id_type: type[TId],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[str, TId]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        id_type: type[TId],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[str, TId | None]]: ...
    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[dict[str, Any], str]]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[TData, str]]: ...
    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[dict[str, Any], str | None]]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[TData, str | None]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        id_type: type[TId],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[dict[str, Any], TId]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        id_type: type[TId],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[dict[str, Any], TId | None]]: ...
    @overload
    def sse[TData, TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        id_type: type[TId],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[TData, TId]]: ...
    @overload
    def sse[TData, TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData] | TypeAdapterTyping[TData] | DecoderTyping[TData],
        id_type: type[TId],
        allow_missing_id: Literal[True],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[TData, TId | None]]: ...
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        id_type: type[Any] | None = None,
        allow_missing_id: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Iterator[SSEEvent[Any, Any]]:
        """Open `path` as a Server-Sent Events stream, yielding one `SSEEvent` per event.

        `response_data_type` decodes `.data` (a class, `TypeAdapter`, or msgspec `Decoder`);
        `.event`/`.id` are always populated regardless. `id_type` controls what `.id` becomes
        (defaults to `str`); `allow_missing_id` controls whether a missing `id` field raises
        (the default) or becomes `None`.

        The client's `timeout` never applies here — an SSE stream is open-ended, and a total
        request timeout would kill every healthy stream on schedule. `timeout` bounds one
        connection attempt only; use `read_timeout` on `build()` to detect a stalled stream.
        `skip_auth` omits the `Authorization` header (and skips invoking `bearer_auth`) for this
        call; `error_type` decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead
        of leaving it `None`.

        A dropped connection (transport error or timeout) reconnects automatically, after
        `reconnect_delay` seconds — or the server's own `retry:` value once it has sent one —
        with a `Last-Event-ID` header carrying the last id seen, so a spec-compliant server
        resumes where it left off. `max_reconnects` caps *consecutive* reconnects that yield no
        event (`None` for unlimited, `0` to disable — the error is raised instead). A clean close
        ends the stream, unless `reconnect_on_close=True` (browser `EventSource` behavior); a 204
        always ends it.

        `interruptible=True` runs the blocking read loop on a daemon worker thread so Ctrl-C
        works while waiting between events (the default `False` leaves Ctrl-C dead in that
        wait — see the `interruptible` section of `docs/sse.md`). Abandoning an interruptible
        stream before it reaches EOF (an early `break`, or the generator getting
        garbage-collected) always leaks the worker thread and its open socket until the
        connection dies — there is no cancellation path. Ctrl-C itself is rarely the exposure
        in a long-lived process — it has no controlling terminal to receive it from, and when
        it does, SIGINT there usually kills the whole process anyway, reclaiming the leak with
        it. The real risk is code that repeatedly breaks out of an interruptible stream early
        while the process stays up — pair that with a short `timeout`/`connect_timeout` so
        each leaked connection is bounded.
        """
        return self._sse_stream(
            path,
            params,
            headers,
            timeout,
            response_data_type,
            id_type,
            skip_auth=skip_auth,
            allow_missing_id=allow_missing_id,
            error_for_status=error_for_status,
            error_type=error_type,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
            reconnect_on_close=reconnect_on_close,
            interruptible=interruptible,
        )

    def _line_stream(
        self,
        request_builder: SyncRequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
        interruptible: bool,
    ) -> Iterator[Any]:
        request_builder = self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = _attach_body_sync(
            request_builder,
            json,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        try:
            # See the matching comment in `HTTPClient._sse_stream` — same 64KB-default buffering
            # issue, same fix. `stream_get`/`stream_post` promise unbuffered/NDJSON-as-it-arrives
            # delivery, which the pyreqwest default silently defeats otherwise.
            request = request_builder.streamed_read_buffer_limit(1).build_streamed()
            request_info = _request_info(request)
            check_status = self._check_status if error_for_status else None
            chunks = _sync_stream_chunks(
                request, check_status, error_type, request_info, interruptible=interruptible
            )
            if response_data_type is None:
                yield from chunks
                return
            buffer = b""
            for chunk in chunks:
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line:
                        yield _decode_json_line(line.decode(), response_data_type)
            if buffer.strip():
                yield _decode_json_line(buffer.decode(), response_data_type)
        except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    @overload
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[bytes]: ...
    @overload
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[dict[str, Any]]: ...
    @overload
    def stream_get[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TLine] | TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[TLine]: ...
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[Any]:
        """Stream GET `path`'s response as raw `bytes` chunks (unbuffered, safe for binary).

        Pass `response_data_type` to switch to newline-buffered NDJSON-style decoding instead —
        each complete line is parsed and decoded as its own value.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.

        `interruptible=True` runs the blocking read loop on a daemon worker thread so Ctrl-C
        works while waiting between chunks (the default `False` leaves Ctrl-C dead in that
        wait — see the `interruptible` section of `docs/streaming.md`). Abandoning an
        interruptible stream before it reaches EOF (an early `break`, or the generator getting
        garbage-collected) always leaks the worker thread and its open socket until the
        connection dies — there is no cancellation path. Ctrl-C itself is rarely the exposure
        in a long-lived process — it has no controlling terminal to receive it from, and when
        it does, SIGINT there usually kills the whole process anyway, reclaiming the leak with
        it. The real risk is code that repeatedly breaks out of an interruptible stream early
        while the process stays up — pair that with a short `timeout`/`connect_timeout` so
        each leaked connection is bounded.
        """
        return self._line_stream(
            self._client.get(path),
            params,
            headers,
            timeout,
            None,
            None,
            None,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            error_type=error_type,
            interruptible=interruptible,
        )

    @overload
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[bytes]: ...
    @overload
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[dict[str, Any]]: ...
    @overload
    def stream_post[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TLine] | TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[TLine]: ...
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Iterator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`form`/`content` options as
        `post` (at most one), same raw-bytes-by-default / NDJSON-via-`response_data_type` split.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.

        `interruptible=True` runs the blocking read loop on a daemon worker thread so Ctrl-C
        works while waiting between chunks (the default `False` leaves Ctrl-C dead in that
        wait — see the `interruptible` section of `docs/streaming.md`). Abandoning an
        interruptible stream before it reaches EOF (an early `break`, or the generator getting
        garbage-collected) always leaks the worker thread and its open socket until the
        connection dies — there is no cancellation path. Ctrl-C itself is rarely the exposure
        in a long-lived process — it has no controlling terminal to receive it from, and when
        it does, SIGINT there usually kills the whole process anyway, reclaiming the leak with
        it. The real risk is code that repeatedly breaks out of an interruptible stream early
        while the process stays up — pair that with a short `timeout`/`connect_timeout` so
        each leaked connection is bounded.
        """
        return self._line_stream(
            self._client.post(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
            interruptible=interruptible,
        )

    def _download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> bytes | None:
        request_builder = self._prepare_request(
            self._client.get(path), params, headers, timeout, skip_auth=skip_auth
        )
        try:
            request = request_builder.build_streamed()
            request_info = _request_info(request)
            with request as raw_response:
                if error_for_status:
                    self._check_status(raw_response, error_type, request_info)
                if dest is None:
                    buffer = bytearray()
                    while True:
                        chunk = raw_response.body_reader.read_chunk()
                        if chunk is None:
                            return bytes(buffer)
                        buffer += chunk
                with dest.open("wb") as file:
                    while True:
                        chunk = raw_response.body_reader.read_chunk()
                        if chunk is None:
                            return None
                        file.write(chunk)
        except (PyreqwestTransportError, PyreqwestRedirectError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    @overload
    def download(
        self,
        path: str,
        dest: None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    def download(
        self,
        path: str,
        dest: Path,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> None: ...
    def download(
        self,
        path: str,
        dest: Path | None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes | None:
        """Download `path`'s response body, optimized for large objects (e.g. a presigned GET
        URL for a multi-GB file) — much lower peak memory than `get()` for big bodies.

        With no `dest`, streams the body into one pre-grown buffer and returns `bytes` — still
        O(body size) memory, but roughly a third of what `get()` uses (pyreqwest's own
        `.bytes()` does two extra full copies internally). Pass `dest` to stream straight to a
        file instead — memory then stays O(chunk size) regardless of how large the body is.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._download(
            path,
            dest,
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    def _send_with_body(
        self,
        request_builder: SyncRequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data],
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Data:
        request_builder = self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = _attach_body_sync(
            request_builder,
            json,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        raw_response, request_info = _send_sync(request_builder)
        return self._parse(
            raw_response,
            response_data_type,
            request_info,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    def _send_with_body_result(
        self,
        request_builder: SyncRequestBuilder,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data],
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Result[Any, Any]:
        request_builder = self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = _attach_body_sync(
            request_builder,
            json,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        raw_response, request_info = _send_sync(request_builder)
        if error_for_status:
            self._check_status(raw_response, error_type, request_info)
        data = self._decode_body(raw_response, response_data_type)
        response_headers = dict(raw_response.headers)
        if response_headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(response_headers, response_headers_type)
        return Result(data, raw_response.status, response_headers, typed_headers, request_info)

    @overload
    def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    def post[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """POST to `path` with at most one of `json`/`form`/`content` (raises `ValueError` if
        more than one is given) and decode the response as `response_data_type`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body(
            self._client.post(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def post_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    def post_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    def post_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    def post_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    def post_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    def post_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    def post_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `post`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body_result(
            self._client.post(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    def put[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """PUT to `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body(
            self._client.put(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def put_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    def put_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    def put_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    def put_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    def put_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    def put_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    def put_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `put`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body_result(
            self._client.put(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    def patch[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """PATCH `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body(
            self._client.patch(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def patch_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    def patch_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    def patch_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    def patch_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    def patch_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    def patch_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    def patch_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `patch`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body_result(
            self._client.patch(path),
            params,
            headers,
            timeout,
            json,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> bytes: ...
    @overload
    def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> dict[str, Any]: ...
    @overload
    def delete[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> TData: ...
    def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Data:
        """DELETE `path` and decode the response as `response_data_type`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body(
            self._client.delete(path),
            params,
            headers,
            timeout,
            None,
            None,
            None,
            response_data_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def delete_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes]: ...
    @overload
    def delete_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any]]: ...
    @overload
    def delete_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData]: ...
    @overload
    def delete_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[bytes, THeaders]: ...
    @overload
    def delete_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[dict[str, Any], THeaders]: ...
    @overload
    def delete_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[TData, THeaders]: ...
    def delete_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[Any, Any]:
        """Like `delete`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `response_headers_type` to also get the headers parsed into
        `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_with_body_result(
            self._client.delete(path),
            params,
            headers,
            timeout,
            None,
            None,
            None,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    @overload
    def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[None]: ...
    @overload
    def head[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[None, THeaders]: ...
    def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Result[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass
        `response_headers_type` to get the response headers parsed into `result.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = self._prepare_request(
            self._client.head(path), params, headers, timeout, skip_auth=skip_auth
        )
        raw_response, request_info = _send_sync(request_builder)
        if error_for_status:
            self._check_status(raw_response, error_type, request_info)
        response_headers = dict(raw_response.headers)
        typed_headers = (
            None
            if response_headers_type is None
            else _parse_typed_headers(response_headers, response_headers_type)
        )
        return Result(None, raw_response.status, response_headers, typed_headers, request_info)
