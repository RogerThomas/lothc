"""Which transport a client gets, and what the native transport refuses rather
than silently doing differently from the pyreqwest one: every client setting and request feature it
hasn't ported is a `NotImplementedError` naming it, at construction for a setting, on use for
anything else.
`test_transport_wire_parity.py` covers what it does support.

The transport a client got shows through `http2=True`: the native transport is HTTP/1.1 by design,
so under `native="on"` it refuses that at construction, which stays true as other settings get
ported. `"auto"` hands `http2=True` to pyreqwest instead, so its tests tell the transports apart by
how each words a connection error. `LOTHC_TRANSPORT` (`task test-native`'s and
`task test-pyreqwest`'s override, which wins over `native=`) is unset for every test here, and set
again by the ones about it, so each runs the same under every task.
"""

import asyncio
import sys
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest

from lothc import HTTPClient, HTTPConnectionError, HTTPTimeoutError, SyncHTTPClient

# The native transport is POSIX only: on Windows `native="on"` raises and `"auto"` picks
# pyreqwest, which the tests below that fake the platform check everywhere.
_native_only = pytest.mark.skipif(
    sys.platform == "win32", reason="the native transport is POSIX only"
)


@pytest.fixture(name="no_transport_override", autouse=True)
def _no_transport_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsets `LOTHC_TRANSPORT`, which wins over `native=`, so every test here gets the transport
    it asks for under every task; a test about the override sets it again."""
    monkeypatch.delenv("LOTHC_TRANSPORT", raising=False)


@pytest.fixture(name="native_client")
async def _native_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, native="on") as client:
        yield client


@pytest.fixture(name="sync_native_client")
def _sync_native_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, native="on") as client:
        yield client


def _pyreqwest_selections() -> list[dict[str, Any]]:
    return [{"native": "off"}, {}, {"native": "auto"}]


def _native_by_default_selections() -> list[dict[str, Any]]:
    return [{}, {"native": "auto"}]


def _invalid_modes() -> list[object]:
    return ["yes", True]


async def _connection_error(selection: dict[str, Any]) -> str:
    """What a request to a closed port raises, as text: the two transports word it differently
    (CLAUDE.md), which tells them apart where `http2=True`, which `"auto"` hands to pyreqwest,
    can't."""
    async with HTTPClient(base_url="http://127.0.0.1:1/", **selection) as client:
        with pytest.raises(HTTPConnectionError) as caught:
            await client.get("items/7")
    return str(caught.value)


def _sync_connection_error(selection: dict[str, Any]) -> str:
    with (
        SyncHTTPClient(base_url="http://127.0.0.1:1/", **selection) as client,
        pytest.raises(HTTPConnectionError) as caught,
    ):
        client.get("items/7")
    return str(caught.value)


@_native_only
def test_native_on_selects_the_native_transport() -> None:
    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        HTTPClient(http2=True, native="on")


@_native_only
def test_sync_native_on_selects_the_native_transport() -> None:
    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        SyncHTTPClient(http2=True, native="on")


@_native_only
@pytest.mark.parametrize("selection", _native_by_default_selections(), ids=["default", "auto"])
async def test_auto_or_the_default_selects_the_native_transport(selection: dict[str, Any]) -> None:
    error = await _connection_error(selection)

    assert error == await _connection_error({"native": "on"})
    assert error != await _connection_error({"native": "off"})


@_native_only
@pytest.mark.parametrize("selection", _native_by_default_selections(), ids=["default", "auto"])
def test_sync_auto_or_the_default_selects_the_native_transport(selection: dict[str, Any]) -> None:
    error = _sync_connection_error(selection)

    assert error == _sync_connection_error({"native": "on"})
    assert error != _sync_connection_error({"native": "off"})


@pytest.mark.parametrize(
    "selection", _pyreqwest_selections(), ids=["native-off", "default", "auto"]
)
async def test_native_off_or_auto_with_http2_selects_pyreqwest(
    selection: dict[str, Any], base_url: str
) -> None:
    async with HTTPClient(base_url=base_url, http2=True, **selection) as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize(
    "selection", _pyreqwest_selections(), ids=["native-off", "default", "auto"]
)
def test_sync_native_off_or_auto_with_http2_selects_pyreqwest(
    selection: dict[str, Any], base_url: str
) -> None:
    with SyncHTTPClient(base_url=base_url, http2=True, **selection) as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("native", _invalid_modes(), ids=["unknown", "bool"])
def test_an_invalid_native_mode_is_rejected(native: object) -> None:
    settings: dict[str, Any] = {"native": native}

    with pytest.raises(ValueError, match="'native' must be 'auto', 'off' or 'on'"):
        HTTPClient(**settings)


@pytest.mark.parametrize("native", _invalid_modes(), ids=["unknown", "bool"])
def test_sync_an_invalid_native_mode_is_rejected(native: object) -> None:
    settings: dict[str, Any] = {"native": native}

    with pytest.raises(ValueError, match="'native' must be 'auto', 'off' or 'on'"):
        SyncHTTPClient(**settings)


@_native_only
def test_the_transport_override_wins_over_native_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOTHC_TRANSPORT", "native")

    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        HTTPClient(http2=True, native="off")


