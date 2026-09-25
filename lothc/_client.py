import asyncio
import codecs
import queue
import secrets
import threading
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Buffer,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import (
    AsyncExitStack,
    ExitStack,
    aclosing,
    asynccontextmanager,
    closing,
    contextmanager,
)
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version
from io import BufferedIOBase, BufferedWriter
from json import dumps as _json_dumps
from json import loads as _json_loads
from mimetypes import guess_type as _guess_mime_type
from os import PathLike
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Protocol,
    Self,
    cast,
    overload,
    runtime_checkable,
)
from urllib.parse import urlsplit

from pyreqwest.bytes import Bytes
from pyreqwest.client import BaseClientBuilder, Client, ClientBuilder, SyncClient, SyncClientBuilder
from pyreqwest.exceptions import BuilderError as PyreqwestBuilderError
from pyreqwest.exceptions import ClientClosedError as PyreqwestClientClosedError
from pyreqwest.exceptions import DecodeError as PyreqwestDecodeError
from pyreqwest.exceptions import NetworkError as PyreqwestNetworkError
from pyreqwest.exceptions import RequestError as PyreqwestRequestError
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

if TYPE_CHECKING:
    # Stub-only (typeshed has no runtime module), and the exact shape `MutableMapping.update`
    # accepts — carrying it here is what lets `update`'s override stay LSP-compatible instead of
    # needing a `reportIncompatibleMethodOverride` suppression.
    from _typeshed import SupportsKeysAndGetItem


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


@dataclass(slots=True)
class RequestInfo:
    """The request actually sent, attached to `Response.request`/`head`'s result and to
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
    final response came from if redirects were followed. `.headers` are the error response's own
    (e.g. a `Retry-After` once retries are exhausted, or a request id to quote to support), and
    `.body` is the whole body; `.body_start` is just its first 100 bytes, as used in the message.
    With `error_type`, `.parsed_body` is the decoded body, or `None` if it didn't match, in which
    case `.parse_error` holds why (the decode library's own `ValueError`). `.parse_error` isn't
    carried across pickling, since pydantic's `ValidationError` can't be pickled.
    """

    def __init__(
        self,
        status: int,
        body: bytes,
        request: RequestInfo,
        parsed_body: Data | None = None,
        *,
        headers: "CaseInsensitiveDict | None" = None,
        parse_error: ValueError | None = None,
    ) -> None:
        self.status = status
        self.parse_error = parse_error
        self.body = body
        self.body_start = body[:100]
        self.request = request
        self.parsed_body = parsed_body
        self.headers = headers if headers is not None else CaseInsensitiveDict()
        snippet = self.body_start.decode(errors="replace")
        truncation_marker = "…" if len(body) > 100 else ""
        super().__init__(f"Request failed with status {status}: {snippet}{truncation_marker}")

    def __reduce__(self) -> tuple[Any, ...]:
        # Exception's own pickling replays `self.args` — just the message here — into `__init__`,
        # which then fails for want of `body`/`request`, so an error could never cross a process
        # boundary (multiprocessing, a task queue). Rebuild from the real fields instead.
        return (
            _rebuild_http_response_error,
            (self.status, self.body, self.request, self.parsed_body, self.headers),
        )


def _rebuild_http_response_error(
    status: int,
    body: bytes,
    request: RequestInfo,
    parsed_body: Data | None,
    headers: "CaseInsensitiveDict",
) -> HTTPResponseError:
    """Unpickling target for `HTTPResponseError.__reduce__` (keyword-only `headers` rules out
    returning the class itself with positional args)."""
    return HTTPResponseError(status, body, request, parsed_body, headers=headers)


class HTTPTransportError(Exception):
    """Raised when a request never got a response at all — pyreqwest's own exception types
    never leak through; they're translated to this (or a subclass) at every call site.
    """


class HTTPConnectionError(HTTPTransportError):
    """The connection was never established, or was lost mid-request."""


class HTTPTimeoutError(HTTPTransportError):
    """A configured timeout elapsed: the total `timeout`, or `connect_timeout`, `read_timeout` or
    `pool_timeout` if set."""


def _translate_transport_error(
    error: PyreqwestRequestError | PyreqwestBuilderError,
) -> HTTPTransportError | RuntimeError:
    """Map any pyreqwest request failure to lothc's own exception, so none ever reaches a caller.

    Catch sites take `RequestError` — the base of every failure pyreqwest raises once a request
    is under way — not a hand-picked list of subclasses: the list silently missed `DecodeError`,
    which is what a body cut short mid-read raises (a `Content-Length` response whose connection
    dies early), so that leaked as a raw pyreqwest exception past `except HTTPTransportError`.
    """
    if isinstance(error, PyreqwestClientClosedError):
        # A programming error, not a network one: making it an `HTTPTransportError` would get it
        # reconnected by `sse()` and caught by handlers written for a flaky network.
        return RuntimeError(f"The client is closed; it was used after its context exited: {error}")
    if isinstance(error, PyreqwestRequestTimeoutError):
        return HTTPTimeoutError(str(error))
    # `DecodeError` is a body cut short mid-read, and pyreqwest raises the bare `RequestError`
    # base for the same fault on a streamed body ("request or response body error"): both mean
    # the connection failed partway through a response, exactly what `HTTPConnectionError` is for.
    # An exact-type check, deliberately: only the bare base class means "body read failed"; its
    # other subclasses (redirect, status, panic) are handled below as plain transport errors.
    if isinstance(error, (PyreqwestNetworkError, PyreqwestDecodeError)) or (
        type(error) is PyreqwestRequestError  # pylint: disable=unidiomatic-typecheck
    ):
        return HTTPConnectionError(str(error))
    # `RedirectError`/`BuilderError`, plus anything pyreqwest adds later (`StatusError` never
    # arises, since lothc checks status itself): still a failure to get a usable response.
    return HTTPTransportError(str(error))


@contextmanager
def _atomic_download_file(dest: Path) -> Generator[BufferedWriter]:
    """A file to stream a download into that only becomes `dest` once the body is complete.

    Writes go to a hidden sibling of `dest` (same directory, so the final rename is atomic),
    which replaces `dest` on success and is deleted on any failure, including cancellation. A
    download that dies halfway therefore never leaves a truncated file where a caller would take
    it for the real thing. Opened with `"xb"` rather than via `tempfile.mkstemp`, which would
    create it `0600` and so silently change the finished file's permissions from the umask
    default the old direct `dest.open("wb")` gave it.

    Writes stay on the calling thread, even for the async client: each chunk write is
    microseconds (measured ~10us for 64KB), and moving them onto a thread made a 200MB download's
    writes 2.5-3.8x slower for no real gain in event-loop responsiveness.
    """
    part = dest.with_name(f".{dest.name}.{secrets.token_hex(8)}.part")
    try:
        with part.open("xb") as file:
            yield file
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _complete_ndjson_lines(pending: bytearray, chunk: Buffer) -> list[bytearray]:
    """Append `chunk` to `pending` and return the non-blank lines it completes, leaving only the
    still-unterminated tail in `pending`.

    Linear in the bytes received. The old `line, buffer = buffer.split(b"\\n", 1)` loop copied the
    whole remainder once per line (a 4MB chunk took ~4.7s; this takes ~15ms), and only the newly
    appended bytes are searched for a newline, since everything before the last one has already
    been consumed; rescanning all of `pending` made one long line quadratic instead. A line that
    is only whitespace is skipped rather than decoded, which covers the `\\r` a CRLF-terminated
    stream leaves on its blank lines.
    """
    start = len(pending)
    pending += chunk
    end = pending.rfind(b"\n", start)
    if end == -1:
        return []
    lines = pending[:end].split(b"\n")
    del pending[: end + 1]
    return [line for line in lines if line.strip()]


def _idle_timeout_error(idle: float) -> HTTPTimeoutError:
    return HTTPTimeoutError(
        f"No data received from the server for {idle}s. For a streaming verb the client's "
        "`timeout` is the longest allowed gap between chunks, not a cap on the whole transfer; "
        "pass a per-call `timeout=` for a total cap, or set `read_timeout` to change the gap."
    )


@asynccontextmanager
async def _within_idle_limit(idle: float | None) -> AsyncGenerator[None]:
    """Fail with `HTTPTimeoutError` if the enclosed wait takes longer than `idle` (`None`: never).

    Only ever wraps a single wait on the server (opening the stream, a status check, one chunk
    read), never a `yield` to the caller, so time a slow consumer spends between chunks is never
    mistaken for a stalled server.
    """
    if idle is None:
        yield
        return
    try:
        async with asyncio.timeout(idle):
            yield
    except TimeoutError as error:
        raise _idle_timeout_error(idle) from error


async def _read_chunk_within(raw_response: RawResponse, idle: float | None) -> Bytes | None:
    """The next body chunk, or `HTTPTimeoutError` if none arrives within `idle` seconds.

    A plain coroutine rather than `_within_idle_limit`, since this runs once per chunk and a
    generator-based context manager costs noticeably more on a many-chunk download.
    """
    if idle is None:
        return await raw_response.body_reader.read_chunk()
    try:
        async with asyncio.timeout(idle):
            return await raw_response.body_reader.read_chunk()
    except TimeoutError as error:
        raise _idle_timeout_error(idle) from error


async def _read_body(raw_response: RawResponse) -> Bytes:
    """The whole body, read once. The translation here doesn't fire today: pyreqwest reads a
    non-streamed body in full inside `send()` (verified with a body cut off at 10MB of a promised
    50MB), so a body cut short already raises there and is translated by `_send`. It stays as a
    guard for the never-leak contract, in case a future pyreqwest reads lazily."""
    try:
        return await raw_response.bytes()
    except PyreqwestRequestError as error:  # pragma: no cover — see docstring
        raise _translate_transport_error(error) from error


def _read_body_sync(raw_response: RawSyncResponse) -> Bytes:
    """Sync mirror of `_read_body`, including why its translation doesn't fire today."""
    try:
        return raw_response.bytes()
    except PyreqwestRequestError as error:  # pragma: no cover — see `_read_body`
        raise _translate_transport_error(error) from error


def _is_permanent_transport_error(error: HTTPTransportError) -> bool:
    """Whether retrying `error` is pointless because the request could never even be built.

    pyreqwest raises `BuilderError` from `.build()`/`.build_streamed()` — a rejected scheme under
    `https_only`, a malformed URL — before anything reaches the network, so no amount of waiting
    changes the outcome. `RedirectError` is deliberately *not* included: a redirect loop can be
    transient, so it stays retryable. Read off `__cause__`, which every site translating one of
    these sets via `raise _translate_transport_error(error) from error`, so the public exception
    type is unchanged and callers catching `HTTPTransportError` are unaffected.
    """
    return isinstance(error.__cause__, PyreqwestBuilderError)


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
    except (PyreqwestRequestError, PyreqwestBuilderError) as error:
        raise _translate_transport_error(error) from error


