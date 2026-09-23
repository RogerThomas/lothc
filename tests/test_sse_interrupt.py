"""Real Ctrl-C interruptibility test for `SyncHTTPClient.sse(interruptible=True)`.

A plain `kill -INT` on a pytest process backgrounded by a shell is not a faithful test here —
POSIX shells start `&` jobs with SIGINT set to SIG_IGN, so CPython never installs its own
handler, which silently invalidates the test in both directions (see CLAUDE.md's dev notes on
this file's own design). The faithful harness is a real controlling PTY: `pty.fork()`, exec a
small client script in the child (which inherits the PTY as its controlling terminal, in the
foreground process group), then write a real `\\x03` byte to the master fd — exactly what a
terminal does when a person presses Ctrl-C.
"""

import contextlib
import os
import select
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# `pty` is POSIX-only — importorskip converts an ImportError on Windows into a clean skip,
# rather than a collection error (a plain `import pty` would blow up before any marker on this
# module gets a chance to run).
pty = pytest.importorskip("pty")

_GOT_EVENT_MARKER = b"GOT_EVENT"

_CLIENT_SCRIPT = """
import sys

from lothc import SyncHTTPClient

base_url, interruptible = sys.argv[1], sys.argv[2] == "true"
with SyncHTTPClient(base_url=base_url) as client:
    for event in client.sse("stall", interruptible=interruptible):
        print(f"GOT_EVENT:{event.data}", flush=True)
"""


class _StallingSSEHandler(BaseHTTPRequestHandler):
    """Sends one SSE event, then withholds the connection forever — never closes, never
    sends a second event — so a client's second `read_chunk()` blocks indefinitely.
    """

    def do_GET(self) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"event: tick\nid: 0\ndata: hello\n\n")
        self.wfile.flush()
        time.sleep(3600)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — stdlib's own signature
        pass  # silence default per-request stderr logging


def _make_stalling_server() -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", 0), _StallingSSEHandler)


def _read_until(master_fd: int, marker: bytes, deadline: float) -> bool:
    buffer = b""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([master_fd], [], [], remaining)
        if not ready:
            continue
        try:
            chunk = os.read(master_fd, 4096)
        except OSError:
            return False
        if not chunk:
            return False
        buffer += chunk
        if marker in buffer:
            return True


def _wait_exited(pid: int, master_fd: int, deadline: float) -> bool:
    # Keep draining master_fd while polling — a child that caught the SIGINT still has to
    # flush a KeyboardInterrupt traceback to its stdout/stderr (both wired to the PTY slave).
    # A PTY's kernel buffer is small; if nobody reads the master side, that write blocks and
    # the child never actually exits, even though the signal was delivered and handled.
    while True:
        finished_pid, _ = os.waitpid(pid, os.WNOHANG)
        if finished_pid == pid:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([master_fd], [], [], min(remaining, 0.05))
        if ready:
            with contextlib.suppress(OSError):
                os.read(master_fd, 4096)


@pytest.mark.parametrize("interruptible", [True, False])
def test_sync_sse_ctrl_c_interruptibility(*, interruptible: bool) -> None:
    """`interruptible=True` lets a real Ctrl-C (SIGINT via a controlling PTY) interrupt a
    stalled sync SSE stream promptly; `interruptible=False` (the default) reproduces the
    original bug — Ctrl-C is dead while blocked in `read_chunk()` — as the negative control.
    """
    with _make_stalling_server() as server, ThreadPoolExecutor(max_workers=1) as pool:
        # `poll_interval=0.01`, not the 0.5s default: `shutdown()` blocks until
        # `serve_forever`'s loop next wakes up, so the default makes every teardown pay up
        # to half a second of pure waiting (measured).
        pool.submit(server.serve_forever, 0.01)
        try:
            base_url = f"http://127.0.0.1:{server.server_port}/"
            pid, master_fd = pty.fork()
            if pid == 0:
                # execvp never returns on success — it replaces this process image outright.
                # The try/finally guards only the failure case: if exec itself raises, this
                # forked child must never fall through into the parent's own test/fixture
                # code (a duplicate pytest process running is a much worse failure mode than
                # a confusing exit code), so os._exit() unconditionally on the way out.
                try:
                    os.execvp(  # noqa: S606 — fixed interpreter path, no shell, no untrusted input
                        sys.executable,
                        [
                            sys.executable,
                            "-c",
                            _CLIENT_SCRIPT,
                            base_url,
                            "true" if interruptible else "false",
                        ],
                    )
                finally:
                    os._exit(127)
            try:
                assert _read_until(master_fd, _GOT_EVENT_MARKER, time.monotonic() + 10)
                # Let the child get from printing the event back into its blocking `read_chunk()`
                # before Ctrl-C arrives. Without this, a loaded machine could deliver the byte
                # while the child was still running Python bytecode, where a KeyboardInterrupt
                # lands immediately either way: the negative control then failed (reproduced 1/25
                # under a 12-way CPU burn), and the `True` case could pass without ever proving a
                # *blocked* read was interrupted. A child that isn't blocked by then is a failure
                # of this harness, not something either case is meant to measure.
                time.sleep(0.2)
                os.write(master_fd, b"\x03")
                # These two deadlines are not the same kind of number, which is why they differ.
                # For `interruptible=True` it's a generous upper bound that a passing run never
                # reaches (a working Ctrl-C exits in ~15-25ms, measured over repeated runs), so
                # its size is free and only bounds how long a genuine regression takes to fail.
                # For the negative control it's a dwell time paid in full on every green run, so
                # every extra second is pure suite latency — 0.5s still leaves ~20x headroom over
                # the slowest exit observed, which is ample for a loaded CI box.
                deadline = 2.0 if interruptible else 0.5
                exited = _wait_exited(pid, master_fd, time.monotonic() + deadline)
                if interruptible:
                    assert exited
                else:
                    # Note what guards this: were the `\x03` write silently not reaching the
                    # child at all, this assertion would still pass — for the wrong reason. The
                    # `interruptible=True` twin is what rules that out, since it exercises the
                    # identical PTY write and fails if delivery breaks.
                    assert not exited
            finally:
                if not interruptible:
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
                    # Already reaped if `_wait_exited` saw it exit, which is exactly when the
                    # negative control fails; raising here would bury that assertion.
                    with contextlib.suppress(ChildProcessError):
                        os.waitpid(pid, 0)
                os.close(master_fd)
        finally:
            server.shutdown()
