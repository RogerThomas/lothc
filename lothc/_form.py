"""Multipart `form=` bodies: validated and described part by part (`_form_parts`), which either
transport builds from. The pyreqwest transport's `FormBuilder` is built here; the native transport
hands the description to `lothc._native` (`_NativeForm`, `native/src/multipart.rs`)."""

from collections.abc import Iterator
from dataclasses import dataclass
from io import BufferedIOBase
from json import dumps as _json_dumps
from mimetypes import guess_type as _guess_mime_type
from pathlib import Path
from typing import (
    Any,
    cast,
)

from pyreqwest.multipart import FormBuilder, PartBuilder

from ._compat import (
    BaseModel,
    Struct,
    msgspec,
)
from ._encode import _pydantic_by_alias
from ._types import Form, JSONPayload


@dataclass(slots=True)
class _FormPart:
    """One part of a multipart body, as reqwest's `multipart::Part` is made from it: `content` a
    `str` is `Part::text`, `bytes` `Part::bytes`, and a `Path` `Part::file` (which opens the file
    and takes its name, and a type guessed from its extension), then `filename` and `mime` are
    applied where set. Each transport builds a part when `_form_parts` reaches it, so an error
    (a missing file, an invalid name or type) surfaces at the same part as on pyreqwest."""

    name: str
    content: str | bytes | Path
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


def _buffered_io_filename(value: BufferedIOBase) -> str | None:
    raw_name = getattr(value, "name", None)
    return Path(raw_name).name if isinstance(raw_name, str) else None


# `explicit_mime` always wins over inference; `filename` is `None` for the Path-override shape,
# whose part keeps the name its own path gives it. A buffered stream is read here, whole.
def _form_file_part(
    name: str,
    content: bytes | Path | BufferedIOBase,
    filename: str | None,
    explicit_mime: str | None,
    *,
    infer_mime_type_from_file_extension: bool,
) -> _FormPart:
    # `bytes(...)`, so a stream whose `read()` gives a `bytearray`/`memoryview` works on both
    # transports (pyreqwest takes any buffer); it's the same object, no copy, for `bytes`.
    body = bytes(content.read()) if isinstance(content, BufferedIOBase) else content
    mime = explicit_mime or _guess_form_part_mime(
        filename, infer_mime_type_from_file_extension=infer_mime_type_from_file_extension
    )
    return _FormPart(name, body, filename, mime)


# `value` is typed `object`, not `Form`'s value union — nothing at runtime stops a caller
# bypassing the type checker, so the `case _` fallback below must stay reachable rather than
# basedpyright proving it `Never` from a narrower declared type. A generator, so a part is only
# described once the one before it is built: a missing file still fails before a later part's
# `TypeError`, as when pyreqwest's builder was driven value by value.
def _form_value_parts(
    name: str, value: object, *, infer_mime_type_from_file_extension: bool
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
        case (str() as filename, bytes() | Path() | BufferedIOBase() as content):
            yield _form_file_part(
                name, content, filename, None, infer_mime_type_from_file_extension=infer
            )
        case (str() as filename, bytes() | Path() | BufferedIOBase() as content, str() as mime):
            yield _form_file_part(
                name, content, filename, mime, infer_mime_type_from_file_extension=infer
            )
        case (Path() as content_path, str() as mime):
            yield _form_file_part(
                name, content_path, None, mime, infer_mime_type_from_file_extension=infer
            )
        case Path():
            yield _form_file_part(
                name, value, value.name, None, infer_mime_type_from_file_extension=infer
            )
        case BufferedIOBase():
            filename = _buffered_io_filename(value)
            yield _form_file_part(
                name, value, filename, None, infer_mime_type_from_file_extension=infer
            )
        case dict() | BaseModel() | Struct():
            body = _encode_json_form_part(cast("JSONPayload", value))
            yield _FormPart(name, body, None, "application/json")
        case tuple():
            items = cast("tuple[object, ...]", value)
            _check_form_repeat(name, items)
            for item in items:
                yield from _form_value_parts(name, item, infer_mime_type_from_file_extension=infer)
        case _:
            raise TypeError(
                f"Unsupported form value for {name!r}: {value!r} ({type(value).__name__})"
            )


def _form_parts(form: Form, *, infer_mime_type_from_file_extension: bool) -> Iterator[_FormPart]:
    """Every part of `form`, in order: a tuple value repeats its name once per element."""
    for name, value in form.items():
        yield from _form_value_parts(
            name,
            value,
            infer_mime_type_from_file_extension=infer_mime_type_from_file_extension,
        )


def _finish_part(part: PartBuilder, form_part: _FormPart) -> PartBuilder:
    if form_part.filename is not None:
        part = part.file_name(form_part.filename)
    return part if form_part.mime is None else part.mime(form_part.mime)


async def _build_file_part(content: bytes | Path) -> PartBuilder:
    if isinstance(content, Path):
        return await PartBuilder.from_file(content)
    return PartBuilder.from_bytes(content)


async def _build_form(form: Form, *, infer_mime_type_from_file_extension: bool) -> FormBuilder:
    form_builder = FormBuilder()
    parts = _form_parts(
        form, infer_mime_type_from_file_extension=infer_mime_type_from_file_extension
    )
    for form_part in parts:
        if isinstance(form_part.content, str):
            form_builder = form_builder.text(form_part.name, form_part.content)
        else:
            part = _finish_part(await _build_file_part(form_part.content), form_part)
            form_builder = form_builder.part(form_part.name, part)
    return form_builder


def _build_sync_file_part(content: bytes | Path) -> PartBuilder:
    if isinstance(content, Path):
        return PartBuilder.from_sync_file(content)
    return PartBuilder.from_bytes(content)


def _build_sync_form(form: Form, *, infer_mime_type_from_file_extension: bool) -> FormBuilder:
    form_builder = FormBuilder()
    parts = _form_parts(
        form, infer_mime_type_from_file_extension=infer_mime_type_from_file_extension
    )
    for form_part in parts:
        if isinstance(form_part.content, str):
            form_builder = form_builder.text(form_part.name, form_part.content)
        else:
            part = _finish_part(_build_sync_file_part(form_part.content), form_part)
            form_builder = form_builder.part(form_part.name, part)
    return form_builder