def _send_sync(request_builder: SyncRequestBuilder) -> tuple[RawSyncResponse, RequestInfo]:
    try:
        built = request_builder.build()
        request_info = _request_info(built)
        return built.send(), request_info
    except (PyreqwestRequestError, PyreqwestBuilderError) as error:
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


def _is_retryable_send_error(error: PyreqwestRequestError) -> bool:
    """A failure a retry can fix: a transport error, or a body cut off partway through.

    A body shorter than its `Content-Length` isn't a `TransportError` in pyreqwest but a
    `DecodeError`, the same type as a corrupt gzip body, which no retry can fix. hyper's
    connection-read cause is what tells the two apart.
    """
    if isinstance(error, PyreqwestTransportError):
        return True
    causes = error.details["causes"] if isinstance(error, PyreqwestDecodeError) else None
    return any(cause["message"] == "error reading a body from connection" for cause in causes or ())


def _backoff_delay(attempt: int, backoff_base: float, retry_after: float | None) -> float:
    if retry_after is not None:
        return retry_after
    return backoff_base * (2**attempt)


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


def _retry_after_exceeds(retry_after: float | None, limit: float | None) -> bool:
    """Whether the server asked for a longer wait than the client will spend inside a call.

    If so the retry middleware stops and hands back that response, so the caller gets an
    `HTTPResponseError` whose `.headers["Retry-After"]` says when to try again, rather than a
    call silently blocking for as long as the server liked (`Retry-After: 3600` slept an hour).
    Waiting only part of the requested time and retrying anyway would just be rejected again.

    >>> _retry_after_exceeds(5.0, 60.0), _retry_after_exceeds(3600.0, 60.0)
    (False, True)
    >>> _retry_after_exceeds(3600.0, None), _retry_after_exceeds(None, 60.0)
    (False, False)
    """
    return retry_after is not None and limit is not None and retry_after > limit


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


def _bearer_token_sent(request: Request) -> str | None:
    """The bearer token `request` carries, or `None` (e.g. a `skip_auth=True` call, which sends
    none and so is left alone)."""
    sent = request.headers.get("authorization")
    if sent is None or not sent.startswith("Bearer "):
        return None
    return sent.removeprefix("Bearer ")


@dataclass
class _ReauthMiddleware:
    """On a 401, discard the token that was rejected; for an idempotent verb, also get a fresh one
    and retry exactly once.

    For a token revoked before it expires, which `OAuthProvider` would otherwise keep sending,
    failing every call, until it naturally expired. Any 401 invalidates, so even a rejected POST
    (never replayed, since it may not be safe to repeat) leaves the *next* call with a fresh
    token. A second 401 on the retry is returned as-is, so a request that genuinely lacks access
    fails after one extra token fetch, never in a loop.
    """

    auth: _InvalidatableAuth
    retry_methods: frozenset[str]

    async def __call__(self, request: Request, next: Next) -> RawResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        sent = _bearer_token_sent(request)
        if sent is None:
            return await next.run(request)
        retryable = request.method in self.retry_methods
        response = await next.run(request.copy() if retryable else request)
        if response.status != 401:
            return response
        self.auth.invalidate(sent)
        if not retryable:
            return response
        request.headers["authorization"] = f"Bearer {await self.auth()}"
        return await next.run(request)


@dataclass
class _SyncReauthMiddleware:
    """Sync mirror of `_ReauthMiddleware`."""

    auth: _SyncInvalidatableAuth
    retry_methods: frozenset[str]

    def __call__(self, request: Request, next: SyncNext) -> RawSyncResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        sent = _bearer_token_sent(request)
        if sent is None:
            return next.run(request)
        retryable = request.method in self.retry_methods
        response = next.run(request.copy() if retryable else request)
        if response.status != 401:
            return response
        self.auth.invalidate(sent)
        if not retryable:
            return response
        request.headers["authorization"] = f"Bearer {self.auth()}"
        return next.run(request)


