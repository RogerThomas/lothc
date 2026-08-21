"""asv benchmarks tracking lothc's OWN performance across its git history for `get()`/`post()`
against small, realistic JSON bodies -- the opposite case from `bench_download.py`'s large-object
concern. A body this small never meaningfully skews a `peakmem_*` measurement, so unlike
`bench_download.py` there's no separate-process server here -- it's the exact same stdlib
`ThreadingHTTPServer` the real pytest suite runs against (`tests/_server.py`), started as a
background thread in `setup()` and torn down in `teardown()`, matching `tests/conftest.py`'s own
`base_url` fixture.

See CLAUDE.md's "Regression benchmarking (asv)" section for what this suite is and isn't, and
`bench_download.py` for the large-object suite this one is deliberately simpler than.
"""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast

from lothc import JSON, SyncHTTPClient


def _make_test_server() -> ThreadingHTTPServer:
    """Loads `tests/_server.py` via importlib (not sys.path -- same approach, same reason, as
    `examples/server.py`) and calls its `make_server()`, the exact server the real pytest suite
    runs against.
    """
    spec = importlib.util.spec_from_file_location(
        "_server", Path(__file__).parent.parent / "tests" / "_server.py"
    )
    assert spec is not None
    assert spec.loader is not None
    server_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_module)
    return cast(ThreadingHTTPServer, server_module.make_server())


class VerbSuite:
    """`get()`/`post()` time/peak memory against small JSON bodies."""

    number = 1
    repeat = 3
    warmup_time = 0

    def setup(self) -> None:
        self._exit_stack = ExitStack()
        server = _make_test_server()
        pool = ThreadPoolExecutor(max_workers=1)
        self._exit_stack.callback(pool.shutdown)
        pool.submit(server.serve_forever)
        self._exit_stack.callback(server.shutdown)
        self._client = self._exit_stack.enter_context(
            SyncHTTPClient.build(base_url=f"http://127.0.0.1:{server.server_port}/")
        )

    def teardown(self) -> None:
        self._exit_stack.close()

    def time_get(self) -> None:
        self._client.get("items/7")

    def peakmem_get(self) -> None:
        self._client.get("items/7")

    def time_get_typed(self) -> None:
        self._client.get("items/7", response_data_type=JSON)

    def peakmem_get_typed(self) -> None:
        self._client.get("items/7", response_data_type=JSON)

    def time_post(self) -> None:
        self._client.post("items", json={"id": 1, "name": "item-1"})

    def peakmem_post(self) -> None:
        self._client.post("items", json={"id": 1, "name": "item-1"})
