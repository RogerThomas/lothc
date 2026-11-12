"""Small helpers shared by several test modules."""

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from lothc import Form, HTTPClient, HTTPConnectionError, HTTPTimeoutError, SyncHTTPClient

type EventsParams = dict[str, str | float]


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


def _partial_body_seen(base_url: str, key: str) -> dict[str, Any] | None:
    """What `/partial-body` recorded for `key` (request line, headers, as much body as arrived),
    once it has (it records only after the client hangs up, which may be just after the client's
    own call raised), or `None` if nothing for `key` ever arrived."""
    with SyncHTTPClient(base_url=base_url) as client:
        for _ in range(200):
            seen = client.get("partial-body-seen", params={"key": key}, error_for_status=False)
            if seen.status == 200:
                return json.loads(seen.data)
            time.sleep(0.01)
    return None


async def post_failing_form(
    client: HTTPClient, base_url: str, key: str, form: Form
) -> dict[str, Any]:
    """What the server received of `form`, posted to `/partial-body`, whose file part opens but
    fails to read (`opens_but_fails_to_read`): the request fails, sent as far as that read.

    pyreqwest now and then sends nothing at all: when tokio's read of the file, on its blocking
    thread, has failed before hyper polls it, hyper drops what it had buffered unwritten (2 tries
    in 15,000 on a Mac held to its efficiency cores). So a try the server saw none of is made
    again, twice at most; whatever does arrive is checked."""
    for _ in range(3):
        with pytest.raises(HTTPConnectionError):
            await client.post("partial-body", params={"key": key}, form=form)
        if (seen := _partial_body_seen(base_url, key)) is not None:
            return seen
    raise AssertionError(f"/partial-body recorded nothing for {key}")


def sync_post_failing_form(
    client: SyncHTTPClient, base_url: str, key: str, form: Form
) -> dict[str, Any]:
    """`post_failing_form`, from a sync client."""
    for _ in range(3):
        with pytest.raises(HTTPConnectionError):
            client.post("partial-body", params={"key": key}, form=form)
        if (seen := _partial_body_seen(base_url, key)) is not None:
            return seen
    raise AssertionError(f"/partial-body recorded nothing for {key}")


async def arrived(base_url: str, key: str) -> None:
    """Wait until the server has received a request counting `key` (`/slow?key=`), so one sent
    in the background is in flight, and under `max_connections` holds its slot: a fixed sleep
    can be outlasted by a stalled machine."""
    async with HTTPClient(base_url=base_url) as client:
        for _ in range(200):
            if (await client.get("hits", params={"key": key}, into=dict)).data["hits"]:
                return
            await asyncio.sleep(0.01)
    raise AssertionError(f"no request for {key} arrived")


def sync_arrived(base_url: str, key: str) -> None:
    """`arrived`, for a request sent on another thread."""
    with SyncHTTPClient(base_url=base_url) as client:
        for _ in range(200):
            if client.get("hits", params={"key": key}, into=dict).data["hits"]:
                return
            time.sleep(0.01)
    raise AssertionError(f"no request for {key} arrived")


def closed(base_url: str, key: str) -> None:
    """Wait until `/large?key=` (or `/silent-close`, `/connection-close`, `/events-silent-close`)
    has closed its connection behind its response, as a fixed sleep would only guess. From a
    client of its own and blocking, even in an async test: no turn of the event loop reports the
    close to the client under test before its next request, as when its caller is busy, so it
    must notice the close on its own."""
    with SyncHTTPClient(base_url=base_url) as client:
        for _ in range(200):
            if client.get("hits", params={"key": f"{key}-closed"}, into=dict).data["hits"]:
                return
            time.sleep(0.01)
    raise AssertionError(f"the server never closed the connection for {key}")


def _longest_server_gap(base_url: str, key: str) -> float:
    """The longest `/events?key=` went between two writes, from the client's `sent`, once it has
    finished (after its client hung up, it finds out on its next write)."""
    with SyncHTTPClient(base_url=base_url) as client:
        for _ in range(200):
            seen = client.get("longest-gap", params={"key": key}, into=dict).data
            if seen["done"]:
                return float(seen["seconds"])
            time.sleep(0.01)
    raise AssertionError(f"/events never finished for {key}")


async def unless_the_server_stalled[T](
    fetch: Callable[[EventsParams], Awaitable[T]], base_url: str, key: str, limit: float
) -> T:
    """`fetch` of `/events` (its `params` given, with a `key` and a `sent`), made again, for up to
    2s, while it times out because the server itself went longer than `limit` between two writes
    (most of it, so a gap the client's clock started on a little late counts), as a busy machine
    makes it now and then: a gap limit is right to give up then, and the try shows nothing about
    the shorter gaps under test. A timeout without such a gap fails the test."""
    deadline = time.monotonic() + 2.0
    attempt = 0
    while True:
        attempt_key = f"{key}-{attempt}"
        try:
            return await fetch({"key": attempt_key, "sent": time.monotonic()})
        except HTTPTimeoutError:
            stalled = _longest_server_gap(base_url, attempt_key) >= 0.8 * limit
            if not stalled or time.monotonic() > deadline:
                raise
        attempt += 1


def sync_unless_the_server_stalled[T](
    fetch: Callable[[EventsParams], T], base_url: str, key: str, limit: float
) -> T:
    """`unless_the_server_stalled`, for a sync `fetch`."""
    deadline = time.monotonic() + 2.0
    attempt = 0
    while True:
        attempt_key = f"{key}-{attempt}"
        try:
            return fetch({"key": attempt_key, "sent": time.monotonic()})
        except HTTPTimeoutError:
            stalled = _longest_server_gap(base_url, attempt_key) >= 0.8 * limit
            if not stalled or time.monotonic() > deadline:
                raise
        attempt += 1


async def release(base_url: str, key: str) -> None:
    """Answer the `/slow?key=` request in flight now, however long it asked to wait: a slot held
    until the test is done with it can't be outlasted by a stalled machine, as a fixed hold can."""
    async with HTTPClient(base_url=base_url) as client:
        await client.get("release", params={"key": key})


def sync_release(base_url: str, key: str) -> None:
    """`release`, from a sync test."""
    with SyncHTTPClient(base_url=base_url) as client:
        client.get("release", params={"key": key})
