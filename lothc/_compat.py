from typing import TYPE_CHECKING, Any, ClassVar, Protocol, Self

if TYPE_CHECKING:
    import msgspec
    from msgspec import Struct
    from msgspec.json import Decoder
    from pydantic import BaseModel, TypeAdapter
else:
    try:
        import msgspec
        from msgspec import Struct
        from msgspec.json import Decoder
    except ImportError:
        msgspec = None

        # `_client.py` has plenty of unquoted `Decoder[Any]` annotations, evaluated eagerly at
        # import time (no `from __future__ import annotations` in that file) — without
        # `__class_getitem__`, subscripting this stub crashes `import lothc` outright whenever
        # msgspec isn't installed, confirmed live.
        class Struct:
            def __class_getitem__(cls, _item: object) -> type[Self]:
                return cls

        class Decoder:
            def __class_getitem__(cls, _item: object) -> type[Self]:
                return cls

    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:
        # Same reasoning as Struct/Decoder above, for _client.py's `TypeAdapter[Any]` annotations.
        class BaseModel:
            def __class_getitem__(cls, _item: object) -> type[Self]:
                return cls

        class TypeAdapter:
            def __class_getitem__(cls, _item: object) -> type[Self]:
                return cls


class StructTyping(Protocol):
    """Structural stand-in for `msgspec.Struct`, used ONLY in lothc's own public type aliases
    (`Data`, `Params`, `Headers`, `TypedHeaders`, `JSONPayload`) — never in an `isinstance`/
    `issubclass` check, which must keep using the real `Struct` above.

    Exists so those aliases stay fully typed (no `Unknown`) even when msgspec is completely
    unresolvable to whatever type checker is running — the `if TYPE_CHECKING: from msgspec import
    Struct` above only gives real types when msgspec is *actually resolvable to that checker*;
    when it isn't (confirmed live: a consumer with only pydantic installed, msgspec absent even
    as a dev dependency), that import fails to resolve and `Struct` becomes `Unknown`, poisoning
    every alias built from it — even calls that never touch msgspec at all. A checker-local,
    import-free `Protocol` sidesteps that entirely: it never needs msgspec resolvable anywhere.

    Precision is unaffected when msgspec genuinely *is* installed and used: msgspec declares
    `__struct_fields__` directly on `Struct` itself (see `msgspec/__init__.pyi`), so any real
    `Struct` subclass structurally satisfies this protocol — confirmed live across basedpyright,
    mypy, ty, and zuban, both with and without msgspec present, that a real subclass passed as
    `response_data_type=` still resolves to its own precise type, never `Unknown`.
    """

    __struct_fields__: ClassVar[tuple[str, ...]]


class BaseModelTyping(Protocol):
    """Same reasoning as `StructTyping`, for `pydantic.BaseModel` — matches on
    `model_validate_json`'s call shape alone (the same method `_decode_body`/`_decode_error_body`
    already call), not a `ClassVar`.

    A `ClassVar[Any] model_config` marker was tried first and rejected: confirmed live that a
    *subclass* re-declaring `model_config = ConfigDict(...)` without repeating `ClassVar` — the
    standard, pydantic-docs-recommended way to configure a model, used throughout real code, e.g.
    `class SSEBase(BaseModel): model_config = ConfigDict(...)` — makes basedpyright and zuban
    treat that subclass's `model_config` as an *instance* variable, no longer satisfying a
    `ClassVar`-typed protocol member at all (mypy and ty were unaffected by this specific
    quirk). That's a real, confirmed regression this exact fix was meant to prevent, not a
    theoretical one — a genuine consumer's real `BaseModel` subclass stopped satisfying `Data`'s
    bound. A method-shaped protocol member has no such ambiguity: methods aren't "reassigned"
    the way a subclass reassigns an inherited class variable, so this is stable across all four
    checkers regardless of how the model configures itself. Confirmed live, with a subclass that
    overrides `model_config` exactly as above, that all four checkers resolve it to its own
    precise type with zero errors."""

    @classmethod
    def model_validate_json(cls, _json_data: str | bytes, /) -> Any: ...  # noqa: ANN401


class TypeAdapterTyping[TData](Protocol):
    """Same reasoning as `StructTyping`/`BaseModelTyping`, for a pre-built `pydantic.TypeAdapter`
    instance (used by `sse`/`stream_get`/`stream_post`'s `response_data_type`, alongside a bare
    class) — matches on `validate_json`'s call shape alone, the only method those verbs ever call
    on it."""

    def validate_json(self, data: str | bytes, /) -> TData: ...


class DecoderTyping[TData](Protocol):
    """Same reasoning as `TypeAdapterTyping`, for a pre-built `msgspec.json.Decoder` instance —
    matches on `decode`'s call shape alone."""

    def decode(self, data: bytes | str, /) -> TData: ...


__all__ = [
    "BaseModel",
    "BaseModelTyping",
    "Decoder",
    "DecoderTyping",
    "Struct",
    "StructTyping",
    "TypeAdapter",
    "TypeAdapterTyping",
    "msgspec",
]
