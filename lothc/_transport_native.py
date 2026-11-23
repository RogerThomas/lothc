"""The loop-native transport over `lothc._native` (hyper on the asyncio loop; poll(2) for sync)."""

import asyncio
import secrets
import time
from base64 import b64encode
from collections.abc import (
    AsyncGenerator,
    Callable,
    Generator,
    Iterator,
    Mapping,
)
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass
from dataclasses import field as _dataclass_field
from datetime import timedelta
from enum import IntEnum
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

from ._compat import (
    BaseModel,
    Struct,
    TypeAdapter,
    msgspec,
)
from ._decode import _Decoder, _decoder_for, _decoders, _Into
from ._encode import _encode_headers, _encode_params, _pydantic_by_alias, _query_pairs
from ._errors import (
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    HTTPTransportError,
    RequestInfo,
    _decode_error,
)
from ._form import _form_parts, _FormPart
from ._headers import CaseInsensitiveDict, _LazyHeaders
from ._native import ClientBuildError as _NativeClientBuildError
from ._native import ConnectOptions as _NativeConnectOptions
from ._native import Pool as _NativePool
from ._native import RawHeaders, Stream, SyncStream
from ._native import SyncPool as _NativeSyncPool
from ._native_loop import _LoopConnections
from ._raises import (
    _call_raises,
    _ErrorResponse,
    _RaisesArg,
    _RaisesTable,
    _status_error,
    _validate_raises,
)
from ._response import Response, _parse_typed_headers
from ._retry import (
    _AttemptHook,
    _call_retries,
    _retried,
    _retried_sync,
    _retry_policy,
    _RetryPolicy,
    _RetryRuling,
    _SyncRetryRuling,
    _through_hook,
    _through_hook_sync,
)
from ._settings import _default_user_agent, _RequestHookFactory, _TransportSettings
from ._sse import SSEEvent, _sse_events, _sse_events_sync
from ._streaming import (
    _Abandonment,
    _ChunkReader,
    _download_stream,
    _download_stream_sync,
    _ReplayedBody,
    _ResponseHead,
    _stream_lines,
    _stream_lines_sync,
    _StreamCall,
    _SyncChunkReader,
    _SyncReplayedBody,
)
from ._types import (
    AuthProvider,
    Form,
    Headers,
    JSONPayload,
    Params,
    SyncAuthProvider,
    TypedHeaders,
    _InvalidatableAuth,
    _SyncInvalidatableAuth,
)


def _reject_unported_settings(settings: _TransportSettings) -> None:
    """Refuse, at construction, any setting the native transport doesn't implement yet, rather
    than silently behaving differently from the pyreqwest transport."""
    unported = {
        "http2": settings.http2,
    }
    for name, used in unported.items():
        if used:
            raise NotImplementedError(
                f'{name}= is not supported by the native transport yet (native="on")'
            )


def _native_total_timeout(settings: _TransportSettings) -> timedelta | None:
    """The client's `timeout` as the pyreqwest transport hands its builder it, a `timedelta`, so
    that a bad one fails on entry with the same error as there (as `_native_idle_timeout`). Each
    request carries it, unless the call passes its own, converted the same way
    (`_native_call_timeout`)."""
    if settings.timeout is None:
        return None
    return timedelta(seconds=settings.timeout)


def _native_call_timeout(timeout: float) -> timedelta:
    """A per-call `timeout` as the pyreqwest transport hands its request builder it
    (`_apply_timeout`): a `timedelta`, so it rounds to the microsecond the same way and a NaN, an
    infinite or an out-of-range one raises the same error, here, and a negative one the same
    `ValueError`, from the native side's conversion as from pyreqwest's."""
    return timedelta(seconds=timeout)


def _optional_timedelta(seconds: float | None) -> timedelta | None:
    return None if seconds is None else timedelta(seconds=seconds)


def _native_connect_options(settings: _TransportSettings) -> _NativeConnectOptions:
    """`resolve`, `local_address`, `tcp_keepalive`, `connect_timeout` and `read_timeout` as the
    pyreqwest transport hands its builder them: each address checked as pyreqwest checks it (a
    `ValueError`), each duration a `timedelta`, so a bad one fails on entry with the same error.
    """
    return _NativeConnectOptions(
        list((settings.resolve or {}).items()),
        settings.local_address,
        _optional_timedelta(settings.tcp_keepalive),
        _optional_timedelta(settings.connect_timeout),
        _optional_timedelta(settings.read_timeout),
    )


def _native_idle_timeout(settings: _TransportSettings) -> timedelta | None:
    """`pool_idle_timeout` as the pyreqwest transport hands it over, a `timedelta` (`None` is
    reqwest's 90s), so a bad value fails on entry with the same error as there (pyo3's conversion
    rejects a negative one; `timedelta` itself a NaN or an enormous one)."""
    if settings.pool_idle_timeout is None:
        return None
    return timedelta(seconds=settings.pool_idle_timeout)


def _native_pool_timeout(settings: _TransportSettings) -> timedelta | None:
    """`pool_timeout` as the pyreqwest transport hands it over, a `timedelta`, so a bad value fails
    on entry with the same error as there (as `_native_idle_timeout`). It only bounds a wait for
    one of `max_connections`' slots, so without that it does nothing, as on pyreqwest."""
    if settings.pool_timeout is None:
        return None
    return timedelta(seconds=settings.pool_timeout)


def _native_no_proxy(settings: _TransportSettings) -> str | None:
    """`no_proxy` as the pyreqwest transport hands it over, one comma-separated list."""
    return None if settings.no_proxy is None else ",".join(settings.no_proxy)


def _static_authorization(
    bearer_token: str | None, basic_auth: tuple[str, str | None] | None
) -> str | None:
    """The `Authorization` value a static `bearer_token`/`basic_auth` sends, as pyreqwest writes
    it: basic auth is `user:` plus the password if there is one (a `None` password keeps the colon).

    >>> _static_authorization(None, ("user", None))
    'Basic dXNlcjo='
    """
    if bearer_token is not None:
        return f"Bearer {bearer_token}"
    if basic_auth is not None:
        username, password = basic_auth
        credentials = f"{username}:{'' if password is None else password}".encode()
        return f"Basic {b64encode(credentials).decode()}"
    return None


class _NativeBody(IntEnum):
    """How the native transport's request builder treats `body` (`native/src/request.rs`)."""

    RAW = 0
    ENCODED_JSON = 1
    JSON = 2
    FORM = 3
    MULTIPART = 4


def _multipart_boundary() -> str:
    """A multipart boundary in reqwest's format (`gen_boundary`): four random 64-bit numbers as
    16 lowercase hex digits each, joined by dashes."""
    digits = secrets.token_hex(32)
    return "-".join(digits[start : start + 16] for start in range(0, 64, 16))


@dataclass(slots=True)
class _NativeForm:
    """A `form=` body as the native side builds it (`native/src/multipart.rs`), each time its
    request is prepared: `parts()` describes the parts afresh, and the boundary is drawn once per
    call, so a replayed attempt (only an empty form is ever replayed, as on pyreqwest) carries the
    same one, as a copy of pyreqwest's request does. The native side says whether the pool
    building it is async (`async_client`), since only that one can pull a file part streamed
    from an async iterable."""

    boundary: str
    form: Form
    infer_mime_type_from_file_extension: bool

    def parts(self, *, async_client: bool) -> Iterator[_FormPart]:
        return _form_parts(
            self.form,
            infer_mime_type_from_file_extension=self.infer_mime_type_from_file_extension,
            async_client=async_client,
        )


def _native_body(
    json: JSONPayload | None,
    data: Params | None,
    form: Form | None,
    content: str | bytes | None,
    *,
    infer_mime_type_from_file_extension: bool,
) -> tuple[object, _NativeBody]:
    """`_attach_body` for the native transport: a model's or Struct's JSON encoded here exactly
    as `_attach_json_body` encodes it, a `dict`/`list` left for the native side to serialize as
    pyreqwest's `body_json` does (sorted keys, serde_json's numbers), `data=` as pairs, and
    `form=` described part by part only once the native side builds it, after the request's
    headers, as pyreqwest builds its form after them."""
    provided_bodies = [body for body in (json, data, form, content) if body is not None]
    if len(provided_bodies) > 1:
        raise ValueError("Provide at most one of 'json', 'data', 'form' or 'content'")
    if form is not None:
        native_form = _NativeForm(_multipart_boundary(), form, infer_mime_type_from_file_extension)
        return native_form, _NativeBody.MULTIPART
    if json is not None:
        if isinstance(json, BaseModel):
            return json.model_dump_json(
                by_alias=_pydantic_by_alias(json)
            ).encode(), _NativeBody.ENCODED_JSON
        if isinstance(json, Struct):
            return msgspec.json.encode(json), _NativeBody.ENCODED_JSON
        return json, _NativeBody.JSON
    if data is not None:
        pairs = data if isinstance(data, dict) else _query_pairs(_encode_params(data))
        return pairs, _NativeBody.FORM
    return content, _NativeBody.RAW


