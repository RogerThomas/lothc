"""Multipart `form=` bodies, validated and built into pyreqwest's `FormBuilder`."""

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
# basedpyright proving it `Never` from a narrower declared type.
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
