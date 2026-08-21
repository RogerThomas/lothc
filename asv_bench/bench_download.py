"""asv benchmarks tracking lothc's OWN performance across its git history for `get()` vs
`download()` against a large body -- see CLAUDE.md's `download()` design note for why this
distinction exists (pyreqwest's default `.bytes()` path does ~3x the payload in peak memory;
`download()` streams into one pre-grown buffer instead).

This is a *different* job than `perf.py`/`benchmarks/` (which compare lothc against other HTTP
client libraries) -- this suite only ever benchmarks lothc against its own history, which is
what `asv` (airspeed velocity) is built for: `asv run` checks out old commits, builds lothc fresh
in an isolated env for each, and re-runs these benchmarks so `asv publish`/`asv preview` can show
whether a given commit made something slower or more memory-hungry.

The large-object HTTP server MUST run as a separate OS process, not a thread in this process --
`peakmem_*` benchmarks measure `resource.getrusage(RUSAGE_SELF).ru_maxrss`, so a server sharing
this process would have its own memory counted toward the very number being tracked.
"""

import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import ClassVar

from lothc import SyncHTTPClient


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"benchmark server never came up on port {port}")


def _stop_server(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    server.wait(timeout=5)


class DownloadSuite:
    """`get()` vs `download()` peak memory/time against a large body."""

    _data_dir: ClassVar[Path] = Path(__file__).parent / "_data"
    _large_file: ClassVar[Path] = _data_dir / "large.bin"
    _large_size: ClassVar[int] = 50_000_000  # 50MB -- shows the memory-copy gap, stays fast

    number = 1
    repeat = 3
    warmup_time = 0

    def _ensure_large_file(self) -> str:
        self._data_dir.mkdir(exist_ok=True)
        if not self._large_file.exists() or self._large_file.stat().st_size != self._large_size:
            self._large_file.write_bytes(bytes(self._large_size))
        return str(self._large_file)

    def setup_cache(self) -> str:
        return self._ensure_large_file()

    def setup(self, large_file_path: str) -> None:
        self._exit_stack = ExitStack()
        port = _free_port()
        server = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-m",
                "http.server",
                str(port),
                "--directory",
                str(Path(large_file_path).parent),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._exit_stack.callback(_stop_server, server)
        _wait_for_port(port)
        self._client = self._exit_stack.enter_context(
            SyncHTTPClient.build(base_url=f"http://127.0.0.1:{port}")
        )
        tmp_dir = self._exit_stack.enter_context(tempfile.TemporaryDirectory())
        self._download_dest = Path(tmp_dir) / "download.bin"

    def teardown(self, large_file_path: str) -> None:  # noqa: ARG002 — asv passes setup_cache's
        # return value positionally to every setup/benchmark/teardown call, matching arg count
        self._exit_stack.close()

    def time_get(self, large_file_path: str) -> None:  # noqa: ARG002 — see teardown
        self._client.get("large.bin")

    def peakmem_get(self, large_file_path: str) -> None:  # noqa: ARG002 — see teardown
        self._client.get("large.bin")

    def time_download_bytes(self, large_file_path: str) -> None:  # noqa: ARG002 — see teardown
        self._client.download("large.bin")

    def peakmem_download_bytes(self, large_file_path: str) -> None:  # noqa: ARG002 — see teardown
        self._client.download("large.bin")

    def time_download_to_file(self, large_file_path: str) -> None:  # noqa: ARG002 — see teardown
        self._client.download("large.bin", self._download_dest)

    def peakmem_download_to_file(self, large_file_path: str) -> None:  # noqa: ARG002 — see teardown
        self._client.download("large.bin", self._download_dest)