type _DecodeFn = Callable[[bytes], Any]


def _decode_callable(decoder: _Decoder | None) -> _DecodeFn | None:
    """What the native side calls on a successful body, as `_decode_body` would decode it."""
    if decoder is None:
        return None
    if isinstance(decoder, TypeAdapter):
        return decoder.validate_json
    return decoder.decode


def _is_cached_decoder(into: object, decoder: _Decoder) -> bool:
    """Whether lothc's own `_decoders` cache holds `decoder` for `into`, so a client may keep its
    decode callable too (anything `_decoders` doesn't hold is rebuilt per call, so it is here)."""
    try:
        return _decoders.get(into) is decoder
    except TypeError:  # an unhashable form
        return False


def _eager_headers(raw_headers: RawHeaders) -> CaseInsensitiveDict:
    """An error's headers, built at once as `_check_status` builds them (off the hot path)."""
    return CaseInsensitiveDict(raw_headers.items())


class _NativeBuilderError(ValueError):
    """The `__cause__` of the `HTTPTransportError` the native transport raises for a request that
    can never be sent (a scheme other than http(s), a redirect to one, a query value it can't
    encode), as pyreqwest's is its `BuilderError`, also a `ValueError`: permanent, so `sse()`
    doesn't reconnect for it (`_NativeStreamSource.permanent`)."""


class _NativeDecodeError(Exception):
    """The `__cause__` of the `HTTPConnectionError` the native transport raises for a body that
    arrived whole but couldn't be decompressed, as pyreqwest's is its `DecodeError`: the one
    connection failure a retry can't fix, so the retry loop leaves it be (`_native_retryable`)."""


def _native_builder_error(message: str) -> HTTPTransportError:
    error = HTTPTransportError(message)
    error.__cause__ = _NativeBuilderError(message)
    return error


def _native_decode_error(message: str) -> HTTPConnectionError:
    error = HTTPConnectionError(message)
    error.__cause__ = _NativeDecodeError(message)
    return error


def _native_decode_failure(
    error: BaseException,
    status: int,
    body: bytes,
    request_info: RequestInfo,
    headers: CaseInsensitiveDict,
) -> BaseException:
    """What the native side raises when decoding a successful response's body (or its headers,
    for `response_headers_type`) raised `error`: `HTTPDecodeError` for a decode failure, its
    headers built at once (off the hot path), and anything else (a `dict` subclass's own
    `TypeError`, a `KeyboardInterrupt`) as it was."""
    if not isinstance(error, (ValueError, RecursionError)):
        return error
    return _decode_error(error, status, body, request_info, CaseInsensitiveDict(headers))


def _native_retryable(error: HTTPTransportError) -> bool:
    """`_is_retryable_send_error` for the native transport's exceptions (`_retried`'s
    `retryable`): a timeout or a failed
    connection (a body cut off partway included), but not a body that couldn't be decompressed,
    and none of the plain `HTTPTransportError`s (a redirect that can't be followed, a request that
    can't be built), which the pyreqwest transport doesn't retry either (`_pyreqwest_retryable`)."""
    if not isinstance(error, (HTTPConnectionError, HTTPTimeoutError)):
        return False
    return not isinstance(error.__cause__, _NativeDecodeError)


@dataclass(slots=True)
class _NativeGlue:
    """lothc's own classes and helpers, which the native pools build every result from, so what
    a caller gets back is exactly what the pyreqwest transport returns or raises."""

    response: Callable[..., Response[Any, Any]]
    request_info: Callable[[str, str, str, str | None], RequestInfo]
    lazy_headers: Callable[[RawHeaders], CaseInsensitiveDict]
    eager_headers: Callable[[RawHeaders], CaseInsensitiveDict]
    status_error: Callable[..., HTTPResponseError[Any]]
    error_response: Callable[..., Response[Any, Any]]
    parse_typed_headers: Callable[..., TypedHeaders]
    connection_error: type[HTTPConnectionError]
    timeout_error: type[HTTPTimeoutError]
    transport_error: type[HTTPTransportError]
    builder_error: Callable[[str], HTTPTransportError]
    decode_error: Callable[[str], HTTPConnectionError]
    decode_failure: Callable[..., BaseException]


def _native_glue() -> _NativeGlue:
    return _NativeGlue(
        Response,
        RequestInfo,
        _LazyHeaders,
        _eager_headers,
        _status_error,
        _ErrorResponse,
        _parse_typed_headers,
        HTTPConnectionError,
        HTTPTimeoutError,
        HTTPTransportError,
        _native_builder_error,
        _native_decode_error,
        _native_decode_failure,
    )


# A `request_hook=`'s headers as the pools take them: a `dict`, or the hook's `enter`, which the
# pool calls only once the request has been built, so one that can't be never enters the hook.
type _HookHeaders = dict[str, str] | Callable[[], dict[str, str]]


def _sent_bearer_token(token: str, *headers: Mapping[str, str] | None) -> str | None:
    """The bearer token a request goes out with (`_bearer_token_sent`): ours, unless `headers=`
    (or, after it, a `request_hook=`'s headers) replaced the `Authorization` header, in which
    case theirs, if it's a bearer token at all."""
    sent = f"Bearer {token}"
    for mapping in headers:
        for name, value in (mapping or {}).items():
            if name.lower() == "authorization":
                sent = value
    return sent.removeprefix("Bearer ") if sent.startswith("Bearer ") else None


def _without_authorization(headers: dict[str, str] | None) -> dict[str, str] | None:
    """`headers` (the call's, or a `request_hook=`'s) minus any `Authorization`, for the
    re-authenticated retry, whose fresh token replaces it (as `_ReauthMiddleware` overwrites the
    header)."""
    if headers is None:
        return None
    return {name: value for name, value in headers.items() if name.lower() != "authorization"}


def _finish_undecoded(
    response: Response[Any, Any],
    decode: _DecodeFn | None,
    error_class: _RaisesTable,
    response_headers_type: type[TypedHeaders] | None,
    elapsed: float,
    ruled: HTTPResponseError[Any] | None,
    *,
    headers_only: bool,
    defer_error: bool,
) -> Response[Any, Any]:
    """The `Response` (or status error, or with `defer_error` the deferred error response) for an
    exchange that went out undecoded so its status could be seen first (a 401 to re-authenticate,
    a status to retry): what the native side would otherwise have built in one pass. `elapsed`
    spans every attempt, as pyreqwest's does. `ruled` is the status error a `retry=` rule was
    shown for `response`, if any: raised or kept as is."""
    if response.status >= 400:
        if defer_error:
            return _ErrorResponse(
                error_class,
                response.status,
                response.headers,
                response.request,
                response.http_version,
                elapsed,
                response.data,
                ruled,
            )
        if ruled is not None:
            raise ruled
        raise _status_error(
            error_class,
            response.status,
            response.data,
            response.request,
            CaseInsensitiveDict(response.headers),
        )
    # As the native side's `outcome()`: a `head`/`options` body is never decoded or carried.
    body = b"" if headers_only else response.data
    data = None if headers_only else body
    if data is not None and decode is not None:
        try:
            data = decode(data)
        except (ValueError, RecursionError) as error:
            raise _decode_error(
                error,
                response.status,
                body,
                response.request,
                CaseInsensitiveDict(response.headers),
            ) from error
    typed_headers = None
    if response_headers_type is not None:
        try:
            typed_headers = _parse_typed_headers(response.headers, response_headers_type)
        except ValueError as error:
            raise _decode_error(
                error,
                response.status,
                body,
                response.request,
                CaseInsensitiveDict(response.headers),
            ) from error
    return Response(
        data,
        response.status,
        response.headers,
        typed_headers,
        response.request,
        response.http_version,
        elapsed,
    )


@dataclass(slots=True)
class _NativeCall:
    """A buffered request as the pool's `request` takes it, kept for each exchange the middleware
    chain makes (`_NativeTransport._send_through_middleware`), as pyreqwest's retries copy it."""

    method: str
    path: str
    query: object
    headers: dict[str, str] | None
    body: object
    body_kind: _NativeBody
    timeout: timedelta | None
    # Whether it may be sent more than once: not with a multipart body that has parts, which
    # pyreqwest can't copy (`_replayable_copy`), so neither a retry nor a 401 replays it.
    replayable: bool = True


def _response_retry_signal(response: Response[Any, Any]) -> tuple[int, str | None]:
    """What the retry loop reads off a buffered response: its status and `Retry-After`."""
    return response.status, response.headers.get("retry-after")


async def _response_status_error(
    error_class: _RaisesTable, response: Response[Any, Any]
) -> HTTPResponseError[Any]:
    """The status error an undecoded exchange's `response` (its body whole, as `.data`) would
    raise, for a `retry=` rule."""
    return _status_error(
        error_class,
        response.status,
        response.data,
        response.request,
        CaseInsensitiveDict(response.headers),
    )


def _response_status_error_sync(
    error_class: _RaisesTable, response: Response[Any, Any]
) -> HTTPResponseError[Any]:
    """Sync mirror of `_response_status_error`."""
    return _status_error(
        error_class,
        response.status,
        response.data,
        response.request,
        CaseInsensitiveDict(response.headers),
    )


