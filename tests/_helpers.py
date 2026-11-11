"""Small helpers shared by several test modules."""

import json
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from lothc import SyncHTTPClient


def _icacls(path: Path, *arguments: str) -> None:
    """Edit `path`'s Windows ACL; `*S-1-1-0` is everyone."""
    subprocess.run(["icacls", str(path), *arguments], check=True, capture_output=True)


def echoed_headers(result: dict[str, Any]) -> dict[str, str]:
    """`/echo-headers`'s decoded body as a name → value dict, names lowercased.

    A repeated header keeps only its last value; a test about duplicates reads the raw list.
    """
    return {header["name"].lower(): header["value"] for header in result["headers"]}


def echoed_header_values(result: dict[str, Any], name: str) -> list[str]:
    """Every value `/echo-headers` received for the lowercase header `name`, in arrival order."""
    return [header["value"] for header in result["headers"] if header["name"].lower() == name]


def clear_proxy_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every variable a client reads proxies from (and CGI's `REQUEST_METHOD`, which turns
    them off), so a test starts from no proxies whatever its caller's environment has;
    `monkeypatch` puts them back afterwards."""
    for name in (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
        "REQUEST_METHOD",
    ):
        monkeypatch.delenv(name, raising=False)


def make_unreadable(path: Path) -> None:
    """Stop this process opening the file `path` for reading. On Windows, where `chmod` only sets
    the read-only flag, that takes an ACL entry denying everyone its data."""
    if sys.platform == "win32":
        _icacls(path, "/deny", "*S-1-1-0:(RD)")
    else:
        path.chmod(0)


@contextmanager
def opens_but_fails_to_read(path: Path) -> Generator[None]:
    """Make `path` something that opens as a file but fails on its first read, for the block: a
    directory on POSIX; on Windows, which won't open a directory as a file, a file whose first
    byte another handle holds locked."""
    if sys.platform == "win32":
        import msvcrt  # Windows only

        path.write_bytes(b"x")
        with path.open("rb") as file:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            yield
    else:
        path.mkdir()
        yield


@contextmanager
def read_only_directory(path: Path) -> Generator[None]:
    """The directory `path` refusing new files (so also any file replaced by renaming one into
    it) until the block ends. On Windows, where `chmod` only sets the read-only flag, which a
    directory ignores, that takes an ACL entry."""
    if sys.platform == "win32":
        _icacls(path, "/deny", "*S-1-1-0:(WD,AD)")
    else:
        path.chmod(0o500)
    try:
        yield
    finally:
        if sys.platform == "win32":
            _icacls(path, "/remove:d", "*S-1-1-0")
        else:
            path.chmod(0o700)


def partial_body_seen(base_url: str, key: str) -> dict[str, Any]:
    """What `/partial-body` recorded for `key` (request line, headers, as much body as arrived),
    once it has: it records only after the client hangs up, which may be just after the client's
    own call raised."""
    with SyncHTTPClient(base_url=base_url) as client:
        for _ in range(200):
            seen = client.get("partial-body-seen", params={"key": key}, error_for_status=False)
            if seen.status == 200:
                return json.loads(seen.data)
            time.sleep(0.01)
    raise AssertionError(f"/partial-body recorded nothing for {key}")
