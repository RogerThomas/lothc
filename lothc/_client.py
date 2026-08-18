import os
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
from datetime import timedelta
from io import BufferedIOBase
from json import loads as _json_loads
from pathlib import Path
from typing import Any, ClassVar, Protocol, Self, cast, is_typeddict, overload

from pyreqwest.client import Client, ClientBuilder, SyncClient, SyncClientBuilder
from pyreqwest.exceptions import NetworkError as PyreqwestNetworkError
from pyreqwest.exceptions import RequestTimeoutError as PyreqwestRequestTimeoutError
from pyreqwest.exceptions import TransportError as PyreqwestTransportError
from pyreqwest.multipart import FormBuilder, PartBuilder
from pyreqwest.request import (
    BaseRequestBuilder,
    ConsumedRequest,
    RequestBuilder,
    SyncConsumedRequest,
    SyncRequestBuilder,
)
from pyreqwest.response import Response as RawResponse
from pyreqwest.response import SyncResponse as RawSyncResponse

from ._compat import BaseModel, Decoder, Struct, TypeAdapter, msgspec, typeguard


class JSON(dict[str, Any]):
    """A JSON object, usable as a data_type without pydantic or msgspec."""


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


class ResponseError(Exception):
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body_start = body[:100]
        snippet = self.body_start.decode(errors="replace")
        truncation_marker = "…" if len(body) > 100 else ""
        super().__init__(f"Request failed with status {status}: {snippet}{truncation_marker}")


class TransportError(Exception):
    pass


class ConnectionError(TransportError):  # noqa: A001 — deliberate, matches requests/httpx precedent
    pass


class TimeoutError(TransportError):  # noqa: A001 — deliberate, matches requests/httpx precedent
    pass


def _translate_transport_error(error: PyreqwestTransportError) -> TransportError:
    if isinstance(error, PyreqwestRequestTimeoutError):
        return TimeoutError(str(error))
    if isinstance(error, PyreqwestNetworkError):
        return ConnectionError(str(error))
    return TransportError(str(error))


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


@dataclass
class SSEEvent:
    data: str
    event: str = "message"
    id: str | None = None


def _parse_sse_record(record: str) -> SSEEvent | None:
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
    return SSEEvent("\n".join(data_lines), event, event_id)


def _validate_typed_dict(data_type: type[Any], value: dict[str, Any]) -> None:
    if typeguard is None:
        if not os.environ.get("LOTHC_SUPPRESS_TYPEGUARD_WARNING"):
            warnings.warn(
                f"{data_type.__name__} is a TypedDict but typeguard is not installed; "
                "skipping runtime validation. Install typeguard to validate it, or set "
                "LOTHC_SUPPRESS_TYPEGUARD_WARNING=1 to silence this warning.",
                stacklevel=3,
            )
        return
    typeguard.check_type(value, data_type)


def _validate_data_type(data_type: object) -> None:
    if not isinstance(data_type, type):
        raise TypeError(f"data_type must be a class, got {data_type!r}")
    if data_type is dict:
        raise TypeError(
            "data_type=dict is not supported; use lothc.JSON, a TypedDict, "
            "a BaseModel subclass, or a Struct subclass instead"
        )


# Return type is whatever data_type is — genuinely dynamic, can't state it statically.
def _decode_sse_data(data: str, data_type: type[Any] | TypeAdapter[Any] | Decoder[Any]) -> Any:  # noqa: ANN401
    if isinstance(data_type, TypeAdapter):
        return data_type.validate_json(data)
    if isinstance(data_type, Decoder):
        return data_type.decode(data)
    _validate_data_type(data_type)
    if issubclass(data_type, dict):
        dict_type = cast("type[dict[str, Any]]", data_type)
        parsed = cast(dict[str, Any], _json_loads(data))
        if is_typeddict(dict_type):
            _validate_typed_dict(dict_type, parsed)
        return dict_type(parsed)
    if issubclass(data_type, Struct):
        return msgspec.json.decode(data.encode(), type=data_type)
    if issubclass(data_type, BaseModel):
        return data_type.model_validate_json(data)
    raise TypeError(f"Unsupported SSE data_type: {data_type!r}")


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


def _parse_typed_headers(headers: dict[str, str], headers_type: type[TypedHeaders]) -> TypedHeaders:
    normalized = {name.lower().replace("-", "_"): value for name, value in headers.items()}
    if issubclass(headers_type, Struct):
        return msgspec.convert(normalized, type=headers_type, strict=False)
    return headers_type.model_validate(normalized)


@dataclass
class Result[TData, THeaders: TypedHeaders | None = None]:
    data: TData
    status: int
    headers: dict[str, str]
    typed_headers: THeaders