def _response_ruling(
    retry: _RetryPolicy, error_class: _RaisesTable, method: str
) -> _RetryRuling[Response[Any, Any]] | None:
    """How `_retried` asks the client's `retry=` rule about a buffered call's attempts, or `None`
    when the client has none."""
    if retry.rule is None:
        return None
    return _RetryRuling(retry.rule, method, partial(_response_status_error, error_class))


def _response_ruling_sync(
    retry: _RetryPolicy, error_class: _RaisesTable, method: str
) -> _SyncRetryRuling[Response[Any, Any]] | None:
    """Sync mirror of `_response_ruling`."""
    if retry.rule is None:
        return None
    return _SyncRetryRuling(retry.rule, method, partial(_response_status_error_sync, error_class))


@dataclass(slots=True)
class _NativeStreamRequest:
    """A native stream's request as the pool takes it: `query`/`headers` encoded (a plain `dict`
    as is, as `send` passes it), the body as `_native_body` makes it, the per-call `timeout` as
    `_native_call_timeout` does."""

    query: object
    headers: dict[str, str] | None
    body: object
    body_kind: _NativeBody
    timeout: timedelta | None


def _native_stream_request(call: _StreamCall) -> _NativeStreamRequest:
    params, headers = call.params, call.headers
    query = (
        params
        if params is None or isinstance(params, dict)
        else _query_pairs(_encode_params(params))
    )
    encoded_headers = (
        headers if headers is None or isinstance(headers, dict) else _encode_headers(headers)
    )
    timeout = None if call.timeout is None else _native_call_timeout(call.timeout)
    if call.json is None and call.data is None and call.form is None and call.content is None:
        return _NativeStreamRequest(query, encoded_headers, None, _NativeBody.RAW, timeout)
    body, body_kind = _native_body(
        call.json,
        call.data,
        call.form,
        call.content,
        infer_mime_type_from_file_extension=call.infer_mime_type_from_file_extension,
    )
    return _NativeStreamRequest(query, encoded_headers, body, body_kind, timeout)


@dataclass(slots=True)
class _NativeStreamAuth:
    """How a native stream authenticates (all `None` for `skip_auth=True`): `bearer_auth`'s token,
    else `static`, the `Authorization` a `bearer_token`/`basic_auth` sends. With `reauth`, a 401
    invalidates the token it went out with and, for an idempotent verb (`retry`), the request goes
    once more with a fresh one (`_ReauthMiddleware`)."""

    bearer_auth: AuthProvider | None
    static: str | None
    reauth: _InvalidatableAuth | None
    retry: bool


@dataclass(slots=True)
class _SyncNativeStreamAuth:
    """Sync mirror of `_NativeStreamAuth`."""

    bearer_auth: SyncAuthProvider | None
    static: str | None
    reauth: _SyncInvalidatableAuth | None
    retry: bool


def _head_retry_signal(opened: tuple[_ResponseHead, RawHeaders]) -> tuple[int, str | None]:
    """What the retry loop reads off a stream's head: its status and `Retry-After`."""
    head, _ = opened
    return head.status, head.headers.get("retry-after")


# Every field is the stream's request, or its state across attempts (auth, retries, a hook), as on
# `_SyncNativeStreamSource`: splitting them into sub-records would only move them further away.
@dataclass(slots=True, eq=False)
class _NativeStreamSource:  # pylint: disable=too-many-instance-attributes
    """`_StreamSource` (`_streaming.py`) over the async native pool: `Pool.stream` prepares the
    request, a `Stream` sends and reads it (following redirects), and the pool raises lothc's own
    exceptions, so there are no `failures` to translate. With the call's `check_status`, an error
    status's body is read here and raised as `_check_status` raises it; with `retries`, the head
    is retried through `_retried`, each attempt on a stream of its own whose 401 re-authenticates
    as `_ReauthMiddleware` does, the same order as the pyreqwest transport's. With a
    `request_hook=`, each attempt makes its own stream with the hook's headers, and the hook's
    context ends once the head has arrived."""

    failures: ClassVar[tuple[type[Exception], ...]] = ()
    _pool: _NativePool
    _method: str
    _path: str
    _call: _StreamCall
    _auth: _NativeStreamAuth
    # The call's `raises=`, else the client's: what an error status is raised as, unless the
    # call's `check_status` is off (`download(raise_for_status=False)`).
    _error_class: _RaisesTable
    _retry: _RetryPolicy
    # How many times opening may be retried (0: sent once).
    _retries: int
    _request_hook: _RequestHookFactory | None
    # The headers `sse()` sets: `accept`, and `last-event-id` once there is one.
    _accept: str | None = None
    _last_event_id: str | None = None
    _request: _NativeStreamRequest | None = _dataclass_field(default=None, init=False)
    _stream: Stream | None = _dataclass_field(default=None, init=False)
    _token: str | None = _dataclass_field(default=None, init=False)
    # The `Authorization` `prepare` settled on, which every attempt goes out with.
    _authorization: str | None = _dataclass_field(default=None, init=False)
    _attempts: int = _dataclass_field(default=0, init=False)
    # With a `request_hook=`, the hook over this stream's attempts, and the current attempt's
    # headers from it; `prepare` then leaves each attempt to make its own stream.
    _hook: _AttemptHook | None = _dataclass_field(default=None, init=False)
    _hook_headers: dict[str, str] | None = _dataclass_field(default=None, init=False)

    def _new_stream(
        self,
        request: _NativeStreamRequest,
        headers: dict[str, str] | None,
        auth: str | None,
        hook_headers: _HookHeaders | None,
    ) -> Stream:
        return self._pool.stream(
            self._method,
            self._path,
            request.query,
            headers,
            auth,
            request.body,
            request.body_kind,
            request.timeout,
            self._accept,
            self._last_event_id,
            hook_headers,
        )

    def _current(self) -> Stream:
        if self._stream is None:
            raise RuntimeError("The stream was opened before it was prepared")
        return self._stream

    def _close(self) -> None:
        if self._stream is not None:
            self._stream.close()

    async def _open_current(self) -> tuple[_ResponseHead, RawHeaders]:
        stream = self._current()
        fut = stream.open()
        try:
            status, raw_headers, http_version = await fut
        except asyncio.CancelledError:
            stream.close()
            raise
        return _ResponseHead(status, _LazyHeaders(raw_headers), http_version), raw_headers

    async def _read_chunk(self) -> bytes | None:
        stream = self._current()
        fut = stream.read()
        try:
            return await fut
        except asyncio.CancelledError:
            stream.close()
            raise

    async def _reauthenticated(
        self, head: _ResponseHead, raw_headers: RawHeaders
    ) -> tuple[_ResponseHead, RawHeaders]:
        reauth, request, token = self._auth.reauth, self._request, self._token
        if reauth is None or request is None or token is None or head.status != 401:
            return head, raw_headers
        sent = _sent_bearer_token(token, request.headers, self._hook_headers)
        if sent is None:
            return head, raw_headers
        reauth.invalidate(sent)
        if not self._auth.retry:
            return head, raw_headers
        self._close()
        headers = _without_authorization(request.headers)
        hook_headers = _without_authorization(self._hook_headers)
        self._stream = self._new_stream(request, headers, f"Bearer {await reauth()}", hook_headers)
        return await self._open_current()

    async def _attempt(self) -> tuple[_ResponseHead, RawHeaders]:
        """One attempt, re-authenticated: on `prepare`'s stream the first time, on a new one with
        the same auth for every retry, as pyreqwest retries a copy of the request as built."""
        request = self._request
        hook = self._hook
        if hook is not None and request is not None:
            self._close()
            self._stream = self._new_stream(
                request, request.headers, self._authorization, hook.enter
            )
            self._hook_headers = hook.headers
        elif self._attempts and request is not None:
            self._close()
            self._stream = self._new_stream(request, request.headers, self._authorization, None)
        self._attempts += 1
        opened = await self._reauthenticated(*await self._open_current())
        if hook is not None:
            hook.responded(opened[0].status)
        return opened

    async def _read_error(self, opened: tuple[_ResponseHead, RawHeaders]) -> HTTPResponseError[Any]:
        """The status error an opened error response raises, its body read whole: raised when
        checking the status, or shown to a `retry=` rule."""
        head, raw_headers = opened
        body = bytearray()
        while (chunk := await self._read_chunk()) is not None:
            body += chunk
        return _status_error(
            self._error_class,
            head.status,
            bytes(body),
            self._current().request_info,
            _eager_headers(raw_headers),
        )

    def _ruling(self) -> _RetryRuling[tuple[_ResponseHead, RawHeaders]] | None:
        rule = self._retry.rule
        if rule is None:
            return None
        return _RetryRuling(rule, self._method, self._read_error)

    async def _open(self, stack: AsyncExitStack) -> tuple[_ResponseHead, _ChunkReader]:
        stack.callback(self._close)
        if self._retries:
            opened, ruled = await _retried(
                self._retry,
                self._retries,
                self._attempt,
                _head_retry_signal,
                _native_retryable,
                self._ruling(),
                self._hook,
            )
            # An error status a `retry=` rule stopped retrying on: its body is already read.
            if ruled is not None:
                if self._call.check_status:
                    raise ruled
                return opened[0], _ReplayedBody(ruled.body).read
        else:
            opened = await self._attempt()
        if self._call.check_status and opened[0].status >= 400:
            raise await self._read_error(opened)
        return opened[0], self._read_chunk

    @property
    def request_info(self) -> RequestInfo:
        # With a hook, before an attempt has made a stream: the request the hook is shown.
        if self._stream is None and self._hook is not None and self._hook.request_info is not None:
            return self._hook.request_info
        return self._current().request_info

    def failure(self, error: Exception, /) -> Exception:
        return error

    def permanent(self, error: HTTPTransportError, /) -> bool:
        # A redirect to a scheme other than http(s), which no reconnect changes.
        return isinstance(error.__cause__, _NativeBuilderError)

    async def prepare(self) -> None:
        auth = self._auth
        if auth.bearer_auth is not None:
            self._token = await auth.bearer_auth()
            self._authorization = f"Bearer {self._token}"
        else:
            self._authorization = auth.static
        request = self._request = _native_stream_request(self._call)
        if self._request_hook is None:
            self._stream = self._new_stream(request, request.headers, self._authorization, None)
        else:
            request_info = self._pool.request_info(self._method, self._path, request.query)
            self._hook = _AttemptHook(self._request_hook, request_info)

    async def open(self, stack: AsyncExitStack) -> tuple[_ResponseHead, _ChunkReader]:
        hook = self._hook
        if hook is None:
            return await self._open(stack)
        # The hook's context ends with the head: what an exception raised by then is, the
        # status error included, goes into it.
        try:
            opened = await self._open(stack)
        except asyncio.CancelledError:
            # Cancelled by the streaming verb's idle limit, say, which raises its own
            # `HTTPTimeoutError` instead: the hook exits with whatever the stack unwinds with.
            stack.push(hook.exit_with)
            raise
        except BaseException as error:
            hook.fail(error)
            raise
        hook.close()
        return opened


