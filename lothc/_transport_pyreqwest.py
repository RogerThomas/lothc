"""The pyreqwest transport: request building, retry/re-auth middleware, and every verb's send."""

import asyncio
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Generator,
    Sequence,
)
from contextlib import (
    AsyncExitStack,
    ExitStack,
)
from dataclasses import dataclass
from dataclasses import field as _dataclass_field
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import (
    Any,
    ClassVar,
)

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
from pyreqwest.middleware.types import Middleware, SyncMiddleware
from pyreqwest.proxy import ProxyBuilder
from pyreqwest.request import (
    BaseRequestBuilder,
    Request,
    RequestBuilder,
    SyncRequestBuilder,
)
from pyreqwest.response import Response as RawResponse
from pyreqwest.response import SyncResponse as RawSyncResponse

from ._compat import (
    BaseModel,
    Decoder,
    Struct,
    TypeAdapter,
    msgspec,
)
from ._decode import _Decoder, _decoder_for, _Into
from ._encode import _encode_headers, _encode_params, _pydantic_by_alias, _query_pairs
from ._errors import (
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    HTTPTransportError,
    _status_error,
    _validate_raises,
)
from ._form import _build_form, _build_sync_form
from ._headers import CaseInsensitiveDict, _LazyHeaders
from ._response import RequestInfo, Response, _parse_typed_headers
from ._retry import _backoff_delay, _is_retryable_status, _parse_retry_after, _retry_after_exceeds
from ._settings import TlsVersion, _default_user_agent, _TransportSettings
from ._sse import SSEEvent, _sse_events, _sse_events_sync
from ._streaming import (
    _ChunkReader,
    _download_stream,
    _download_stream_sync,
    _ResponseHead,
    _stream_lines,
    _stream_lines_sync,
    _StreamCall,
    _SyncChunkReader,
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


def _client_build_error(error: PyreqwestBuilderError) -> ValueError:
    """pyreqwest's `BuilderError` from building a client (a TLS version range rustls can't
    satisfy, an unusable `identity_pem`, a `user_agent` that isn't a valid header value) as a
    plain `ValueError`, so no pyreqwest type reaches the caller.

    The message is pyreqwest's own, with its causes after it on one line
    (`builder error: empty supported tls versions`), which is also what the native transport
    raises. `BuilderError` already subclasses `ValueError`, so `except ValueError` still works.
    """
    causes = [cause["message"] for cause in error.details["causes"] or []]
    return ValueError(": ".join([error.message, *(c for c in causes if c != error.message)]))


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


@dataclass(slots=True)
class _PyreqwestMockHook:
    """`lothc.testing`'s mocker on this transport, set by `_mocking.py` while a `lothc_mocker`
    fixture is active and added to every request as it's built, so it sees whatever any client
    sends, however and whenever that client was made. A request's own middleware runs after the
    client's (retries, re-authentication), so a retry asks the mocks again, and before reqwest
    sends anything, so a mocked 3xx is never followed. Unmocked, a request pays one read of it."""

    middleware: Middleware | None = None
    sync_middleware: SyncMiddleware | None = None


_mock_hook = _PyreqwestMockHook()


async def _send(request_builder: RequestBuilder) -> tuple[RawResponse, RequestInfo]:
    if (mock := _mock_hook.middleware) is not None:
        request_builder = request_builder.with_middleware(mock)
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
    if (mock := _mock_hook.sync_middleware) is not None:
        request_builder = request_builder.with_middleware(mock)
    try:
        built = request_builder.build()
        request_info = _request_info(built)
        return built.send(), request_info
    except (PyreqwestRequestError, PyreqwestBuilderError) as error:
        raise _translate_transport_error(error) from error


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


def _bearer_token_sent(request: Request) -> str | None:
    """The bearer token `request` carries, or `None` (e.g. a `skip_auth=True` call, which sends
    none and so is left alone)."""
    sent = request.headers.get("authorization")
    if sent is None or not sent.startswith("Bearer "):
        return None
    return sent.removeprefix("Bearer ")


def _replayable_copy(request: Request) -> Request | None:
    """A copy of `request` to send while the original is kept for a replay, or `None` when its
    body can't be copied: a multipart `form=` body, which reqwest streams (`Body::try_clone` has
    nothing to clone), so pyreqwest's `copy()` raises `RuntimeError: Failed to clone request`.

    `None` means "send the original once": reqwest's own retry policy skips a request whose body
    it can't clone the same way, and failing the whole call, before anything is sent, only
    because a retry *might* be needed made `form=` unusable on any client with retries.
    """
    try:
        return request.copy()
    except RuntimeError:
        return None


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
        # `None` (not replayed) for a non-idempotent verb, or a body that can't be copied.
        replay = _replayable_copy(request) if request.method in self.retry_methods else None
        response = await next.run(request if replay is None else replay)
        if response.status != 401:
            return response
        self.auth.invalidate(sent)
        if replay is None:
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
        # `None` (not replayed) for a non-idempotent verb, or a body that can't be copied.
        replay = _replayable_copy(request) if request.method in self.retry_methods else None
        response = next.run(request if replay is None else replay)
        if response.status != 401:
            return response
        self.auth.invalidate(sent)
        if replay is None:
            return response
        request.headers["authorization"] = f"Bearer {self.auth()}"
        return next.run(request)


@dataclass
class _RetryMiddleware:
    max_retries: int
    retry_methods: frozenset[str]
    backoff_base: float = 0.1
    # The longest `Retry-After` honoured; a longer one ends retrying instead (`None`: no limit).
    max_retry_after: float | None = 60.0

    async def __call__(self, request: Request, next: Next) -> RawResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        if request.method not in self.retry_methods:
            return await next.run(request)
        for attempt in range(self.max_retries + 1):
            # Only the first attempt can find the body uncopyable: a later one means it copied.
            attempt_request = _replayable_copy(request)
            if attempt_request is None:
                return await next.run(request)
            try:
                response = await next.run(attempt_request)
            except PyreqwestRequestError as error:
                if attempt == self.max_retries or not _is_retryable_send_error(error):
                    raise
                await asyncio.sleep(_backoff_delay(attempt, self.backoff_base, retry_after=None))
                continue
            if attempt == self.max_retries or not _is_retryable_status(response.status):
                return response
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if _retry_after_exceeds(retry_after, self.max_retry_after):
                return response
            await asyncio.sleep(_backoff_delay(attempt, self.backoff_base, retry_after))
        # range(max_retries+1) is never empty, so this is never actually reached.
        raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class _SyncRetryMiddleware:
    max_retries: int
    retry_methods: frozenset[str]
    backoff_base: float = 0.1
    # The longest `Retry-After` honoured; a longer one ends retrying instead (`None`: no limit).
    max_retry_after: float | None = 60.0

    def __call__(self, request: Request, next: SyncNext) -> RawSyncResponse:  # noqa: A002 — matches pyreqwest's own middleware signature — pylint: disable=redefined-builtin,line-too-long
        if request.method not in self.retry_methods:
            return next.run(request)
        for attempt in range(self.max_retries + 1):
            # Only the first attempt can find the body uncopyable: a later one means it copied.
            attempt_request = _replayable_copy(request)
            if attempt_request is None:
                return next.run(request)
            try:
                response = next.run(attempt_request)
            except PyreqwestRequestError as error:
                if attempt == self.max_retries or not _is_retryable_send_error(error):
                    raise
                time.sleep(_backoff_delay(attempt, self.backoff_base, retry_after=None))
                continue
            if attempt == self.max_retries or not _is_retryable_status(response.status):
                return response
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            if _retry_after_exceeds(retry_after, self.max_retry_after):
                return response
            time.sleep(_backoff_delay(attempt, self.backoff_base, retry_after))
        # range(max_retries+1) is never empty, so this is never actually reached.
        raise AssertionError("unreachable")  # pragma: no cover


# Return type is whatever the decoder produces — genuinely dynamic, can't state it statically.
def _decode_body(body: Bytes, decoder: _Decoder | None) -> Any:  # noqa: ANN401
    """A successful response's whole body, decoded by an already-resolved `decoder` (`None` for
    raw bytes), resolved before sending so an unusable `into` never sends a request. Shared by
    both clients, which read the body first: a separate async step would add a frame that every
    resume of the request's task passes through."""
    if decoder is None:
        return body.to_bytes()
    if isinstance(decoder, Decoder):
        # msgspec reads the buffer directly; the others need real `bytes`.
        return decoder.decode(body)
    if isinstance(decoder, TypeAdapter):
        return decoder.validate_json(body.to_bytes())
    return decoder.decode(body.to_bytes())


def _apply_params[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, params: Params | None
) -> TBuilder:
    if params is None:
        return request_builder
    return request_builder.query(_query_pairs(_encode_params(params)))


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


def _response_head(raw_response: RawResponse | RawSyncResponse) -> _ResponseHead:
    return _ResponseHead(
        raw_response.status, _LazyHeaders(raw_response.headers), raw_response.version
    )


# A transport's bound `_check_status`, as a stream source runs it.
type _StatusCheck = Callable[
    [RawResponse, type[HTTPResponseError[Any]] | None, RequestInfo], Awaitable[None]
]
type _SyncStatusCheck = Callable[
    [RawSyncResponse, type[HTTPResponseError[Any]] | None, RequestInfo], None
]


def _translated(error: Exception) -> Exception:
    if isinstance(error, (PyreqwestRequestError, PyreqwestBuilderError)):
        return _translate_transport_error(error)
    return error


@dataclass(slots=True, eq=False)
class _PyreqwestStreamSource:
    """`_StreamSource` (`_streaming.py`) over a pyreqwest streamed request: `build` gives the
    prepared builder (auth, params, headers, body), opening builds and enters it, and the body is
    pyreqwest's `read_chunk`."""

    failures: ClassVar[tuple[type[Exception], ...]] = (
        PyreqwestRequestError,
        PyreqwestBuilderError,
    )
    _build: Callable[[], Awaitable[RequestBuilder]]
    _check_status: _StatusCheck | None
    _raises: type[HTTPResponseError[Any]] | None
    _request_builder: RequestBuilder | None = _dataclass_field(default=None, init=False)
    _info: RequestInfo | None = _dataclass_field(default=None, init=False)

    @property
    def request_info(self) -> RequestInfo:
        if self._info is None:
            raise RuntimeError("The stream was never opened")
        return self._info

    def failure(self, error: Exception, /) -> Exception:
        return _translated(error)

    def permanent(self, error: HTTPTransportError, /) -> bool:
        return _is_permanent_transport_error(error)

    async def prepare(self) -> None:
        self._request_builder = await self._build()

    async def open(self, stack: AsyncExitStack) -> tuple[_ResponseHead, _ChunkReader]:
        if self._request_builder is None:
            raise RuntimeError("The stream was opened before it was prepared")
        # pyreqwest's default streamed_read_buffer_limit is 64KB — it withholds received bytes
        # internally until that fills or the stream ends, which for an SSE stream (each record a
        # few dozen bytes) means every event arrives in one burst at stream end instead of as it
        # happens, and makes a slow (say, 1KB/s) download look silent for over a minute to the
        # idle limit. 1 forces it to hand over whatever it has as soon as it has it. Measured: no
        # throughput cost (100MB in 79ms vs 80ms on the default).
        request_builder = self._request_builder.streamed_read_buffer_limit(1)
        if (mock := _mock_hook.middleware) is not None:
            request_builder = request_builder.with_middleware(mock)
        request = request_builder.build_streamed()
        request_info = self._info = _request_info(request)
        raw_response = await stack.enter_async_context(request)
        if self._check_status is not None:
            await self._check_status(raw_response, self._raises, request_info)
        return _response_head(raw_response), raw_response.body_reader.read_chunk


@dataclass(slots=True, eq=False)
class _SyncPyreqwestStreamSource:
    """Sync mirror of `_PyreqwestStreamSource`."""

    failures: ClassVar[tuple[type[Exception], ...]] = (
        PyreqwestRequestError,
        PyreqwestBuilderError,
    )
    _build: Callable[[], SyncRequestBuilder]
    _check_status: _SyncStatusCheck | None
    _raises: type[HTTPResponseError[Any]] | None
    _request_builder: SyncRequestBuilder | None = _dataclass_field(default=None, init=False)
    _info: RequestInfo | None = _dataclass_field(default=None, init=False)

    @property
    def request_info(self) -> RequestInfo:
        if self._info is None:
            raise RuntimeError("The stream was never opened")
        return self._info

    def failure(self, error: Exception, /) -> Exception:
        return _translated(error)

    def permanent(self, error: HTTPTransportError, /) -> bool:
        return _is_permanent_transport_error(error)

    def prepare(self) -> None:
        self._request_builder = self._build()

    def open(self, stack: ExitStack) -> tuple[_ResponseHead, _SyncChunkReader]:
        if self._request_builder is None:
            raise RuntimeError("The stream was opened before it was prepared")
        # See the matching comment in `_PyreqwestStreamSource.open`.
        request_builder = self._request_builder.streamed_read_buffer_limit(1)
        if (mock := _mock_hook.sync_middleware) is not None:
            request_builder = request_builder.with_middleware(mock)
        request = request_builder.build_streamed()
        request_info = self._info = _request_info(request)
        raw_response = stack.enter_context(request)
        if self._check_status is not None:
            self._check_status(raw_response, self._raises, request_info)
        return _response_head(raw_response), raw_response.body_reader.read_chunk


@dataclass(slots=True, eq=False, repr=False)
class _PyreqwestTransport:
    """`HTTPClient`'s transport: everything pyreqwest-specific. It owns the pyreqwest client
    (built by `open()`, closed by `close()`, rebuilt on every open since a pyreqwest builder
    builds only once), applies auth, builds and sends requests, checks status, and runs the
    streaming reads. The client keeps the public API and calls `send`/`send_headers_only`/`sse`/
    `stream_lines`/`download` directly from each verb, so no client-side wrapper coroutine sits
    between a verb and pyreqwest. See `_SyncPyreqwestTransport` for the sync mirror.

    `repr=False`: a generated repr would print the bearer token or basic-auth password.
    """

    # The streaming verbs (`sse`, `stream_get`, `stream_post`, `download`) swap the client's total
    # `timeout` for this per-request one whenever the caller doesn't pass their own: pyreqwest has
    # no "no timeout" override, and a total timeout on a long-lived body kills every healthy
    # stream or large download at the 30s mark. A year is "never". Stall detection comes from
    # `_stream_idle_timeout` instead (not for `sse`, whose quiet gaps are legitimate).
    _streaming_default_timeout: ClassVar[float] = 365 * 24 * 60 * 60

    _settings: _TransportSettings
    _bearer_token: str | None
    _bearer_auth: AuthProvider | None
    _basic_auth: tuple[str, str | None] | None
    _raises: type[HTTPResponseError[Any]]
    # The verbs a re-authenticated 401 may be retried for: the idempotent ones.
    _idempotent_methods: frozenset[str]
    # The longest gap `stream_get`/`stream_post`/`download` tolerate between chunks: the client's
    # `timeout`, unless `read_timeout` was set, in which case pyreqwest already enforces that
    # itself at the socket and this stays `None`. Not applied to `sse`.
    _stream_idle_timeout: float | None = _dataclass_field(init=False)
    _pyreqwest_client: Client | None = _dataclass_field(default=None, init=False)
    _exit_stack: AsyncExitStack | None = _dataclass_field(default=None, init=False)

    def __post_init__(self) -> None:
        self._stream_idle_timeout = (
            self._settings.timeout if self._settings.read_timeout is None else None
        )

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
        self,
        raw_response: RawResponse,
        raises: type[HTTPResponseError[Any]] | None,
        request_info: RequestInfo,
    ) -> None:
        if raw_response.status < 400:
            return
        body = (await _read_body(raw_response)).to_bytes()
        raise _status_error(
            raises if raises is not None else self._raises,
            raw_response.status,
            body,
            request_info,
            CaseInsensitiveDict(raw_response.headers),
        )

    async def _build_stream_request(
        self, request_builder: RequestBuilder, call: _StreamCall, last_event_id: str
    ) -> RequestBuilder:
        request_builder = await self._prepare_request(
            request_builder, call.params, call.headers, call.timeout, skip_auth=call.skip_auth
        )
        if last_event_id:
            # `.headers()` replaces a same-named value where `.header()` appends one, so a
            # caller's own `Last-Event-ID` (sent as-is until this stream has seen an id)
            # gives way to the tracked one instead of going out beside it.
            request_builder = request_builder.headers({"last-event-id": last_event_id})
        return await _attach_body(
            request_builder,
            call.json,
            call.data,
            call.form,
            call.content,
            infer_mime_type_from_file_extension=call.infer_mime_type_from_file_extension,
        )

    def _stream_source(
        self, request_builder: RequestBuilder, call: _StreamCall, last_event_id: str = ""
    ) -> _PyreqwestStreamSource:
        return _PyreqwestStreamSource(
            partial(self._build_stream_request, request_builder, call, last_event_id),
            self._check_status if call.error_for_status else None,
            call.raises,
        )

    def _sse_source(
        self, path: str, call: _StreamCall, last_event_id: str
    ) -> _PyreqwestStreamSource:
        request_builder = self._client.get(path).header("accept", "text/event-stream")
        return self._stream_source(request_builder, call, last_event_id)

    async def open(self) -> None:
        if self._pyreqwest_client is not None:
            raise RuntimeError("This HTTPClient is already open; enter it once at a time.")
        builder = _configure_client_builder(ClientBuilder(), self._settings)
        if self._settings.max_retries > 0:
            builder = builder.with_middleware(
                _RetryMiddleware(
                    self._settings.max_retries,
                    self._settings.retry_methods,
                    self._settings.backoff_base,
                    self._settings.max_retry_after,
                )
            )
        if isinstance(self._bearer_auth, _InvalidatableAuth):
            builder = builder.with_middleware(
                _ReauthMiddleware(self._bearer_auth, self._idempotent_methods)
            )
        try:
            pyreqwest_client = builder.build()
        except PyreqwestBuilderError as error:
            raise _client_build_error(error) from error
        stack = AsyncExitStack()
        self._pyreqwest_client = await stack.enter_async_context(pyreqwest_client)
        self._exit_stack = stack

    async def close(self) -> None:
        stack = self._exit_stack
        self._pyreqwest_client = None
        self._exit_stack = None
        if stack is not None:
            await stack.aclose()

    async def send(
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
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> Response[Any, Any]:
        """Every body verb's shared path, taking the HTTP method as a string.

        The one hot path of every non-streaming request, so it calls `_send` and `_read_body`
        directly and does the rest inline: each extra async layer between a verb and pyreqwest's
        coroutine is another frame every resume of the request's task passes through.
        """
        _validate_raises(raises)
        # Resolved before sending, so an unusable `into` fails without a request (a POST that
        # ran and then raised `TypeError` would have done its side effect for nothing).
        decoder = _decoder_for(into)
        request_builder = await self._prepare_request(
            self._client.request(method, path), params, headers, timeout, skip_auth=skip_auth
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
            await self._check_status(raw_response, raises, request_info)
        decoded = _decode_body(await _read_body(raw_response), decoder)
        response_headers = _LazyHeaders(raw_response.headers)
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
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> Response[None, Any]:
        """`head`/`options`: status and headers only, the body is never read."""
        _validate_raises(raises)
        request_builder = await self._prepare_request(
            self._client.request(method, path), params, headers, timeout, skip_auth=skip_auth
        )
        started = time.monotonic()
        raw_response, request_info = await _send(request_builder)
        elapsed = time.monotonic() - started
        if error_for_status:
            await self._check_status(raw_response, raises, request_info)
        response_headers = _LazyHeaders(raw_response.headers)
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

    def sse(
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        into: _Into,
        *,
        skip_auth: bool,
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
    ) -> AsyncGenerator[SSEEvent[Any]]:
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        call = _StreamCall(
            params,
            headers,
            effective_timeout,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
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
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> AsyncGenerator[Any]:
        """`stream_get`/`stream_post`: raw chunks, or one decoded value per line with `into`.

        Not a generator itself, so a closed client raises at the call, not at the first
        iteration.
        """
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        call = _StreamCall(
            params,
            headers,
            effective_timeout,
            json,
            data,
            form,
            content,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            raises=raises,
        )
        source = self._stream_source(self._client.request(method, path), call)
        return _stream_lines(source, into, raises, idle)

    async def download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> Response[bytes] | Response[Path]:
        _validate_raises(raises)
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        call = _StreamCall(
            params,
            headers,
            effective_timeout,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            raises=raises,
        )
        return await _download_stream(self._stream_source(self._client.get(path), call), dest, idle)


@dataclass(slots=True, eq=False, repr=False)
class _SyncPyreqwestTransport:
    """`SyncHTTPClient`'s transport: everything pyreqwest-specific. It owns the pyreqwest client
    (built by `open()`, closed by `close()`, rebuilt on every open since a pyreqwest builder
    builds only once), applies auth, builds and sends requests, checks status, and runs the
    streaming reads. The client keeps the public API and calls `send`/`send_headers_only`/`sse`/
    `stream_lines`/`download` directly from each verb, so no client-side wrapper coroutine sits
    between a verb and pyreqwest. See `_PyreqwestTransport` for the async mirror.

    `repr=False`: a generated repr would print the bearer token or basic-auth password.
    """

    # The streaming verbs (`sse`, `stream_get`, `stream_post`, `download`) swap the client's total
    # `timeout` for this per-request one whenever the caller doesn't pass their own: pyreqwest has
    # no "no timeout" override, and a total timeout on a long-lived body kills every healthy
    # stream or large download at the 30s mark. A year is "never". Stall detection comes from
    # `_stream_idle_timeout` instead (not for `sse`, whose quiet gaps are legitimate).
    _streaming_default_timeout: ClassVar[float] = 365 * 24 * 60 * 60

    _settings: _TransportSettings
    _bearer_token: str | None
    _bearer_auth: SyncAuthProvider | None
    _basic_auth: tuple[str, str | None] | None
    _raises: type[HTTPResponseError[Any]]
    # The verbs a re-authenticated 401 may be retried for: the idempotent ones.
    _idempotent_methods: frozenset[str]
    # The longest gap `stream_get`/`stream_post`/`download` tolerate between chunks: the client's
    # `timeout`, unless `read_timeout` was set, in which case pyreqwest already enforces that
    # itself at the socket and this stays `None`. Not applied to `sse`.
    _stream_idle_timeout: float | None = _dataclass_field(init=False)
    _pyreqwest_client: SyncClient | None = _dataclass_field(default=None, init=False)
    _exit_stack: ExitStack | None = _dataclass_field(default=None, init=False)

    def __post_init__(self) -> None:
        self._stream_idle_timeout = (
            self._settings.timeout if self._settings.read_timeout is None else None
        )

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
        raises: type[HTTPResponseError[Any]] | None,
        request_info: RequestInfo,
    ) -> None:
        if raw_response.status < 400:
            return
        body = _read_body_sync(raw_response).to_bytes()
        raise _status_error(
            raises if raises is not None else self._raises,
            raw_response.status,
            body,
            request_info,
            CaseInsensitiveDict(raw_response.headers),
        )

    def _build_stream_request(
        self, request_builder: SyncRequestBuilder, call: _StreamCall, last_event_id: str
    ) -> SyncRequestBuilder:
        request_builder = self._prepare_request(
            request_builder, call.params, call.headers, call.timeout, skip_auth=call.skip_auth
        )
        if last_event_id:
            # `.headers()` replaces a same-named value where `.header()` appends one, so a
            # caller's own `Last-Event-ID` (sent as-is until this stream has seen an id)
            # gives way to the tracked one instead of going out beside it.
            request_builder = request_builder.headers({"last-event-id": last_event_id})
        return _attach_body_sync(
            request_builder,
            call.json,
            call.data,
            call.form,
            call.content,
            infer_mime_type_from_file_extension=call.infer_mime_type_from_file_extension,
        )

    def _stream_source(
        self, request_builder: SyncRequestBuilder, call: _StreamCall, last_event_id: str = ""
    ) -> _SyncPyreqwestStreamSource:
        return _SyncPyreqwestStreamSource(
            partial(self._build_stream_request, request_builder, call, last_event_id),
            self._check_status if call.error_for_status else None,
            call.raises,
        )

    def _sse_source(
        self, path: str, call: _StreamCall, last_event_id: str
    ) -> _SyncPyreqwestStreamSource:
        request_builder = self._client.get(path).header("accept", "text/event-stream")
        return self._stream_source(request_builder, call, last_event_id)

    def open(self) -> None:
        if self._pyreqwest_client is not None:
            raise RuntimeError("This SyncHTTPClient is already open; enter it once at a time.")
        builder = _configure_client_builder(SyncClientBuilder(), self._settings)
        if self._settings.max_retries > 0:
            builder = builder.with_middleware(
                _SyncRetryMiddleware(
                    self._settings.max_retries,
                    self._settings.retry_methods,
                    self._settings.backoff_base,
                    self._settings.max_retry_after,
                )
            )
        if isinstance(self._bearer_auth, _SyncInvalidatableAuth):
            builder = builder.with_middleware(
                _SyncReauthMiddleware(self._bearer_auth, self._idempotent_methods)
            )
        try:
            pyreqwest_client = builder.build()
        except PyreqwestBuilderError as error:
            raise _client_build_error(error) from error
        stack = ExitStack()
        self._pyreqwest_client = stack.enter_context(pyreqwest_client)
        self._exit_stack = stack

    def close(self) -> None:
        stack = self._exit_stack
        self._pyreqwest_client = None
        self._exit_stack = None
        if stack is not None:
            stack.close()

    def send(
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
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> Response[Any, Any]:
        """Every body verb's shared path, taking the HTTP method as a string.

        Mirrors the async client's, where keeping this one layer deep is what makes it fast.
        """
        _validate_raises(raises)
        # Resolved before sending, so an unusable `into` fails without a request (a POST that
        # ran and then raised `TypeError` would have done its side effect for nothing).
        decoder = _decoder_for(into)
        request_builder = self._prepare_request(
            self._client.request(method, path), params, headers, timeout, skip_auth=skip_auth
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
            self._check_status(raw_response, raises, request_info)
        decoded = _decode_body(_read_body_sync(raw_response), decoder)
        response_headers = _LazyHeaders(raw_response.headers)
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
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> Response[None, Any]:
        """`head`/`options`: status and headers only, the body is never read."""
        _validate_raises(raises)
        request_builder = self._prepare_request(
            self._client.request(method, path), params, headers, timeout, skip_auth=skip_auth
        )
        started = time.monotonic()
        raw_response, request_info = _send_sync(request_builder)
        elapsed = time.monotonic() - started
        if error_for_status:
            self._check_status(raw_response, raises, request_info)
        response_headers = _LazyHeaders(raw_response.headers)
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

    def sse(
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        into: _Into,
        *,
        skip_auth: bool,
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
        max_reconnects: int | None,
        reconnect_delay: float,
        reconnect_on_close: bool,
        interruptible: bool,
    ) -> Generator[SSEEvent[Any]]:
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        call = _StreamCall(
            params,
            headers,
            effective_timeout,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
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
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
        interruptible: bool,
    ) -> Generator[Any]:
        """`stream_get`/`stream_post`: raw chunks, or one decoded value per line with `into`.

        Not a generator itself, so a closed client raises at the call, not at the first
        iteration.
        """
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        call = _StreamCall(
            params,
            headers,
            effective_timeout,
            json,
            data,
            form,
            content,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            raises=raises,
        )
        source = self._stream_source(self._client.request(method, path), call)
        return _stream_lines_sync(source, into, raises, idle, interruptible=interruptible)

    def download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        timeout: float | None,
        *,
        skip_auth: bool,
        error_for_status: bool,
        raises: type[HTTPResponseError[Any]] | None,
    ) -> Response[bytes] | Response[Path]:
        _validate_raises(raises)
        effective_timeout = timeout if timeout is not None else self._streaming_default_timeout
        idle = self._stream_idle_timeout if timeout is None else None
        call = _StreamCall(
            params,
            headers,
            effective_timeout,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            raises=raises,
        )
        return _download_stream_sync(self._stream_source(self._client.get(path), call), dest, idle)
