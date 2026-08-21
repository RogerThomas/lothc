import asyncio
import os
import time
import warnings
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Callable,
    Generator,
    Iterator,
    Mapping,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from io import BufferedIOBase
from json import loads as _json_loads
from pathlib import Path
from types import UnionType
from typing import Any, ClassVar, Protocol, Self, cast, get_args, is_typeddict, overload

from pyreqwest.client import Client, ClientBuilder, SyncClient, SyncClientBuilder
from pyreqwest.exceptions import NetworkError as PyreqwestNetworkError
from pyreqwest.exceptions import RequestTimeoutError as PyreqwestRequestTimeoutError
from pyreqwest.exceptions import TransportError as PyreqwestTransportError
from pyreqwest.middleware import Next, SyncNext
from pyreqwest.multipart import FormBuilder, PartBuilder
from pyreqwest.proxy import ProxyBuilder
from pyreqwest.request import (
    BaseRequestBuilder,
    ConsumedRequest,
    Request,
    RequestBuilder,
    SyncConsumedRequest,
    SyncRequestBuilder,
)
from pyreqwest.response import Response as RawResponse
from pyreqwest.response import SyncResponse as RawSyncResponse

from ._compat import BaseModel, Decoder, Struct, TypeAdapter, msgspec, typeguard


class JSON(dict[str, Any]):
    """A JSON object, usable as a response_data_type without pydantic or msgspec."""


class _IsTypedDict(Protocol):
    __required_keys__: ClassVar[frozenset[str]]


type File = tuple[str, bytes] | Path | BufferedIOBase
type Data = BaseModel | Struct | bytes | JSON | _IsTypedDict
type TypedHeaders = BaseModel | Struct
type Json = dict[str, Any] | BaseModel | Struct
type Form = dict[str, int | bytes | str | File]
type Params = Mapping[str, str | int | float | bool] | BaseModel | Struct
type Headers = Mapping[str, str] | BaseModel | Struct
type AsyncAuthProvider = Callable[[], Awaitable[str]]
type AuthProvider = Callable[[], str]


class HTTPResponseError(Exception):
    """Raised when a request gets a 4xx/5xx response (`error_for_status=True`, the default).

    A response WAS received — for a request that never got one, see `HTTPTransportError`.
    """

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body_start = body[:100]
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


def _translate_transport_error(error: PyreqwestTransportError) -> HTTPTransportError:
    if isinstance(error, PyreqwestRequestTimeoutError):
        return HTTPTimeoutError(str(error))
    if isinstance(error, PyreqwestNetworkError):
        return HTTPConnectionError(str(error))
    return HTTPTransportError(str(error))


async def _send(request: ConsumedRequest) -> RawResponse:
    try:
        return await request.send()
    except PyreqwestTransportError as error:
        raise _translate_transport_error(error) from error


def _send_sync(request: SyncConsumedRequest) -> RawSyncResponse:
    try:
        return request.send()
    except PyreqwestTransportError as error:
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
        raise AssertionError("unreachable")  # range(max_retries+1) is never empty


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
        raise AssertionError("unreachable")  # range(max_retries+1) is never empty