# As on `_NativeStreamSource`.
@dataclass(slots=True, eq=False)
class _SyncNativeStreamSource:  # pylint: disable=too-many-instance-attributes
    """Sync mirror of `_NativeStreamSource`, over a `SyncStream`."""

    failures: ClassVar[tuple[type[Exception], ...]] = ()
    _pool: _NativeSyncPool
    _method: str
    _path: str
    _call: _StreamCall
    _auth: _SyncNativeStreamAuth
    # The call's `raises=`, else the client's: what an error status is raised as, unless the
    # call's `check_status` is off (`download(raise_for_status=False)`).
    _error_class: _RaisesTable
    _retry: _RetryPolicy
    # How many times opening may be retried (0: sent once).
    _retries: int
    _request_hook: _RequestHookFactory | None
    # The headers `sse()` sets: `accept`, and `last-event-id` once there is one.
    _accept: str | None = None
    _last_event_id: str | None = None
    _request: _NativeStreamRequest | None = _dataclass_field(default=None, init=False)
    _stream: SyncStream | None = _dataclass_field(default=None, init=False)
    _token: str | None = _dataclass_field(default=None, init=False)
    # The `Authorization` `prepare` settled on, which every attempt goes out with.
    _authorization: str | None = _dataclass_field(default=None, init=False)
    _attempts: int = _dataclass_field(default=0, init=False)
    # With a `request_hook=`, the hook over this stream's attempts, and the current attempt's
    # headers from it; `prepare` then leaves each attempt to make its own stream.
    _hook: _AttemptHook | None = _dataclass_field(default=None, init=False)
    _hook_headers: dict[str, str] | None = _dataclass_field(default=None, init=False)
    # Whether the caller gave up before the head arrived (`abandon`).
    _abandonment: _Abandonment = _dataclass_field(default_factory=_Abandonment, init=False)

    def _new_stream(
        self,
        request: _NativeStreamRequest,
        headers: dict[str, str] | None,
        auth: str | None,
        hook_headers: _HookHeaders | None,
    ) -> SyncStream:
        return self._pool.stream(
            self._method,
            self._path,
            request.query,
            headers,
            auth,
            request.body,
            request.body_kind,
            request.timeout,
            self._accept,
            self._last_event_id,
            hook_headers,
        )

    def _current(self) -> SyncStream:
        if self._stream is None:
            raise RuntimeError("The stream was opened before it was prepared")
        return self._stream

    def _close(self) -> None:
        if self._stream is not None:
            self._stream.close()

    def _open_current(self) -> tuple[_ResponseHead, RawHeaders]:
        status, raw_headers, http_version = self._current().open()
        return _ResponseHead(status, _LazyHeaders(raw_headers), http_version), raw_headers

    def _read_chunk(self) -> bytes | None:
        return self._current().read()

    def _reauthenticated(
        self, head: _ResponseHead, raw_headers: RawHeaders
    ) -> tuple[_ResponseHead, RawHeaders]:
        reauth, request, token = self._auth.reauth, self._request, self._token
        if reauth is None or request is None or token is None or head.status != 401:
            return head, raw_headers
        sent = _sent_bearer_token(token, request.headers, self._hook_headers)
        if sent is None:
            return head, raw_headers
        reauth.invalidate(sent)
        if not self._auth.retry:
            return head, raw_headers
        self._close()
        headers = _without_authorization(request.headers)
        hook_headers = _without_authorization(self._hook_headers)
        self._stream = self._new_stream(request, headers, f"Bearer {reauth()}", hook_headers)
        return self._open_current()

    def _attempt(self) -> tuple[_ResponseHead, RawHeaders]:
        """One attempt, re-authenticated: on `prepare`'s stream the first time, on a new one with
        the same auth for every retry, as pyreqwest retries a copy of the request as built."""
        request = self._request
        hook = self._hook
        if hook is not None and request is not None:
            self._close()
            self._stream = self._new_stream(
                request, request.headers, self._authorization, hook.enter
            )
            self._hook_headers = hook.headers
        elif self._attempts and request is not None:
            self._close()
            self._stream = self._new_stream(request, request.headers, self._authorization, None)
        self._attempts += 1
        opened = self._reauthenticated(*self._open_current())
        if hook is not None:
            hook.responded(opened[0].status)
        return opened

    def _read_error(self, opened: tuple[_ResponseHead, RawHeaders]) -> HTTPResponseError[Any]:
        """Sync mirror of `_NativeStreamSource._read_error`."""
        head, raw_headers = opened
        body = bytearray()
        while (chunk := self._read_chunk()) is not None:
            body += chunk
        return _status_error(
            self._error_class,
            head.status,
            bytes(body),
            self._current().request_info,
            _eager_headers(raw_headers),
        )

    def _ruling(self) -> _SyncRetryRuling[tuple[_ResponseHead, RawHeaders]] | None:
        rule = self._retry.rule
        if rule is None:
            return None
        return _SyncRetryRuling(rule, self._method, self._read_error)

    def _open(self, stack: ExitStack) -> tuple[_ResponseHead, _SyncChunkReader]:
        stack.callback(self._close)
        if self._retries:
            opened, ruled = _retried_sync(
                self._retry,
                self._retries,
                partial(self._abandonment.attempt, self._attempt),
                _head_retry_signal,
                _native_retryable,
                self._ruling(),
                self._hook,
            )
            # An error status a `retry=` rule stopped retrying on: its body is already read.
            if ruled is not None:
                if self._call.check_status:
                    raise ruled
                return opened[0], _SyncReplayedBody(ruled.body).read
        else:
            opened = self._attempt()
        if self._call.check_status and opened[0].status >= 400:
            raise self._read_error(opened)
        return opened[0], self._read_chunk

    @property
    def request_info(self) -> RequestInfo:
        # With a hook, before an attempt has made a stream: the request the hook is shown.
        if self._stream is None and self._hook is not None and self._hook.request_info is not None:
            return self._hook.request_info
        return self._current().request_info

    def failure(self, error: Exception, /) -> Exception:
        return error

    def permanent(self, error: HTTPTransportError, /) -> bool:
        # A redirect to a scheme other than http(s), which no reconnect changes.
        return isinstance(error.__cause__, _NativeBuilderError)

    def prepare(self) -> None:
        auth = self._auth
        if auth.bearer_auth is not None:
            self._token = auth.bearer_auth()
            self._authorization = f"Bearer {self._token}"
        else:
            self._authorization = auth.static
        request = self._request = _native_stream_request(self._call)
        if self._request_hook is None:
            self._stream = self._new_stream(request, request.headers, self._authorization, None)
        else:
            request_info = self._pool.request_info(self._method, self._path, request.query)
            self._hook = _AttemptHook(self._request_hook, request_info)

    def abandon(self, error: BaseException, /) -> None:
        self._abandonment.error = error

    def open(self, stack: ExitStack) -> tuple[_ResponseHead, _SyncChunkReader]:
        hook = self._hook
        if hook is None:
            return self._open(stack)
        # The hook's context ends with the head: what an exception raised by then is, the
        # status error included, goes into it.
        try:
            opened = self._open(stack)
        except BaseException as error:
            abandoned = self._abandonment.error
            hook.fail(error if abandoned is None else abandoned)
            raise
        # Given up on by a caller that has already raised (`abandon`): the hook exits with its
        # own instance of that, however late the head came.
        abandoned = self._abandonment.error
        if abandoned is None:
            hook.close()
        else:
            hook.fail(abandoned)
        return opened


