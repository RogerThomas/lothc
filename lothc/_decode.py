"""Turning an `into=` type form into one cached decoder, and decoding bodies and lines with it."""

from collections import defaultdict
from collections.abc import (
    Buffer,
    Mapping,
)
from dataclasses import dataclass, fields, is_dataclass
from json import loads as _json_loads
from typing import (
    Annotated,
    Any,
    TypeAliasType,
    TypeIs,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from typing_extensions import TypeForm, is_typeddict

from ._compat import (
    BaseModel,
    Decoder,
    DecoderTyping,
    Struct,
    TypeAdapter,
    TypeAdapterTyping,
    msgspec,
    msgspec_installed,
    pydantic_installed,
)


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
        " with `into=list[...]` (or any other type matching the body) instead."
    )


type _JSONInput = str | bytes | bytearray


@dataclass(slots=True)
class _JSONObjectDecoder:
    """`into=dict`/`dict[str, Any]`: stdlib `json`, so it works with neither extra installed."""

    def decode(self, data: _JSONInput) -> dict[str, Any]:
        # stdlib `json`, not pyreqwest's `.json()`: that raises pyreqwest's own
        # `JSONDecodeError`, and reading the body separately is what lets a body cut short be
        # told apart from one that just isn't valid JSON.
        return _require_json_object(_json_loads(data))


@dataclass(slots=True)
class _DictSubclassDecoder:
    """`into=` a bare `dict` subclass (`OrderedDict`, `defaultdict`, a caller's own): stdlib
    `json` like `dict`, then built as that subclass, so it needs no extra. msgspec would reject
    most of them (it only builds a plain `dict`)."""

    _form: type[dict[Any, Any]]

    def decode(self, data: _JSONInput) -> dict[Any, Any]:
        parsed = _require_json_object(_json_loads(data))
        if issubclass(self._form, defaultdict):
            # `defaultdict`'s first positional argument is its `default_factory`.
            return self._form(None, parsed)
        return self._form(parsed)


@dataclass(slots=True)
class _TextDecoder:
    """`into=str`: the body (or NDJSON line, or SSE `data`) as UTF-8 text, never JSON-parsed,
    mirroring `bytes` as the raw bytes. Strict, so invalid UTF-8 raises `UnicodeDecodeError`, a
    `ValueError` like every other decode failure."""

    def decode(self, data: Buffer | str) -> str:
        return data if isinstance(data, str) else str(data, "utf-8")


@dataclass(slots=True)
class _SSEBytesDecoder:
    """`sse(into=bytes)`: an event's `data`, which arrives as text, back as its UTF-8 bytes.
    Only `sse()` uses it; everywhere else `bytes` is no decoder at all (`None`)."""

    def decode(self, data: _JSONInput) -> bytes:
        return data.encode() if isinstance(data, str) else bytes(data)


type _Decoder = (
    Decoder[Any]
    | TypeAdapter[Any]
    | _JSONObjectDecoder
    | _DictSubclassDecoder
    | _TextDecoder
    | _SSEBytesDecoder
)
type _Into = TypeForm[Any] | TypeAdapterTyping[Any] | DecoderTyping[Any] | None

# One decoder per `into` form, shared by both clients and every verb. Building one is the
# expensive part (measured: msgspec `Decoder(list[User])` ~1.5us, pydantic `TypeAdapter` ~31us),
# a hit is one dict lookup. Keyed by the form itself: `list[User]` written at two call sites is
# two equal, equally-hashed objects, so it's one entry. Forms come from source code, not data, but
# one can still be a new key per call: a form whose metadata compares by identity (see
# `_FormTraits`) is never stored, and `_decoders_max_size` stops anything that rule misses (an
# inline lambda validator) growing it forever. Once full, new forms decode uncached; nothing is
# evicted, so the forms cached first, the hot ones, stay cached.
_decoders: dict[object, _Decoder] = {}
_decoders_max_size = 4096


