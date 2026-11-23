"""Multipart `form=` bodies: validated and described part by part (`_form_parts`), which either
transport builds from. The pyreqwest transport's `FormBuilder` is built here; the native transport
hands the description to `lothc._native` (`_NativeForm`, `native/src/multipart.rs`). A file part
streamed from an iterator (`_ChunkStream`) or an async iterable (`_AsyncChunkStream`) is pulled
chunk by chunk as either transport writes the body, never held whole."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextvars import Context, copy_context
from dataclasses import dataclass
from io import BufferedIOBase
from json import dumps as _json_dumps
from mimetypes import guess_type as _guess_mime_type
from pathlib import Path
from typing import (
    Any,
    Protocol,
    Self,
    cast,
    runtime_checkable,
)

from pyreqwest.multipart import FormBuilder, PartBuilder

from ._compat import (
    BaseModel,
    Struct,
    msgspec,
)
from ._encode import _pydantic_by_alias
from ._types import Form, JSONPayload


# What a `match` takes for a streamed file part's content: an iterator, or an async iterable, of
# anything (each chunk is checked as it's pulled, `_checked_chunk`). As `case Iterator()` would
# take it, but with its items known (`object`), where `collections.abc`'s leave them unknown.
@runtime_checkable
class _ChunkSource(Protocol):
    def __iter__(self) -> Self: ...
    def __next__(self) -> object: ...


@runtime_checkable
class _AsyncChunkSource(Protocol):
    def __aiter__(self) -> AsyncIterator[object]: ...


def _checked_chunk(name: str, chunk: object) -> bytes:
    """A chunk of streamed form part `name`, which must be `bytes` on either transport, as the
    type says (pyreqwest's own stream would also take a `bytearray` or `memoryview`): anything
    else raises from the send, where the stream yielded it.

    >>> _checked_chunk("f", b"a")
    b'a'
    >>> _checked_chunk("f", "a")
    Traceback (most recent call last):
    ...
    TypeError: Streamed form part 'f' yielded a str chunk; each chunk must be bytes
    """
    if not isinstance(chunk, bytes):
        raise TypeError(
            f"Streamed form part {name!r} yielded a {type(chunk).__name__} chunk; each chunk "
            "must be bytes"
        )
    return chunk


@dataclass(slots=True)
class _ChunkStream:
    """A file part streamed from an iterator, which either transport pulls a chunk at a time as
    it writes the body (pyreqwest's `PartBuilder.from_stream`, the native transport's
    `multipart.rs`), each checked to be `bytes`. An exception the iterator raises comes out of
    the send as it was, as pyreqwest re-raises it."""

    _name: str
    _chunks: Iterator[object]

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> bytes:
        return _checked_chunk(self._name, next(self._chunks))


@dataclass(slots=True)
class _AsyncChunkStream:
    """`_ChunkStream` for an async iterable, on an async client only. pyreqwest iterates it; the
    native transport `pull`s each chunk as a task on the loop, run in `_context` (a copy of the
    caller's, taken when the part was described), as pyreqwest runs each of its pulls in the
    context it took when the part was built."""

    _name: str
    _chunks: AsyncIterator[object]
    _context: Context

    async def _next_chunk(self) -> bytes | None:
        try:
            chunk = await anext(self._chunks)
        except StopAsyncIteration:
            return None
        return _checked_chunk(self._name, chunk)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        return _checked_chunk(self._name, await anext(self._chunks))

    def pull(
        self, ready: Callable[[asyncio.Task[bytes | None]], object]
    ) -> asyncio.Task[bytes | None]:
        """The next chunk (`None` once there are no more), as a task on the running loop that
        calls `ready` once it's done: how the native transport reads one without blocking."""
        task = asyncio.get_running_loop().create_task(self._next_chunk(), context=self._context)
        task.add_done_callback(ready)
        return task


@dataclass(slots=True)
class _FormPart:
    """One part of a multipart body, as reqwest's `multipart::Part` is made from it: `content` a
    `str` is `Part::text`, `bytes` `Part::bytes`, a `Path` `Part::file` (which opens the file and
    takes its name, and a type guessed from its extension), and a `_ChunkStream` or
    `_AsyncChunkStream` `Part::stream`, whose length is unknown, so the whole body goes out
    chunked, with no `Content-Length`; then `filename` and `mime` are applied where set. Each
    transport builds a part when `_form_parts` reaches it, so an error (a missing file, an
    invalid name or type) surfaces at the same part as on pyreqwest."""

    name: str
    content: str | bytes | Path | _ChunkStream | _AsyncChunkStream
    filename: str | None = None
    mime: str | None = None


def _guess_form_part_mime(
    filename: str | None, *, infer_mime_type_from_file_extension: bool
) -> str | None:
    if not infer_mime_type_from_file_extension or filename is None:
        return None
    return _guess_mime_type(filename)[0]


def _form_scalar_text(value: int | float) -> str:
    """A numeric form field's text. A `bool` (an `int` subclass) goes out as `"true"`/`"false"`,
    matching how `params=` encodes one, rather than Python's own `"True"`/`"False"`.

    >>> [_form_scalar_text(v) for v in (True, False, 7, 1.5)]
    ['true', 'false', '7', '1.5']
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode_json_form_part(value: list[Any] | JSONPayload) -> bytes:
    if isinstance(value, BaseModel):
        return _json_dumps(
            value.model_dump(mode="json", by_alias=_pydantic_by_alias(value))
        ).encode()
    if isinstance(value, Struct):
        return msgspec.json.encode(value)
    return _json_dumps(value).encode()


# Mirrors _form_value_parts's own branch order (not _FormRepeat's member order) so a value's kind
# here is always the branch it will actually take. Returns None for anything unclassifiable and
# leaves the reporting to _form_value_parts's `case _`, which names the offending value.
def _form_value_kind(value: object) -> str | None:
    match value:
        case str() | int() | float():
            return "text"
        case bytes():
            return "bytes"
        case list():
            return "json"
        case (
            (str(), bytes() | Path() | BufferedIOBase() | _ChunkSource() | _AsyncChunkSource())
            | (
                str(),
                bytes() | Path() | BufferedIOBase() | _ChunkSource() | _AsyncChunkSource(),
                str(),
            )
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


def _buffered_io_filename(value: BufferedIOBase) -> str | None:
    raw_name = getattr(value, "name", None)
    return Path(raw_name).name if isinstance(raw_name, str) else None


# What a file part's content goes to a transport as. A buffered stream is read here, whole
# (`bytes(...)`, so one whose `read()` gives a `bytearray`/`memoryview` works on both transports;
# no copy for `bytes`), before the iterator case, since it's an iterator too. A stream of chunks
# is only wrapped: nothing is read from it until the body is written.
def _file_part_content(
    name: str,
    content: bytes | Path | BufferedIOBase | _ChunkSource | _AsyncChunkSource,
    *,
    async_client: bool,
) -> bytes | Path | _ChunkStream | _AsyncChunkStream:
    match content:
        case BufferedIOBase():
            return bytes(content.read())
        case bytes() | Path():
            return content
        case _ChunkSource():
            return _ChunkStream(name, content)
        case _:
            if not async_client:
                raise TypeError(
                    f"Unsupported form value for {name!r}: a SyncHTTPClient streams a file part "
                    f"from an iterator, not an async iterable ({type(content).__name__})"
                )
            return _AsyncChunkStream(name, aiter(content), copy_context())


# `explicit_mime` always wins over inference; `filename` is `None` for the Path-override shape,
# whose part keeps the name its own path gives it.
def _form_file_part(
    name: str,
    content: bytes | Path | BufferedIOBase | _ChunkSource | _AsyncChunkSource,
    filename: str | None,
    explicit_mime: str | None,
    *,
    infer_mime_type_from_file_extension: bool,
    async_client: bool,
) -> _FormPart:
    body = _file_part_content(name, content, async_client=async_client)
    mime = explicit_mime or _guess_form_part_mime(
        filename, infer_mime_type_from_file_extension=infer_mime_type_from_file_extension
    )
    return _FormPart(name, body, filename, mime)


# `value` is typed `object`, not `Form`'s value union — nothing at runtime stops a caller
# bypassing the type checker, so the `case _` fallback below must stay reachable rather than
# basedpyright proving it `Never` from a narrower declared type. A generator, so a part is only
# described once the one before it is built: a missing file still fails before a later part's
# `TypeError`, as when pyreqwest's builder was driven value by value. `async_client` says whether
# a file may be streamed from an async iterable, which only an async client can pull.
def _form_value_parts(
    name: str, value: object, *, infer_mime_type_from_file_extension: bool, async_client: bool
) -> Iterator[_FormPart]:
    infer = infer_mime_type_from_file_extension
    match value:
        case str():
            yield _FormPart(name, value)
        case int() | float():
            yield _FormPart(name, _form_scalar_text(value))
        case bytes():
            yield _FormPart(name, value)
        # Must come before the file patterns below: a sequence pattern matches a `list` just as
        # happily as a `tuple`, so ["a.png", b"..."] would otherwise be read as a file when a list
        # always means "JSON-encode me as one part" (docs/verbs.md) — never a file, never a repeat.
        case list():
            body = _encode_json_form_part(cast(list[Any], value))
            yield _FormPart(name, body, None, "application/json")
        case (
            str() as filename,
            bytes() | Path() | BufferedIOBase() | _ChunkSource() | _AsyncChunkSource() as content,
        ):
            yield _form_file_part(
                name,
                content,
                filename,
                None,
                infer_mime_type_from_file_extension=infer,
                async_client=async_client,
            )
        case (
            str() as filename,
            bytes() | Path() | BufferedIOBase() | _ChunkSource() | _AsyncChunkSource() as content,
            str() as mime,
        ):
            yield _form_file_part(
                name,
                content,
                filename,
                mime,
                infer_mime_type_from_file_extension=infer,
                async_client=async_client,
            )
        case (Path() as content_path, str() as mime):
            yield _form_file_part(
                name,
                content_path,
                None,
                mime,
                infer_mime_type_from_file_extension=infer,
                async_client=async_client,
            )
        case Path():
            yield _form_file_part(
                name,
                value,
                value.name,
                None,
                infer_mime_type_from_file_extension=infer,
                async_client=async_client,
            )
        case BufferedIOBase():
            filename = _buffered_io_filename(value)
            yield _form_file_part(
                name,
                value,
                filename,
                None,
                infer_mime_type_from_file_extension=infer,
                async_client=async_client,
            )
        case dict() | BaseModel() | Struct():
            body = _encode_json_form_part(cast("JSONPayload", value))
            yield _FormPart(name, body, None, "application/json")
        case tuple():
            items = cast("tuple[object, ...]", value)
            _check_form_repeat(name, items)
            for item in items:
                yield from _form_value_parts(
                    name,
                    item,
                    infer_mime_type_from_file_extension=infer,
                    async_client=async_client,
                )
        case _:
            raise TypeError(
                f"Unsupported form value for {name!r}: {value!r} ({type(value).__name__})"
            )


def _form_parts(
    form: Form, *, infer_mime_type_from_file_extension: bool, async_client: bool
) -> Iterator[_FormPart]:
    """Every part of `form`, in order: a tuple value repeats its name once per element. On a
    sync client (not `async_client`), a file streamed from an async iterable is a `TypeError`."""
    for name, value in form.items():
        yield from _form_value_parts(
            name,
            value,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
            async_client=async_client,
        )


def _finish_part(part: PartBuilder, form_part: _FormPart) -> PartBuilder:
    if form_part.filename is not None:
        part = part.file_name(form_part.filename)
    return part if form_part.mime is None else part.mime(form_part.mime)


async def _build_file_part(content: bytes | Path | _ChunkStream | _AsyncChunkStream) -> PartBuilder:
    if isinstance(content, Path):
        return await PartBuilder.from_file(content)
    if isinstance(content, bytes):
        return PartBuilder.from_bytes(content)
    return PartBuilder.from_stream(content)


async def _build_form(form: Form, *, infer_mime_type_from_file_extension: bool) -> FormBuilder:
    form_builder = FormBuilder()
    parts = _form_parts(
        form,
        infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        async_client=True,
    )
    for form_part in parts:
        if isinstance(form_part.content, str):
            form_builder = form_builder.text(form_part.name, form_part.content)
        else:
            part = _finish_part(await _build_file_part(form_part.content), form_part)
            form_builder = form_builder.part(form_part.name, part)
    return form_builder


def _build_sync_file_part(content: bytes | Path | _ChunkStream | _AsyncChunkStream) -> PartBuilder:
    if isinstance(content, Path):
        return PartBuilder.from_sync_file(content)
    if isinstance(content, bytes):
        return PartBuilder.from_bytes(content)
    return PartBuilder.from_stream(content)


def _build_sync_form(form: Form, *, infer_mime_type_from_file_extension: bool) -> FormBuilder:
    form_builder = FormBuilder()
    parts = _form_parts(
        form,
        infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        async_client=False,
    )
    for form_part in parts:
        if isinstance(form_part.content, str):
            form_builder = form_builder.text(form_part.name, form_part.content)
        else:
            part = _finish_part(_build_sync_file_part(form_part.content), form_part)
            form_builder = form_builder.part(form_part.name, part)
    return form_builder
