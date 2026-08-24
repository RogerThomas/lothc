from typing import TYPE_CHECKING, Self

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


__all__ = ["BaseModel", "Decoder", "Struct", "TypeAdapter", "msgspec"]