# Every field is a constructor setting or something derived from one once, as on
# `_SyncNativeTransport`, plus the loop's `_connections`, which the sync transport has no need of.
@dataclass(slots=True, eq=False, repr=False)
class _NativeTransport:  # pylint: disable=too-many-instance-attributes
    """`HTTPClient`'s native transport, selected by `native=` (`_uses_native_transport`) when a
    client is constructed: lothc's own HTTP/1.1 client in `lothc._native` (hyper on the asyncio
    loop's thread, no tokio, see `native/src/pool.rs`), behind the same seam as
    `_PyreqwestTransport`.

    Requests are built the way the pyreqwest transport builds them, byte for byte, and results
    come back as lothc's own `Response`/exceptions, built by the native side in one pass from
    `_NativeGlue`. The streaming verbs and `download` share the pyreqwest transport's
    implementation (`_streaming.py`, `_sse.py`), fed by `_NativeStreamSource`. Redirects are
    followed natively, inside each exchange, as reqwest follows them inside `send()`; retries and
    re-authentication run here around the exchanges, as pyreqwest's middleware runs around
    `send()`. Anything not ported yet is a `NotImplementedError` naming it, never a silent
    difference: unported client settings at construction (`_reject_unported_settings`). See
    `_SyncNativeTransport` for the sync mirror.

    `repr=False`: a generated repr would print the bearer token or basic-auth password.
    """

    _settings: _TransportSettings
    _bearer_token: str | None
    _bearer_auth: AuthProvider | None
    _basic_auth: tuple[str, str | None] | None
    _raises: _RaisesTable
    # The client's `raise_for_status`, for a call that doesn't pass its own.
    _raise_for_status: bool
    # The verbs a re-authenticated 401 may be retried for: the idempotent ones.
    _idempotent_methods: frozenset[str]
    # The `Authorization` a static `bearer_token`/`basic_auth` sends, built once.
    _static_auth: str | None = _dataclass_field(init=False)
    # `bearer_auth`, when it can `invalidate()` a rejected token (`_ReauthMiddleware`'s case).
    _reauth: _InvalidatableAuth | None = _dataclass_field(init=False)
    # The client's retry policy, which `_call_retries` reads each call's retries from.
    _retry: _RetryPolicy = _dataclass_field(init=False)
    # The client's `request_hook=`, entered per attempt (`_AttemptHook`); a call with one goes
    # through `_send_through_middleware`, inside `_through_hook`.
    _request_hook: _RequestHookFactory | None = _dataclass_field(init=False)
    # The longest gap `stream_get`/`stream_post`/`download` tolerate between chunks: the client's
    # `timeout`, unless `read_timeout` was set, which the native side then enforces at the socket
    # (see `_PyreqwestTransport`). Not applied to `sse`.
    _stream_idle_timeout: float | None = _dataclass_field(init=False)
    # The client's `timeout`, converted on entry (`_native_total_timeout`): each request's, unless
    # the call passes its own.
    _total_timeout: timedelta | None = _dataclass_field(default=None, init=False)
    # `into` -> the callable the native side decodes a body with, for the forms `_decoders`
    # caches; anything else is resolved per call, as `_decoder_for` rebuilds it per call.
    _decode_cache: dict[object, _DecodeFn | None] = _dataclass_field(init=False)
    _native_pool: _NativePool | None = _dataclass_field(default=None, init=False)
    _connections: _LoopConnections | None = _dataclass_field(default=None, init=False)

    def __post_init__(self) -> None:
        _reject_unported_settings(self._settings)
        self._static_auth = _static_authorization(self._bearer_token, self._basic_auth)
        self._reauth = (
            self._bearer_auth if isinstance(self._bearer_auth, _InvalidatableAuth) else None
        )
        self._retry = _retry_policy(self._settings)
        self._request_hook = self._settings.request_hook
        self._stream_idle_timeout = (
            self._settings.timeout if self._settings.read_timeout is None else None
        )
        self._decode_cache = {None: None, bytes: None}

    def _closed_error(self) -> RuntimeError:
        return RuntimeError(
            "The client is closed (or was never opened): use it as "
            "`async with HTTPClient(...) as client:`."
        )

    def _resolve_decode(self, into: _Into) -> _DecodeFn | None:
        decoder = _decoder_for(into)
        decode = _decode_callable(decoder)
        if decoder is None or _is_cached_decoder(into, decoder):
            self._decode_cache[into] = decode
        return decode

    async def _exchange(
        self,
        pool: _NativePool,
        method: str,
        path: str,
        query: object,
        headers: dict[str, str] | None,
        auth: str | None,
        body: object,
        body_kind: _NativeBody,
        timeout: timedelta | None,
        decode: _DecodeFn | None,
        error_class: _RaisesTable | None,
        response_headers_type: type[TypedHeaders] | None,
        hook_headers: _HookHeaders | None,
        *,
        headers_only: bool,
        defer_error: bool,
    ) -> Response[Any, Any]:
        fut = pool.request(
            method,
            path,
            query,
            headers,
            auth,
            body,
            body_kind,
            decode,
            timeout,
            error_class,
            response_headers_type,
            headers_only,
            defer_error,
            hook_headers,
        )
        try:
            return await fut
        except asyncio.CancelledError:
            pool.cancel(fut)
            raise

    async def _reauthenticated(
        self,
        pool: _NativePool,
        call: _NativeCall,
        reauth: _InvalidatableAuth,
        token: str,
        response: Response[Any, Any],
        hook_headers: dict[str, str] | None,
    ) -> Response[Any, Any]:
        """`_ReauthMiddleware` on a 401: the rejected token (`token`, unless the call's or the
        hook's headers replaced it) is invalidated, and an idempotent request goes out once more
        with a fresh one."""
        sent = _sent_bearer_token(token, call.headers, hook_headers)
        if sent is None:
            return response
        reauth.invalidate(sent)
        if call.method not in self._idempotent_methods or not call.replayable:
            return response
        return await self._exchange(
            pool,
            call.method,
            call.path,
            call.query,
            _without_authorization(call.headers),
            f"Bearer {await reauth()}",
            call.body,
            call.body_kind,
            call.timeout,
            None,
            None,
            None,
            _without_authorization(hook_headers),
            headers_only=False,
            defer_error=False,
        )

    async def _attempt(
        self,
        pool: _NativePool,
        call: _NativeCall,
        auth: str | None,
        reauth: _InvalidatableAuth | None,
        token: str | None,
        hook: _AttemptHook | None,
    ) -> Response[Any, Any]:
        """One pass through `_ReauthMiddleware` (`_reauthenticated`), with the hook's headers for
        this attempt, if any. Each exchange comes back undecoded and unraised, so its status is
        seen first."""
        response = await self._exchange(
            pool,
            call.method,
            call.path,
            call.query,
            call.headers,
            auth,
            call.body,
            call.body_kind,
            call.timeout,
            None,
            None,
            None,
            None if hook is None else hook.enter,
            headers_only=False,
            defer_error=False,
        )
        if reauth is not None and token is not None and response.status == 401:
            hook_headers = None if hook is None else hook.headers
            response = await self._reauthenticated(
                pool, call, reauth, token, response, hook_headers
            )
        if hook is not None:
            hook.responded(response.status)
        return response

    async def _send_through_middleware(
        self,
        pool: _NativePool,
        call: _NativeCall,
        decode: _DecodeFn | None,
        error_class: _RaisesTable,
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        headers_only: bool,
        defer_error: bool,
        retries: int,
        hook: _AttemptHook | None,
    ) -> Response[Any, Any]:
        """The pyreqwest transport's order: `_retried` (up to `retries` times) around
        `_ReauthMiddleware` (for a `bearer_auth` that can invalidate), each `_attempt`. The auth is
        fetched once, before either, as pyreqwest applies it to the request each attempt copies,
        so `.elapsed` starts after it. With a `hook` (the client's `request_hook=`, the call made
        through `_through_hook`), it's entered for each attempt."""
        reauth = None if skip_auth else self._reauth
        token = None
        if skip_auth:
            auth = None
        elif self._bearer_auth is not None:
            token = await self._bearer_auth()
            auth = f"Bearer {token}"
        else:
            auth = self._static_auth
        if hook is not None:
            # After the auth, as without a hook: what the request would raise first, this does.
            hook.request_info = pool.request_info(call.method, call.path, call.query)
        started = time.monotonic()
        attempt = partial(self._attempt, pool, call, auth, reauth, token, hook)
        if retries and call.replayable:
            response, ruled = await _retried(
                self._retry,
                retries,
                attempt,
                _response_retry_signal,
                _native_retryable,
                _response_ruling(self._retry, error_class, call.method),
                hook,
            )
        else:
            response = await attempt()
            ruled = None
        return _finish_undecoded(
            response,
            decode,
            error_class,
            response_headers_type,
            time.monotonic() - started,
            ruled,
            headers_only=headers_only,
            defer_error=defer_error,
        )

    def _stream_source(
        self,
        pool: _NativePool,
        method: str,
        path: str,
        call: _StreamCall,
        accept: str | None = None,
        last_event_id: str | None = None,
    ) -> _NativeStreamSource:
        error_class = _call_raises(call.raises, self._raises)
        # A multipart body with parts is sent once (`_NativeCall.replayable`).
        replayable = not call.form
        if call.skip_auth:
            auth = _NativeStreamAuth(None, None, None, retry=False)
        else:
            retry = method in self._idempotent_methods and replayable
            auth = _NativeStreamAuth(self._bearer_auth, self._static_auth, self._reauth, retry)
        # Refused before sending even when the body can't be replayed.
        retries = _call_retries(self._retry, method, call.max_retries)
        return _NativeStreamSource(
            pool,
            method,
            path,
            call,
            auth,
            error_class,
            self._retry,
            retries if replayable else 0,
            self._request_hook,
            accept,
            last_event_id,
        )

    def _sse_source(self, path: str, call: _StreamCall, last_event_id: str) -> _NativeStreamSource:
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        return self._stream_source(
            pool, "GET", path, call, "text/event-stream", last_event_id or None
        )

    async def open(self) -> None:
        if self._native_pool is not None:
            raise RuntimeError("This HTTPClient is already open; enter it once at a time.")
        settings = self._settings
        event_loop = asyncio.get_running_loop()
        total_timeout = _native_total_timeout(settings)
        connect = _native_connect_options(settings)
        connections = _LoopConnections(event_loop, connect)
        # A plain `ValueError` from the native one, as `_client_build_error` makes pyreqwest's.
        try:
            pool = _NativePool(
                settings.base_url or None,
                None
                if settings.default_headers is None
                else _encode_headers(settings.default_headers),
                settings.user_agent if settings.user_agent is not None else _default_user_agent(),
                settings.https_only,
                settings.cookie_store,
                settings.follow_redirects,
                settings.max_redirects,
                list(settings.root_certificates or ()),
                settings.identity_pem,
                settings.min_tls_version,
                settings.max_tls_version,
                settings.danger_accept_invalid_certs,
                total_timeout,
                _native_idle_timeout(settings),
                settings.pool_max_idle_per_host,
                settings.max_connections,
                _native_pool_timeout(settings),
                settings.proxy,
                _native_no_proxy(settings),
                _native_glue(),
                connect,
                event_loop,
                connections.spawn,
                connections.close,
            )
        except _NativeClientBuildError as error:
            raise ValueError(str(error)) from error
        pool.set_callbacks(pool.on_deadline, pool.on_writable, pool.on_readable, pool.wake)
        connections.attach(pool)
        self._native_pool = pool
        self._connections = connections
        self._total_timeout = total_timeout

    async def close(self) -> None:
        pool, connections = self._native_pool, self._connections
        self._native_pool = None
        self._connections = None
        if pool is not None:
            pool.close()
        if connections is not None:
            connections.stop()

    # Every local but a few is a verb argument; the hot path stays inline (see the docstring).
    async def send(  # pylint: disable=too-many-locals
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
        into: _Into,
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        raise_for_status: bool | None,
        raises: _RaisesArg | None,
        max_retries: int | None,
    ) -> Response[Any, Any]:
        """Every body verb's shared path, as `_PyreqwestTransport.send`. The hot path awaits the
        pool's Future right here (no further coroutine), and a plain-`dict` `params`/`headers`
        goes to the native side as is, skipping lothc's Python encoders."""
        error_class = _call_raises(raises, self._raises)
        retries = _call_retries(self._retry, method, max_retries)
        try:
            decode = self._decode_cache[into]
        except (KeyError, TypeError):  # not cached yet, or an unhashable form
            decode = self._resolve_decode(into)
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        query = (
            params
            if params is None or isinstance(params, dict)
            else _query_pairs(_encode_params(params))
        )
        encoded_headers = (
            headers if headers is None or isinstance(headers, dict) else _encode_headers(headers)
        )
        if json is None and data is None and form is None and content is None:
            body, body_kind = None, _NativeBody.RAW
        else:
            body, body_kind = _native_body(
                json,
                data,
                form,
                content,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
        effective_timeout = (
            self._total_timeout if timeout is None else _native_call_timeout(timeout)
        )
        defer_error = not (self._raise_for_status if raise_for_status is None else raise_for_status)
        if (
            (self._reauth is not None and not skip_auth)
            or retries
            or self._request_hook is not None
        ):
            call = _NativeCall(
                method, path, query, encoded_headers, body, body_kind, effective_timeout, not form
            )
            if self._request_hook is None:
                return await self._send_through_middleware(
                    pool,
                    call,
                    decode,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=False,
                    defer_error=defer_error,
                    retries=retries,
                    hook=None,
                )
            hook = _AttemptHook(self._request_hook)
            return await _through_hook(
                hook,
                partial(
                    self._send_through_middleware,
                    pool,
                    call,
                    decode,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=False,
                    defer_error=defer_error,
                    retries=retries,
                    hook=hook,
                ),
            )
        if skip_auth:
            auth = None
        elif self._bearer_auth is not None:
            auth = f"Bearer {await self._bearer_auth()}"
        else:
            auth = self._static_auth
        fut = pool.request(
            method,
            path,
            query,
            encoded_headers,
            auth,
            body,
            body_kind,
            decode,
            effective_timeout,
            error_class,
            response_headers_type,
            False,  # noqa: FBT003 — the native side's positional `headers_only`
            defer_error,
        )
        try:
            return await fut
        except asyncio.CancelledError:
            pool.cancel(fut)
            raise

    async def send_headers_only(
        self,
        method: str,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        raise_for_status: bool | None,
        raises: _RaisesArg | None,
        max_retries: int | None,
    ) -> Response[None, Any]:
        """`head`/`options`: status and headers only, the body is never decoded."""
        error_class = _call_raises(raises, self._raises)
        retries = _call_retries(self._retry, method, max_retries)
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        query = (
            params
            if params is None or isinstance(params, dict)
            else _query_pairs(_encode_params(params))
        )
        encoded_headers = (
            headers if headers is None or isinstance(headers, dict) else _encode_headers(headers)
        )
        effective_timeout = (
            self._total_timeout if timeout is None else _native_call_timeout(timeout)
        )
        defer_error = not (self._raise_for_status if raise_for_status is None else raise_for_status)
        if (
            (self._reauth is not None and not skip_auth)
            or retries
            or self._request_hook is not None
        ):
            call = _NativeCall(
                method, path, query, encoded_headers, None, _NativeBody.RAW, effective_timeout
            )
            if self._request_hook is None:
                return await self._send_through_middleware(
                    pool,
                    call,
                    None,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=True,
                    defer_error=defer_error,
                    retries=retries,
                    hook=None,
                )
            hook = _AttemptHook(self._request_hook)
            return await _through_hook(
                hook,
                partial(
                    self._send_through_middleware,
                    pool,
                    call,
                    None,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=True,
                    defer_error=defer_error,
                    retries=retries,
                    hook=hook,
                ),
            )
        if skip_auth:
            auth = None
        elif self._bearer_auth is not None:
            auth = f"Bearer {await self._bearer_auth()}"
        else:
            auth = self._static_auth
        return await self._exchange(
            pool,
            method,
            path,
            query,
            encoded_headers,
            auth,
            None,
            _NativeBody.RAW,
            effective_timeout,
            None,
            error_class,
            response_headers_type,
            None,
            headers_only=True,
            defer_error=defer_error,
        )

    def sse(
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        into: _Into,
        *,
        skip_auth: bool,
        raises: _RaisesArg | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
    ) -> AsyncGenerator[SSEEvent[Any]]:
        """`_PyreqwestTransport.sse`. `timeout` is each connection's total cap, if given."""
        call = _StreamCall(
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            check_status=True,
            raises=raises,
        )
        return _sse_events(
            partial(self._sse_source, path, call),
            into,
            raises,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
            reconnect_on_close=reconnect_on_close,
        )

    def stream_lines(
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
        into: _Into,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        raises: _RaisesArg | None,
        max_retries: int | None,
    ) -> AsyncGenerator[Any]:
        """`_PyreqwestTransport.stream_lines`: not a generator itself, so a closed client raises
        at the call."""
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        idle = self._stream_idle_timeout if timeout is None else None
        call = _StreamCall(
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            check_status=True,
            raises=raises,
            max_retries=max_retries,
        )
        return _stream_lines(self._stream_source(pool, method, path, call), into, raises, idle)

    async def download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
        raise_for_status: bool | None,
        raises: _RaisesArg | None,
        max_retries: int | None,
    ) -> Response[bytes] | Response[Path]:
        _validate_raises(raises)
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        idle = self._stream_idle_timeout if timeout is None else None
        check_status = self._raise_for_status if raise_for_status is None else raise_for_status
        call = _StreamCall(
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            check_status=check_status,
            raises=raises,
            max_retries=max_retries,
        )
        source = self._stream_source(pool, "GET", path, call)
        # Unchecked, a 4xx/5xx comes back as the error response `_download_stream` builds.
        deferred = None if check_status else _call_raises(raises, self._raises)
        return await _download_stream(source, dest, idle, deferred)