@dataclass
class _RetryMiddleware:
    _retryable_statuses: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    max_retries: int
    retry_methods: frozenset[str]
    backoff_base: float = 0.1
    # The longest `Retry-After` honoured; a longer one ends retrying instead (`None`: no limit).
    max_retry_after: float | None = 60.0

    async def __call__(self, request: Request, next: Next) -> RawResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        if request.method not in self.retry_methods:
            return await next.run(request)
        for attempt in range(self.max_retries + 1):
            try:
                response = await next.run(request.copy())
            except PyreqwestRequestError as error:
                if attempt == self.max_retries or not _is_retryable_send_error(error):
                    raise
                await asyncio.sleep(_backoff_delay(attempt, self.backoff_base, retry_after=None))
                continue
            if attempt == self.max_retries or response.status not in self._retryable_statuses:
                return response
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if _retry_after_exceeds(retry_after, self.max_retry_after):
                return response
            await asyncio.sleep(_backoff_delay(attempt, self.backoff_base, retry_after))
        # range(max_retries+1) is never empty, so this is never actually reached.
        raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class _SyncRetryMiddleware:
    _retryable_statuses: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    max_retries: int
    retry_methods: frozenset[str]
    backoff_base: float = 0.1
    # The longest `Retry-After` honoured; a longer one ends retrying instead (`None`: no limit).
    max_retry_after: float | None = 60.0

    def __call__(self, request: Request, next: SyncNext) -> RawSyncResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        if request.method not in self.retry_methods:
            return next.run(request)
        for attempt in range(self.max_retries + 1):
            try:
                response = next.run(request.copy())
            except PyreqwestRequestError as error:
                if attempt == self.max_retries or not _is_retryable_send_error(error):
                    raise
                time.sleep(_backoff_delay(attempt, self.backoff_base, retry_after=None))
                continue
            if attempt == self.max_retries or response.status not in self._retryable_statuses:
                return response
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if _retry_after_exceeds(retry_after, self.max_retry_after):
                return response
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
def _form_scalar_text(value: int | float) -> str:
    """A numeric form field's text. A `bool` (an `int` subclass) goes out as `"true"`/`"false"`,
    matching how `params=` encodes one, rather than Python's own `"True"`/`"False"`.

    >>> [_form_scalar_text(v) for v in (True, False, 7, 1.5)]
    ['true', 'false', '7', '1.5']
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _pydantic_by_alias(model: BaseModel) -> bool:
    """Whether to encode `model` by its aliases: yes, unless the model explicitly opts out with
    `serialize_by_alias=False`.

    pydantic's own default is to *validate* by alias but *serialize* by field name, so a camelCase
    model decoded from an API went back to it as snake_case. msgspec's `rename=` applies in both
    directions, so lothc now treats pydantic the same way, while still deferring to a model that
    states otherwise, so it never contradicts that model's own `model_dump()`.
    """
    return model.model_config.get("serialize_by_alias", True)


def _encode_json_form_part(value: list[Any] | JSONPayload) -> bytes:
    if isinstance(value, BaseModel):
        return _json_dumps(
            value.model_dump(mode="json", by_alias=_pydantic_by_alias(value))
        ).encode()
    if isinstance(value, Struct):
        return msgspec.json.encode(value)
    return _json_dumps(value).encode()


# Mirrors _apply_form_value's own branch order (not _FormRepeat's member order) so a value's kind
# here is always the branch it will actually take. Returns None for anything unclassifiable and
# leaves the reporting to _apply_form_value's `case _`, which names the offending value.
def _form_value_kind(value: object) -> str | None:
    match value:
        case str() | int() | float():
            return "text"
        case bytes():
            return "bytes"
        case list():
            return "json"
        case (
            (str(), bytes() | Path() | BufferedIOBase())
            | (str(), bytes() | Path() | BufferedIOBase(), str())
            | (Path(), str())
            | Path()
            | BufferedIOBase()
        ):
            return "file"
        case dict() | BaseModel() | Struct():
            return "json"
        case _:
            return None


# The runtime half of _FormRepeat (see its comment above): a repeated part name carries one kind
# of value, so a mixed tuple raises here instead of going out as parts the caller didn't ask for.
# An empty tuple is rejected too — no type can express "non-empty" here, and silently contributing
# no parts at all is never what a caller who wrote a repeat meant (same call as `Params`'s own
# empty-sequence rejection in lothc.testing).
def _check_form_repeat(name: str, items: tuple[object, ...]) -> None:
    if not items:
        raise ValueError(
            f"Empty form value tuple for {name!r}: a tuple repeats one part name once per "
            "element, so an empty one would send no parts at all."
        )
    kinds = {kind for item in items if (kind := _form_value_kind(item)) is not None}
    if len(kinds) > 1:
        found = ", ".join(sorted(kinds))
        raise TypeError(
            f"Mixed form value kinds for {name!r}: a tuple repeats one part name once per "
            f"element, so every element must be the same kind of value, got {found}. A file "
            'part needs an explicit filename — write ("name.txt", content) or '
            '("name.txt", content, "text/plain").'
        )


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
        case int() | float():
            return form_builder.text(name, _form_scalar_text(value))
        case bytes():
            return form_builder.part(name, PartBuilder.from_bytes(value))
        # Must come before the file patterns below: a sequence pattern matches a `list` just as
        # happily as a `tuple`, so ["a.png", b"..."] would otherwise be read as a file when a list
        # always means "JSON-encode me as one part" (docs/verbs.md) — never a file, never a repeat.
        case list():
            body = _encode_json_form_part(cast(list[Any], value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
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
        case dict() | BaseModel() | Struct():
            body = _encode_json_form_part(cast("JSONPayload", value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
        case tuple():
            items = cast("tuple[object, ...]", value)
            _check_form_repeat(name, items)
            for item in items:
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


@dataclass(slots=True, kw_only=True)
class SSEEvent[TData]:
    """One Server-Sent Event, as yielded by `sse()`.

    `.event` defaults to `"message"` and `.id` to `""` when the wire omits them, as the SSE spec
    (and a browser's `EventSource`) does. `.data` is decoded per `sse()`'s `response_data_type`,
    raw `str` by default.
    """

    id: str = ""
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
    last_event_id: str = ""
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
            # The spec dispatches an empty event type as "message", not as "".
            event = value or "message"
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


def _may_complete_sse_record(buffer: bytearray, new_from: int) -> bool:
    r"""Whether the bytes appended to `buffer` from `new_from` on can have completed a record.

    A record only ends at a blank line, so if none arrived with the latest chunk there is nothing
    for `_split_sse_records` to find, and calling it anyway re-normalized the whole buffer once
    per chunk: quadratic for one large event (a 4MB event in 16KB chunks took ~470ms). Looks back
    3 bytes, since the longest boundary, `\r\n\r\n`, can straddle the previous chunk by that
    much. A false positive only costs one needless split; a false negative can't happen.

    >>> _may_complete_sse_record(bytearray(b"data: a\n\n"), 0)
    True
    >>> _may_complete_sse_record(bytearray(b"data: a\ndata: b"), 8)
    False
    >>> _may_complete_sse_record(bytearray(b"data: a\r\n\r\n"), 10)
    True
    """
    window = bytes(buffer[max(0, new_from - 3) :])
    return b"\n\n" in window.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


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


def _require_json_object(parsed: object) -> dict[str, Any]:
    """`parsed` if it's a JSON object; otherwise a clear `ValueError`, not `dict()`'s own
    "object is not iterable"-style `TypeError` for an array or `null` body.

    >>> _require_json_object({"a": 1})
    {'a': 1}
    >>> _require_json_object([1])  # doctest: +ELLIPSIS
    Traceback (most recent call last):
    ...
    ValueError: Expected a JSON object for a `dict` target, got a JSON array. ...
    """
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    match parsed:
        case list():
            kind = "array"
        case str():
            kind = "string"
        case bool():
            kind = "boolean"
        case None:
            kind = "null"
        case _:
            kind = "number"
    raise ValueError(
        f"Expected a JSON object for a `dict` target, got a JSON {kind}. Decode a top-level array"
        " with a pydantic `TypeAdapter` or a msgspec `Decoder` as `response_data_type` instead."
    )


def _decode_error_body(body: bytes, error_type: type[Data]) -> Data:
    """Decode an error response's body for `HTTPResponseError.parsed_body`. Takes plain bytes,
    not a live response object — by the time this runs, `.bytes()` has already been consumed once
    to populate `body_start`, and a pyreqwest response body can't be read twice."""
    _validate_response_data_type(error_type)
    if issubclass(error_type, bytes):
        return body
    if issubclass(error_type, dict):
        return error_type(_require_json_object(_json_loads(body)))
    if issubclass(error_type, Struct):
        return msgspec.json.decode(body, type=error_type)
    # Statically, `Data`'s remaining member here is always BaseModel (basedpyright flags the
    # check itself as unnecessary) — but `error_type` is only actually validated to be *some*
    # class by `_validate_response_data_type`, not proven to be a `Data` member at runtime, so an
    # uncovered class must still fall through to the raise below.
    if issubclass(error_type, BaseModel):  # pyright: ignore[reportUnnecessaryIsInstance] — pylint: disable=line-too-long
        return error_type.model_validate_json(body)
    raise TypeError(f"Unsupported error_type: {error_type!r}")


def _try_decode_error_body(
    body: bytes, error_type: type[Data]
) -> tuple[Data | None, ValueError | None]:
    """Decode an error body for `HTTPResponseError.parsed_body`, best-effort: a body that doesn't
    match `error_type` gives `(None, the decode error)` instead of raising.

    Unlike `response_data_type`, where the decode library's own error rightly propagates, an
    error body is where a server is least likely to honour its own contract: a proxy's 502 HTML
    page against `error_type=ErrorModel` used to raise pydantic's `ValidationError` in place of
    the `HTTPResponseError`, losing the status and skipping `except HTTPResponseError` handlers.
    Every decode failure here is a `ValueError` (pydantic's, msgspec's, `json`'s, and a non-UTF-8
    body's). An unusable `error_type` itself is a caller bug, not a bad body, so the `TypeError`
    it raises still propagates.
    """
    try:
        return _decode_error_body(body, error_type), None
    except ValueError as error:
        return None, error


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
        return dict_type(_require_json_object(_json_loads(data)))
    if issubclass(response_data_type, Struct):
        return msgspec.json.decode(data.encode(), type=response_data_type)
    if issubclass(response_data_type, BaseModel):
        return response_data_type.model_validate_json(data)
    raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")


def _dispatch_sse_record(
    record: bytes,
    state: _SSEStreamState,
    response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
) -> SSEEvent[Any] | None:
    """Apply one record's `retry:`/`id:` fields to `state`, then build its event (if it has data).

    `.id` comes from the *persisted* last-event-id buffer, not this record alone — per spec an
    event with no `id:` line carries the most recent id seen on the stream, and the buffer is
    only cleared by an explicit empty `id:` line.
    """
    # UTF-8 with replacement, as the spec's own decode step does, rather than raising.
    parsed_record = _parse_sse_record(record.decode(errors="replace"))
    if parsed_record.retry_ms is not None:
        state.retry_delay = parsed_record.retry_ms / 1000
    if parsed_record.id is not None:
        state.last_event_id = parsed_record.id
    if parsed_record.data is None:
        return None
    if response_data_type is None:
        return SSEEvent(id=state.last_event_id, event=parsed_record.event, data=parsed_record.data)
    decoded = _decode_json_line(parsed_record.data, response_data_type)
    return SSEEvent(id=state.last_event_id, event=parsed_record.event, data=decoded)


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
        case int() | float():
            return form_builder.text(name, _form_scalar_text(value))
        case bytes():
            return form_builder.part(name, PartBuilder.from_bytes(value))
        # See _apply_form_value's own note here — same reasoning, sync mirror.
        case list():
            body = _encode_json_form_part(cast(list[Any], value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
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
        case dict() | BaseModel() | Struct():
            body = _encode_json_form_part(cast("JSONPayload", value))
            return form_builder.part(name, PartBuilder.from_bytes(body).mime("application/json"))
        case tuple():
            items = cast("tuple[object, ...]", value)
            _check_form_repeat(name, items)
            for item in items:
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
                for name, value in params.model_dump(
                    mode="json", by_alias=_pydantic_by_alias(params)
                ).items()
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
            dumped = headers.model_dump(mode="json", by_alias=_pydantic_by_alias(headers))
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


def _configure_client_builder[TBuilder: BaseClientBuilder](
    builder: TBuilder, settings: _TransportSettings
) -> TBuilder:
    """Apply every setting except the retry middleware, which needs the async or sync class."""
    if settings.timeout is not None:
        builder = builder.timeout(timedelta(seconds=settings.timeout))
    builder = _apply_default_headers(builder, settings.default_headers)
    builder = builder.user_agent(
        settings.user_agent if settings.user_agent is not None else _default_user_agent()
    )
    if settings.http2:
        builder = builder.http2(True)  # noqa: FBT003 — pyreqwest's own positional flag
    for host, ip in (settings.resolve or {}).items():
        # Port 0: pyreqwest ignores it, sending to the URL's own port (or the scheme's default).
        builder = builder.resolve(host, ip, 0)
    if settings.local_address is not None:
        builder = builder.local_address(settings.local_address)
    if settings.tcp_keepalive is not None:
        builder = builder.tcp_keepalive(timedelta(seconds=settings.tcp_keepalive))
    if settings.base_url:
        builder = builder.base_url(settings.base_url)
    builder = builder.default_cookie_store(settings.cookie_store)
    builder = builder.follow_redirects(settings.follow_redirects)
    if settings.max_redirects is not None:
        builder = builder.max_redirects(settings.max_redirects)
    if settings.proxy is not None:
        proxy = ProxyBuilder.all(settings.proxy)
        if settings.no_proxy is not None:
            proxy = proxy.no_proxy(",".join(settings.no_proxy))
        builder = builder.proxy(proxy)
    return _apply_tls_and_pool_config(
        builder,
        connect_timeout=settings.connect_timeout,
        read_timeout=settings.read_timeout,
        root_certificates=settings.root_certificates,
        identity_pem=settings.identity_pem,
        min_tls_version=settings.min_tls_version,
        max_tls_version=settings.max_tls_version,
        danger_accept_invalid_certs=settings.danger_accept_invalid_certs,
        https_only=settings.https_only,
        max_connections=settings.max_connections,
        pool_idle_timeout=settings.pool_idle_timeout,
        pool_max_idle_per_host=settings.pool_max_idle_per_host,
        pool_timeout=settings.pool_timeout,
    )


def _apply_default_headers[TBuilder: BaseClientBuilder](
    builder: TBuilder, default_headers: Headers | None
) -> TBuilder:
    """Encode `default_headers` exactly like a per-request `headers=` (so a model works here too,
    and a `None` field is omitted rather than sent as "None") and set them on the client."""
    encoded = None if default_headers is None else _encode_headers(default_headers)
    return builder.default_headers(encoded) if encoded else builder


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
    """Part of `_configure_client_builder`: `ClientBuilder`/`SyncClientBuilder` both inherit these
    methods from the same `BaseClientBuilder`, so one function configures either."""
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


def _attach_json_body[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, payload: JSONPayload
) -> TBuilder:
    """Attach a `json=` body, encoding a model or `Struct` straight to bytes with its own library.

    Not via `_encode_json_payload` + `.body_json()`, which converted twice (model -> builtins ->
    JSON): encoding directly is 2.9x faster for pydantic and 11x for msgspec on a 2000-item body,
    with byte-identical output. A plain `dict`/`list` still goes through `.body_json()`, which
    pyreqwest already serializes natively. `.headers()`, never `.header()`, sets the Content-Type:
    `.header()` appends, so a caller's own Content-Type would go out as a second value, whereas
    `.headers()` replaces it, which is exactly what `.body_json()` does.
    """
    if isinstance(payload, BaseModel):
        body = payload.model_dump_json(by_alias=_pydantic_by_alias(payload)).encode()
    elif isinstance(payload, Struct):
        body = msgspec.json.encode(payload)
    else:
        return request_builder.body_json(payload)
    return request_builder.body_bytes(body).headers({"content-type": "application/json"})


async def _attach_body[TBuilder: BaseRequestBuilder](  # pylint: disable=too-many-return-statements
    request_builder: TBuilder,
    json: JSONPayload | None,
    data: Params | None,
    form: Form | None,
    content: str | bytes | None,
    *,
    infer_mime_type_from_file_extension: bool,
) -> TBuilder:
    provided_bodies = [body for body in (json, data, form, content) if body is not None]
    if len(provided_bodies) > 1:
        raise ValueError("Provide at most one of 'json', 'data', 'form' or 'content'")
    if json is not None:
        return _attach_json_body(request_builder, json)
    if data is not None:
        return request_builder.form(_query_pairs(_encode_params(data)))
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
    data: Params | None,
    form: Form | None,
    content: str | bytes | None,
    *,
    infer_mime_type_from_file_extension: bool,
) -> TBuilder:
    provided_bodies = [body for body in (json, data, form, content) if body is not None]
    if len(provided_bodies) > 1:
        raise ValueError("Provide at most one of 'json', 'data', 'form' or 'content'")
    if json is not None:
        return _attach_json_body(request_builder, json)
    if data is not None:
        return request_builder.form(_query_pairs(_encode_params(data)))
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


def _offer_stream_item(
    item_queue: queue.Queue[_StreamQueueItem], item: _StreamQueueItem, abandoned: threading.Event
) -> bool:
    """Hand `item` to the consumer, waiting for room, unless the consumer has gone.

    The queue is bounded so a consumer slower than the network gets backpressure instead of the
    worker buffering the whole body in memory, which an unbounded queue did (a slow NDJSON
    consumer would eventually hold the entire stream). Waiting in short slices, rather than one
    blocking `put`, is what lets a worker whose consumer stopped early notice and exit, which
    releases the connection; a plain `put` into a full queue would block forever instead.
    Returns whether the item was delivered.
    """
    while not abandoned.is_set():
        try:
            item_queue.put(item, timeout=0.2)
        except queue.Full:
            continue
        return True
    return False


def _drain_stream_chunks(
    request: SyncStreamRequest,
    check_status: Callable[[RawSyncResponse, type[Data] | None, RequestInfo], None] | None,
    error_type: type[Data] | None,
    request_info: RequestInfo,
    item_queue: queue.Queue[_StreamQueueItem],
    abandoned: threading.Event,
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
                    _offer_stream_item(item_queue, ("eof", None), abandoned)
                    return
                if not _offer_stream_item(item_queue, ("chunk", bytes(chunk)), abandoned):
                    return
    except Exception as error:  # noqa: BLE001 — forwarded to the consumer thread below, not swallowed — pylint: disable=broad-except
        _offer_stream_item(item_queue, ("error", error), abandoned)


def _interruptible_chunk_iter(
    request: SyncStreamRequest,
    check_status: Callable[[RawSyncResponse, type[Data] | None, RequestInfo], None] | None,
    error_type: type[Data] | None,
    request_info: RequestInfo,
    *,
    idle_timeout: float | None = None,
    poll_timeout: float = 0.2,
    max_buffered_chunks: int = 64,
) -> Iterator[bytes]:
    """Yield a streamed response's chunks with Ctrl-C-interruptible waits between them.

    Also the only way the sync client can bound how long it waits on the server: a blocking
    `read_chunk()` takes no timeout, but these timed `Queue.get()`s can give up after
    `idle_timeout` seconds with no chunk (covering the wait for headers too, since the worker
    opens the response). Time the caller spends between chunks isn't counted: the clock
    restarts when the caller asks for the next one.

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
    item_queue: queue.Queue[_StreamQueueItem] = queue.Queue(maxsize=max_buffered_chunks)
    abandoned = threading.Event()
    worker = threading.Thread(
        target=_drain_stream_chunks,
        args=(request, check_status, error_type, request_info, item_queue, abandoned),
        daemon=True,
    )
    worker.start()
    # A quarter of the idle limit (never more than `poll_timeout`, which Ctrl-C needs): the
    # limit is only checked when a wait times out, so a coarse poll would let a stall run up
    # to one extra poll past it, and would make one poll the whole window, so a clock left
    # running across the caller's own pause between chunks could never be told apart.
    wait = poll_timeout if idle_timeout is None else min(poll_timeout, idle_timeout / 4)
    waiting_since = time.monotonic()
    try:
        while True:
            try:
                item = item_queue.get(timeout=wait)
            except queue.Empty:
                if idle_timeout is not None and time.monotonic() - waiting_since > idle_timeout:
                    raise _idle_timeout_error(idle_timeout) from None
                continue
            match item:
                case ("chunk", bytes_chunk):
                    yield bytes_chunk
                    waiting_since = time.monotonic()
                case ("eof", None):
                    return
                case ("error", error):
                    raise error.with_traceback(error.__traceback__)
    finally:
        # However iteration ends (exhausted, an error, an early `break`, garbage collection),
        # tell the worker to stop rather than leave it waiting to hand over more chunks.
        abandoned.set()


def _sync_stream_chunks(
    request: SyncStreamRequest,
    check_status: Callable[[RawSyncResponse, type[Data] | None, RequestInfo], None] | None,
    error_type: type[Data] | None,
    request_info: RequestInfo,
    *,
    interruptible: bool,
    idle_timeout: float | None = None,
) -> Iterator[bytes]:
    # An idle limit needs the worker-thread path too: it's the only one that can stop waiting.
    if interruptible or idle_timeout is not None:
        yield from _interruptible_chunk_iter(
            request, check_status, error_type, request_info, idle_timeout=idle_timeout
        )
        return
    with request as raw_response:
        if check_status is not None:
            check_status(raw_response, error_type, request_info)
        while True:
            chunk = raw_response.body_reader.read_chunk()
            if chunk is None:
                return
            yield bytes(chunk)


# What CaseInsensitiveDict's constructor and update() both accept — mirrors the shapes
# MutableMapping.update itself takes, which is what keeps the override LSP-compatible.
type HeaderSource = Mapping[str, str] | SupportsKeysAndGetItem[str, str] | Iterable[tuple[str, str]]


def _header_pairs(data: HeaderSource) -> Iterator[tuple[str, str]]:
    """Every `(key, value)` pair in `data`, repeating a key once per value it holds.

    `hasattr(data, "keys")` rather than `isinstance(data, Mapping)` is what tells the two
    non-`CaseInsensitiveDict` shapes apart, since `SupportsKeysAndGetItem` is a structural
    protocol with no runtime `isinstance` support — it's also exactly how `dict.update` itself
    decides. Each branch then needs a `cast`, because neither `hasattr` nor the `else` narrows
    the union for a checker.
    """
    if isinstance(data, CaseInsensitiveDict):
        # `Mapping.items()` is single-valued, so reading another CaseInsensitiveDict through the
        # branch below would drop every repeated header's extra values — the one thing the class
        # exists to keep. Goes through the public `get_all` rather than the other instance's
        # `_store` so there's one definition of "all values for this key", not two.
        for key in data:
            for value in data.get_all(key):
                yield (key, value)
    elif isinstance(data, Mapping):
        # `.items()`, never `keys()` + `[key]`: pyreqwest's `HeaderMap` is a Mapping whose
        # `keys()` repeats a duplicated name while `[name]` always returns that name's *first*
        # value, so the pair below would yield the same value once per duplicate (caught by
        # `test_get_result_headers_keep_every_value_of_a_repeated_header`). `.items()` is a live
        # view of the real pairs and gets it right.
        yield from cast("Mapping[str, str]", data).items()
    elif hasattr(data, "keys"):
        source = cast("SupportsKeysAndGetItem[str, str]", data)
        for key in source.keys():  # noqa: SIM118
            yield (key, source[key])
    else:
        yield from cast("Iterable[tuple[str, str]]", data)


class CaseInsensitiveDict(MutableMapping[str, str]):
    """A mapping whose keys compare case-insensitively, as HTTP field names do (RFC 9110 §5.1).

    `Response.headers` and `lothc.testing`'s `MockRequest.headers` are this, so
    `headers["Content-Type"]` and `headers["content-type"]` are the same lookup. Iteration,
    `.keys()` and `dict(...)` yield keys with the casing they were stored under; only lookups,
    `in`, and `==` ignore case — the same split `requests`/`niquests` make.

    Deliberately a `MutableMapping`, not a `dict` subclass: a `dict` subclass can only override
    the Python-level lookups, so `{**headers}` and `dict(headers)` would silently fall back to
    exact-match keys. None of requests, niquests, httpx or httpx2 subclass `dict` here either.
    """

    # Not a @dataclass, unlike most classes here (style-guide.md §1): the one field is a
    # normalized `{lowercased key: (key as stored, every value)}` store rather than anything a
    # caller passes in, and a generated __init__ can't express that translation.
    def __init__(self, data: HeaderSource | None = None) -> None:
        self._store: dict[str, tuple[str, list[str]]] = {}
        if data is not None:
            # Appending rather than overwriting is what keeps every value of a repeated header
            # reachable via `get_all`. Iterating pairs is required for that — `dict(data)` would
            # throw the extras away — while appending in arrival order keeps `__getitem__` on the
            # *first* value, which is what a plain `dict(raw_response.headers)` always returned.
            for key, value in _header_pairs(data):
                lowered = key.lower()
                if lowered in self._store:
                    self._store[lowered][1].append(value)
                else:
                    self._store[lowered] = (key, [value])

    def __getitem__(self, key: str) -> str:
        return self._store[key.lower()][1][0]

    def __setitem__(self, key: str, value: str) -> None:
        # Replaces every value for that name, matching pyreqwest `HeaderMap.__setitem__`'s own
        # documented behaviour rather than quietly appending to a name that already has values.
        self._store[key.lower()] = (key, [value])

    def __delitem__(self, key: str) -> None:
        del self._store[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def __eq__(self, other: object) -> bool:
        # Against another CaseInsensitiveDict, every value counts — two responses whose only
        # difference is a dropped `set-cookie` must not compare equal. Against any other mapping
        # only the single-valued view can be compared, since that's all the other side holds.
        if isinstance(other, CaseInsensitiveDict):
            mine = {key: values for key, (_, values) in self._store.items()}
            theirs = {key: values for key, (_, values) in other._store.items()}
            return mine == theirs
        if not isinstance(other, Mapping):
            return NotImplemented
        other_items = cast("Mapping[str, str]", other).items()
        lowered = {str(key).lower(): value for key, value in other_items}
        return {key: values[0] for key, (_, values) in self._store.items()} == lowered

    def __repr__(self) -> str:
        # Falls back to the pair-list form (which the constructor also accepts, so either form
        # round-trips) as soon as any name is repeated — a repr rendering three `set-cookie`
        # values as one dict entry would hide exactly what this class exists to keep.
        if any(len(values) > 1 for _, values in self._store.values()):
            return f"{type(self).__name__}({list(_header_pairs(self))!r})"
        return f"{type(self).__name__}({dict(self.items())!r})"

    def update(
        self,
        data: HeaderSource = (),
        /,
        **kwargs: str,
    ) -> None:
        """Merge `data` in, replacing every value of each name it carries and keeping the rest.

        Overridden because `MutableMapping.update`'s own mixin assigns one value per key, so
        merging another `CaseInsensitiveDict` through it would drop a repeated header's extra
        values — the same trap `__init__` has to avoid.
        """
        replaced: set[str] = set()
        for key, value in _header_pairs(data):
            lowered = key.lower()
            if lowered in replaced:
                self._store[lowered][1].append(value)
            else:
                replaced.add(lowered)
                self._store[lowered] = (key, [value])
        for key, value in kwargs.items():
            self._store[key.lower()] = (key, [value])

    def copy(self) -> "CaseInsensitiveDict":
        """An independent copy, repeated header values included.

        Present because swapping a plain `dict` for a `MutableMapping` otherwise takes
        `dict.copy` away, and it's the natural way to get a mutable snapshot — `requests`'s own
        `CaseInsensitiveDict` carries it for the same reason. `dict`'s `|` merge operator has no
        equivalent here (nor in requests); build a new one from `_header_pairs` if you need it.
        """
        return CaseInsensitiveDict(self)

    def get_all(self, key: str) -> list[str]:
        """Every value sent under `key`, in arrival order — `[]` if the header wasn't sent.

        `headers[key]` gives only the first, which is the wrong answer for a genuinely repeatable
        field like `set-cookie`. The returned list is a copy, so mutating it changes nothing here.
        """
        lowered = key.lower()
        if lowered not in self._store:
            return []
        return list(self._store[lowered][1])


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
    """What `get`/`post`/`put`/`patch`/`delete`/`head` return: the decoded body (`.data`)
    alongside the status and headers.

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


class HTTPClient:
    """Async typed HTTP client: `async with HTTPClient(base_url=...) as client:`.

    See `SyncHTTPClient` for the sync mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({"GET", "PUT", "DELETE", "HEAD"})
    # The streaming verbs (`sse`, `stream_get`, `stream_post`, `download`) swap the client's total
    # `timeout` for this per-request one whenever the caller doesn't pass their own: pyreqwest has
    # no "no timeout" override, and a total timeout on a long-lived body kills every healthy
    # stream or large download at the 30s mark. A year is "never". Stall detection comes from
    # `_stream_idle_timeout` instead (not for `sse`, whose quiet gaps are legitimate).
    _streaming_default_timeout: ClassVar[float] = 365 * 24 * 60 * 60

    _bearer_token: str | None
    _bearer_auth: AuthProvider | None
    _basic_auth: tuple[str, str | None] | None
    # The longest gap `stream_get`/`stream_post`/`download` tolerate between chunks: the client's
    # `timeout`, unless `read_timeout` was set, in which case pyreqwest already enforces that
    # itself at the socket and this stays `None`. Not applied to `sse`.
    _stream_idle_timeout: float | None
    _transport: _TransportSettings
    _pyreqwest_client: Client | None
    _exit_stack: AsyncExitStack | None

    # Not a @dataclass, unlike most classes here (style-guide.md §1): the constructor takes the
    # client's *settings*, while the pyreqwest client it wraps only exists between entering and
    # exiting, and is rebuilt from those settings on every entry, since a pyreqwest builder can
    # only be built once. That also means one client can be entered again after it exits.
    # Every "local" pylint counts here is a keyword setting, not working state.
    def __init__(  # pylint: disable=too-many-locals
        self,
        *,
        base_url: str | None = None,
        bearer_token: str | None = None,
        bearer_auth: AuthProvider | None = None,
        basic_auth: tuple[str, str | None] | None = None,
        default_headers: Headers | None = None,
        user_agent: str | None = None,
        timeout: float | None = 30.0,
        cookie_store: bool = False,
        follow_redirects: bool = True,
        max_redirects: int | None = None,
        proxy: str | None = None,
        no_proxy: list[str] | tuple[str, ...] | None = None,
        max_retries: int = 0,
        retry_methods: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
        backoff_base: float = 0.1,
        max_retry_after: float | None = 60.0,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        root_certificates: Sequence[bytes] | None = None,
        identity_pem: bytes | None = None,
        min_tls_version: TlsVersion | None = None,
        max_tls_version: TlsVersion | None = None,
        danger_accept_invalid_certs: bool = False,
        https_only: bool = False,
        http2: bool = False,
        resolve: Mapping[str, str] | None = None,
        local_address: str | None = None,
        tcp_keepalive: float | None = None,
        max_connections: int | None = None,
        pool_idle_timeout: float | None = None,
        pool_max_idle_per_host: int | None = None,
        pool_timeout: float | None = None,
    ) -> None:
        """The client's settings. Nothing opens until it's entered, as
        `async with HTTPClient(...) as client:`.

        `bearer_token` is a static token; `bearer_auth` is an async callable resolved fresh on
        every request; `basic_auth` is a `(username, password)` pair — provide at most one of the
        three. `max_retries` enables a real retry middleware (backoff, `Retry-After`-aware); with
        no `retry_methods`, only the idempotent verbs (`GET`/`PUT`/`DELETE`/`HEAD`) retry, and
        `backoff_base` scales the wait between attempts (`backoff_base * 2 ** attempt`, so the
        0.1 default waits 0.1s then 0.2s) — a `Retry-After` header on the response wins over the
        computed delay, up to `max_retry_after` (60s by default): a server asking for a longer wait
        ends retrying, and its 429/503 is raised as `HTTPResponseError` so you can schedule a retry
        from `e.headers["Retry-After"]`. Pass `max_retry_after=None` to honour any wait.

        `connect_timeout` bounds only the TCP connect phase (separate from `timeout`, which
        covers the whole request); `read_timeout` bounds the idle gap between two consecutive
        body chunks (the right knob for a long-lived `sse()` stream, which `timeout` never
        applies to). `root_certificates` (each a PEM-encoded cert) and
        `identity_pem` (a PEM bundle containing both a client cert and its private key) cover
        trusting a custom/internal CA and mTLS respectively. `danger_accept_invalid_certs`
        disables certificate validation entirely — insecure, for local/test use only.
        `max_connections`/`pool_idle_timeout`/`pool_max_idle_per_host`/`pool_timeout` tune the
        underlying connection pool; leave them `None` to keep pyreqwest's own defaults.

        `user_agent` sets the `User-Agent` header every request sends, `python-lothc/<version>`
        by default (a per-request `headers=` still overrides it). `no_proxy` lists hosts that
        bypass `proxy`, each written as one entry of the `NO_PROXY` environment variable would be
        (e.g. `"localhost"`); it needs `proxy`.
        `http2=True` negotiates HTTP/2 with servers that support it (over TLS, via ALPN),
        falling back to HTTP/1.1 otherwise. `resolve` maps hostnames to IP addresses, skipping
        DNS for them (the URL's port is still used); `local_address` is the source IP to connect
        from; `tcp_keepalive` (seconds) turns on TCP keepalive probes for idle connections.
        """
        if sum(value is not None for value in (bearer_token, bearer_auth, basic_auth)) > 1:
            raise ValueError(
                "Provide at most one of 'bearer_token', 'bearer_auth', or 'basic_auth'"
            )
        self._bearer_token = bearer_token
        self._bearer_auth = bearer_auth
        self._basic_auth = basic_auth
        self._stream_idle_timeout = timeout if read_timeout is None else None
        self._transport = _TransportSettings(
            base_url=base_url,
            default_headers=default_headers,
            user_agent=user_agent,
            timeout=timeout,
            cookie_store=cookie_store,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            proxy=proxy,
            no_proxy=no_proxy,
            max_retries=max_retries,
            backoff_base=backoff_base,
            max_retry_after=max_retry_after,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            root_certificates=root_certificates,
            identity_pem=identity_pem,
            min_tls_version=min_tls_version,
            max_tls_version=max_tls_version,
            danger_accept_invalid_certs=danger_accept_invalid_certs,
            https_only=https_only,
            http2=http2,
            resolve=resolve,
            local_address=local_address,
            tcp_keepalive=tcp_keepalive,
            max_connections=max_connections,
            pool_idle_timeout=pool_idle_timeout,
            pool_max_idle_per_host=pool_max_idle_per_host,
            pool_timeout=pool_timeout,
            retry_methods=_retry_method_set(retry_methods, self._default_retry_methods),
        )
        self._pyreqwest_client = None
        self._exit_stack = None

    async def __aenter__(self) -> Self:
        if self._pyreqwest_client is not None:
            raise RuntimeError("This HTTPClient is already open; enter it once at a time.")
        builder = _configure_client_builder(ClientBuilder(), self._transport)
        if self._transport.max_retries > 0:
            builder = builder.with_middleware(
                _RetryMiddleware(
                    self._transport.max_retries,
                    self._transport.retry_methods,
                    self._transport.backoff_base,
                    self._transport.max_retry_after,
                )
            )
        if isinstance(self._bearer_auth, _InvalidatableAuth):
            builder = builder.with_middleware(
                _ReauthMiddleware(self._bearer_auth, self._default_retry_methods)
            )
        stack = AsyncExitStack()
        self._pyreqwest_client = await stack.enter_async_context(builder.build())
        self._exit_stack = stack
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        stack = self._exit_stack
        self._pyreqwest_client = None
        self._exit_stack = None
        if stack is not None:
            await stack.aclose()

    @property
    def _client(self) -> Client:
        if self._pyreqwest_client is None:
            raise RuntimeError(
                "The client is closed (or was never opened): use it as "
                "`async with HTTPClient(...) as client:`."
            )
        return self._pyreqwest_client

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
        body = (await _read_body(raw_response)).to_bytes()
        parsed_body, parse_error = (
            (None, None) if error_type is None else _try_decode_error_body(body, error_type)
        )
        raise HTTPResponseError(
            raw_response.status,
            body,
            request_info,
            parsed_body,
            headers=CaseInsensitiveDict(raw_response.headers),
            parse_error=parse_error,
        )

    async def _decode_body(
        self,
        raw_response: RawResponse,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
    ) -> Data:
        body = await _read_body(raw_response)
        # A pydantic `TypeAdapter` or msgspec `Decoder` is how a top-level JSON array (or any
        # non-model shape) gets a typed decode, e.g. `TypeAdapter(list[Item])`, the same targets
        # `sse()`/`stream_*` already accept for each line.
        if isinstance(response_data_type, TypeAdapter):
            return response_data_type.validate_json(body.to_bytes())
        if isinstance(response_data_type, Decoder):
            return response_data_type.decode(body)
        # See `_decode_json_line`: the two structural Protocols can't be narrowed away above.
        response_data_type = cast("type[Data]", response_data_type)
        _validate_response_data_type(response_data_type)
        if issubclass(response_data_type, bytes):
            return body.to_bytes()
        if issubclass(response_data_type, dict):
            # stdlib `json`, not pyreqwest's `.json()`: that raises pyreqwest's own
            # `JSONDecodeError`, and reading the body separately is what lets a body cut short be
            # told apart from one that just isn't valid JSON.
            return response_data_type(_require_json_object(_json_loads(body.to_bytes())))
        if issubclass(response_data_type, Struct):
            return msgspec.json.decode(body, type=response_data_type)
        # Statically, `Data`'s remaining member here is always BaseModel (basedpyright flags the
        # check itself as unnecessary) — but `response_data_type` is only actually validated to
        # be *some* class by `_validate_response_data_type`, not proven to be a `Data` member at
        # runtime, so an uncovered class must still fall through to the raise below.
        if issubclass(response_data_type, BaseModel):  # pyright: ignore[reportUnnecessaryIsInstance] — pylint: disable=line-too-long
            return response_data_type.model_validate_json(body.to_bytes())
        raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")

    async def _parse(
        self,
        raw_response: RawResponse,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
        request_info: RequestInfo,
        *,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Data:
        if error_for_status:
            await self._check_status(raw_response, error_type, request_info)
        return await self._decode_body(raw_response, response_data_type)

    async def _send_method(
        self,
        method: str,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        data: Params | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Response[Any, Any]:
        """Every body verb's shared path, taking the HTTP method as a string."""
        return await self._send_with_body(
            self._client.request(method, path),
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
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
        data: Params | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Response[Any, Any]:
        request_builder = await self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = await _attach_body(
            request_builder,
            json,
            data,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        started = time.monotonic()
        raw_response, request_info = await _send(request_builder)
        elapsed = time.monotonic() - started
        if error_for_status:
            await self._check_status(raw_response, error_type, request_info)
        decoded = await self._decode_body(raw_response, response_data_type)
        response_headers = CaseInsensitiveDict(raw_response.headers)
        if response_headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(response_headers, response_headers_type)
        return Response(
            decoded,
            raw_response.status,
            response_headers,
            typed_headers,
            request_info,
            raw_response.version,
            elapsed,
        )

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
    ) -> Response[bytes]: ...
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
    ) -> Response[dict[str, Any]]: ...
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
    ) -> Response[TData]: ...
    @overload
    async def get[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def get[THeaders: TypedHeaders](
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
    ) -> Response[bytes, THeaders]: ...
    @overload
    async def get[THeaders: TypedHeaders](
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
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    async def get[TData: Data, THeaders: TypedHeaders](
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
    ) -> Response[TData, THeaders]: ...
    @overload
    async def get[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """GET `path`, returning a `Response` whose `.data` is the body decoded as
        `response_data_type` (raw `bytes` by default), alongside `.status`, `.headers`,
        `.request`, `.http_version` and `.elapsed`. Pass `response_headers_type` to also get the
        headers parsed into `.typed_headers`.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_method(
            "GET",
            path,
            params,
            headers,
            timeout,
            None,
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
    async def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    async def post[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def post[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    async def post[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    async def post[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    async def post[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    async def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """POST to `path` with at most one of `json`/`data`/`form`/`content` (raises
        `ValueError` if more than one is given), returning a `Response` like `get`'s.
        `data` is a urlencoded body (`a=1&b=2`), `form` a `multipart/form-data` one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_method(
            "POST",
            path,
            params,
            headers,
            timeout,
            json,
            data,
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    async def put[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def put[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    async def put[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    async def put[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    async def put[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    async def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """PUT to `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_method(
            "PUT",
            path,
            params,
            headers,
            timeout,
            json,
            data,
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    async def patch[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def patch[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    async def patch[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    async def patch[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    async def patch[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    async def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """PATCH `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_method(
            "PATCH",
            path,
            params,
            headers,
            timeout,
            json,
            data,
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
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
    @overload
    async def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
    @overload
    async def delete[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    async def delete[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def delete[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    async def delete[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    async def delete[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    async def delete[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    async def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """DELETE `path`, returning a `Response` like `get`'s. A body is rare on a DELETE but
        allowed: the same `json`/`data`/`form`/`content` options as `post`, at most one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._send_method(
            "DELETE",
            path,
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    async def _sse_connection(
        self,
        request_builder: RequestBuilder,
        state: _SSEStreamState,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> AsyncGenerator[SSEEvent[Any]]:
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
                buffer = bytearray()
                strip_bom = True
                while True:
                    chunk = await raw_response.body_reader.read_chunk()
                    if chunk is None:
                        return
                    new_from = len(buffer)
                    buffer += chunk
                    if strip_bom and len(buffer) >= len(codecs.BOM_UTF8):
                        buffer = buffer.removeprefix(codecs.BOM_UTF8)
                        strip_bom = False
                        new_from = 0
                    if not _may_complete_sse_record(buffer, new_from):
                        continue
                    records, tail = _split_sse_records(bytes(buffer))
                    buffer = bytearray(tail)
                    for record in records:
                        event = _dispatch_sse_record(
                            record,
                            state,
                            response_data_type,
                        )
                        if event is not None:
                            yield event
        except (PyreqwestRequestError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    async def _sse_stream(  # pylint: disable=too-many-locals
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        skip_auth: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
    ) -> AsyncGenerator[SSEEvent[Any]]:
        state = _SSEStreamState(retry_delay=reconnect_delay)
        consecutive_reconnects = 0
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        while True:
            request_builder = self._client.get(path).header("accept", "text/event-stream")
            request_builder = await self._prepare_request(
                request_builder, params, headers, effective_timeout, skip_auth=skip_auth
            )
            if state.last_event_id:
                request_builder = request_builder.header("last-event-id", state.last_event_id)
            connection = self._sse_connection(
                request_builder,
                state,
                response_data_type,
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
            except HTTPTransportError as error:
                # A permanent failure can't be reconnected away, so it propagates on the first
                # attempt instead of burning the whole budget sleeping between retries that
                # cannot possibly succeed.
                if _is_permanent_transport_error(error) or _sse_reconnect_budget_exhausted(
                    max_reconnects, consecutive_reconnects
                ):
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
    ) -> AsyncGenerator[SSEEvent[str]]: ...
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
    ) -> AsyncGenerator[SSEEvent[dict[str, Any]]]: ...
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
    ) -> AsyncGenerator[SSEEvent[TData]]: ...
    def sse(
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
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncGenerator[SSEEvent[Any]]:
        """Open `path` as a Server-Sent Events stream, yielding one `SSEEvent` per event.

        `response_data_type` decodes `.data` (a class, `TypeAdapter`, or msgspec `Decoder`);
        `.event`/`.id` are always populated regardless (`"message"`/`""` when the server omits
        them).

        The client's `timeout` never applies here — an SSE stream is open-ended, and a total
        request timeout would kill every healthy stream on schedule. `timeout` bounds one
        connection attempt only; use the client's `read_timeout` to detect a stalled stream.
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
            skip_auth=skip_auth,
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
        data: Params | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> AsyncGenerator[Any]:
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        request_builder = await self._prepare_request(
            request_builder, params, headers, effective_timeout, skip_auth=skip_auth
        )
        request_builder = await _attach_body(
            request_builder,
            json,
            data,
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
            async with AsyncExitStack() as stack:
                async with _within_idle_limit(idle):
                    raw_response = await stack.enter_async_context(request)
                    if error_for_status:
                        await self._check_status(raw_response, error_type, request_info)
                if response_data_type is None:
                    while True:
                        chunk = await _read_chunk_within(raw_response, idle)
                        if chunk is None:
                            return
                        yield bytes(chunk)
                pending = bytearray()
                while True:
                    chunk = await _read_chunk_within(raw_response, idle)
                    if chunk is None:
                        break
                    for line in _complete_ndjson_lines(pending, chunk):
                        yield _decode_json_line(line.decode(), response_data_type)
                if pending.strip():
                    yield _decode_json_line(pending.decode(), response_data_type)
        except (PyreqwestRequestError, PyreqwestBuilderError) as error:
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
    ) -> AsyncGenerator[bytes]: ...
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
    ) -> AsyncGenerator[dict[str, Any]]: ...
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
    ) -> AsyncGenerator[TLine]: ...
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
    ) -> AsyncGenerator[Any]:
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncGenerator[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncGenerator[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TLine] | TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncGenerator[TLine]: ...
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> AsyncGenerator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`data`/`form`/`content`
        options as `post` (at most one), same raw-bytes-by-default / NDJSON-via-
        `response_data_type` split.

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
            data,
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
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        request_builder = await self._prepare_request(
            self._client.get(path), params, headers, effective_timeout, skip_auth=skip_auth
        )
        try:
            # `limit(1)` so a chunk is handed over as soon as it arrives. pyreqwest's 64KB default
            # withholds bytes until that much is buffered, so a slow but healthy download (say,
            # 1KB/s) would look silent for over a minute and trip the idle limit. Measured: no
            # throughput cost (100MB in 79ms vs 80ms on the default).
            request = request_builder.streamed_read_buffer_limit(1).build_streamed()
            request_info = _request_info(request)
            async with AsyncExitStack() as stack:
                async with _within_idle_limit(idle):
                    raw_response = await stack.enter_async_context(request)
                    if error_for_status:
                        await self._check_status(raw_response, error_type, request_info)
                if dest is None:
                    buffer = bytearray()
                    while True:
                        chunk = await _read_chunk_within(raw_response, idle)
                        if chunk is None:
                            return bytes(buffer)
                        buffer += chunk
                with _atomic_download_file(dest) as file:
                    while True:
                        chunk = await _read_chunk_within(raw_response, idle)
                        if chunk is None:
                            return None
                        file.write(chunk)
        except (PyreqwestRequestError, PyreqwestBuilderError) as error:
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
        dest: str | PathLike[str],
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
        dest: str | PathLike[str] | None = None,
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
        O(body size) memory, but about two-thirds of what `get()` peaks at (measured: 105MB vs
        154MB for a 50MB body), since `get()` goes through pyreqwest's own `.bytes()`. Pass
        `dest` to stream straight to a file instead — memory then stays O(chunk size) regardless
        of how large the body is.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return await self._download(
            path,
            None if dest is None else Path(dest),
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
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
    ) -> Response[None]: ...
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
    ) -> Response[None, THeaders]: ...
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
    ) -> Response[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass
        `response_headers_type` to get the response headers parsed into `.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = await self._prepare_request(
            self._client.head(path), params, headers, timeout, skip_auth=skip_auth
        )
        started = time.monotonic()
        raw_response, request_info = await _send(request_builder)
        elapsed = time.monotonic() - started
        if error_for_status:
            await self._check_status(raw_response, error_type, request_info)
        response_headers = CaseInsensitiveDict(raw_response.headers)
        typed_headers = (
            None
            if response_headers_type is None
            else _parse_typed_headers(response_headers, response_headers_type)
        )
        return Response(
            None,
            raw_response.status,
            response_headers,
            typed_headers,
            request_info,
            raw_response.version,
            elapsed,
        )


class SyncHTTPClient:
    """Sync typed HTTP client: `with SyncHTTPClient(base_url=...) as client:`.

    See `HTTPClient` for the async mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({"GET", "PUT", "DELETE", "HEAD"})
    # The streaming verbs (`sse`, `stream_get`, `stream_post`, `download`) swap the client's total
    # `timeout` for this per-request one whenever the caller doesn't pass their own: pyreqwest has
    # no "no timeout" override, and a total timeout on a long-lived body kills every healthy
    # stream or large download at the 30s mark. A year is "never". Stall detection comes from
    # `_stream_idle_timeout` instead (not for `sse`, whose quiet gaps are legitimate).
    _streaming_default_timeout: ClassVar[float] = 365 * 24 * 60 * 60

    _bearer_token: str | None
    _bearer_auth: SyncAuthProvider | None
    _basic_auth: tuple[str, str | None] | None
    # The longest gap `stream_get`/`stream_post`/`download` tolerate between chunks: the client's
    # `timeout`, unless `read_timeout` was set, in which case pyreqwest already enforces that
    # itself at the socket and this stays `None`. Not applied to `sse`.
    _stream_idle_timeout: float | None
    _transport: _TransportSettings
    _pyreqwest_client: SyncClient | None
    _exit_stack: ExitStack | None

    # Not a @dataclass, unlike most classes here (style-guide.md §1): the constructor takes the
    # client's *settings*, while the pyreqwest client it wraps only exists between entering and
    # exiting, and is rebuilt from those settings on every entry, since a pyreqwest builder can
    # only be built once. That also means one client can be entered again after it exits.
    # Every "local" pylint counts here is a keyword setting, not working state.
    def __init__(  # pylint: disable=too-many-locals
        self,
        *,
        base_url: str | None = None,
        bearer_token: str | None = None,
        bearer_auth: SyncAuthProvider | None = None,
        basic_auth: tuple[str, str | None] | None = None,
        default_headers: Headers | None = None,
        user_agent: str | None = None,
        timeout: float | None = 30.0,
        cookie_store: bool = False,
        follow_redirects: bool = True,
        max_redirects: int | None = None,
        proxy: str | None = None,
        no_proxy: list[str] | tuple[str, ...] | None = None,
        max_retries: int = 0,
        retry_methods: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
        backoff_base: float = 0.1,
        max_retry_after: float | None = 60.0,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        root_certificates: Sequence[bytes] | None = None,
        identity_pem: bytes | None = None,
        min_tls_version: TlsVersion | None = None,
        max_tls_version: TlsVersion | None = None,
        danger_accept_invalid_certs: bool = False,
        https_only: bool = False,
        http2: bool = False,
        resolve: Mapping[str, str] | None = None,
        local_address: str | None = None,
        tcp_keepalive: float | None = None,
        max_connections: int | None = None,
        pool_idle_timeout: float | None = None,
        pool_max_idle_per_host: int | None = None,
        pool_timeout: float | None = None,
    ) -> None:
        """The client's settings. Nothing opens until it's entered, as
        `with SyncHTTPClient(...) as client:`.

        `bearer_token` is a static token; `bearer_auth` is a callable resolved fresh on every
        request; `basic_auth` is a `(username, password)` pair — provide at most one of the
        three. `max_retries` enables a real retry middleware (backoff, `Retry-After`-aware); with
        no `retry_methods`, only the idempotent verbs (`GET`/`PUT`/`DELETE`/`HEAD`) retry, and
        `backoff_base` scales the wait between attempts (`backoff_base * 2 ** attempt`, so the
        0.1 default waits 0.1s then 0.2s) — a `Retry-After` header on the response wins over the
        computed delay, up to `max_retry_after` (60s by default): a server asking for a longer wait
        ends retrying, and its 429/503 is raised as `HTTPResponseError` so you can schedule a retry
        from `e.headers["Retry-After"]`. Pass `max_retry_after=None` to honour any wait.

        `connect_timeout` bounds only the TCP connect phase (separate from `timeout`, which
        covers the whole request); `read_timeout` bounds the idle gap between two consecutive
        body chunks (the right knob for a long-lived `sse()` stream, which `timeout` never
        applies to). `root_certificates` (each a PEM-encoded cert) and
        `identity_pem` (a PEM bundle containing both a client cert and its private key) cover
        trusting a custom/internal CA and mTLS respectively. `danger_accept_invalid_certs`
        disables certificate validation entirely — insecure, for local/test use only.
        `max_connections`/`pool_idle_timeout`/`pool_max_idle_per_host`/`pool_timeout` tune the
        underlying connection pool; leave them `None` to keep pyreqwest's own defaults.

        `user_agent` sets the `User-Agent` header every request sends, `python-lothc/<version>`
        by default (a per-request `headers=` still overrides it). `no_proxy` lists hosts that
        bypass `proxy`, each written as one entry of the `NO_PROXY` environment variable would be
        (e.g. `"localhost"`); it needs `proxy`.
        `http2=True` negotiates HTTP/2 with servers that support it (over TLS, via ALPN),
        falling back to HTTP/1.1 otherwise. `resolve` maps hostnames to IP addresses, skipping
        DNS for them (the URL's port is still used); `local_address` is the source IP to connect
        from; `tcp_keepalive` (seconds) turns on TCP keepalive probes for idle connections.
        """
        if sum(value is not None for value in (bearer_token, bearer_auth, basic_auth)) > 1:
            raise ValueError(
                "Provide at most one of 'bearer_token', 'bearer_auth', or 'basic_auth'"
            )
        self._bearer_token = bearer_token
        self._bearer_auth = bearer_auth
        self._basic_auth = basic_auth
        self._stream_idle_timeout = timeout if read_timeout is None else None
        self._transport = _TransportSettings(
            base_url=base_url,
            default_headers=default_headers,
            user_agent=user_agent,
            timeout=timeout,
            cookie_store=cookie_store,
            follow_redirects=follow_redirects,
            max_redirects=max_redirects,
            proxy=proxy,
            no_proxy=no_proxy,
            max_retries=max_retries,
            backoff_base=backoff_base,
            max_retry_after=max_retry_after,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            root_certificates=root_certificates,
            identity_pem=identity_pem,
            min_tls_version=min_tls_version,
            max_tls_version=max_tls_version,
            danger_accept_invalid_certs=danger_accept_invalid_certs,
            https_only=https_only,
            http2=http2,
            resolve=resolve,
            local_address=local_address,
            tcp_keepalive=tcp_keepalive,
            max_connections=max_connections,
            pool_idle_timeout=pool_idle_timeout,
            pool_max_idle_per_host=pool_max_idle_per_host,
            pool_timeout=pool_timeout,
            retry_methods=_retry_method_set(retry_methods, self._default_retry_methods),
        )
        self._pyreqwest_client = None
        self._exit_stack = None

    def __enter__(self) -> Self:
        if self._pyreqwest_client is not None:
            raise RuntimeError("This SyncHTTPClient is already open; enter it once at a time.")
        builder = _configure_client_builder(SyncClientBuilder(), self._transport)
        if self._transport.max_retries > 0:
            builder = builder.with_middleware(
                _SyncRetryMiddleware(
                    self._transport.max_retries,
                    self._transport.retry_methods,
                    self._transport.backoff_base,
                    self._transport.max_retry_after,
                )
            )
        if isinstance(self._bearer_auth, _SyncInvalidatableAuth):
            builder = builder.with_middleware(
                _SyncReauthMiddleware(self._bearer_auth, self._default_retry_methods)
            )
        stack = ExitStack()
        self._pyreqwest_client = stack.enter_context(builder.build())
        self._exit_stack = stack
        return self

    def __exit__(self, *exc_info: object) -> None:
        stack = self._exit_stack
        self._pyreqwest_client = None
        self._exit_stack = None
        if stack is not None:
            stack.close()

    @property
    def _client(self) -> SyncClient:
        if self._pyreqwest_client is None:
            raise RuntimeError(
                "The client is closed (or was never opened): use it as "
                "`with SyncHTTPClient(...) as client:`."
            )
        return self._pyreqwest_client

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
        body = _read_body_sync(raw_response).to_bytes()
        parsed_body, parse_error = (
            (None, None) if error_type is None else _try_decode_error_body(body, error_type)
        )
        raise HTTPResponseError(
            raw_response.status,
            body,
            request_info,
            parsed_body,
            headers=CaseInsensitiveDict(raw_response.headers),
            parse_error=parse_error,
        )

    def _decode_body(
        self,
        raw_response: RawSyncResponse,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
    ) -> Data:
        body = _read_body_sync(raw_response)
        # A pydantic `TypeAdapter` or msgspec `Decoder` is how a top-level JSON array (or any
        # non-model shape) gets a typed decode, e.g. `TypeAdapter(list[Item])`, the same targets
        # `sse()`/`stream_*` already accept for each line.
        if isinstance(response_data_type, TypeAdapter):
            return response_data_type.validate_json(body.to_bytes())
        if isinstance(response_data_type, Decoder):
            return response_data_type.decode(body)
        # See `_decode_json_line`: the two structural Protocols can't be narrowed away above.
        response_data_type = cast("type[Data]", response_data_type)
        _validate_response_data_type(response_data_type)
        if issubclass(response_data_type, bytes):
            return body.to_bytes()
        if issubclass(response_data_type, dict):
            # See the async mirror's note on stdlib `json` here.
            return response_data_type(_require_json_object(_json_loads(body.to_bytes())))
        if issubclass(response_data_type, Struct):
            return msgspec.json.decode(body, type=response_data_type)
        # See the async `_decode_body`'s comment above the equivalent check — same reasoning.
        if issubclass(response_data_type, BaseModel):  # pyright: ignore[reportUnnecessaryIsInstance] — pylint: disable=line-too-long
            return response_data_type.model_validate_json(body.to_bytes())
        raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")

    def _parse(
        self,
        raw_response: RawSyncResponse,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
        request_info: RequestInfo,
        *,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Data:
        if error_for_status:
            self._check_status(raw_response, error_type, request_info)
        return self._decode_body(raw_response, response_data_type)

    def _send_method(
        self,
        method: str,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        json: JSONPayload | None,
        data: Params | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Response[Any, Any]:
        """Every body verb's shared path, taking the HTTP method as a string."""
        return self._send_with_body(
            self._client.request(method, path),
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
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
        data: Params | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any],
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
    ) -> Response[Any, Any]:
        request_builder = self._prepare_request(
            request_builder, params, headers, timeout, skip_auth=skip_auth
        )
        request_builder = _attach_body_sync(
            request_builder,
            json,
            data,
            form,
            content,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )
        started = time.monotonic()
        raw_response, request_info = _send_sync(request_builder)
        elapsed = time.monotonic() - started
        if error_for_status:
            self._check_status(raw_response, error_type, request_info)
        decoded = self._decode_body(raw_response, response_data_type)
        response_headers = CaseInsensitiveDict(raw_response.headers)
        if response_headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(response_headers, response_headers_type)
        return Response(
            decoded,
            raw_response.status,
            response_headers,
            typed_headers,
            request_info,
            raw_response.version,
            elapsed,
        )

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
    ) -> Response[bytes]: ...
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
    ) -> Response[dict[str, Any]]: ...
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
    ) -> Response[TData]: ...
    @overload
    def get[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def get[THeaders: TypedHeaders](
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
    ) -> Response[bytes, THeaders]: ...
    @overload
    def get[THeaders: TypedHeaders](
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
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    def get[TData: Data, THeaders: TypedHeaders](
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
    ) -> Response[TData, THeaders]: ...
    @overload
    def get[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """GET `path`, returning a `Response` whose `.data` is the body decoded as
        `response_data_type` (raw `bytes` by default), alongside `.status`, `.headers`,
        `.request`, `.http_version` and `.elapsed`. Pass `response_headers_type` to also get the
        headers parsed into `.typed_headers`.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_method(
            "GET",
            path,
            params,
            headers,
            timeout,
            None,
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
    def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    def post[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def post[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    def post[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    def post[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    def post[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """POST to `path` with at most one of `json`/`data`/`form`/`content` (raises
        `ValueError` if more than one is given), returning a `Response` like `get`'s.
        `data` is a urlencoded body (`a=1&b=2`), `form` a `multipart/form-data` one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_method(
            "POST",
            path,
            params,
            headers,
            timeout,
            json,
            data,
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    def put[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def put[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    def put[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    def put[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    def put[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """PUT to `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_method(
            "PUT",
            path,
            params,
            headers,
            timeout,
            json,
            data,
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    def patch[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def patch[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    def patch[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    def patch[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    def patch[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """PATCH `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_method(
            "PATCH",
            path,
            params,
            headers,
            timeout,
            json,
            data,
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
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes]: ...
    @overload
    def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[dict[str, Any]],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any]]: ...
    @overload
    def delete[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[TData],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData]: ...
    @overload
    def delete[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def delete[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[bytes, THeaders]: ...
    @overload
    def delete[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[dict[str, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    def delete[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TData, THeaders]: ...
    @overload
    def delete[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        response_data_type: type[Data] | TypeAdapterTyping[Any] | DecoderTyping[Any] = bytes,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
    ) -> Response[Any, Any]:
        """DELETE `path`, returning a `Response` like `get`'s. A body is rare on a DELETE but
        allowed: the same `json`/`data`/`form`/`content` options as `post`, at most one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._send_method(
            "DELETE",
            path,
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            response_data_type,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            error_type=error_type,
        )

    def _sse_connection(
        self,
        request_builder: SyncRequestBuilder,
        state: _SSEStreamState,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        error_for_status: bool,
        error_type: type[Data] | None,
        interruptible: bool,
    ) -> Generator[SSEEvent[Any]]:
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
            buffer = bytearray()
            strip_bom = True
            for chunk in _sync_stream_chunks(
                request, response_check, error_type, request_info, interruptible=interruptible
            ):
                new_from = len(buffer)
                buffer += chunk
                if strip_bom and len(buffer) >= len(codecs.BOM_UTF8):
                    buffer = buffer.removeprefix(codecs.BOM_UTF8)
                    strip_bom = False
                    new_from = 0
                if not _may_complete_sse_record(buffer, new_from):
                    continue
                records, tail = _split_sse_records(bytes(buffer))
                buffer = bytearray(tail)
                for record in records:
                    event = _dispatch_sse_record(
                        record,
                        state,
                        response_data_type,
                    )
                    if event is not None:
                        yield event
        except (PyreqwestRequestError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error

    def _sse_stream(  # pylint: disable=too-many-locals
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        skip_auth: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
        interruptible: bool,
    ) -> Generator[SSEEvent[Any]]:
        state = _SSEStreamState(retry_delay=reconnect_delay)
        consecutive_reconnects = 0
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        while True:
            request_builder = self._client.get(path).header("accept", "text/event-stream")
            request_builder = self._prepare_request(
                request_builder, params, headers, effective_timeout, skip_auth=skip_auth
            )
            if state.last_event_id:
                request_builder = request_builder.header("last-event-id", state.last_event_id)
            connection = self._sse_connection(
                request_builder,
                state,
                response_data_type,
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
            except HTTPTransportError as error:
                # A permanent failure can't be reconnected away, so it propagates on the first
                # attempt instead of burning the whole budget sleeping between retries that
                # cannot possibly succeed.
                if _is_permanent_transport_error(error) or _sse_reconnect_budget_exhausted(
                    max_reconnects, consecutive_reconnects
                ):
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
    ) -> Generator[SSEEvent[str]]: ...
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
    ) -> Generator[SSEEvent[dict[str, Any]]]: ...
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
    ) -> Generator[SSEEvent[TData]]: ...
    def sse(
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
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Generator[SSEEvent[Any]]:
        """Open `path` as a Server-Sent Events stream, yielding one `SSEEvent` per event.

        `response_data_type` decodes `.data` (a class, `TypeAdapter`, or msgspec `Decoder`);
        `.event`/`.id` are always populated regardless (`"message"`/`""` when the server omits
        them).

        The client's `timeout` never applies here — an SSE stream is open-ended, and a total
        request timeout would kill every healthy stream on schedule. `timeout` bounds one
        connection attempt only; use the client's `read_timeout` to detect a stalled stream.
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
            skip_auth=skip_auth,
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
        data: Params | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        error_for_status: bool,
        error_type: type[Data] | None,
        interruptible: bool,
    ) -> Generator[Any]:
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        request_builder = self._prepare_request(
            request_builder, params, headers, effective_timeout, skip_auth=skip_auth
        )
        request_builder = _attach_body_sync(
            request_builder,
            json,
            data,
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
                request,
                check_status,
                error_type,
                request_info,
                interruptible=interruptible,
                idle_timeout=idle,
            )
            if response_data_type is None:
                yield from chunks
                return
            pending = bytearray()
            for chunk in chunks:
                for line in _complete_ndjson_lines(pending, chunk):
                    yield _decode_json_line(line.decode(), response_data_type)
            if pending.strip():
                yield _decode_json_line(pending.decode(), response_data_type)
        except (PyreqwestRequestError, PyreqwestBuilderError) as error:
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
    ) -> Generator[bytes]: ...
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
    ) -> Generator[dict[str, Any]]: ...
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
    ) -> Generator[TLine]: ...
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
    ) -> Generator[Any]:
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Generator[bytes]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[dict[str, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Generator[dict[str, Any]]: ...
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
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TLine] | TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Generator[TLine]: ...
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        json: JSONPayload | None = None,
        data: Params | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        error_type: type[Data] | None = None,
        interruptible: bool = False,
    ) -> Generator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`data`/`form`/`content`
        options as `post` (at most one), same raw-bytes-by-default / NDJSON-via-
        `response_data_type` split.

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
            data,
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
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        request_builder = self._prepare_request(
            self._client.get(path), params, headers, effective_timeout, skip_auth=skip_auth
        )
        try:
            # See the async mirror: `limit(1)` so the idle limit sees chunks as they arrive.
            request = request_builder.streamed_read_buffer_limit(1).build_streamed()
            request_info = _request_info(request)
            chunks = _sync_stream_chunks(
                request,
                self._check_status if error_for_status else None,
                error_type,
                request_info,
                interruptible=False,
                idle_timeout=idle,
            )
            if dest is None:
                buffer = bytearray()
                for chunk in chunks:
                    buffer += chunk
                return bytes(buffer)
            with _atomic_download_file(dest) as file:
                for chunk in chunks:
                    file.write(chunk)
        except (PyreqwestRequestError, PyreqwestBuilderError) as error:
            raise _translate_transport_error(error) from error
        return None

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
        dest: str | PathLike[str],
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
        dest: str | PathLike[str] | None = None,
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
        O(body size) memory, but about two-thirds of what `get()` peaks at (measured: 105MB vs
        154MB for a 50MB body), since `get()` goes through pyreqwest's own `.bytes()`. Pass
        `dest` to stream straight to a file instead — memory then stays O(chunk size) regardless
        of how large the body is.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        return self._download(
            path,
            None if dest is None else Path(dest),
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
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
    ) -> Response[None]: ...
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
    ) -> Response[None, THeaders]: ...
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
    ) -> Response[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass
        `response_headers_type` to get the response headers parsed into `.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `error_type`
        decodes a 4xx/5xx body onto `HTTPResponseError.parsed_body` instead of leaving it
        `None`.
        """
        request_builder = self._prepare_request(
            self._client.head(path), params, headers, timeout, skip_auth=skip_auth
        )
        started = time.monotonic()
        raw_response, request_info = _send_sync(request_builder)
        elapsed = time.monotonic() - started
        if error_for_status:
            self._check_status(raw_response, error_type, request_info)
        response_headers = CaseInsensitiveDict(raw_response.headers)
        typed_headers = (
            None
            if response_headers_type is None
            else _parse_typed_headers(response_headers, response_headers_type)
        )
        return Response(
            None,
            raw_response.status,
            response_headers,
            typed_headers,
            request_info,
            raw_response.version,
            elapsed,
        )
