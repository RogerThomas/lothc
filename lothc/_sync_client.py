"""`SyncHTTPClient`, the sync client: its verbs and their overloads."""

from collections.abc import (
    Generator,
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
from ._transport_native import _SyncNativeTransport
from ._transport_pyreqwest import _SyncPyreqwestTransport
from ._types import Form, Headers, JSONPayload, Params, SyncAuthProvider, TypedHeaders


class SyncHTTPClient:
    """Sync typed HTTP client: `with SyncHTTPClient(base_url=...) as client:`.

    See `HTTPClient` for the async mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({
        "GET",
        "PUT",
        "DELETE",
        "HEAD",
        "OPTIONS",
    })

    _settings: _TransportSettings
    _transport: _SyncPyreqwestTransport | _SyncNativeTransport

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
        native: NativeMode = "auto",
        raise_for_status: bool = True,
        raises: type[HTTPResponseError[Any]] = HTTPResponseError,
    ) -> None:
        """The client's settings. Nothing opens until it's entered, as
        `with SyncHTTPClient(...) as client:`.

        `bearer_token` is a static token; `bearer_auth` is a callable resolved fresh on every
        request; `basic_auth` is a `(username, password)` pair — provide at most one of the
        three. `max_retries` enables a real retry middleware (backoff, `Retry-After`-aware); with
        no `retry_methods`, only the idempotent verbs (`GET`/`PUT`/`DELETE`/`HEAD`/`OPTIONS`)
        retry, and `backoff_base` scales the wait between attempts (`backoff_base * 2 **
        attempt`, so the 0.1 default waits 0.1s then 0.2s) — a `Retry-After` header on the
        response wins over the computed delay, up to `max_retry_after` (60s by default): a server
        asking for a longer wait ends retrying, and its 429/503 is raised as `HTTPResponseError`
        (or returned, with `raise_for_status=False`) so you can schedule a retry from its
        `headers["Retry-After"]`. Pass `max_retry_after=None` to honour any wait.

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

        `raise_for_status=False` makes `get`/`post`/`put`/`patch`/`delete`/`head`/`options`/
        `download` return a 4xx/5xx as a `Response` instead of raising (each verb's own
        `raise_for_status=` overrides it): `.status`, `.error` (the body decoded as `raises`
        decodes it) and `.raw_error` say what went wrong, and reading `.data` raises the error.
        The streaming verbs always raise.
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
            _SyncNativeTransport
            if _uses_native_transport(self._settings)
            else _SyncPyreqwestTransport
        )
        self._transport = transport(
            self._settings,
            bearer_token,
            bearer_auth,
            basic_auth,
            raises,
            raise_for_status,
            self._default_retry_methods,
        )

    def __enter__(self) -> Self:
        self._transport.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._transport.close()

    @overload
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: None = None,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
    @overload
    def get[TAdapted](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def get[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[TData],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
    @overload
    def get[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: None = None,
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
    @overload
    def get[TAdapted, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    def get[TData, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[TData],
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """GET `path`, returning a `Response` whose `.data` is the body decoded as
        `into` (raw `bytes` by default), alongside `.status`, `.headers`,
        `.request`, `.http_version` and `.elapsed`. Pass `response_headers_type` to also get the
        headers parsed into `.typed_headers`.

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, and reading its `.data`
        raises that error instead.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return self._transport.send(
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
            raise_for_status=raise_for_status,
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def post[TData](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    def post[TData, THeaders: TypedHeaders](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """POST to `path` with at most one of `json`/`data`/`form`/`content` (raises
        `ValueError` if more than one is given), returning a `Response` like `get`'s.
        `data` is a urlencoded body (`a=1&b=2`), `form` a `multipart/form-data` one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, and reading its `.data`
        raises that error instead.
        """
        return self._transport.send(
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
            raise_for_status=raise_for_status,
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def put[TData](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    def put[TData, THeaders: TypedHeaders](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """PUT to `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, and reading its `.data`
        raises that error instead.
        """
        return self._transport.send(
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
            raise_for_status=raise_for_status,
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def patch[TData](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    def patch[TData, THeaders: TypedHeaders](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """PATCH `path`. Same body/decode rules as `post`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, and reading its `.data`
        raises that error instead.
        """
        return self._transport.send(
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
            raise_for_status=raise_for_status,
            raises=raises,
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
        into: None = None,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any]]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted]: ...
    @overload
    def delete[TData](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData]: ...
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
        into: None = None,
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[dict[str, Any], THeaders]: ...
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
        into: TypeAdapterTyping[TAdapted] | DecoderTyping[TAdapted],
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TAdapted, THeaders]: ...
    @overload
    def delete[TData, THeaders: TypedHeaders](
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[TData, THeaders]: ...
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        response_headers_type: type[TypedHeaders] | None = None,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Any, Any]:
        """DELETE `path`, returning a `Response` like `get`'s. A body is rare on a DELETE but
        allowed: the same `json`/`data`/`form`/`content` options as `post`, at most one.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, and reading its `.data`
        raises that error instead.
        """
        return self._transport.send(
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
            raise_for_status=raise_for_status,
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
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeAdapterTyping[TData] | DecoderTyping[TData],
        raises: type[HTTPResponseError[Any]] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Generator[SSEEvent[TData]]: ...
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
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
        max_reconnects: int | None = 5,
        reconnect_delay: float = 3.0,
        reconnect_on_close: bool = False,
        interruptible: bool = False,
    ) -> Generator[SSEEvent[Any]]:
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

        A 4xx/5xx response always raises `HTTPResponseError`, whatever the client's
        `raise_for_status`.

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
        return self._transport.sse(
            path,
            params,
            headers,
            timeout,
            into,
            skip_auth=skip_auth,
            raises=raises,
            max_reconnects=max_reconnects,
            reconnect_delay=reconnect_delay,
            reconnect_on_close=reconnect_on_close,
            interruptible=interruptible,
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
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        raises: type[HTTPResponseError[Any]] | None = None,
        interruptible: bool = False,
    ) -> Generator[TLine]: ...
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
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
        interruptible: bool = False,
    ) -> Generator[Any]:
        """Stream GET `path`'s response as raw `bytes` chunks (unbuffered, safe for binary).

        Pass `into` to switch to newline-buffered NDJSON-style decoding instead —
        each complete line is parsed and decoded as its own value.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        A 4xx/5xx response always raises `HTTPResponseError`, whatever the client's
        `raise_for_status`.

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
            raises=raises,
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
        into: None = None,
        infer_mime_type_from_file_extension: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: type[dict[Any, Any]],
        infer_mime_type_from_file_extension: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeAdapterTyping[TLine] | DecoderTyping[TLine],
        infer_mime_type_from_file_extension: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
        interruptible: bool = False,
    ) -> Generator[TLine]: ...
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
        raises: type[HTTPResponseError[Any]] | None = None,
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
        into: TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None = None,
        infer_mime_type_from_file_extension: bool = True,
        raises: type[HTTPResponseError[Any]] | None = None,
        interruptible: bool = False,
    ) -> Generator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`data`/`form`/`content`
        options as `post` (at most one), same raw-bytes-by-default / NDJSON-via-
        `into` split.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        A 4xx/5xx response always raises `HTTPResponseError`, whatever the client's
        `raise_for_status`.

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
            raises=raises,
            interruptible=interruptible,
        )

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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[bytes]: ...
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[Path]: ...
    def download(
        self,
        path: str,
        dest: str | PathLike[str] | None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        raise_for_status: bool | None = None,
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

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, with nothing written to
        `dest`, and reading its `.data` raises that error instead.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.
        """
        return self._transport.download(
            path,
            None if dest is None else Path(dest),
            params,
            headers,
            timeout,
            skip_auth=skip_auth,
            raise_for_status=raise_for_status,
            raises=raises,
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
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
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass
        `response_headers_type` to get the response headers parsed into `.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, and reading its `.data`
        raises that error instead.
        """
        return self._transport.send_headers_only(
            "HEAD",
            path,
            params,
            headers,
            timeout,
            response_headers_type,
            skip_auth=skip_auth,
            raise_for_status=raise_for_status,
            raises=raises,
        )

    @overload
    def options(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None]: ...
    @overload
    def options[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[THeaders],
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None, THeaders]: ...
    def options(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        timeout: float | None = None,
        skip_auth: bool = False,
        response_headers_type: type[TypedHeaders] | None = None,
        raise_for_status: bool | None = None,
        raises: type[HTTPResponseError[Any]] | None = None,
    ) -> Response[None, Any]:
        """OPTIONS `path` (e.g. a CORS preflight) — status and headers only, any body is discarded.
        Pass `response_headers_type` to get the response headers parsed into `.typed_headers`.

        `timeout` overrides the client's own for this call only; `skip_auth` omits the
        `Authorization` header (and skips invoking `bearer_auth`) for this call; `raises` overrides
        the client's own `raises` (the `HTTPResponseError` subclass a 4xx/5xx is raised as) for
        this call.

        Raises `HTTPResponseError` on a 4xx/5xx response, unless `raise_for_status` (this call's,
        else the client's) is `False`: the `Response` then comes back, and reading its `.data`
        raises that error instead.
        """
        return self._transport.send_headers_only(
            "OPTIONS",
            path,
            params,
            headers,
            timeout,
            response_headers_type,
            skip_auth=skip_auth,
            raise_for_status=raise_for_status,
            raises=raises,
        )
