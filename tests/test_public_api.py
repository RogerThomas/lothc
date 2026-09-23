"""lothc's public API must never expose a pyreqwest type: pyreqwest is an implementation detail,
so no exported class, function, method, overload or type alias may mention one in its signature.
"""

import inspect
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any, TypeAliasType, get_args, get_overloads

import pytest

import lothc
import lothc.testing


def _pyreqwest_types_in(annotation: object, seen: set[int]) -> list[str]:
    if id(annotation) in seen:
        return []
    seen.add(id(annotation))
    found: list[str] = []
    module = getattr(annotation, "__module__", None)
    if isinstance(module, str) and module.startswith("pyreqwest"):
        found.append(repr(annotation))
    if isinstance(annotation, TypeAliasType):
        try:
            value = annotation.__value__
        except NameError:
            # Refers to a `TYPE_CHECKING`-only name (typeshed's), which can't be pyreqwest's.
            value = None
        found += _pyreqwest_types_in(value, seen)
    for arg in get_args(annotation):
        found += _pyreqwest_types_in(arg, seen)
    return found


def _signature_annotations(function: Callable[..., Any]) -> list[object]:
    annotations: list[object] = []
    for candidate in [function, *get_overloads(function)]:
        try:
            signature = inspect.signature(candidate)
        except (TypeError, ValueError):
            continue
        annotations += [parameter.annotation for parameter in signature.parameters.values()]
        annotations.append(signature.return_annotation)
    return annotations


def _public_members(module: ModuleType) -> Iterator[tuple[str, object]]:
    for name in module.__all__:
        exported = getattr(module, name)
        if isinstance(exported, TypeAliasType):
            yield name, exported
        elif inspect.isclass(exported):
            for attribute, value in vars(exported).items():
                if attribute.startswith("_") and attribute != "__init__":
                    continue
                target = value.fget if isinstance(value, property) else value
                if callable(target):
                    yield f"{name}.{attribute}", target
        elif callable(exported):
            yield name, exported


@pytest.mark.parametrize("module", [lothc, lothc.testing], ids=["lothc", "lothc.testing"])
def test_no_pyreqwest_type_appears_in_the_public_api(module: ModuleType) -> None:
    offenders: dict[str, list[str]] = {}
    for qualname, member in _public_members(module):
        if isinstance(member, TypeAliasType):
            annotations: list[object] = [member]
        elif callable(member):
            annotations = _signature_annotations(member)
        else:
            continue
        found = [
            hit for annotation in annotations for hit in _pyreqwest_types_in(annotation, set())
        ]
        if found:
            offenders[qualname] = sorted(set(found))

    assert offenders == {}
