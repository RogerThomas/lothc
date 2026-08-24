"""Exercises `lothc/_compat.py`'s `except ImportError` fallback stubs — which never trigger in
this project's normal dev environment, where pydantic/msgspec are both installed extras.

Rather than spinning up a real dependency-less environment (a separate venv/subprocess whose
coverage wouldn't be tracked by this same `pytest --cov` run), this blocks the two imports
in-process via a temporary `builtins.__import__` shim and a fresh `importlib.import_module`
call, then restores `sys.modules` to the real, already-imported modules afterward. `lothc`
itself is never re-imported here, so `lothc._client`'s own already-bound `BaseModel`/`Struct`
names (captured once at process start) are untouched — only the throwaway `compat` module object
sees the blocked imports.
"""

import builtins
import importlib
import sys
from collections.abc import Iterator, Mapping, Sequence
from types import ModuleType

import pytest

_blocked_modules = frozenset({"msgspec", "pydantic"})
_real_import = builtins.__import__


def _blocking_import(
    name: str,
    globals: Mapping[str, object] | None = None,  # noqa: A002 — must match __import__'s real signature
    locals: Mapping[str, object] | None = None,  # noqa: A002
    fromlist: Sequence[str] | None = (),
    level: int = 0,
) -> ModuleType:
    if name.split(".")[0] in _blocked_modules:
        raise ImportError(f"blocked for test: {name}")
    return _real_import(name, globals, locals, fromlist, level)


@pytest.fixture(name="compat_without_optional_deps")
def _compat_without_optional_deps() -> Iterator[ModuleType]:
    saved_modules = {
        key: value
        for key, value in sys.modules.items()
        if key == "lothc._compat" or key.split(".")[0] in _blocked_modules
    }
    for key in saved_modules:
        del sys.modules[key]

    # `_blocking_import`'s signature matches `__import__`'s exactly — confirmed via mypy/zuban/
    # basedpyright all accepting this line, and via ty's own error printing both signatures as
    # textually identical yet still rejecting it. Genuine ty limitation, not a real mismatch.
    builtins.__import__ = _blocking_import  # ty: ignore[invalid-assignment]
    try:
        yield importlib.import_module("lothc._compat")
    finally:
        builtins.__import__ = _real_import
        for key in list(sys.modules):
            if key == "lothc._compat" or key.split(".")[0] in _blocked_modules:
                del sys.modules[key]
        sys.modules.update(saved_modules)


def test_msgspec_falls_back_to_none(compat_without_optional_deps: ModuleType) -> None:
    assert compat_without_optional_deps.msgspec is None


def test_struct_and_decoder_fall_back_to_empty_stub_classes(
    compat_without_optional_deps: ModuleType,
) -> None:
    compat = compat_without_optional_deps

    assert isinstance(compat.Struct(), compat.Struct)
    assert isinstance(compat.Decoder(), compat.Decoder)
    assert not isinstance(object(), compat.Struct)


def test_base_model_and_type_adapter_fall_back_to_empty_stub_classes(
    compat_without_optional_deps: ModuleType,
) -> None:
    compat = compat_without_optional_deps

    assert isinstance(compat.BaseModel(), compat.BaseModel)
    assert isinstance(compat.TypeAdapter(), compat.TypeAdapter)
    assert not isinstance(object(), compat.BaseModel)