@dataclass(slots=True)
class _FormTraits:
    """What `_decoder_for` learns from walking an `into` form before building its decoder.

    `model_bases` holds `Struct` and/or `BaseModel`; `BaseModel` also stands for anything only
    pydantic acts on (see `_is_pydantic_marker`). `identity_keyed` is whether the form, written
    inline, is a new cache key on every call (see `_compares_by_identity`).
    """

    model_bases: set[type]
    identity_keyed: bool


def _is_pydantic_marker(value: object) -> bool:
    """Whether `value`, an `Annotated` metadata item or a class, is something only pydantic acts
    on, which msgspec would silently ignore or reject: pydantic's own metadata (`Field(...)`,
    `AfterValidator`, `StringConstraints`), an `annotated_types` constraint (`Gt`, `MaxLen`:
    msgspec has its own `msgspec.Meta`), or anything with pydantic's validator or schema hook (a
    `BaseModel`, a pydantic dataclass, `HttpUrl`, a caller's custom type)."""
    if isinstance(value, type):
        return hasattr(value, "__pydantic_validator__") or hasattr(
            value, "__get_pydantic_core_schema__"
        )
    package = type(value).__module__.partition(".")[0]
    if package in {"pydantic", "pydantic_core", "annotated_types"}:
        return True
    return hasattr(value, "__get_pydantic_core_schema__")


def _compares_by_identity(value: object) -> bool:
    """Whether `value`, an `Annotated` metadata item, is only ever equal to itself, so a form
    carrying it is a new cache key each time it's written: an instance whose type has no
    `__eq__` (pydantic's `FieldInfo`, from an inline `Field(...)`), or a dataclass holding one.

    A class or other callable doesn't count: it's usually the same object on every call (a
    module-level function, so `AfterValidator(check)` is a stable key). An inline lambda or
    closure isn't, which is what `_decoders_max_size` is the backstop for.

    >>> _compares_by_identity("doc"), _compares_by_identity(len), _compares_by_identity(object())
    (False, False, True)
    """
    if callable(value):
        return False
    if type(value).__eq__ is object.__eq__:
        return True
    if is_dataclass(value):
        return any(
            _compares_by_identity(getattr(value, field.name))
            for field in fields(value)
            if field.compare
        )
    return False


def _is_class(value: object) -> TypeIs[type[object]]:
    # A `TypeIs`, not an inline `isinstance`: that narrows `object` to a partially unknown
    # `type[Unknown]`, which strict mode rejects as an argument.
    return isinstance(value, type)


def _class_field_annotations(cls: type[object]) -> list[object]:
    """The field annotations of a TypedDict, dataclass or NamedTuple, which msgspec and pydantic
    both decode field by field, so a model inside one decides the library. Empty for any other
    class, and for one whose annotations don't resolve (the library then says so itself)."""
    is_named_tuple = tuple in cls.__mro__ and hasattr(cls, "_fields")
    if not (is_typeddict(cls) or is_dataclass(cls) or is_named_tuple):
        return []
    try:
        hints = get_type_hints(cls, include_extras=True)
    except (NameError, TypeError):
        return []
    return list(hints.values())


def _form_traits(form: object) -> _FormTraits:
    """Walk `form`: generic arguments, unions, `Annotated` (its type and its metadata), PEP 695
    `type` aliases, and the fields of TypedDicts, dataclasses and NamedTuples, each alias and
    class once so a recursive one ends. A model class's own fields aren't walked: they're its
    library's business."""
    model_bases: set[type] = set()
    identity_keyed = False
    seen: set[object] = set()
    # Each entry also says whether it sits behind an alias or a class: those compare by their
    # own (stable) identity as a cache key, so metadata inside them can't make the key unstable.
    pending: list[tuple[object, bool]] = [(form, False)]
    while pending:
        current, behind_stable_key = pending.pop()
        if isinstance(current, TypeAliasType) or _is_class(current):
            if current in seen:
                continue
            seen.add(current)
        if isinstance(current, TypeAliasType):
            pending.append((current.__value__, True))
        elif _is_class(current):
            if issubclass(current, Struct):
                model_bases.add(Struct)
            elif issubclass(current, BaseModel) or _is_pydantic_marker(current):
                model_bases.add(BaseModel)
            pending.extend((annotation, True) for annotation in _class_field_annotations(current))
        elif get_origin(current) is Annotated:
            annotated, *metadata = get_args(current)
            pending.append((annotated, behind_stable_key))
            for item in metadata:
                if _is_pydantic_marker(item):
                    model_bases.add(BaseModel)
                if not behind_stable_key and _compares_by_identity(item):
                    identity_keyed = True
        else:
            origin = get_origin(current)
            if origin is not None:
                pending.append((origin, behind_stable_key))
            pending.extend((argument, behind_stable_key) for argument in get_args(current))
    return _FormTraits(model_bases, identity_keyed)


