import asyncio
from importlib.metadata import version
from urllib.parse import urlsplit

import pytest
from pydantic import BaseModel

from lothc import HTTPClient, HTTPConnectionError, SyncHTTPClient


class _DefaultHeaders(BaseModel):
    x_api_key: str
    x_unset: str | None = None


async def _async_bearer_auth() -> str:
    return "token-value"


def _sync_bearer_auth() -> str:
    return "token-value"


async def test_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with pytest.raises(ValueError, match="at most one"):
        async with HTTPClient(
            base_url=base_url, bearer_token="token-value", bearer_auth=_async_bearer_auth
        ):
            pass


async def test_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    async with HTTPClient(timeout=None) as client:
        result = (await client.get(f"{base_url}items/7", response_data_type=dict)).data

    assert result == {"id": 7, "name": "item-7"}


async def test_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_redirects=5) as client:
        result = await client.get("redirect")

    assert result.status == 200


async def test_build_with_proxy_configured_does_not_error(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, proxy="http://127.0.0.1:1"):
        pass


def test_sync_build_rejects_both_bearer_token_and_bearer_auth(base_url: str) -> None:
    with (
        pytest.raises(ValueError, match="at most one"),
        SyncHTTPClient(
            base_url=base_url, bearer_token="token-value", bearer_auth=_sync_bearer_auth
        ),
    ):
        pass


def test_sync_build_with_no_base_url_accepts_absolute_urls(base_url: str) -> None:
    with SyncHTTPClient(timeout=None) as client:
        result = client.get(f"{base_url}items/7", response_data_type=dict).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_build_with_default_headers_sent_on_every_request(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, default_headers={"x-api-key": "secret"}) as client:
        result = client.get("echo-headers", response_data_type=dict).data

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-api-key"] == "secret"


def test_sync_build_with_max_redirects_still_follows_redirect(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_redirects=5) as client:
        result = client.get("redirect")

    assert result.status == 200


def test_sync_build_with_proxy_configured_does_not_error(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, proxy="http://127.0.0.1:1"):
        pass


async def test_build_accepts_a_model_as_default_headers(base_url: str) -> None:
    # Encoded like per-request `headers=`: `_` becomes `-`, and a `None` field is omitted.
    async with HTTPClient(
        base_url=base_url, default_headers=_DefaultHeaders(x_api_key="secret")
    ) as client:
        result = (await client.get("echo-headers", response_data_type=dict)).data

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["x-api-key"] == "secret"
    assert "x-unset" not in headers


async def _fetch_once(client: HTTPClient) -> dict[str, object]:
    async with client:
        return (await client.get("items/7", response_data_type=dict)).data


async def test_client_used_before_it_is_opened_raises_a_clear_error(base_url: str) -> None:
    client = HTTPClient(base_url=base_url)

    with pytest.raises(RuntimeError, match="never opened"):
        await client.get("items/7")


def test_sync_client_used_before_it_is_opened_raises_a_clear_error(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)

    with pytest.raises(RuntimeError, match="never opened"):
        client.get("items/7")


async def test_client_can_be_opened_again_after_it_closes(base_url: str) -> None:
    # Each entry builds a fresh pyreqwest client from the stored settings.
    client = HTTPClient(base_url=base_url)
    async with client:
        first = (await client.get("items/7", response_data_type=dict)).data
    async with client:
        second = (await client.get("items/7", response_data_type=dict)).data

    assert first == second == {"id": 7, "name": "item-7"}


def test_sync_client_can_be_opened_again_after_it_closes(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)
    with client:
        first = client.get("items/7", response_data_type=dict).data
    with client:
        second = client.get("items/7", response_data_type=dict).data

    assert first == second


async def test_entering_an_already_open_client_raises(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        with pytest.raises(RuntimeError, match="already open"):
            await client.__aenter__()


def test_sync_entering_an_already_open_client_raises(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url) as client,
        pytest.raises(RuntimeError, match="already open"),
    ):
        client.__enter__()


def test_conflicting_auth_is_rejected_when_the_client_is_constructed() -> None:
    # At construction, before anything is opened, not on entry.
    with pytest.raises(ValueError, match="at most one"):
        HTTPClient(bearer_token="bearer-token", basic_auth=("user", "pass"))


def test_one_client_works_across_separate_event_loops(base_url: str) -> None:
    # A module-level client reused by separate `asyncio.run()` calls, each opening it afresh.
    client = HTTPClient(base_url=base_url)

    assert asyncio.run(_fetch_once(client)) == asyncio.run(_fetch_once(client))


async def test_a_request_in_flight_when_the_client_closes_raises_runtime_error(
    base_url: str,
) -> None:
    # pyreqwest's own ClientClosedError for the in-flight request is translated, not leaked.
    client = HTTPClient(base_url=base_url)
    await client.__aenter__()
    request = asyncio.create_task(client.get("slow", params={"seconds": 0.5}))
    await asyncio.sleep(0.1)
    await client.__aexit__(None, None, None)

    with pytest.raises(RuntimeError, match="client is closed"):
        await request


async def test_exiting_a_client_that_is_not_open_is_harmless(base_url: str) -> None:
    client = HTTPClient(base_url=base_url)
    await client.__aexit__(None, None, None)
    async with client:
        pass
    await client.__aexit__(None, None, None)


