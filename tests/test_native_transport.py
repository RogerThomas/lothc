"""Which transport a client gets, and what the native transport (`native=True`) refuses rather
than silently doing differently from the pyreqwest one: every client setting and request feature it
hasn't ported is a `NotImplementedError` naming it, at construction for a setting, on use for
anything else (a proxy the environment names included).
`test_transport_wire_parity.py` covers what it does support.

The transport a client got shows through `http2=True`: the native transport is HTTP/1.1 by design,
so it refuses that at construction, which stays true as other settings get ported. `LOTHC_TRANSPORT`
(`task test-native`'s override, which wins over `native=`) is set or unset by every selection test,
so each runs the same under `task test` and `task test-native`.
"""

import asyncio
import sys
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from _helpers import clear_proxy_variables

from lothc import HTTPClient, HTTPTimeoutError, SyncHTTPClient


@pytest.fixture(name="native_client")
async def _native_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, native=True) as client:
        yield client


@pytest.fixture(name="sync_native_client")
def _sync_native_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, native=True) as client:
        yield client


def _pyreqwest_selections() -> list[dict[str, Any]]:
    return [{"native": False}, {}]


def test_native_true_selects_the_native_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)

    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        HTTPClient(http2=True, native=True)


def test_sync_native_true_selects_the_native_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)

    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        SyncHTTPClient(http2=True, native=True)


@pytest.mark.parametrize("selection", _pyreqwest_selections(), ids=["native-false", "default"])
async def test_native_false_or_the_default_selects_pyreqwest(
    selection: dict[str, Any], base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)

    async with HTTPClient(base_url=base_url, http2=True, **selection) as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("selection", _pyreqwest_selections(), ids=["native-false", "default"])
def test_sync_native_false_or_the_default_selects_pyreqwest(
    selection: dict[str, Any], base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)

    with SyncHTTPClient(base_url=base_url, http2=True, **selection) as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_the_transport_override_wins_over_native_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOTHC_TRANSPORT", "native")

    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        HTTPClient(http2=True, native=False)


def test_sync_the_transport_override_wins_over_native_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTHC_TRANSPORT", "native")

    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        SyncHTTPClient(http2=True, native=False)


def _windows_native_selections() -> list[tuple[bool, str | None]]:
    """(the client's `native=`, `LOTHC_TRANSPORT`): the argument, then the override."""
    return [(True, None), (False, "native")]


def _select_transport_env(monkeypatch: pytest.MonkeyPatch, transport: str | None) -> None:
    if transport is None:
        monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)
    else:
        monkeypatch.setenv("LOTHC_TRANSPORT", transport)


@pytest.mark.parametrize(
    ("native", "transport"), _windows_native_selections(), ids=["native-true", "override"]
)
def test_native_transport_is_refused_on_windows(
    transport: str | None, monkeypatch: pytest.MonkeyPatch, *, native: bool
) -> None:
    _select_transport_env(monkeypatch, transport)
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(NotImplementedError, match="not supported on Windows"):
        HTTPClient(native=native)


@pytest.mark.parametrize(
    ("native", "transport"), _windows_native_selections(), ids=["native-true", "override"]
)
def test_sync_native_transport_is_refused_on_windows(
    transport: str | None, monkeypatch: pytest.MonkeyPatch, *, native: bool
) -> None:
    _select_transport_env(monkeypatch, transport)
    monkeypatch.setattr(sys, "platform", "win32")

    with pytest.raises(NotImplementedError, match="not supported on Windows"):
        SyncHTTPClient(native=native)


