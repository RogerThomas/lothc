"""Encoding `params=`/`headers=` and pydantic models, shared by the transports and testing."""

from collections.abc import Mapping
from typing import (
    Any,
    cast,
)

from ._compat import (
    BaseModel,
    Struct,
    msgspec,
)
from ._types import Headers, Params, _QueryValue


def _pydantic_by_alias(model: BaseModel) -> bool:
    """Whether to encode `model` by its aliases: yes, unless the model explicitly opts out with
    `serialize_by_alias=False`.

    pydantic's own default is to *validate* by alias but *serialize* by field name, so a camelCase
    model decoded from an API went back to it as snake_case. msgspec's `rename=` applies in both
    directions, so lothc now treats pydantic the same way, while still deferring to a model that
    states otherwise, so it never contradicts that model's own `model_dump()`.
    """
    return model.model_config.get("serialize_by_alias", True)


def _encode_params(params: Params) -> Mapping[str, _QueryValue]:
    """Turns any `Params` (`Mapping[str, _QueryValue]`, pydantic `BaseModel`, or msgspec `Struct`)
    into a plain `Mapping` the way a real request's query string would be encoded (non-`None`
    values only) — shared by `_apply_params` and `lothc.testing`'s mock query matching. A
    plain-`Mapping` input is returned as-is (no copy) — nothing here mutates it, and both real
    callers either flatten it into a fresh structure of their own (`_apply_params`'s
    `_query_pairs`) or hand it straight to pyreqwest's own `Url.parse_with_params`
    (`lothc.testing`'s `_query_param_match_values`), so there's nothing to gain from forcing a
    fresh `dict` here too."""
    match params:
        case BaseModel():
            return {
                name: value
                for name, value in params.model_dump(
                    mode="json", by_alias=_pydantic_by_alias(params)
                ).items()
                if value is not None
            }
        case Struct():
            builtins = cast(dict[str, Any], msgspec.to_builtins(params))
            return {name: value for name, value in builtins.items() if value is not None}
        case _:
            # basedpyright can't narrow a `match` fallback past `BaseModelTyping`/`StructTyping`
            # (structural Protocols — see _compat.py's `StructTyping` docstring) the way it can
            # narrow a sequential `issubclass` chain, so it still sees them as possible here even
            # though the two `case` patterns above already excluded any real BaseModel/Struct.
            return cast("Mapping[str, _QueryValue]", params)


def _exact_query_value(
    value: str | int | float | bool,  # noqa: FBT001 — a query value, not a flag
) -> str | int | float | bool:
    """`value` as pyreqwest's query/form encoder must get it to send it exactly: an `int` outside
    the signed 64-bit range as its own digits (pyreqwest turns one into a float, so `2**63` went
    out as `9.223372036854776e18`), anything else unchanged. A `bool` is an `int` in range, so it
    still goes out as `true`/`false`.

    >>> [_exact_query_value(v) for v in (2**63, -(2**63) - 1, 2**63 - 1, True, 1.5, "a")]
    ['9223372036854775808', '-9223372036854775809', 9223372036854775807, True, 1.5, 'a']
    """
    if isinstance(value, int) and not -(2**63) <= value < 2**63:
        return str(value)
    return value


def _query_pairs(params: Mapping[str, _QueryValue]) -> list[tuple[str, str | int | float | bool]]:
    """Expands an `_encode_params`-normalized `Params` mapping into the flat list-of-pairs shape
    pyreqwest's `RequestBuilder.query()` actually needs to produce a repeated query key on the
    wire — confirmed live that `.query()` raises `pyreqwest.exceptions.BuilderError: unsupported
    value` if a `Mapping`'s own value is itself a `list`/`tuple`, even though
    `Url.parse_with_params` (used by `lothc.testing`'s own `_query_param_match_values` instead)
    happily accepts that exact shape (either spelling) and expands it correctly — a genuine
    inconsistency between the two pyreqwest APIs, not something to build on. A flat
    `Sequence[tuple[str, QueryPrimitive]]` works for `.query()`, so that's the shape used here. A
    `list`/`tuple` value means "repeat this query param once per element" — see `Params`'s own
    doc comment above. Every value goes through `_exact_query_value`, so an `int` too big for
    64 bits isn't sent as a float. Shared by `params=`, `data=` and `lothc.testing`'s `params=`
    matching, so all three agree on every value's wire string."""
    pairs: list[tuple[str, str | int | float | bool]] = []
    for name, value in params.items():
        # A tuple of types, not `list | tuple`: ~100ns faster per value, on every request's params.
        if isinstance(value, (list, tuple)):
            pairs.extend((name, _exact_query_value(item)) for item in value)
        else:
            pairs.append((name, _exact_query_value(value)))
    return pairs


def _encode_headers(headers: Headers) -> dict[str, str]:
    """Turns any `Headers` (`Mapping[str, str]`, pydantic `BaseModel`, or msgspec `Struct`) into a
    plain `dict[str, str]` the way a real request's headers would be encoded (`_` -> `-`,
    non-`None` values only) — shared by `_apply_headers` and `lothc.testing`'s mock-response
    header encoding. Deliberately NOT symmetric with `_encode_params`'s no-copy fallback above,
    despite looking like the same situation: `_encode_params`'s result is always used once,
    immediately, then discarded (`_apply_params`/`_query_param_match_values`), so returning the
    caller's own `Mapping` unchanged is safe. `_encode_headers`'s result is not always used that
    way — `lothc.testing`'s `LOTHCMock.with_headers` stores it (`_last_encoded_headers`) to
    re-apply later from `with_data`, so an uncopied fallback would alias the caller's own mutable
    `dict` and let a mutation between `.with_headers(...)` and `.with_data(...)` silently change
    the mocked response's headers (confirmed live — a no-copy version of this fallback introduced
    exactly that bug before being caught by review). Always copy here, even though
    `_apply_headers`'s own use of the result doesn't itself need one."""
    match headers:
        case BaseModel():
            dumped = headers.model_dump(mode="json", by_alias=_pydantic_by_alias(headers))
        case Struct():
            dumped = cast(dict[str, Any], msgspec.to_builtins(headers))
        case _:
            # Same basedpyright match-fallback narrowing gap as `_apply_params` above.
            return dict(cast("Mapping[str, str]", headers))
    return {
        name.replace("_", "-"): str(value) for name, value in dumped.items() if value is not None
    }