def _is_bare_dict_subclass(form: object) -> TypeIs[type[dict[Any, Any]]]:
    # A TypedDict is a `dict` subclass at runtime too, but means a typed JSON object.
    return _is_class(form) and not is_typeddict(form) and issubclass(form, dict)


def _is_json_object_form(form: object) -> bool:
    """Whether `form` means "any JSON object", which stdlib `json` decodes with no extra: `dict`,
    `dict[str, Any]` however it's spelled (`typing.Dict[str, Any]`, bare `typing.Dict`), and
    `Mapping[str, Any]`, since the `dict` it gets is one.

    >>> from typing import Dict
    >>> [_is_json_object_form(f) for f in (dict, Dict[str, Any], Mapping[str, Any], dict[str, int])]
    [True, True, True, False]
    """
    if form is dict:
        return True
    # `in` a tuple, not a set: an argument can be unhashable (`Annotated` with a `dict` in it).
    return get_origin(form) in {dict, Mapping} and get_args(form) in ((), (str, Any))


def _is_tuple(value: object) -> TypeIs[tuple[object, ...]]:
    # A `TypeIs`, not an inline `isinstance`: that narrows to a partially unknown tuple.
    return isinstance(value, tuple)


def _has_msgspec_custom_type(form: object) -> bool:
    """Whether msgspec reads some part of `form` as a "custom type" (`Path`, `deque[int]`,
    `IPv4Address`, a parametrized `dict` subclass such as `OrderedDict[str, int]`): one it only
    decodes through a `dec_hook`, which lothc doesn't pass, so its `Decoder` builds fine and then
    rejects every body. Walks `msgspec.inspect.type_info`'s tree, a recursive one ending at the
    first node seen twice. `object` is a custom type to `type_info` but decodes as `Any`."""
    try:
        root = msgspec.inspect.type_info(form)
    except TypeError:
        # Not a type msgspec knows at all: its `Decoder` reports that itself.
        return False
    seen: set[int] = set()
    pending: list[object] = [root]
    while pending:
        node = pending.pop()
        if _is_tuple(node):
            pending.extend(node)
            continue
        if not isinstance(node, (msgspec.inspect.Type, msgspec.inspect.Field)) or id(node) in seen:
            continue
        if isinstance(node, msgspec.inspect.CustomType) and node.cls is not object:
            return True
        seen.add(id(node))
        pending.extend(getattr(node, name) for name in node.__struct_fields__)
    return False


def _msgspec_decoder(form: object) -> Decoder[Any]:
    try:
        return Decoder(form)
    except TypeError as error:
        raise TypeError(f"Unsupported into: {form!r} ({error})") from error


def _pydantic_adapter(form: object) -> TypeAdapter[Any]:
    try:
        return TypeAdapter(form)
    # pydantic's schema-generation error is a `RuntimeError` as of 2.13, a `TypeError` before.
    except (TypeError, RuntimeError) as error:
        raise TypeError(f"Unsupported into: {form!r} ({error})") from error