async def test_pyreqwest_still_works_on_windows(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    async with HTTPClient(base_url=base_url, native=False) as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_pyreqwest_still_works_on_windows(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    with SyncHTTPClient(base_url=base_url, native=False) as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def _unported_settings() -> list[tuple[str, Any]]:
    return [
        ("connect_timeout", 1.0),
        ("read_timeout", 1.0),
        ("http2", True),
        ("max_connections", 1),
    ]


@pytest.mark.parametrize(("setting", "value"), _unported_settings())
def test_native_transport_refuses_an_unported_setting(setting: str, value: object) -> None:
    settings: dict[str, Any] = {setting: value}

    with pytest.raises(NotImplementedError, match=f"{setting}= is not supported by the native"):
        HTTPClient(native=True, **settings)


@pytest.mark.parametrize(("setting", "value"), _unported_settings())
def test_sync_native_transport_refuses_an_unported_setting(setting: str, value: object) -> None:
    settings: dict[str, Any] = {setting: value}

    with pytest.raises(NotImplementedError, match=f"{setting}= is not supported by the native"):
        SyncHTTPClient(native=True, **settings)


def _unported_proxies() -> list[tuple[str, str]]:
    return [
        ("https://127.0.0.1:1", "an https:// proxy"),
        ("HTTPS://127.0.0.1:1", "an https:// proxy"),
        ("socks4://127.0.0.1:1", "a socks4:// proxy"),
        ("socks4a://127.0.0.1:1", "a socks4a:// proxy"),
    ]


@pytest.mark.parametrize(("proxy", "message"), _unported_proxies())
def test_native_transport_refuses_an_unported_proxy(proxy: str, message: str) -> None:
    with pytest.raises(NotImplementedError, match=f"{message} .*is not supported by the native"):
        HTTPClient(proxy=proxy, native=True)


@pytest.mark.parametrize(("proxy", "message"), _unported_proxies())
def test_sync_native_transport_refuses_an_unported_proxy(proxy: str, message: str) -> None:
    with pytest.raises(NotImplementedError, match=f"{message} .*is not supported by the native"):
        SyncHTTPClient(proxy=proxy, native=True)


def _unported_proxy_variables() -> list[tuple[str, str, str, str]]:
    """(variable, its value, the fixture of a server the request goes to, the refusal)."""
    return [
        ("HTTPS_PROXY", "https://127.0.0.1:1", "https_base_url", "an https:// proxy"),
        ("HTTP_PROXY", "https://127.0.0.1:1", "base_url", "an https:// proxy"),
        ("ALL_PROXY", "socks4://127.0.0.1:1", "base_url", "a socks4:// proxy"),
    ]


@pytest.mark.parametrize(
    ("variable", "value", "server_url_fixture", "message"), _unported_proxy_variables()
)
async def test_native_transport_refuses_an_unported_proxy_variable_when_a_request_uses_it(
    variable: str,
    value: str,
    server_url_fixture: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    clear_proxy_variables(monkeypatch)
    monkeypatch.setenv(variable, value)
    server_url: str = request.getfixturevalue(server_url_fixture)

    async with HTTPClient(base_url=server_url, native=True) as client:
        with pytest.raises(
            NotImplementedError, match=f"{message} .*is not supported by the native"
        ):
            await client.get("items/7")


@pytest.mark.parametrize(
    ("variable", "value", "server_url_fixture", "message"), _unported_proxy_variables()
)
def test_sync_native_transport_refuses_an_unported_proxy_variable_when_a_request_uses_it(
    variable: str,
    value: str,
    server_url_fixture: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    clear_proxy_variables(monkeypatch)
    monkeypatch.setenv(variable, value)
    server_url: str = request.getfixturevalue(server_url_fixture)

    with (
        SyncHTTPClient(base_url=server_url, native=True) as client,
        pytest.raises(NotImplementedError, match=f"{message} .*is not supported by the native"),
    ):
        client.get("items/7")


async def test_native_transport_sends_what_an_unported_proxy_variable_does_not_cover(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "https://127.0.0.1:1")

    async with HTTPClient(base_url=base_url, native=True) as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_native_transport_sends_what_an_unported_proxy_variable_does_not_cover(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "https://127.0.0.1:1")

    with SyncHTTPClient(base_url=base_url, native=True) as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


async def test_native_transport_pools_connections_per_origin(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(root_certificates=[tls_ca_pem], native=True) as client:
        results = [
            await client.get(f"{server_url}items/7", into=dict)
            for server_url in (base_url, https_base_url, base_url, https_base_url)
        ]

    assert [result.request.url for result in results] == [
        f"{server_url}items/7"
        for server_url in (base_url, https_base_url, base_url, https_base_url)
    ]


def test_sync_native_transport_pools_connections_per_origin(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(root_certificates=[tls_ca_pem], native=True) as client:
        results = [
            client.get(f"{server_url}items/7", into=dict)
            for server_url in (base_url, https_base_url, base_url, https_base_url)
        ]

    assert [result.request.url for result in results] == [
        f"{server_url}items/7"
        for server_url in (base_url, https_base_url, base_url, https_base_url)
    ]


async def test_native_transport_keeps_working_after_a_cancelled_request(
    native_client: HTTPClient,
) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(native_client.get("slow", params={"seconds": 5}), 0.05)

    assert (await native_client.get("items/7", into=dict)).status == 200


async def test_native_transport_times_out_a_request_in_flight(native_client: HTTPClient) -> None:
    with pytest.raises(HTTPTimeoutError):
        await native_client.get("slow", params={"seconds": 5}, timeout=0.05)

    assert (await native_client.get("items/7", into=dict)).status == 200


def test_sync_native_transport_times_out_a_request_in_flight(
    sync_native_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPTimeoutError):
        sync_native_client.get("slow", params={"seconds": 5}, timeout=0.05)

    assert sync_native_client.get("items/7", into=dict).status == 200
