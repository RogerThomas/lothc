#!yeet
"""Manual smoke-testing server for `examples/run_http.py` — reuses the same stdlib-only handler
the automated test suite runs against (`tests/_server.py`), bound to a fixed port instead of an
OS-assigned one, so `run_http.py`'s default `base_url` finds it without extra wiring.
"""

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast


def main(port: int = 8701) -> None:
    spec = importlib.util.spec_from_file_location(
        "_server", Path(__file__).parent.parent / "tests" / "_server.py"
    )
    assert spec is not None
    assert spec.loader is not None
    server_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server_module)
    test_app_handler = cast(type[BaseHTTPRequestHandler], server_module.TestAppHandler)

    server = ThreadingHTTPServer(("127.0.0.1", port), test_app_handler)
    print(f"Serving on http://127.0.0.1:{port}/")
    with server:
        server.serve_forever()
