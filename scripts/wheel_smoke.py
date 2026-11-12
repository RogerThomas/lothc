#!yeet
"""Checks an installed lothc wheel in an environment without its optional extras (pydantic,
msgspec): `lothc` and `lothc.testing` import from the wheel, not a checkout, and a real request
goes through the native transport, async and sync. On Windows, where the native transport
doesn't run, `native="on"` must refuse instead.

`lothc.testing` is a pytest plugin, so the environment needs pytest.
"""

import asyncio
import importlib
import importlib.metadata
import importlib.util
import sys
import sysconfig
from collections.abc import Callable
from contextlib import AbstractContextManager
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import cast

from lothc import HTTPClient, SyncHTTPClient


def _test_server_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_server", Path(__file__).parent.parent / "tests" / "_server.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError("tests/_server.py not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_installed() -> None:
    present = [name for name in ("pydantic", "msgspec") if importlib.util.find_spec(name)]
    if present:
        raise RuntimeError(f"Optional extras installed: {present}")
    checkout = Path(__file__).parent.parent
    for name in ("lothc", "lothc.testing", "lothc._native"):
        module_file = Path(cast(str, importlib.import_module(name).__file__))
        if module_file.is_relative_to(checkout):
            raise RuntimeError(f"{name} imported from the checkout: {module_file}")
        print(f"{name}: {module_file}")
    # A free-threaded build turns the GIL back on for an extension that doesn't declare it can
    # run without it; `_is_gil_enabled` is CPython's only way to ask.
    gil = sys._is_gil_enabled()  # pyright: ignore[reportPrivateUsage]
    if sysconfig.get_config_var("Py_GIL_DISABLED") and gil:
        raise RuntimeError("Importing lothc turned the GIL on")


def _check_item(data: dict[str, object]) -> None:
    if data != {"id": 1, "name": "item-1"}:
        raise RuntimeError(f"Unexpected body: {data!r}")


async def _async_request(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, native="on") as client:
        _check_item((await client.get("items/1", into=dict)).data)


def _sync_request(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, native="on") as client:
        _check_item(client.get("items/1", into=dict).data)


def _check_native_refused() -> None:
    for client_type in (HTTPClient, SyncHTTPClient):
        try:
            client_type(native="on")
        except NotImplementedError:
            continue
        raise RuntimeError(f"{client_type.__name__}(native='on') didn't refuse")


async def main() -> None:
    print(f"lothc {importlib.metadata.version('lothc')} on Python {sys.version}")
    _check_installed()
    if sys.platform == "win32":
        _check_native_refused()
        print("native='on' refused, as on every Windows install")
        return
    module = _test_server_module()
    make_server = cast(Callable[[], ThreadingHTTPServer], module.make_server)
    serve_in_background = cast(
        Callable[[ThreadingHTTPServer], AbstractContextManager[ThreadingHTTPServer]],
        module.serve_in_background,
    )
    with serve_in_background(make_server()) as server:
        base_url = f"http://127.0.0.1:{server.server_port}/"
        await _async_request(base_url)
        await asyncio.to_thread(_sync_request, base_url)
    print("native requests answered, async and sync")