async def _build_form(form: Form) -> FormBuilder:
    form_builder = FormBuilder()
    for name, value in form.items():
        match value:
            case str():
                form_builder = form_builder.text(name, value)
            case int():
                form_builder = form_builder.text(name, str(value))
            case bytes():
                form_builder = form_builder.part(name, PartBuilder.from_bytes(value))
            case Path():
                form_builder = form_builder.part(name, await PartBuilder.from_file(value))
            case BufferedIOBase():
                part = PartBuilder.from_bytes(value.read())
                file_name = getattr(value, "name", None)
                if isinstance(file_name, str):
                    part = part.file_name(Path(file_name).name)
                form_builder = form_builder.part(name, part)
            case (filename, content):
                form_builder = form_builder.part(
                    name, PartBuilder.from_bytes(content).file_name(filename)
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


def _parse_sse_record(record: str) -> SSEEvent[str, str | None] | None:
    data_lines: list[str] = []
    event = "message"
    event_id: str | None = None
    for line in record.split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            event = value
        elif field == "id":
            event_id = value
    if not data_lines:
        return None
    return SSEEvent(id=event_id, event=event, data="\n".join(data_lines))


def _coerce_sse_id(raw_id: str | None, id_type: type[Any] | UnionType | None) -> Any:  # noqa: ANN401 — pylint: disable=line-too-long
    if id_type is None:
        return raw_id
    members = get_args(id_type) if isinstance(id_type, UnionType) else (id_type,)
    optional = type(None) in members
    # comparing type objects themselves, not checking an instance's type — isinstance() can't
    # express "is this class literally NoneType"
    real_type = next(
        (member for member in members if member is not type(None)),  # pylint: disable=unidiomatic-typecheck
        str,
    )
    if raw_id is None:
        if optional:
            return None
        raise ValueError(f"SSE event missing required 'id' field (id_type={id_type!r})")
    return raw_id if real_type is str else real_type(raw_id)


def _validate_typed_dict(response_data_type: type[Any], value: dict[str, Any]) -> None:
    if typeguard is None:
        if not os.environ.get("LOTHC_SUPPRESS_TYPEGUARD_WARNING"):
            warnings.warn(
                f"{response_data_type.__name__} is a TypedDict but typeguard is not installed; "
                "skipping runtime validation. Install typeguard to validate it, or set "
                "LOTHC_SUPPRESS_TYPEGUARD_WARNING=1 to silence this warning.",
                stacklevel=3,
            )
        return
    typeguard.check_type(value, response_data_type)


def _validate_response_data_type(response_data_type: object) -> None:
    if not isinstance(response_data_type, type):
        raise TypeError(f"response_data_type must be a class, got {response_data_type!r}")
    if response_data_type is dict:
        raise TypeError(
            "response_data_type=dict is not supported; use lothc.JSON, a TypedDict, "
            "a BaseModel subclass, or a Struct subclass instead"
        )


# Return type is whatever response_data_type is — genuinely dynamic, can't state it statically.
def _decode_json_line(
    data: str, response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any]
) -> Any:  # noqa: ANN401
    if isinstance(response_data_type, TypeAdapter):
        return response_data_type.validate_json(data)
    if isinstance(response_data_type, Decoder):
        return response_data_type.decode(data)
    _validate_response_data_type(response_data_type)
    if issubclass(response_data_type, dict):
        dict_type = cast("type[dict[str, Any]]", response_data_type)
        parsed = cast(dict[str, Any], _json_loads(data))
        if is_typeddict(dict_type):
            _validate_typed_dict(dict_type, parsed)
        return dict_type(parsed)
    if issubclass(response_data_type, Struct):
        return msgspec.json.decode(data.encode(), type=response_data_type)
    if issubclass(response_data_type, BaseModel):
        return response_data_type.model_validate_json(data)
    raise TypeError(f"Unsupported SSE response_data_type: {response_data_type!r}")


def _build_sync_form(form: Form) -> FormBuilder:
    form_builder = FormBuilder()
    for name, value in form.items():
        match value:
            case str():
                form_builder = form_builder.text(name, value)
            case int():
                form_builder = form_builder.text(name, str(value))
            case bytes():
                form_builder = form_builder.part(name, PartBuilder.from_bytes(value))
            case Path():
                form_builder = form_builder.part(name, PartBuilder.from_sync_file(value))
            case BufferedIOBase():
                part = PartBuilder.from_bytes(value.read())
                file_name = getattr(value, "name", None)
                if isinstance(file_name, str):
                    part = part.file_name(Path(file_name).name)
                form_builder = form_builder.part(name, part)
            case (filename, content):
                form_builder = form_builder.part(
                    name, PartBuilder.from_bytes(content).file_name(filename)
                )
    return form_builder


def _apply_params[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, params: Params | None
) -> TBuilder:
    match params:
        case None:
            return request_builder
        case BaseModel():
            values = {
                name: value
                for name, value in params.model_dump(mode="json").items()
                if value is not None
            }
        case Struct():
            builtins = cast(dict[str, Any], msgspec.to_builtins(params))
            values = {name: value for name, value in builtins.items() if value is not None}
        case _:
            values = params
    return request_builder.query(values)


def _apply_headers[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, headers: Headers | None
) -> TBuilder:
    match headers:
        case None:
            return request_builder
        case BaseModel():
            dumped = headers.model_dump(mode="json")
        case Struct():
            dumped = cast(dict[str, Any], msgspec.to_builtins(headers))
        case _:
            return request_builder.headers(dict(headers))
    normalized = {
        name.replace("_", "-"): str(value) for name, value in dumped.items() if value is not None
    }
    return request_builder.headers(normalized)


def _prepare[TBuilder: BaseRequestBuilder](
    request_builder: TBuilder, params: Params | None, headers: Headers | None
) -> TBuilder:
    return _apply_headers(_apply_params(request_builder, params), headers)