def test_sync_exiting_a_client_that_is_not_open_is_harmless(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url)
    client.__exit__(None, None, None)
    with client:
        pass
    client.__exit__(None, None, None)


async def test_user_agent_is_sent_on_every_request(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, user_agent="agent-name/1.0") as client:
        result = (await client.get("echo-headers", response_data_type=dict)).data

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["user-agent"] == "agent-name/1.0"


def test_sync_user_agent_is_sent_on_every_request(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, user_agent="agent-name/1.0") as client:
        result = client.get("echo-headers", response_data_type=dict).data

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["user-agent"] == "agent-name/1.0"


async def test_a_per_request_user_agent_header_overrides_user_agent(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, user_agent="agent-name/1.0") as client:
        result = (
            await client.get(
                "echo-headers", headers={"user-agent": "override/2.0"}, response_data_type=dict
            )
        ).data

    values = [h["value"] for h in result["headers"] if h["name"].lower() == "user-agent"]
    assert values == ["override/2.0"]


async def test_a_proxy_that_is_down_fails_requests(base_url: str) -> None:
    # The control for the `no_proxy` tests below: without an exclusion, requests go through the
    # (unreachable) proxy and fail.
    async with HTTPClient(base_url=base_url, proxy="http://127.0.0.1:1") as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("items/7")


async def test_no_proxy_hosts_bypass_the_proxy(base_url: str) -> None:
    async with HTTPClient(
        base_url=base_url, proxy="http://127.0.0.1:1", no_proxy=["127.0.0.1"]
    ) as client:
        result = (await client.get("items/7", response_data_type=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_no_proxy_hosts_bypass_the_proxy(base_url: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, proxy="http://127.0.0.1:1", no_proxy=["localhost", "127.0.0.1"]
    ) as client:
        result = client.get("items/7", response_data_type=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_no_proxy_that_does_not_match_still_uses_the_proxy(base_url: str) -> None:
    async with HTTPClient(
        base_url=base_url, proxy="http://127.0.0.1:1", no_proxy=["other-host"]
    ) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("items/7")


def test_no_proxy_without_a_proxy_is_rejected() -> None:
    with pytest.raises(ValueError, match="'no_proxy' needs a 'proxy'"):
        HTTPClient(no_proxy=["localhost"])


def test_sync_no_proxy_without_a_proxy_is_rejected() -> None:
    with pytest.raises(ValueError, match="'no_proxy' needs a 'proxy'"):
        SyncHTTPClient(no_proxy=["localhost"])


async def test_http2_falls_back_to_http1_against_an_http1_only_server(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, http2=True) as client:
        result = (await client.get("items/7", response_data_type=dict)).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_http2_falls_back_to_http1_against_an_http1_only_server(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, http2=True) as client:
        result = client.get("items/7", response_data_type=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_the_default_user_agent_names_lothc_and_its_version(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        result = (await client.get("echo-headers", response_data_type=dict)).data

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["user-agent"] == f"python-lothc/{version('lothc')}"


def test_sync_the_default_user_agent_names_lothc_and_its_version(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url) as client:
        result = client.get("echo-headers", response_data_type=dict).data

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["user-agent"] == f"python-lothc/{version('lothc')}"


async def test_resolve_sends_a_hostname_to_the_given_address(base_url: str) -> None:
    # `lothc.test` exists only through the override; the URL's own port is still used.
    port = urlsplit(base_url).port
    async with HTTPClient(resolve={"lothc.test": "127.0.0.1"}) as client:
        result = (
            await client.get(f"http://lothc.test:{port}/items/7", response_data_type=dict)
        ).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_resolve_sends_a_hostname_to_the_given_address(base_url: str) -> None:
    port = urlsplit(base_url).port
    with SyncHTTPClient(resolve={"lothc.test": "127.0.0.1"}) as client:
        result = client.get(f"http://lothc.test:{port}/items/7", response_data_type=dict).data

    assert result == {"id": 7, "name": "item-7"}


async def test_local_address_is_the_address_connections_come_from(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, local_address="127.0.0.1") as client:
        result = (await client.get("items/7", response_data_type=dict)).data

    assert result == {"id": 7, "name": "item-7"}


async def test_a_local_address_this_machine_does_not_have_fails_to_connect(base_url: str) -> None:
    # 192.0.2.1 is reserved for documentation (TEST-NET-1), so it can't be bound: proof that
    # `local_address` really reaches the socket.
    async with HTTPClient(base_url=base_url, local_address="192.0.2.1") as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("items/7")


async def test_tcp_keepalive_is_accepted(base_url: str) -> None:
    # Keepalive probes aren't observable from here; this only checks the setting is wired in
    # without breaking requests.
    async with HTTPClient(base_url=base_url, tcp_keepalive=30.0) as client:
        result = (await client.get("items/7", response_data_type=dict)).data

    assert result == {"id": 7, "name": "item-7"}


@pytest.mark.parametrize("base_url", ["http://host/api", "http://host/api/?key=value"])
def test_an_unjoinable_base_url_is_rejected_at_construction(base_url: str) -> None:
    with pytest.raises(ValueError, match="trailing slash"):
        HTTPClient(base_url=base_url)


def test_sync_an_unjoinable_base_url_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="trailing slash"):
        SyncHTTPClient(base_url="http://host/api")