@dataclass
class HTTPClient:
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
    ) -> AsyncGenerator[Self]:
        if bearer_token is not None and bearer_auth is not None:
            raise ValueError("Provide at most one of 'bearer_token' or 'bearer_auth'")
        pyreqwest_client_builder = ClientBuilder()
        if timeout is not None:
            pyreqwest_client_builder = pyreqwest_client_builder.timeout(timedelta(seconds=timeout))
        if arbitrary_headers:
            pyreqwest_client_builder = pyreqwest_client_builder.default_headers(arbitrary_headers)
        if base_url:
            pyreqwest_client_builder = pyreqwest_client_builder.base_url(base_url)
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
        raise ResponseError(raw_response.status, bytes(await raw_response.bytes()))

    async def _decode_body(self, raw_response: RawResponse, data_type: type[Data]) -> Data:
        _validate_data_type(data_type)
        if issubclass(data_type, bytes):
            return bytes(await raw_response.bytes())
        if issubclass(data_type, dict):
            parsed = cast(dict[str, Any], await raw_response.json())
            if is_typeddict(data_type):
                _validate_typed_dict(data_type, parsed)
            return data_type(parsed)
        if issubclass(data_type, Struct):
            return msgspec.json.decode(bytes(await raw_response.bytes()), type=data_type)
        if issubclass(data_type, BaseModel):
            return data_type.model_validate(await raw_response.json())
        raise TypeError(f"Unsupported data_type: {data_type!r}")

    async def _parse(
        self, raw_response: RawResponse, data_type: type[Data], *, error_for_status: bool
    ) -> Data:
        if error_for_status:
            await self._check_status(raw_response)
        return await self._decode_body(raw_response, data_type)

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
        data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    async def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        request_builder = await self._prepare_request(self._client.get(path), params, headers)
        raw_response = await _send(request_builder.build())
        return await self._parse(raw_response, data_type, error_for_status=error_for_status)

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
        data_type: type[TData],
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
        data_type: type[TData],
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[TData, THeaders]: ...
    async def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[Data] = bytes,
        headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
    ) -> Result[Any, Any]:
        request_builder = await self._prepare_request(self._client.get(path), params, headers)
        raw_response = await _send(request_builder.build())
        if error_for_status:
            await self._check_status(raw_response)
        data = await self._decode_body(raw_response, data_type)
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
        data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None,
        *,
        error_for_status: bool,
    ) -> AsyncIterator[Any]:
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
                        parsed_event = _parse_sse_record(record.decode())
                        if parsed_event is None:
                            continue
                        if data_type is None:
                            yield parsed_event
                        else:
                            yield _decode_sse_data(parsed_event.data, data_type)
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> AsyncIterator[SSEEvent]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[TData] | TypeAdapter[TData] | Decoder[TData],
        error_for_status: bool = True,
    ) -> AsyncIterator[TData]: ...
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        error_for_status: bool = True,
    ) -> AsyncIterator[Any]:
        return self._sse_stream(path, params, headers, data_type, error_for_status=error_for_status)

    async def _send_with_body(
        self,
        request_builder: RequestBuilder,
        params: Params | None,
        headers: Headers | None,
        json: Json | None,
        form: Form | None,
        content: str | bytes | None,
        data_type: type[Data],
        *,
        error_for_status: bool,
    ) -> Data:
        request_builder = await self._prepare_request(request_builder, params, headers)
        provided_bodies = [body for body in (json, form, content) if body is not None]
        if len(provided_bodies) > 1:
            raise ValueError("Provide at most one of 'json', 'form' or 'content'")
        if isinstance(json, BaseModel):
            request_builder = request_builder.body_json(json.model_dump(mode="json"))
        elif isinstance(json, Struct):
            request_builder = request_builder.body_json(msgspec.to_builtins(json))
        elif json is not None:
            request_builder = request_builder.body_json(json)
        elif form is not None:
            request_builder = request_builder.multipart(await _build_form(form))
        elif isinstance(content, str):
            request_builder = request_builder.body_text(content)
        elif content is not None:
            request_builder = request_builder.body_bytes(content)
        raw_response = await _send(request_builder.build())
        return await self._parse(raw_response, data_type, error_for_status=error_for_status)

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
        data_type: type[TData],
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
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        return await self._send_with_body(
            self._client.post(path),
            params,
            headers,
            json,
            form,
            content,
            data_type,
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
        data_type: type[TData],
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
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        return await self._send_with_body(
            self._client.put(path),
            params,
            headers,
            json,
            form,
            content,
            data_type,
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
        data_type: type[TData],
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
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        return await self._send_with_body(
            self._client.patch(path),
            params,
            headers,
            json,
            form,
            content,
            data_type,
            error_for_status=error_for_status,
        )


@dataclass
class SyncHTTPClient:
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
    ) -> Generator[Self]:
        if bearer_token is not None and bearer_auth is not None:
            raise ValueError("Provide at most one of 'bearer_token' or 'bearer_auth'")
        sync_client_builder = SyncClientBuilder()
        if timeout is not None:
            sync_client_builder = sync_client_builder.timeout(timedelta(seconds=timeout))
        if arbitrary_headers:
            sync_client_builder = sync_client_builder.default_headers(arbitrary_headers)
        if base_url:
            sync_client_builder = sync_client_builder.base_url(base_url)
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
        raise ResponseError(raw_response.status, bytes(raw_response.bytes()))

    def _decode_body(self, raw_response: RawSyncResponse, data_type: type[Data]) -> Data:
        _validate_data_type(data_type)
        if issubclass(data_type, bytes):
            return bytes(raw_response.bytes())
        if issubclass(data_type, dict):
            parsed = cast(dict[str, Any], raw_response.json())
            if is_typeddict(data_type):
                _validate_typed_dict(data_type, parsed)
            return data_type(parsed)
        if issubclass(data_type, Struct):
            return msgspec.json.decode(bytes(raw_response.bytes()), type=data_type)
        if issubclass(data_type, BaseModel):
            return data_type.model_validate(raw_response.json())
        raise TypeError(f"Unsupported data_type: {data_type!r}")

    def _parse(
        self, raw_response: RawSyncResponse, data_type: type[Data], *, error_for_status: bool
    ) -> Data:
        if error_for_status:
            self._check_status(raw_response)
        return self._decode_body(raw_response, data_type)

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
        data_type: type[TData],
        error_for_status: bool = True,
    ) -> TData: ...
    def get(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        request_builder = self._prepare_request(self._client.get(path), params, headers)
        raw_response = _send_sync(request_builder.build())
        return self._parse(raw_response, data_type, error_for_status=error_for_status)

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
        data_type: type[TData],
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
        data_type: type[TData],
        headers_type: type[THeaders],
        error_for_status: bool = True,
    ) -> Result[TData, THeaders]: ...
    def get_result(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[Data] = bytes,
        headers_type: type[TypedHeaders] | None = None,
        error_for_status: bool = True,
    ) -> Result[Any, Any]:
        request_builder = self._prepare_request(self._client.get(path), params, headers)
        raw_response = _send_sync(request_builder.build())
        if error_for_status:
            self._check_status(raw_response)
        data = self._decode_body(raw_response, data_type)
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
        data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None,
        *,
        error_for_status: bool,
    ) -> Iterator[Any]:
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
                        parsed_event = _parse_sse_record(record.decode())
                        if parsed_event is None:
                            continue
                        if data_type is None:
                            yield parsed_event
                        else:
                            yield _decode_sse_data(parsed_event.data, data_type)
        except PyreqwestTransportError as error:
            raise _translate_transport_error(error) from error

    @overload
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        error_for_status: bool = True,
    ) -> Iterator[SSEEvent]: ...
    @overload
    def sse[TData](
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[TData] | TypeAdapter[TData] | Decoder[TData],
        error_for_status: bool = True,
    ) -> Iterator[TData]: ...
    def sse(
        self,
        path: str,
        *,
        params: Params | None = None,
        headers: Headers | None = None,
        data_type: type[Any] | TypeAdapter[Any] | Decoder[Any] | None = None,
        error_for_status: bool = True,
    ) -> Iterator[Any]:
        return self._sse_stream(path, params, headers, data_type, error_for_status=error_for_status)

    def _send_with_body(
        self,
        request_builder: SyncRequestBuilder,
        params: Params | None,
        headers: Headers | None,
        json: Json | None,
        form: Form | None,
        content: str | bytes | None,
        data_type: type[Data],
        *,
        error_for_status: bool,
    ) -> Data:
        request_builder = self._prepare_request(request_builder, params, headers)
        provided_bodies = [body for body in (json, form, content) if body is not None]
        if len(provided_bodies) > 1:
            raise ValueError("Provide at most one of 'json', 'form' or 'content'")
        if isinstance(json, BaseModel):
            request_builder = request_builder.body_json(json.model_dump(mode="json"))
        elif isinstance(json, Struct):
            request_builder = request_builder.body_json(msgspec.to_builtins(json))
        elif json is not None:
            request_builder = request_builder.body_json(json)
        elif form is not None:
            request_builder = request_builder.multipart(_build_sync_form(form))
        elif isinstance(content, str):
            request_builder = request_builder.body_text(content)
        elif content is not None:
            request_builder = request_builder.body_bytes(content)
        raw_response = _send_sync(request_builder.build())
        return self._parse(raw_response, data_type, error_for_status=error_for_status)

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
        data_type: type[TData],
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
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        return self._send_with_body(
            self._client.post(path),
            params,
            headers,
            json,
            form,
            content,
            data_type,
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
        data_type: type[TData],
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
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        return self._send_with_body(
            self._client.put(path),
            params,
            headers,
            json,
            form,
            content,
            data_type,
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
        data_type: type[TData],
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
        data_type: type[Data] = bytes,
        error_for_status: bool = True,
    ) -> Data:
        return self._send_with_body(
            self._client.patch(path),
            params,
            headers,
            json,
            form,
            content,
            data_type,
            error_for_status=error_for_status,
        )