async def _attach_body[TBuilder: BaseRequestBuilder](  # pylint: disable=too-many-return-statements
    request_builder: TBuilder, json: Json | None, form: Form | None, content: str | bytes | None
) -> TBuilder:
    provided_bodies = [body for body in (json, form, content) if body is not None]
    if len(provided_bodies) > 1:
        raise ValueError("Provide at most one of 'json', 'form' or 'content'")
    if isinstance(json, BaseModel):
        return request_builder.body_json(json.model_dump(mode="json"))
    if isinstance(json, Struct):
        return request_builder.body_json(msgspec.to_builtins(json))
    if json is not None:
        return request_builder.body_json(json)
    if form is not None:
        return request_builder.multipart(await _build_form(form))
    if isinstance(content, str):
        return request_builder.body_text(content)
    if content is not None:
        return request_builder.body_bytes(content)
    return request_builder


def _attach_body_sync[TBuilder: BaseRequestBuilder](  # pylint: disable=too-many-return-statements
    request_builder: TBuilder, json: Json | None, form: Form | None, content: str | bytes | None
) -> TBuilder:
    provided_bodies = [body for body in (json, form, content) if body is not None]
    if len(provided_bodies) > 1:
        raise ValueError("Provide at most one of 'json', 'form' or 'content'")
    if isinstance(json, BaseModel):
        return request_builder.body_json(json.model_dump(mode="json"))
    if isinstance(json, Struct):
        return request_builder.body_json(msgspec.to_builtins(json))
    if json is not None:
        return request_builder.body_json(json)
    if form is not None:
        return request_builder.multipart(_build_sync_form(form))
    if isinstance(content, str):
        return request_builder.body_text(content)
    if content is not None:
        return request_builder.body_bytes(content)
    return request_builder


def _parse_typed_headers(headers: dict[str, str], headers_type: type[TypedHeaders]) -> TypedHeaders:
    normalized = {name.lower().replace("-", "_"): value for name, value in headers.items()}
    if issubclass(headers_type, Struct):
        return msgspec.convert(normalized, type=headers_type, strict=False)
    return headers_type.model_validate(normalized)


@dataclass
class Result[TData, THeaders: TypedHeaders | None = None]:
    """The decoded body alongside status/headers, as returned by `get_result()`/`head()`.

    `.typed_headers` is `None` unless `headers_type` was passed to the call that produced this.
    """

    data: TData
    status: int
    headers: dict[str, str]
    typed_headers: THeaders


