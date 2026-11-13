"""`HTTPClient`, the async client: its verbs and their overloads."""

from collections.abc import (
    AsyncGenerator,
    Mapping,
    Sequence,
)
from os import PathLike
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Self,
    overload,
)

from typing_extensions import TypeForm

from ._compat import (
    DecoderTyping,
    TypeAdapterTyping,
)
from ._errors import HTTPResponseError, _error_body_decoding
from ._response import Response
from ._settings import (
    NativeMode,
    TlsVersion,
    _retry_method_set,
    _TransportSettings,
    _uses_native_transport,
)
from ._sse import SSEEvent
from ._transport_native import _NativeTransport
from ._transport_pyreqwest import _PyreqwestTransport
from ._types import AuthProvider, Form, Headers, JSONPayload, Params, TypedHeaders


class HTTPClient:
    """Async typed HTTP client: `async with HTTPClient(base_url=...) as client:`.

    See `SyncHTTPClient` for the sync mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({
        "GET",
        "PUT",
        "DELETE",
        "HEAD",
        "OPTIONS",
    })

    _settings: _TransportSettings
    _transport: _PyreqwestTransport | _NativeTransport

    # Not a @dataclass, unlike most classes here (style-guide.md §1): the constructor takes the
    # client's *settings*, while the transport's pyreqwest client only exists between entering
    # and exiting, and is rebuilt from those settings on every entry, since a pyreqwest builder
    # can only be built once. That also means one client can be entered again after it exits.
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
        native: NativeMode = "auto",
        raises: type[HTTPResponseError[Any]] = HTTPResponseError,
    ) -> None:
        """The client's settings. Nothing opens until it's entered, as
        `async with HTTPClient(...) as client:`.

        `bearer_token` is a static token; `bearer_auth` is an async callable resolved fresh on
        every request; `basic_auth` is a `(username, password)` pair — provide at most one of the
        three. `max_retries` enables a real retry middleware (backoff, `Retry-After`-aware); with
        no `retry_methods`, only the idempotent verbs (`GET`/`PUT`/`DELETE`/`HEAD`/`OPTIONS`)
        retry, and `backoff_base` scales the wait between attempts (`backoff_base * 2 **
        attempt`, so the 0.1 default waits 0.1s then 0.2s) — a `Retry-After` header on the
        response wins over the computed delay, up to `max_retry_after` (60s by default): a server
        asking for a longer wait ends retrying, and its 429/503 is raised as `HTTPResponseError`
        so you can schedule a retry from `e.headers["Retry-After"]`. Pass `max_retry_after=None`
        to honour any wait.

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

        `native` picks the transport. `"auto"` (the default) uses lothc's native transport, about
        twice as fast on `get(into=Struct)` in lothc's benchmarks, on Linux and macOS, and
        pyreqwest on Windows or with `http2=True`, which native doesn't support yet. `"off"`
        always uses pyreqwest; `"on"` always uses native, raising `NotImplementedError` here on
        Windows or with `http2=True`. `lothc.testing`'s mocker intercepts either.

        `raises` is the `HTTPResponseError` class every 4xx/5xx is raised as (each verb's own
        `raises=` overrides it). A subclass that pins the error type,
        `class APIError(HTTPResponseError[NotFound | Conflict]): pass`, decodes the body onto
        `.error` as that type; a bad class raises `TypeError` here, not on the first error.
        """
        if sum(value is not None for value in (bearer_token, bearer_auth, basic_auth)) > 1:
            raise ValueError(
                "Provide at most one of 'bearer_token', 'bearer_auth', or 'basic_auth'"
            )
        # Resolved now, so a bad class fails here rather than on the first 4xx/5xx.
        _error_body_decoding(raises)
        self._settings = _TransportSettings(
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
            native=native,
            retry_methods=_retry_method_set(retry_methods, self._default_retry_methods),
        )
        # `native=`, or the private test-only `LOTHC_TRANSPORT`, which wins over it.
        transport = (
            _NativeTransport if _uses_native_transport(self._settings) else _PyreqwestTransport
        )
        self._transport = transport(
            self._settings,
            bearer_token,
            bearer_auth,
            basic_auth,
            raises,
            self._default_retry_methods,
        )

    async def __aenter__(self) -> Self:
        await self._transport.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._transport.close()

    @overload
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
    @overload
    async def get[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def get[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[TData],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
    @overload
    async def get[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: None = None,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    async def get[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    async def get[TData, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """GET `path`, returning a `Response` whose `.data` is the body decoded as
        `into` (raw `bytes` by default), alongside `.status`, `.headers`,
        `.request`, `.http_version` and `.elapsed`. Pass `response_headers_type` to also get the
        headers parsed into `.typed_headers`.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.send(
            "GET",
            path,
            params,
            headers,
            timeout,
            None,
            None,
            None,
            None,
            into,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def post[TData](
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
        into: TypeForm[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    async def post[TData, THeaders: TypedHeaders](
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
        into: TypeForm[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """POST to `path` with at most one of `json`/`data`/`form`/`content` (raises
        `ValueError` if more than one is given), returning a `Response` like `get`'s.
        `data` is a urlencoded body (`a=1&b=2`), `form` a `multipart/form-data` one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.send(
            "POST",
            path,
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            into,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def put[TData](
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
        into: TypeForm[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    async def put[TData, THeaders: TypedHeaders](
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
        into: TypeForm[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """PUT to `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.send(
            "PUT",
            path,
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            into,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def patch[TData](
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
        into: TypeForm[TData],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    async def patch[TData, THeaders: TypedHeaders](
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
        into: TypeForm[TData],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """PATCH `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.send(
            "PATCH",
            path,
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            into,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            raises=raises,
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
        into: None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    async def delete[TData](
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
        into: TypeForm[TData],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    async def delete[TData, THeaders: TypedHeaders](
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
        into: TypeForm[TData],
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """DELETE `path`, returning a `Response` like `get`'s. A body is rare on a DELETE but
        allowed: the same `json`/`data`/`form`/`content` options as `post`, at most one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.send(
            "DELETE",
            path,
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            into,
            response_headers_type,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            raises=raises,
        )

    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeAdapterTyping[TData] | DecoderTyping[TData],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncGenerator[SSEEvent[TData]]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[TData],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
    ) -> AsyncGenerator[SSEEvent[Any]]:
        """Open `path` as a Server-Sent Events stream, yielding one `SSEEvent` per event.

        `into` decodes `.data` (any type, e.g. `list[Model]`, or a pre-built `TypeAdapter`/
        msgspec `Decoder`); `.event`/`.id` are always populated regardless (`"message"`/`""`
        when the server omits them).

        The client's `timeout` never applies here — an SSE stream is open-ended, and a total
        request timeout would kill every healthy stream on schedule. `timeout` bounds one
        connection attempt only; use the client's `read_timeout` to detect a stalled stream.
        `skip_auth` omits the `Authorization` header (and skips invoking `bearer_auth`) for this
        call; `raises` overrides the client's own `raises` (the `HTTPResponseError` subclass a
        4xx/5xx is raised as) for this call.

        A dropped connection (transport error or timeout) reconnects automatically, after
        `reconnect_delay` seconds — or the server's own `retry:` value once it has sent one —
        with a `Last-Event-ID` header carrying the last id seen, so a spec-compliant server
        resumes where it left off. `max_reconnects` caps *consecutive* reconnects that yield no
        event (`None` for unlimited, `0` to disable — the error is raised instead). A clean close
        ends the stream, unless `reconnect_on_close=True` (browser `EventSource` behavior); a 204
        always ends it.
        """
        return self._transport.sse(
            path,
            params,
            headers,
            timeout,
            into,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            raises=raises,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
            reconnect_on_close=reconnect_on_close,
        )

    @overload
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> AsyncGenerator[TLine]: ...
    @overload
    def stream_get[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[TLine],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> AsyncGenerator[TLine]: ...
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> AsyncGenerator[Any]:
        """Stream GET `path`'s response as raw `bytes` chunks (unbuffered, safe for binary).

        Pass `into` to switch to newline-buffered NDJSON-style decoding instead —
        each complete line is parsed and decoded as its own value.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return self._transport.stream_lines(
            "GET",
            path,
            params,
            headers,
            timeout,
            None,
            None,
            None,
            None,
            into,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=True,
            error_for_status=error_for_status,
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> AsyncGenerator[TLine]: ...
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
        into: TypeForm[TLine],
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> AsyncGenerator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`data`/`form`/`content`
        options as `post` (at most one), same raw-bytes-by-default / NDJSON-via-
        `into` split.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return self._transport.stream_lines(
            "POST",
            path,
            params,
            headers,
            timeout,
            json,
            data,
            form,
            content,
            into,
            skip_auth=skip_auth,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            error_for_status=error_for_status,
            raises=raises,
        )

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
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[bytes]: ...
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
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Path]: ...
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
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[bytes] | Response[Path]:
        """Download `path`'s response body, optimized for large objects (e.g. a presigned GET
        URL for a multi-GB file) — much lower peak memory than `get()` for big bodies.

        Returns a `Response` like `get()`. With no `dest`, streams the body into one pre-grown
        buffer and `.data` is the `bytes` — still O(body size) memory, but about two-thirds of
        what `get()` peaks at (measured: 105MB vs 154MB for a 50MB body), since `get()` goes
        through pyreqwest's own `.bytes()`. Pass `dest` to stream straight to a file instead —
        memory then stays O(chunk size) regardless of how large the body is, and `.data` is
        `dest` as a `Path`. `.elapsed` runs until the whole body has been read.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.download(
            path,
            None if dest is None else Path(dest),
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            raises=raises,
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
        raises: type[HTTPResponseError[Any]] | None = None,
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
        raises: type[HTTPResponseError[Any]] | None = None,
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
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass
        `response_headers_type` to get the response headers parsed into `.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.send_headers_only(
            "HEAD",
            path,
            params,
            headers,
            timeout,
            response_headers_type,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            raises=raises,
        )

    @overload
    async def options(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None]: ...
    @overload
    async def options[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None, THeaders]: ...
    async def options(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None, Any]:
        """OPTIONS `path` (e.g. a CORS preflight) — status and headers only, any body is discarded.
        Pass `response_headers_type` to get the response headers parsed into `.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return await self._transport.send_headers_only(
            "OPTIONS",
            path,
            params,
            headers,
            timeout,
            response_headers_type,
            skip_auth=skip_auth,
            error_for_status=error_for_status,
            raises=raises,
        )
