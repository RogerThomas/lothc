from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import msgspec
    import typeguard
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

        class Struct: ...

        class Decoder: ...

    try:
        from pydantic import BaseModel, TypeAdapter
    except ImportError:

        class BaseModel: ...

        class TypeAdapter: ...

    try:
        import typeguard
    except ImportError:
        typeguard = None


__all__ = ["BaseModel", "Decoder", "Struct", "TypeAdapter", "msgspec", "typeguard"]
