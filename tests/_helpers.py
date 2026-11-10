"""Small helpers shared by several test modules."""

import json
import time
from typing import Any

import pytest

from lothc import SyncHTTPClient


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
