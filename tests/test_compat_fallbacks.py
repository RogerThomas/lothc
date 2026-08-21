"""Exercises `lothc/_compat.py`'s `except ImportError` fallback stubs, and the real client's
own "typeguard not installed" warning path — both of which never trigger in this project's
normal dev environment, where pydantic/msgspec/typeguard are all installed extras.

Rather than spinning up a real dependency-less environment (a separate venv/subprocess whose
coverage wouldn't be tracked by this same `pytest --cov` run), this blocks the three imports
in-process via a temporary `builtins.__import__` shim and a fresh `importlib.import_module`
call, then restores `sys.modules` to the real, already-imported modules afterward. `lothc`
itself is never re-imported here, so `lothc._client`'s own already-bound `BaseModel`/`Struct`/
`typeguard` names (captured once at process start) are untouched — only the throwaway
`compat` module object sees the blocked imports.
"""

import builtins
import importlib
import sys
import warnings
from collections.abc import Iterator
from types import ModuleType
from typing import TypedDict

import pytest

from lothc import HTTPClient, SyncHTTPClient

_blocked_modules = frozenset({"msgspec", "pydantic", "typeguard"})
_real_import = builtins.__import__


def _blocking_import(name: str, *args: object, **kwargs: object) -> object:
    if name.split(".")[0] in _blocked_modules:
        raise ImportError(f"blocked for test: {name}")
    return _real_import(name, *args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(name="compat_without_optional_deps")
def _compat_without_optional_deps() -> Iterator[ModuleType]:
    saved_modules = {
        key: value
        for key, value in sys.modules.items()
        if key == "lothc._compat" or key.split(".")[0] in _blocked_modules
    }
    for key in saved_modules:
        del sys.modules[key]

    builtins.__import__ = _blocking_import
    try:
        yield importlib.import_module("lothc._compat")
    finally:
        builtins.__import__ = _real_import
        for key in list(sys.modules):
            if key == "lothc._compat" or key.split(".")[0] in _blocked_modules:
                del sys.modules[key]
        sys.modules.update(saved_modules)


def test_msgspec_and_typeguard_fall_back_to_none(
    compat_without_optional_deps: ModuleType,
) -> None:
    assert compat_without_optional_deps.msgspec is None
    assert compat_without_optional_deps.typeguard is None


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


class _ItemDict(TypedDict):
    id: int
    name: str


async def test_typed_dict_decode_warns_and_skips_validation_when_typeguard_missing(
    client: HTTPClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lothc._client.typeguard", None)

    with pytest.warns(UserWarning, match="typeguard is not installed"):
        item = await client.get("items/7", response_data_type=_ItemDict)

    assert item == {"id": 7, "name": "item-7"}


def test_typed_dict_decode_warning_suppressed_by_env_var(
    sync_client: SyncHTTPClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("lothc._client.typeguard", None)
    monkeypatch.setenv("LOTHC_SUPPRESS_TYPEGUARD_WARNING", "1")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        item = sync_client.get("items/7", response_data_type=_ItemDict)

    assert item == {"id": 7, "name": "item-7"}
