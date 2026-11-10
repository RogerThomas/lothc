"""Small helpers shared by several test modules."""

from typing import Any

import pytest


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