@dataclass(slots=True, eq=False, repr=False)
class _SyncNativeTransport:
    """`SyncHTTPClient`'s native transport, selected by `native=` (`_uses_native_transport`) when
    a client is constructed: lothc's own HTTP/1.1 client in `lothc._native` (hyper driven on the
    calling thread, waiting in `poll(2)` with the GIL released, see `native/src/sync_pool.rs`),
    behind the same seam as `_SyncPyreqwestTransport`. Threads share one client's connections.

    Requests are built the way the pyreqwest transport builds them, byte for byte, and results
    come back as lothc's own `Response`/exceptions, built by the native side in one pass from
    `_NativeGlue`. The streaming verbs and `download` share the pyreqwest transport's
    implementation (`_streaming.py`, `_sse.py`), fed by `_SyncNativeStreamSource`. Redirects,
    retries and re-authentication as `_NativeTransport`. Anything not ported yet is a
    `NotImplementedError` naming it, never a silent difference: unported client settings at
    construction (`_reject_unported_settings`). See `_NativeTransport` for the async mirror.

    `repr=False`: a generated repr would print the bearer token or basic-auth password.
    """

    _settings: _TransportSettings
    _bearer_token: str | None
    _bearer_auth: SyncAuthProvider | None
    _basic_auth: tuple[str, str | None] | None
    _raises: _RaisesTable
    # The client's `raise_for_status`, for a call that doesn't pass its own.
    _raise_for_status: bool
    # The verbs a re-authenticated 401 may be retried for: the idempotent ones.
    _idempotent_methods: frozenset[str]
    # The `Authorization` a static `bearer_token`/`basic_auth` sends, built once.
    _static_auth: str | None = _dataclass_field(init=False)
    # `bearer_auth`, when it can `invalidate()` a rejected token (`_ReauthMiddleware`'s case).
    _reauth: _SyncInvalidatableAuth | None = _dataclass_field(init=False)
    # The client's retry policy, which `_call_retries` reads each call's retries from.
    _retry: _RetryPolicy = _dataclass_field(init=False)
    # The client's `request_hook=`, entered per attempt (`_AttemptHook`); a call with one goes
    # through `_send_through_middleware`, inside `_through_hook`.
    _request_hook: _RequestHookFactory | None = _dataclass_field(init=False)
    # The longest gap `stream_get`/`stream_post`/`download` tolerate between chunks: the client's
    # `timeout`, unless `read_timeout` was set, which the native side then enforces at the socket
    # (see `_PyreqwestTransport`). Not applied to `sse`.
    _stream_idle_timeout: float | None = _dataclass_field(init=False)
    # The client's `timeout`, converted on entry (`_native_total_timeout`): each request's, unless
    # the call passes its own.
    _total_timeout: timedelta | None = _dataclass_field(default=None, init=False)
    # `into` -> the callable the native side decodes a body with, for the forms `_decoders`
    # caches; anything else is resolved per call, as `_decoder_for` rebuilds it per call.
    _decode_cache: dict[object, _DecodeFn | None] = _dataclass_field(init=False)
    _native_pool: _NativeSyncPool | None = _dataclass_field(default=None, init=False)

    def __post_init__(self) -> None:
        _reject_unported_settings(self._settings)
        self._static_auth = _static_authorization(self._bearer_token, self._basic_auth)
        self._reauth = (
            self._bearer_auth if isinstance(self._bearer_auth, _SyncInvalidatableAuth) else None
        )
        self._retry = _retry_policy(self._settings)
        self._request_hook = self._settings.request_hook
        self._stream_idle_timeout = (
            self._settings.timeout if self._settings.read_timeout is None else None
        )
        self._decode_cache = {None: None, bytes: None}

    def _closed_error(self) -> RuntimeError:
        return RuntimeError(
            "The client is closed (or was never opened): use it as "
            "`with SyncHTTPClient(...) as client:`."
        )

    def _resolve_decode(self, into: _Into) -> _DecodeFn | None:
        decoder = _decoder_for(into)
        decode = _decode_callable(decoder)
        if decoder is None or _is_cached_decoder(into, decoder):
            self._decode_cache[into] = decode
        return decode

    def _exchange(
        self,
        pool: _NativeSyncPool,
        method: str,
        path: str,
        query: object,
        headers: dict[str, str] | None,
        auth: str | None,
        body: object,
        body_kind: _NativeBody,
        timeout: timedelta | None,
        decode: _DecodeFn | None,
        error_class: _RaisesTable | None,
        response_headers_type: type[TypedHeaders] | None,
        hook_headers: _HookHeaders | None,
        *,
        headers_only: bool,
        defer_error: bool,
    ) -> Response[Any, Any]:
        return pool.request(
            method,
            path,
            query,
            headers,
            auth,
            body,
            body_kind,
            decode,
            timeout,
            error_class,
            response_headers_type,
            headers_only,
            defer_error,
            hook_headers,
        )

    def _reauthenticated(
        self,
        pool: _NativeSyncPool,
        call: _NativeCall,
        reauth: _SyncInvalidatableAuth,
        token: str,
        response: Response[Any, Any],
        hook_headers: dict[str, str] | None,
    ) -> Response[Any, Any]:
        """`_ReauthMiddleware` on a 401: the rejected token (`token`, unless the call's or the
        hook's headers replaced it) is invalidated, and an idempotent request goes out once more
        with a fresh one."""
        sent = _sent_bearer_token(token, call.headers, hook_headers)
        if sent is None:
            return response
        reauth.invalidate(sent)
        if call.method not in self._idempotent_methods or not call.replayable:
            return response
        return self._exchange(
            pool,
            call.method,
            call.path,
            call.query,
            _without_authorization(call.headers),
            f"Bearer {reauth()}",
            call.body,
            call.body_kind,
            call.timeout,
            None,
            None,
            None,
            _without_authorization(hook_headers),
            headers_only=False,
            defer_error=False,
        )

    def _attempt(
        self,
        pool: _NativeSyncPool,
        call: _NativeCall,
        auth: str | None,
        reauth: _SyncInvalidatableAuth | None,
        token: str | None,
        hook: _AttemptHook | None,
    ) -> Response[Any, Any]:
        """One pass through `_ReauthMiddleware` (`_reauthenticated`), with the hook's headers for
        this attempt, if any. Each exchange comes back undecoded and unraised, so its status is
        seen first."""
        response = self._exchange(
            pool,
            call.method,
            call.path,
            call.query,
            call.headers,
            auth,
            call.body,
            call.body_kind,
            call.timeout,
            None,
            None,
            None,
            None if hook is None else hook.enter,
            headers_only=False,
            defer_error=False,
        )
        if reauth is not None and token is not None and response.status == 401:
            hook_headers = None if hook is None else hook.headers
            response = self._reauthenticated(pool, call, reauth, token, response, hook_headers)
        if hook is not None:
            hook.responded(response.status)
        return response

    def _send_through_middleware(
        self,
        pool: _NativeSyncPool,
        call: _NativeCall,
        decode: _DecodeFn | None,
        error_class: _RaisesTable,
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        headers_only: bool,
        defer_error: bool,
        retries: int,
        hook: _AttemptHook | None,
    ) -> Response[Any, Any]:
        """Sync mirror of `_NativeTransport._send_through_middleware`."""
        reauth = None if skip_auth else self._reauth
        token = None
        if skip_auth:
            auth = None
        elif self._bearer_auth is not None:
            token = self._bearer_auth()
            auth = f"Bearer {token}"
        else:
            auth = self._static_auth
        if hook is not None:
            # After the auth, as without a hook: what the request would raise first, this does.
            hook.request_info = pool.request_info(call.method, call.path, call.query)
        started = time.monotonic()
        attempt = partial(self._attempt, pool, call, auth, reauth, token, hook)
        if retries and call.replayable:
            response, ruled = _retried_sync(
                self._retry,
                retries,
                attempt,
                _response_retry_signal,
                _native_retryable,
                _response_ruling_sync(self._retry, error_class, call.method),
                hook,
            )
        else:
            response = attempt()
            ruled = None
        return _finish_undecoded(
            response,
            decode,
            error_class,
            response_headers_type,
            time.monotonic() - started,
            ruled,
            headers_only=headers_only,
            defer_error=defer_error,
        )

    def _stream_source(
        self,
        pool: _NativeSyncPool,
        method: str,
        path: str,
        call: _StreamCall,
        accept: str | None = None,
        last_event_id: str | None = None,
    ) -> _SyncNativeStreamSource:
        error_class = _call_raises(call.raises, self._raises)
        # A multipart body with parts is sent once (`_NativeCall.replayable`).
        replayable = not call.form
        if call.skip_auth:
            auth = _SyncNativeStreamAuth(None, None, None, retry=False)
        else:
            retry = method in self._idempotent_methods and replayable
            auth = _SyncNativeStreamAuth(self._bearer_auth, self._static_auth, self._reauth, retry)
        # Refused before sending even when the body can't be replayed.
        retries = _call_retries(self._retry, method, call.max_retries)
        return _SyncNativeStreamSource(
            pool,
            method,
            path,
            call,
            auth,
            error_class,
            self._retry,
            retries if replayable else 0,
            self._request_hook,
            accept,
            last_event_id,
        )

    def _sse_source(
        self, path: str, call: _StreamCall, last_event_id: str
    ) -> _SyncNativeStreamSource:
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        return self._stream_source(
            pool, "GET", path, call, "text/event-stream", last_event_id or None
        )

    def open(self) -> None:
        if self._native_pool is not None:
            raise RuntimeError("This SyncHTTPClient is already open; enter it once at a time.")
        settings = self._settings
        total_timeout = _native_total_timeout(settings)
        # A plain `ValueError` from the native one, as `_client_build_error` makes pyreqwest's.
        try:
            self._native_pool = _NativeSyncPool(
                settings.base_url or None,
                None
                if settings.default_headers is None
                else _encode_headers(settings.default_headers),
                settings.user_agent if settings.user_agent is not None else _default_user_agent(),
                settings.https_only,
                settings.cookie_store,
                settings.follow_redirects,
                settings.max_redirects,
                list(settings.root_certificates or ()),
                settings.identity_pem,
                settings.min_tls_version,
                settings.max_tls_version,
                settings.danger_accept_invalid_certs,
                total_timeout,
                _native_idle_timeout(settings),
                settings.pool_max_idle_per_host,
                settings.max_connections,
                _native_pool_timeout(settings),
                settings.proxy,
                _native_no_proxy(settings),
                _native_glue(),
                _native_connect_options(settings),
            )
        except _NativeClientBuildError as error:
            raise ValueError(str(error)) from error
        self._total_timeout = total_timeout

    def close(self) -> None:
        pool = self._native_pool
        self._native_pool = None
        if pool is not None:
            pool.close()

    def send(  # pylint: disable=too-many-locals
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
        into: _Into,
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        raise_for_status: bool | None,
        raises: _RaisesArg | None,
        max_retries: int | None,
    ) -> Response[Any, Any]:
        """Every body verb's shared path, as `_SyncPyreqwestTransport.send`. A plain-`dict`
        `params`/`headers` goes to the native side as is, skipping lothc's Python encoders."""
        error_class = _call_raises(raises, self._raises)
        retries = _call_retries(self._retry, method, max_retries)
        try:
            decode = self._decode_cache[into]
        except (KeyError, TypeError):  # not cached yet, or an unhashable form
            decode = self._resolve_decode(into)
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        query = (
            params
            if params is None or isinstance(params, dict)
            else _query_pairs(_encode_params(params))
        )
        encoded_headers = (
            headers if headers is None or isinstance(headers, dict) else _encode_headers(headers)
        )
        if json is None and data is None and form is None and content is None:
            body, body_kind = None, _NativeBody.RAW
        else:
            body, body_kind = _native_body(
                json,
                data,
                form,
                content,
                infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            )
        effective_timeout = (
            self._total_timeout if timeout is None else _native_call_timeout(timeout)
        )
        defer_error = not (self._raise_for_status if raise_for_status is None else raise_for_status)
        if (
            (self._reauth is not None and not skip_auth)
            or retries
            or self._request_hook is not None
        ):
            call = _NativeCall(
                method, path, query, encoded_headers, body, body_kind, effective_timeout, not form
            )
            if self._request_hook is None:
                return self._send_through_middleware(
                    pool,
                    call,
                    decode,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=False,
                    defer_error=defer_error,
                    retries=retries,
                    hook=None,
                )
            hook = _AttemptHook(self._request_hook)
            return _through_hook_sync(
                hook,
                partial(
                    self._send_through_middleware,
                    pool,
                    call,
                    decode,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=False,
                    defer_error=defer_error,
                    retries=retries,
                    hook=hook,
                ),
            )
        if skip_auth:
            auth = None
        elif self._bearer_auth is not None:
            auth = f"Bearer {self._bearer_auth()}"
        else:
            auth = self._static_auth
        return pool.request(
            method,
            path,
            query,
            encoded_headers,
            auth,
            body,
            body_kind,
            decode,
            effective_timeout,
            error_class,
            response_headers_type,
            False,  # noqa: FBT003 — the native side's positional `headers_only`
            defer_error,
        )

    def send_headers_only(
        self,
        method: str,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        response_headers_type: type[TypedHeaders] | None,
        *,
        skip_auth: bool,
        raise_for_status: bool | None,
        raises: _RaisesArg | None,
        max_retries: int | None,
    ) -> Response[None, Any]:
        """`head`/`options`: status and headers only, the body is never decoded."""
        error_class = _call_raises(raises, self._raises)
        retries = _call_retries(self._retry, method, max_retries)
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        query = (
            params
            if params is None or isinstance(params, dict)
            else _query_pairs(_encode_params(params))
        )
        encoded_headers = (
            headers if headers is None or isinstance(headers, dict) else _encode_headers(headers)
        )
        effective_timeout = (
            self._total_timeout if timeout is None else _native_call_timeout(timeout)
        )
        defer_error = not (self._raise_for_status if raise_for_status is None else raise_for_status)
        if (
            (self._reauth is not None and not skip_auth)
            or retries
            or self._request_hook is not None
        ):
            call = _NativeCall(
                method, path, query, encoded_headers, None, _NativeBody.RAW, effective_timeout
            )
            if self._request_hook is None:
                return self._send_through_middleware(
                    pool,
                    call,
                    None,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=True,
                    defer_error=defer_error,
                    retries=retries,
                    hook=None,
                )
            hook = _AttemptHook(self._request_hook)
            return _through_hook_sync(
                hook,
                partial(
                    self._send_through_middleware,
                    pool,
                    call,
                    None,
                    error_class,
                    response_headers_type,
                    skip_auth=skip_auth,
                    headers_only=True,
                    defer_error=defer_error,
                    retries=retries,
                    hook=hook,
                ),
            )
        if skip_auth:
            auth = None
        elif self._bearer_auth is not None:
            auth = f"Bearer {self._bearer_auth()}"
        else:
            auth = self._static_auth
        return self._exchange(
            pool,
            method,
            path,
            query,
            encoded_headers,
            auth,
            None,
            _NativeBody.RAW,
            effective_timeout,
            None,
            error_class,
            response_headers_type,
            None,
            headers_only=True,
            defer_error=defer_error,
        )

    def sse(
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        into: _Into,
        *,
        skip_auth: bool,
        raises: _RaisesArg | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
        interruptible: bool,
    ) -> Generator[SSEEvent[Any]]:
        """`_SyncPyreqwestTransport.sse`. `timeout` is each connection's total cap, if given."""
        call = _StreamCall(
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            check_status=True,
            raises=raises,
        )
        return _sse_events_sync(
            partial(self._sse_source, path, call),
            into,
            raises,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
            reconnect_on_close=reconnect_on_close,
            interruptible=interruptible,
        )

    def stream_lines(
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
        into: _Into,
        *,
        skip_auth: bool,
        infer_mime_type_from_file_extension: bool,
        raises: _RaisesArg | None,
        max_retries: int | None,
        interruptible: bool,
    ) -> Generator[Any]:
        """`_SyncPyreqwestTransport.stream_lines`: not a generator itself, so a closed client
        raises at the call."""
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        idle = self._stream_idle_timeout if timeout is None else None
        call = _StreamCall(
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            check_status=True,
            raises=raises,
            max_retries=max_retries,
        )
        return _stream_lines_sync(
            self._stream_source(pool, method, path, call),
            into,
            raises,
            idle,
            interruptible=interruptible,
        )

    def download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
        raise_for_status: bool | None,
        raises: _RaisesArg | None,
        max_retries: int | None,
    ) -> Response[bytes] | Response[Path]:
        _validate_raises(raises)
        pool = self._native_pool
        if pool is None:
            raise self._closed_error()
        idle = self._stream_idle_timeout if timeout is None else None
        check_status = self._raise_for_status if raise_for_status is None else raise_for_status
        call = _StreamCall(
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            check_status=check_status,
            raises=raises,
            max_retries=max_retries,
        )
        source = self._stream_source(pool, "GET", path, call)
        # Unchecked, a 4xx/5xx comes back as the error response `_download_stream` builds.
        deferred = None if check_status else _call_raises(raises, self._raises)
        return _download_stream_sync(source, dest, idle, deferred)