def _build_decoder(form: object, model_bases: set[type]) -> _Decoder:
    """The decoder for a type expression: raw text for `str`, stdlib `json` for a JSON object
    (`dict`, `dict[str, Any]`, `Mapping[str, Any]`) and bare `dict` subclasses, else whichever
    library the form's models (or pydantic-only metadata) belong to, else msgspec (faster) unless
    it can't decode part of the form, else pydantic."""
    if form is str:
        return _TextDecoder()
    if _is_json_object_form(form):
        return _JSONObjectDecoder()
    if _is_bare_dict_subclass(form):
        return _DictSubclassDecoder(form)
    if Struct in model_bases and BaseModel in model_bases:
        raise TypeError(
            f"into={form!r} mixes msgspec Structs and pydantic BaseModels (or pydantic-only"
            " metadata such as Field(...)); one form can only be decoded by one library, so use"
            " one library's models throughout."
        )
    if BaseModel in model_bases:
        if not pydantic_installed:
            raise TypeError(
                f"into={form!r} carries validation only pydantic enforces (pydantic Field or"
                " validator metadata, annotated_types constraints, a pydantic type), which"
                " msgspec would silently skip: install lothc[pydantic]."
            )
        return _pydantic_adapter(form)
    # A Struct form stays with msgspec whatever its fields are: pydantic can't build a Struct.
    if msgspec_installed and (Struct in model_bases or not _has_msgspec_custom_type(form)):
        return _msgspec_decoder(form)
    if msgspec_installed and not pydantic_installed:
        raise TypeError(
            f"into={form!r} contains a type msgspec can't decode from JSON (e.g. Path, deque,"
            " IPv4Address, or a parametrized dict subclass such as OrderedDict[str, int]), so"
            " every response would fail to decode: install lothc[pydantic], which can. A bare"
            " dict subclass (into=OrderedDict) needs neither extra."
        )
    if pydantic_installed:
        return _pydantic_adapter(form)
    raise TypeError(
        f"into={form!r} needs msgspec or pydantic to decode it: install lothc[msgspec] or"
        " lothc[pydantic]. Only bytes, str, dict (and its bare subclasses), dict[str, Any] and"
        " Mapping[str, Any] work without either."
    )


def _is_prebuilt_decoder(into: object) -> TypeIs[Decoder[Any] | TypeAdapter[Any]]:
    # A `TypeIs`, not an inline `isinstance`: that narrows `into`'s `TypeForm[Any]` member to a
    # partially unknown `Decoder[Unknown]`.
    return isinstance(into, (Decoder, TypeAdapter))


def _decoder_for(into: object) -> _Decoder | None:
    """The decoder for `into`, or `None` for raw bytes. A pre-built `TypeAdapter`/`Decoder` is
    used as-is (never cached: one built per call would grow the cache forever). Takes `object`,
    not `_Into`, since a `raises=` class's error type comes from its bases at runtime. An
    unusable form raises `TypeError`, which is why every verb resolves it before sending."""
    if into is None or into is bytes:
        return None
    if _is_prebuilt_decoder(into):
        return into
    try:
        decoder = _decoders.get(into)
    except TypeError:
        # An unhashable form (e.g. `Annotated` with a `dict` in its metadata) still works,
        # just rebuilt on every call.
        return _build_decoder(into, _form_traits(into).model_bases)
    if decoder is None:
        traits = _form_traits(into)
        decoder = _build_decoder(into, traits.model_bases)
        # Storing a form that's a new key on every call would grow the cache forever; it's
        # rebuilt per call instead (hoisting it into a `type` alias makes it a stable key).
        if not traits.identity_keyed and len(_decoders) < _decoders_max_size:
            _decoders[into] = decoder
    return decoder


# Return type is whatever the decoder produces — genuinely dynamic, can't state it statically.
def _decode_json_line(data: _JSONInput, decoder: _Decoder) -> Any:  # noqa: ANN401
    """One NDJSON line or SSE `data` field (or a whole error body), decoded by an
    already-resolved `decoder` (resolved once per stream by `_decoder_for`, not once per line).
    An NDJSON line arrives as the `bytearray` it was split into: every decoder takes it as is."""
    if isinstance(decoder, TypeAdapter):
        return decoder.validate_json(data)
    return decoder.decode(data)