@dataclass
class HTTPClient:
    """Async typed HTTP client, built on pyreqwest. Construct via `HTTPClient.build(...)`.

    See `SyncHTTPClient` for the sync mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({"GET", "PUT", "DELETE", "HEAD"})

    _client: Client
    _bearer_token: str | None = None
    _bearer_auth: AsyncAuthProvider | None = None

    @classmethod
    @asynccontextmanager
    async def build(
        cls,
        *,
        base_url: str | None = None,
        bearer_token: str | None = None,
        bearer_auth: AsyncAuthProvider | None = None,
        arbitrary_headers: dict[str, str] | None = None,
        timeout: float | None = 30.0,
        cookie_store: bool = False,
        follow_redirects: bool = True,
        max_redirects: int | None = None,
        proxy: str | None = None,
        max_retries: int = 0,
        retry_methods: frozenset[str] | None = None,
    ) -> AsyncGenerator[Self]:
        """Build an `HTTPClient` as an async context manager.

        `bearer_token` is a static token; `bearer_auth` is an async callable resolved fresh on
        every request — provide at most one. `max_retries` enables a real retry middleware
        (backoff, `Retry-After`-aware); with no `retry_methods`, only the idempotent verbs
        (`GET`/`PUT`/`DELETE`/`HEAD`) retry.
        """
        if bearer_token is not None and bearer_auth is not None:
            raise ValueError("Provide at most one of 'bearer_token' or 'bearer_auth'")
        pyreqwest_client_builder = ClientBuilder()
        if timeout is not None:
            pyreqwest_client_builder = pyreqwest_client_builder.timeout(timedelta(seconds=timeout))
        if arbitrary_headers:
            pyreqwest_client_builder = pyreqwest_client_builder.default_headers(arbitrary_headers)
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
        async with pyreqwest_client_builder.build() as client:
            yield cls(client, bearer_token, bearer_auth)

    async def _apply_bearer_auth(self, request_builder: RequestBuilder) -> RequestBuilder:
        if self._bearer_auth is not None:
            return request_builder.bearer_auth(await self._bearer_auth())
        if self._bearer_token is not None:
            return request_builder.bearer_auth(self._bearer_token)
        return request_builder

    async def _prepare_request(
        self, request_builder: RequestBuilder, params: Params | None, headers: Headers | None
    ) -> RequestBuilder:
        request_builder = await self._apply_bearer_auth(request_builder)
        return _prepare(request_builder, params, headers)

    async def _check_status(self, raw_response: RawResponse) -> None:
        if raw_response.status < 400:
            return
        raise HTTPResponseError(raw_response.status, (await raw_response.bytes()).to_bytes())

    async def _decode_body(self, raw_response: RawResponse, response_data_type: type[Data]) -> Data:
        _validate_response_data_type(response_data_type)
        if issubclass(response_data_type, bytes):
            return (await raw_response.bytes()).to_bytes()
        if issubclass(response_data_type, dict):
            parsed = cast(dict[str, Any], await raw_response.json())
            if is_typeddict(response_data_type):
                _validate_typed_dict(response_data_type, parsed)
            return response_data_type(parsed)
        if issubclass(response_data_type, Struct):
            return msgspec.json.decode(await raw_response.bytes(), type=response_data_type)
        if issubclass(response_data_type, BaseModel):
            return response_data_type.model_validate_json((await raw_response.bytes()).to_bytes())
        raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")

    async def _parse(
        self, raw_response: RawResponse, response_data_type: type[Data], *, error_for_status: bool
    ) -> Data:
        if error_for_status:
            await self._check_status(raw_response)
        return await self._decode_body(raw_response, response_data_type)

    @overload
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    async def get[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """GET `path` and decode the body as `response_data_type` (raw `bytes` by default).

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.
        """
        request_builder = await self._prepare_request(self._client.get(path), params, headers)
        raw_response = await _send(request_builder.build())
        return await self._parse(
            raw_response, response_data_type, error_for_status=error_for_status
        )

    @overload
    async def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> Result[bytes]: ...
    @overload
    async def get_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> Result[TData]: ...
    @overload
    async def get_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[bytes, THeaders]: ...
    @overload
    async def get_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[TData, THeaders]: ...
    async def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Data] = bytes,
        headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
    ) -> Result[Any, Any]:
        """Like `get`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `headers_type` to also get the headers parsed into
        `result.typed_headers`.
        """
        request_builder = await self._prepare_request(self._client.get(path), params, headers)
        raw_response = await _send(request_builder.build())
        if error_for_status:
            await self._check_status(raw_response)
        data = await self._decode_body(raw_response, response_data_type)
        headers = dict(raw_response.headers)
        if headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(headers, headers_type)
        return Result(data, raw_response.status, headers, typed_headers)

    async def _sse_stream(
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None,
        id_type: type[Any] | UnionType | None,
        *,
        error_for_status: bool,
    ) -> AsyncIterator[SSEEvent[Any, Any]]:
        request_builder = self._client.get(path).header("accept", "text/event-stream")
        request_builder = await self._prepare_request(request_builder, params, headers)
        request = request_builder.build_streamed()
        try:
            async with request as raw_response:
                if error_for_status:
                    await self._check_status(raw_response)
                buffer = b""
                while True:
                    chunk = await raw_response.body_reader.read_chunk()
                    if chunk is None:
                        return
                    buffer += bytes(chunk).replace(b"\r\n", b"\n")
                    while b"\n\n" in buffer:
                        record, buffer = buffer.split(b"\n\n", 1)
                        parsed_record = _parse_sse_record(record.decode())
                        if parsed_record is None:
                            continue
                        event_id = _coerce_sse_id(parsed_record.id, id_type)
                        if response_data_type is None:
                            yield SSEEvent(
                                id=event_id, event=parsed_record.event, data=parsed_record.data
                            )
                        else:
                            decoded = _decode_json_line(parsed_record.data, response_data_type)
                            yield SSEEvent(id=event_id, event=parsed_record.event, data=decoded)
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        id_type: None,
        error_for_status: bool = True,
    ) -> AsyncIterator[SSEEvent[str, str | None]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        id_type: type[TId] = str,
        error_for_status: bool = True,
    ) -> AsyncIterator[SSEEvent[str, TId]]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData] | TypeAdapter[TData] | Decoder[TData],
        id_type: None,
        error_for_status: bool = True,
    ) -> AsyncIterator[SSEEvent[TData, str | None]]: ...
    @overload
    def sse[TData, TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData] | TypeAdapter[TData] | Decoder[TData],
        id_type: type[TId] = str,
        error_for_status: bool = True,
    ) -> AsyncIterator[SSEEvent[TData, TId]]: ...
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        id_type: type[Any] | UnionType | None = str,
        error_for_status: bool = True,
    ) -> AsyncIterator[SSEEvent[Any, Any]]:
        """Open `path` as a Server-Sent Events stream, yielding one `SSEEvent` per event.

        `response_data_type` decodes `.data` (a class, `TypeAdapter`, or msgspec `Decoder`);
        `.event`/`.id` are always populated regardless. `id_type` is the single knob for both
        whether `id` is required and what type it becomes: a bare type (default `str`) means
        required, that type unioned with `None` (or bare `None`) means optional.
        """
        return self._sse_stream(
            path,
            params,
            headers,
            response_data_type,
            id_type,
            error_for_status=error_for_status,
        )

    async def _line_stream(
        self,
        request_builder: RequestBuilder,
        params: Params | None,
        headers: Headers | None,
        json: Json | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None,
        *,
        error_for_status: bool,
    ) -> AsyncIterator[Any]:
        request_builder = await self._prepare_request(request_builder, params, headers)
        request_builder = await _attach_body(request_builder, json, form, content)
        request = request_builder.build_streamed()
        try:
            async with request as raw_response:
                if error_for_status:
                    await self._check_status(raw_response)
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
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> AsyncIterator[bytes]: ...
    @overload
    def stream_get[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TLine] | TypeAdapter[TLine] | Decoder[TLine],
        error_for_status: bool = True,
    ) -> AsyncIterator[TLine]: ...
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        error_for_status: bool = True,
    ) -> AsyncIterator[Any]:
        """Stream GET `path`'s response as raw `bytes` chunks (unbuffered, safe for binary).

        Pass `response_data_type` to switch to newline-buffered NDJSON-style decoding instead —
        each complete line is parsed and decoded as its own value.
        """
        return self._line_stream(
            self._client.get(path),
            params,
            headers,
            None,
            None,
            None,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> AsyncIterator[bytes]: ...
    @overload
    def stream_post[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TLine] | TypeAdapter[TLine] | Decoder[TLine],
        error_for_status: bool = True,
    ) -> AsyncIterator[TLine]: ...
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        error_for_status: bool = True,
    ) -> AsyncIterator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`form`/`content` options as
        `post` (at most one), same raw-bytes-by-default / NDJSON-via-`response_data_type` split.
        """
        return self._line_stream(
            self._client.post(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    async def _download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        *,
        error_for_status: bool,
    ) -> bytes | None:
        request_builder = await self._prepare_request(self._client.get(path), params, headers)
        request = request_builder.build_streamed()
        try:
            async with request as raw_response:
                if error_for_status:
                    await self._check_status(raw_response)
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
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    async def download(
        self,
        path: str,
        dest: None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    async def download(
        self,
        path: str,
        dest: Path,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> None: ...
    async def download(
        self,
        path: str,
        dest: Path | None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes | None:
        """Download `path`'s response body, optimized for large objects (e.g. a presigned GET
        URL for a multi-GB file) — much lower peak memory than `get()` for big bodies.

        With no `dest`, streams the body into one pre-grown buffer and returns `bytes` — still
        O(body size) memory, but roughly a third of what `get()` uses (pyreqwest's own
        `.bytes()` does two extra full copies internally). Pass `dest` to stream straight to a
        file instead — memory then stays O(chunk size) regardless of how large the body is.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.
        """
        return await self._download(path, dest, params, headers, error_for_status=error_for_status)

    async def _send_with_body(
        self,
        request_builder: RequestBuilder,
        params: Params | None,
        headers: Headers | None,
        json: Json | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data],
        *,
        error_for_status: bool,
    ) -> Data:
        request_builder = await self._prepare_request(request_builder, params, headers)
        request_builder = await _attach_body(request_builder, json, form, content)
        raw_response = await _send(request_builder.build())
        return await self._parse(
            raw_response, response_data_type, error_for_status=error_for_status
        )

    @overload
    async def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    async def post[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    async def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """POST to `path` with at most one of `json`/`form`/`content` (raises `ValueError` if
        more than one is given) and decode the response as `response_data_type`.
        """
        return await self._send_with_body(
            self._client.post(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    async def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    async def put[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    async def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """PUT to `path`. Same body/decode rules as `post`."""
        return await self._send_with_body(
            self._client.put(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    async def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    async def patch[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    async def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """PATCH `path`. Same body/decode rules as `post`."""
        return await self._send_with_body(
            self._client.patch(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    async def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    async def delete[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    async def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """DELETE `path` and decode the response as `response_data_type`."""
        return await self._send_with_body(
            self._client.delete(path),
            params,
            headers,
            None,
            None,
            None,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    async def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> Result[None]: ...
    @overload
    async def head[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[None, THeaders]: ...
    async def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
    ) -> Result[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass `headers_type` to get the
        response headers parsed into `result.typed_headers`.
        """
        request_builder = await self._prepare_request(self._client.head(path), params, headers)
        raw_response = await _send(request_builder.build())
        if error_for_status:
            await self._check_status(raw_response)
        response_headers = dict(raw_response.headers)
        typed_headers = (
            None if headers_type is None else _parse_typed_headers(response_headers, headers_type)
        )
        return Result(None, raw_response.status, response_headers, typed_headers)


@dataclass
class SyncHTTPClient:
    """Sync typed HTTP client, built on pyreqwest. Construct via `SyncHTTPClient.build(...)`.

    See `HTTPClient` for the async mirror — same methods, same overload shapes.
    """

    _default_retry_methods: ClassVar[frozenset[str]] = frozenset({"GET", "PUT", "DELETE", "HEAD"})

    _client: SyncClient
    _bearer_token: str | None = None
    _bearer_auth: AuthProvider | None = None

    @classmethod
    @contextmanager
    def build(
        cls,
        *,
        base_url: str | None = None,
        bearer_token: str | None = None,
        bearer_auth: AuthProvider | None = None,
        arbitrary_headers: dict[str, str] | None = None,
        timeout: float | None = 30.0,
        cookie_store: bool = False,
        follow_redirects: bool = True,
        max_redirects: int | None = None,
        proxy: str | None = None,
        max_retries: int = 0,
        retry_methods: frozenset[str] | None = None,
    ) -> Generator[Self]:
        """Build a `SyncHTTPClient` as a context manager.

        `bearer_token` is a static token; `bearer_auth` is a callable resolved fresh on every
        request — provide at most one. `max_retries` enables a real retry middleware (backoff,
        `Retry-After`-aware); with no `retry_methods`, only the idempotent verbs
        (`GET`/`PUT`/`DELETE`/`HEAD`) retry.
        """
        if bearer_token is not None and bearer_auth is not None:
            raise ValueError("Provide at most one of 'bearer_token' or 'bearer_auth'")
        sync_client_builder = SyncClientBuilder()
        if timeout is not None:
            sync_client_builder = sync_client_builder.timeout(timedelta(seconds=timeout))
        if arbitrary_headers:
            sync_client_builder = sync_client_builder.default_headers(arbitrary_headers)
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
        with sync_client_builder.build() as client:
            yield cls(client, bearer_token, bearer_auth)

    def _apply_bearer_auth(self, request_builder: SyncRequestBuilder) -> SyncRequestBuilder:
        if self._bearer_auth is not None:
            return request_builder.bearer_auth(self._bearer_auth())
        if self._bearer_token is not None:
            return request_builder.bearer_auth(self._bearer_token)
        return request_builder

    def _prepare_request(
        self, request_builder: SyncRequestBuilder, params: Params | None, headers: Headers | None
    ) -> SyncRequestBuilder:
        request_builder = self._apply_bearer_auth(request_builder)
        return _prepare(request_builder, params, headers)

    def _check_status(self, raw_response: RawSyncResponse) -> None:
        if raw_response.status < 400:
            return
        raise HTTPResponseError(raw_response.status, raw_response.bytes().to_bytes())

    def _decode_body(self, raw_response: RawSyncResponse, response_data_type: type[Data]) -> Data:
        _validate_response_data_type(response_data_type)
        if issubclass(response_data_type, bytes):
            return raw_response.bytes().to_bytes()
        if issubclass(response_data_type, dict):
            parsed = cast(dict[str, Any], raw_response.json())
            if is_typeddict(response_data_type):
                _validate_typed_dict(response_data_type, parsed)
            return response_data_type(parsed)
        if issubclass(response_data_type, Struct):
            return msgspec.json.decode(raw_response.bytes(), type=response_data_type)
        if issubclass(response_data_type, BaseModel):
            return response_data_type.model_validate_json(raw_response.bytes().to_bytes())
        raise TypeError(f"Unsupported response_data_type: {response_data_type!r}")

    def _parse(
        self,
        raw_response: RawSyncResponse,
        response_data_type: type[Data],
        *,
        error_for_status: bool,
    ) -> Data:
        if error_for_status:
            self._check_status(raw_response)
        return self._decode_body(raw_response, response_data_type)

    @overload
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    def get[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """GET `path` and decode the body as `response_data_type` (raw `bytes` by default).

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.
        """
        request_builder = self._prepare_request(self._client.get(path), params, headers)
        raw_response = _send_sync(request_builder.build())
        return self._parse(raw_response, response_data_type, error_for_status=error_for_status)

    @overload
    def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> Result[bytes]: ...
    @overload
    def get_result[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> Result[TData]: ...
    @overload
    def get_result[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[bytes, THeaders]: ...
    @overload
    def get_result[TData: Data, THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[TData, THeaders]: ...
    def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Data] = bytes,
        headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
    ) -> Result[Any, Any]:
        """Like `get`, but return a `Result` carrying the decoded body alongside the response
        status and headers. Pass `headers_type` to also get the headers parsed into
        `result.typed_headers`.
        """
        request_builder = self._prepare_request(self._client.get(path), params, headers)
        raw_response = _send_sync(request_builder.build())
        if error_for_status:
            self._check_status(raw_response)
        data = self._decode_body(raw_response, response_data_type)
        headers = dict(raw_response.headers)
        if headers_type is None:
            typed_headers = None
        else:
            typed_headers = _parse_typed_headers(headers, headers_type)
        return Result(data, raw_response.status, headers, typed_headers)

    def _sse_stream(
        self,
        path: str,
        params: Params | None,
        headers: Headers | None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None,
        id_type: type[Any] | UnionType | None,
        *,
        error_for_status: bool,
    ) -> Iterator[SSEEvent[Any, Any]]:
        request_builder = self._client.get(path).header("accept", "text/event-stream")
        request_builder = self._prepare_request(request_builder, params, headers)
        request = request_builder.build_streamed()
        try:
            with request as raw_response:
                if error_for_status:
                    self._check_status(raw_response)
                buffer = b""
                while True:
                    chunk = raw_response.body_reader.read_chunk()
                    if chunk is None:
                        return
                    buffer += bytes(chunk).replace(b"\r\n", b"\n")
                    while b"\n\n" in buffer:
                        record, buffer = buffer.split(b"\n\n", 1)
                        parsed_record = _parse_sse_record(record.decode())
                        if parsed_record is None:
                            continue
                        event_id = _coerce_sse_id(parsed_record.id, id_type)
                        if response_data_type is None:
                            yield SSEEvent(
                                id=event_id, event=parsed_record.event, data=parsed_record.data
                            )
                        else:
                            decoded = _decode_json_line(parsed_record.data, response_data_type)
                            yield SSEEvent(id=event_id, event=parsed_record.event, data=decoded)
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        id_type: None,
        error_for_status: bool = True,
    ) -> Iterator[SSEEvent[str, str | None]]: ...
    @overload
    def sse[TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        id_type: type[TId] = str,
        error_for_status: bool = True,
    ) -> Iterator[SSEEvent[str, TId]]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData] | TypeAdapter[TData] | Decoder[TData],
        id_type: None,
        error_for_status: bool = True,
    ) -> Iterator[SSEEvent[TData, str | None]]: ...
    @overload
    def sse[TData, TId](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData] | TypeAdapter[TData] | Decoder[TData],
        id_type: type[TId] = str,
        error_for_status: bool = True,
    ) -> Iterator[SSEEvent[TData, TId]]: ...
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        id_type: type[Any] | UnionType | None = str,
        error_for_status: bool = True,
    ) -> Iterator[SSEEvent[Any, Any]]:
        """Open `path` as a Server-Sent Events stream, yielding one `SSEEvent` per event.

        `response_data_type` decodes `.data` (a class, `TypeAdapter`, or msgspec `Decoder`);
        `.event`/`.id` are always populated regardless. `id_type` is the single knob for both
        whether `id` is required and what type it becomes: a bare type (default `str`) means
        required, that type unioned with `None` (or bare `None`) means optional.
        """
        return self._sse_stream(
            path,
            params,
            headers,
            response_data_type,
            id_type,
            error_for_status=error_for_status,
        )

    def _line_stream(
        self,
        request_builder: SyncRequestBuilder,
        params: Params | None,
        headers: Headers | None,
        json: Json | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None,
        *,
        error_for_status: bool,
    ) -> Iterator[Any]:
        request_builder = self._prepare_request(request_builder, params, headers)
        request_builder = _attach_body_sync(request_builder, json, form, content)
        request = request_builder.build_streamed()
        try:
            with request as raw_response:
                if error_for_status:
                    self._check_status(raw_response)
                if response_data_type is None:
                    while True:
                        chunk = raw_response.body_reader.read_chunk()
                        if chunk is None:
                            return
                        yield bytes(chunk)
                buffer = b""
                while True:
                    chunk = raw_response.body_reader.read_chunk()
                    if chunk is None:
                        break
                    buffer += bytes(chunk)
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line:
                            yield _decode_json_line(line.decode(), response_data_type)
                if buffer.strip():
                    yield _decode_json_line(buffer.decode(), response_data_type)
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> Iterator[bytes]: ...
    @overload
    def stream_get[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TLine] | TypeAdapter[TLine] | Decoder[TLine],
        error_for_status: bool = True,
    ) -> Iterator[TLine]: ...
    def stream_get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        error_for_status: bool = True,
    ) -> Iterator[Any]:
        """Stream GET `path`'s response as raw `bytes` chunks (unbuffered, safe for binary).

        Pass `response_data_type` to switch to newline-buffered NDJSON-style decoding instead —
        each complete line is parsed and decoded as its own value.
        """
        return self._line_stream(
            self._client.get(path),
            params,
            headers,
            None,
            None,
            None,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> Iterator[bytes]: ...
    @overload
    def stream_post[TLine](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TLine] | TypeAdapter[TLine] | Decoder[TLine],
        error_for_status: bool = True,
    ) -> Iterator[TLine]: ...
    def stream_post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        error_for_status: bool = True,
    ) -> Iterator[Any]:
        """Like `stream_get`, but POST a body first — same `json`/`form`/`content` options as
        `post` (at most one), same raw-bytes-by-default / NDJSON-via-`response_data_type` split.
        """
        return self._line_stream(
            self._client.post(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    def _download(
        self,
        path: str,
        dest: Path | None,
        params: Params | None,
        headers: Headers | None,
        *,
        error_for_status: bool,
    ) -> bytes | None:
        request_builder = self._prepare_request(self._client.get(path), params, headers)
        request = request_builder.build_streamed()
        try:
            with request as raw_response:
                if error_for_status:
                    self._check_status(raw_response)
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
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    def download(
        self,
        path: str,
        dest: None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    def download(
        self,
        path: str,
        dest: Path,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> None: ...
    def download(
        self,
        path: str,
        dest: Path | None = None,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes | None:
        """Download `path`'s response body, optimized for large objects (e.g. a presigned GET
        URL for a multi-GB file) — much lower peak memory than `get()` for big bodies.

        With no `dest`, streams the body into one pre-grown buffer and returns `bytes` — still
        O(body size) memory, but roughly a third of what `get()` uses (pyreqwest's own
        `.bytes()` does two extra full copies internally). Pass `dest` to stream straight to a
        file instead — memory then stays O(chunk size) regardless of how large the body is.

        Raises `HTTPResponseError` on a 4xx/5xx response unless `error_for_status=False`.
        """
        return self._download(path, dest, params, headers, error_for_status=error_for_status)

    def _send_with_body(
        self,
        request_builder: SyncRequestBuilder,
        params: Params | None,
        headers: Headers | None,
        json: Json | None,
        form: Form | None,
        content: str | bytes | None,
        response_data_type: type[Data],
        *,
        error_for_status: bool,
    ) -> Data:
        request_builder = self._prepare_request(request_builder, params, headers)
        request_builder = _attach_body_sync(request_builder, json, form, content)
        raw_response = _send_sync(request_builder.build())
        return self._parse(raw_response, response_data_type, error_for_status=error_for_status)

    @overload
    def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    def post[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    def post(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """POST to `path` with at most one of `json`/`form`/`content` (raises `ValueError` if
        more than one is given) and decode the response as `response_data_type`.
        """
        return self._send_with_body(
            self._client.post(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    def put[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    def put(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """PUT to `path`. Same body/decode rules as `post`."""
        return self._send_with_body(
            self._client.put(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    def patch[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    def patch(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        json: Json | None = None,
        form: Form | None = None,
        content: str | bytes | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """PATCH `path`. Same body/decode rules as `post`."""
        return self._send_with_body(
            self._client.patch(path),
            params,
            headers,
            json,
            form,
            content,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> bytes: ...
    @overload
    def delete[TData: Data](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    def delete(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        response_data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        """DELETE `path` and decode the response as `response_data_type`."""
        return self._send_with_body(
            self._client.delete(path),
            params,
            headers,
            None,
            None,
            None,
            response_data_type,
            error_for_status=error_for_status,
        )

    @overload
    def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> Result[None]: ...
    @overload
    def head[THeaders: TypedHeaders](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[None, THeaders]: ...
    def head(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
    ) -> Result[None, Any]:
        """HEAD `path` — headers-only, no body is ever decoded. Pass `headers_type` to get the
        response headers parsed into `result.typed_headers`.
        """
        request_builder = self._prepare_request(self._client.head(path), params, headers)
        raw_response = _send_sync(request_builder.build())
        if error_for_status:
            self._check_status(raw_response)
        response_headers = dict(raw_response.headers)
        typed_headers = (
            None if headers_type is None else _parse_typed_headers(response_headers, headers_type)
        )
        return Result(None, raw_response.status, response_headers, typed_headers)