@_native_only
def test_sync_the_transport_override_wins_over_native_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTHC_TRANSPORT", "native")

    with pytest.raises(NotImplementedError, match="http2= is not supported by the native"):
        SyncHTTPClient(http2=True, native="off")


async def test_the_pyreqwest_override_wins_over_native_on(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOTHC_TRANSPORT", "pyreqwest")

    async with HTTPClient(base_url=base_url, http2=True, native="on") as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_the_pyreqwest_override_wins_over_native_on(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOTHC_TRANSPORT", "pyreqwest")

    with SyncHTTPClient(base_url=base_url, http2=True, native="on") as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def _windows_native_selections() -> list[tuple[str, str | None]]:
    """(the client's `native=`, `LOTHC_TRANSPORT`): the argument, then the override."""
    return [("on", None), ("off", "native")]


def _select_transport_env(monkeypatch: pytest.MonkeyPatch, transport: str | None) -> None:
    if transport is not None:
        monkeypatch.setenv("LOTHC_TRANSPORT", transport)


@pytest.mark.parametrize(
    ("native", "transport"), _windows_native_selections(), ids=["native-on", "override"]
)
def test_native_transport_is_refused_on_windows(
    native: str, transport: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _select_transport_env(monkeypatch, transport)
    monkeypatch.setattr(sys, "platform", "win32")
    settings: dict[str, Any] = {"native": native}

    with pytest.raises(NotImplementedError, match="not supported on Windows"):
        HTTPClient(**settings)


@pytest.mark.parametrize(
    ("native", "transport"), _windows_native_selections(), ids=["native-on", "override"]
)
def test_sync_native_transport_is_refused_on_windows(
    native: str, transport: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _select_transport_env(monkeypatch, transport)
    monkeypatch.setattr(sys, "platform", "win32")
    settings: dict[str, Any] = {"native": native}

    with pytest.raises(NotImplementedError, match="not supported on Windows"):
        SyncHTTPClient(**settings)


@pytest.mark.parametrize("selection", _native_by_default_selections(), ids=["default", "auto"])
async def test_auto_or_the_default_selects_pyreqwest_on_windows(
    selection: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    assert await _connection_error(selection) == await _connection_error({"native": "off"})


@pytest.mark.parametrize("selection", _native_by_default_selections(), ids=["default", "auto"])
def test_sync_auto_or_the_default_selects_pyreqwest_on_windows(
    selection: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    assert _sync_connection_error(selection) == _sync_connection_error({"native": "off"})


async def test_pyreqwest_still_works_on_windows(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    async with HTTPClient(base_url=base_url, native="off") as client:
        result = await client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def test_sync_pyreqwest_still_works_on_windows(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    with SyncHTTPClient(base_url=base_url, native="off") as client:
        result = client.get("items/7", into=dict)

    assert result.data == {"id": 7, "name": "item-7"}


def _unported_settings() -> list[tuple[str, Any]]:
    return [
        ("http2", True),
    ]


@_native_only
@pytest.mark.parametrize(("setting", "value"), _unported_settings())
def test_native_transport_refuses_an_unported_setting(setting: str, value: object) -> None:
    settings: dict[str, Any] = {setting: value}

    with pytest.raises(NotImplementedError, match=f"{setting}= is not supported by the native"):
        HTTPClient(native="on", **settings)


@_native_only
@pytest.mark.parametrize(("setting", "value"), _unported_settings())
def test_sync_native_transport_refuses_an_unported_setting(setting: str, value: object) -> None:
    settings: dict[str, Any] = {setting: value}

    with pytest.raises(NotImplementedError, match=f"{setting}= is not supported by the native"):
        SyncHTTPClient(native="on", **settings)


@_native_only
async def test_native_transport_pools_connections_per_origin(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    async with HTTPClient(root_certificates=[tls_ca_pem], native="on") as client:
        results = [
            await client.get(f"{server_url}items/7", into=dict)
            for server_url in (base_url, https_base_url, base_url, https_base_url)
        ]

    assert [result.request.url for result in results] == [
        f"{server_url}items/7"
        for server_url in (base_url, https_base_url, base_url, https_base_url)
    ]


@_native_only
def test_sync_native_transport_pools_connections_per_origin(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(root_certificates=[tls_ca_pem], native="on") as client:
        results = [
            client.get(f"{server_url}items/7", into=dict)
            for server_url in (base_url, https_base_url, base_url, https_base_url)
        ]

    assert [result.request.url for result in results] == [
        f"{server_url}items/7"
        for server_url in (base_url, https_base_url, base_url, https_base_url)
    ]


@_native_only
async def test_native_transport_keeps_working_after_a_cancelled_request(
    native_client: HTTPClient,
) -> None:
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(native_client.get("slow", params={"seconds": 5}), 0.05)

    assert (await native_client.get("items/7", into=dict)).status == 200


@_native_only
async def test_native_transport_times_out_a_request_in_flight(native_client: HTTPClient) -> None:
    with pytest.raises(HTTPTimeoutError):
        await native_client.get("slow", params={"seconds": 5}, timeout=0.05)

    assert (await native_client.get("items/7", into=dict)).status == 200


@_native_only
def test_sync_native_transport_times_out_a_request_in_flight(
    sync_native_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPTimeoutError):
        sync_native_client.get("slow", params={"seconds": 5}, timeout=0.05)

    assert sync_native_client.get("items/7", into=dict).status == 200
