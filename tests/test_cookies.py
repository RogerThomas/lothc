import json
from collections.abc import AsyncGenerator, Generator
from typing import Any
from urllib.parse import urlsplit

import pytest
from _helpers import clear_proxy_variables
from _oauth_server import TokenServer
from _proxy_server import serve_http_proxy

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


@pytest.fixture(name="cookie_client")
async def _cookie_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, cookie_store=True) as client:
        yield client


@pytest.fixture(name="sync_cookie_client")
def _sync_cookie_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, cookie_store=True) as client:
        yield client


async def test_cookie_store_disabled_by_default(client: HTTPClient) -> None:
    await client.get("set-cookie")
    result = (await client.get("read-cookie", into=dict)).data

    assert result["cookie"] is None


async def test_cookie_store_persists_cookies_across_requests(cookie_client: HTTPClient) -> None:
    await cookie_client.get("set-cookie")
    result = (await cookie_client.get("read-cookie", into=dict)).data

    assert result["cookie"] == "session=abc123"


def test_sync_cookie_store_disabled_by_default(sync_client: SyncHTTPClient) -> None:
    sync_client.get("set-cookie")
    result = sync_client.get("read-cookie", into=dict).data

    assert result["cookie"] is None


def test_sync_cookie_store_persists_cookies_across_requests(
    sync_cookie_client: SyncHTTPClient,
) -> None:
    sync_cookie_client.get("set-cookie")
    result = sync_cookie_client.get("read-cookie", into=dict).data

    assert result["cookie"] == "session=abc123"


async def test_a_cookie_set_by_a_redirect_is_sent_to_its_target(cookie_client: HTTPClient) -> None:
    result = (
        await cookie_client.get(
            "redirect-to",
            params={"to": "/read-cookie", "cookie": "session=redirected"},
            into=dict,
        )
    ).data

    assert result["cookie"] == "session=redirected"


def test_sync_a_cookie_set_by_a_redirect_is_sent_to_its_target(
    sync_cookie_client: SyncHTTPClient,
) -> None:
    result = sync_cookie_client.get(
        "redirect-to",
        params={"to": "/read-cookie", "cookie": "session=redirected"},
        into=dict,
    ).data

    assert result["cookie"] == "session=redirected"


async def test_the_cookie_jar_starts_empty_each_time_the_client_is_opened(base_url: str) -> None:
    # Each entry builds a fresh connection pool and jar (CLAUDE.md, "Client lifecycle").
    client = HTTPClient(base_url=base_url, cookie_store=True)
    async with client:
        await client.get("set-cookie")
    async with client:
        result = (await client.get("read-cookie", into=dict)).data

    assert result["cookie"] is None


def test_sync_the_cookie_jar_starts_empty_each_time_the_client_is_opened(base_url: str) -> None:
    client = SyncHTTPClient(base_url=base_url, cookie_store=True)
    with client:
        client.get("set-cookie")
    with client:
        result = client.get("read-cookie", into=dict).data

    assert result["cookie"] is None


# `/cookies/...` (`_server.py`) sets each `set=` value as a `Set-Cookie` of its own, and reports
# the `Cookie` the request came with.


async def _sent(client: HTTPClient, path: str = "cookies/read", **call: Any) -> str | None:  # noqa: ANN401 — a verb's keyword arguments
    return (await client.get(path, into=dict, **call)).data["cookie"]


def _sync_sent(client: SyncHTTPClient, path: str = "cookies/read", **call: Any) -> str | None:  # noqa: ANN401 — a verb's keyword arguments
    return client.get(path, into=dict, **call).data["cookie"]


def _other_origin(base_url: str) -> str:
    """`base_url` by another host name, `localhost`: the same server, but another cookie host."""
    return base_url.replace("127.0.0.1", "localhost")


async def test_a_request_sends_the_jars_cookies_in_the_order_they_were_first_set(
    cookie_client: HTTPClient,
) -> None:
    await cookie_client.get("multi-set-cookie")
    await cookie_client.get("cookies/set", params={"set": "csrf=replaced; Path=/"})

    assert await _sent(cookie_client) == "session=abc; csrf=replaced; theme=dark"


def test_sync_a_request_sends_the_jars_cookies_in_the_order_they_were_first_set(
    sync_cookie_client: SyncHTTPClient,
) -> None:
    sync_cookie_client.get("multi-set-cookie")
    sync_cookie_client.get("cookies/set", params={"set": "csrf=replaced; Path=/"})

    assert _sync_sent(sync_cookie_client) == "session=abc; csrf=replaced; theme=dark"


async def test_a_cookie_is_sent_only_under_its_path(cookie_client: HTTPClient) -> None:
    await cookie_client.get(
        "cookies/set", params={"set": ["a=1; Path=/cookies/a", "b=2; Path=/cookies/b"]}
    )
    # Without `Path`, the default path is the setting URL's directory.
    await cookie_client.get("cookies/dir/set", params={"set": "c=3"})

    assert await _sent(cookie_client, "cookies/a/read") == "a=1"
    assert await _sent(cookie_client, "cookies/dir/read") == "c=3"
    assert await _sent(cookie_client, "cookies/read") is None


def test_sync_a_cookie_is_sent_only_under_its_path(sync_cookie_client: SyncHTTPClient) -> None:
    sync_cookie_client.get(
        "cookies/set", params={"set": ["a=1; Path=/cookies/a", "b=2; Path=/cookies/b"]}
    )
    sync_cookie_client.get("cookies/dir/set", params={"set": "c=3"})

    assert _sync_sent(sync_cookie_client, "cookies/a/read") == "a=1"
    assert _sync_sent(sync_cookie_client, "cookies/dir/read") == "c=3"
    assert _sync_sent(sync_cookie_client, "cookies/read") is None


def _expiring_cookies() -> list[str]:
    return [
        "a=1; Path=/; Max-Age=0",
        "a=1; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
    ]


@pytest.mark.parametrize("expiring", _expiring_cookies())
async def test_an_expired_cookie_removes_the_jars_cookie(
    cookie_client: HTTPClient, expiring: str
) -> None:
    await cookie_client.get("cookies/set", params={"set": ["a=1; Path=/", "b=2; Path=/"]})
    await cookie_client.get("cookies/set", params={"set": expiring})

    assert await _sent(cookie_client) == "b=2"


@pytest.mark.parametrize("expiring", _expiring_cookies())
def test_sync_an_expired_cookie_removes_the_jars_cookie(
    sync_cookie_client: SyncHTTPClient, expiring: str
) -> None:
    sync_cookie_client.get("cookies/set", params={"set": ["a=1; Path=/", "b=2; Path=/"]})
    sync_cookie_client.get("cookies/set", params={"set": expiring})

    assert _sync_sent(sync_cookie_client) == "b=2"


async def test_a_cookie_is_not_sent_to_another_host_and_one_for_another_domain_is_refused(
    base_url: str, cookie_client: HTTPClient
) -> None:
    await cookie_client.get(
        "cookies/set", params={"set": ["a=1; Path=/", "b=2; Path=/; Domain=example.com"]}
    )

    assert await _sent(cookie_client) == "a=1"
    assert await _sent(cookie_client, f"{_other_origin(base_url)}cookies/read") is None


def test_sync_a_cookie_is_not_sent_to_another_host_and_one_for_another_domain_is_refused(
    base_url: str, sync_cookie_client: SyncHTTPClient
) -> None:
    sync_cookie_client.get(
        "cookies/set", params={"set": ["a=1; Path=/", "b=2; Path=/; Domain=example.com"]}
    )

    assert _sync_sent(sync_cookie_client) == "a=1"
    assert _sync_sent(sync_cookie_client, f"{_other_origin(base_url)}cookies/read") is None


async def test_a_cookie_goes_to_its_host_on_any_port_and_a_secure_one_to_loopback_too(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    # Both servers are `localhost`, on different ports. Plain http to a loopback host counts as
    # secure, as cookie_store has it (see the proxied test below for any other host).
    async with HTTPClient(cookie_store=True, root_certificates=[tls_ca_pem]) as client:
        await client.get(f"{https_base_url}cookies/set", params={"set": "s=1; Path=/; Secure"})

        assert await _sent(client, f"{https_base_url}cookies/read") == "s=1"
        assert await _sent(client, f"{_other_origin(base_url)}cookies/read") == "s=1"


def test_sync_a_cookie_goes_to_its_host_on_any_port_and_a_secure_one_to_loopback_too(
    base_url: str, https_base_url: str, tls_ca_pem: bytes
) -> None:
    with SyncHTTPClient(cookie_store=True, root_certificates=[tls_ca_pem]) as client:
        client.get(f"{https_base_url}cookies/set", params={"set": "s=1; Path=/; Secure"})

        assert _sync_sent(client, f"{https_base_url}cookies/read") == "s=1"
        assert _sync_sent(client, f"{_other_origin(base_url)}cookies/read") == "s=1"


async def test_a_cookie_header_of_the_callers_own_is_sent_instead_of_the_jars(
    cookie_client: HTTPClient,
) -> None:
    await cookie_client.get("set-cookie")

    assert await _sent(cookie_client, headers={"Cookie": "own=1"}) == "own=1"


def test_sync_a_cookie_header_of_the_callers_own_is_sent_instead_of_the_jars(
    sync_cookie_client: SyncHTTPClient,
) -> None:
    sync_cookie_client.get("set-cookie")

    assert _sync_sent(sync_cookie_client, headers={"Cookie": "own=1"}) == "own=1"


async def test_a_default_cookie_header_is_sent_instead_of_the_jars(base_url: str) -> None:
    async with HTTPClient(
        base_url=base_url, cookie_store=True, default_headers={"Cookie": "default=1"}
    ) as client:
        await client.get("set-cookie")

        assert await _sent(client) == "default=1"


def test_sync_a_default_cookie_header_is_sent_instead_of_the_jars(base_url: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, cookie_store=True, default_headers={"Cookie": "default=1"}
    ) as client:
        client.get("set-cookie")

        assert _sync_sent(client) == "default=1"


async def test_a_redirect_keeps_the_callers_cookie_on_its_origin_and_sends_the_jars_elsewhere(
    base_url: str, cookie_client: HTTPClient
) -> None:
    other = _other_origin(base_url)
    await cookie_client.get(f"{other}cookies/set", params={"set": "o=1; Path=/"})
    own = {"Cookie": "own=1"}

    same_origin = await _sent(
        cookie_client, "redirect-to", params={"to": "/cookies/read", "cookie": "r=1"}, headers=own
    )
    other_origin = await _sent(
        cookie_client, "redirect-to", params={"to": f"{other}cookies/read"}, headers=own
    )

    assert (same_origin, other_origin) == ("own=1", "o=1")


def test_sync_a_redirect_keeps_the_callers_cookie_on_its_origin_and_sends_the_jars_elsewhere(
    base_url: str, sync_cookie_client: SyncHTTPClient
) -> None:
    other = _other_origin(base_url)
    sync_cookie_client.get(f"{other}cookies/set", params={"set": "o=1; Path=/"})
    own = {"Cookie": "own=1"}

    same_origin = _sync_sent(
        sync_cookie_client,
        "redirect-to",
        params={"to": "/cookies/read", "cookie": "r=1"},
        headers=own,
    )
    other_origin = _sync_sent(
        sync_cookie_client, "redirect-to", params={"to": f"{other}cookies/read"}, headers=own
    )

    assert (same_origin, other_origin) == ("own=1", "o=1")


async def test_a_cookie_set_on_one_hop_is_sent_on_the_next_only_where_it_matches(
    base_url: str, cookie_client: HTTPClient
) -> None:
    other = _other_origin(base_url)
    # 127.0.0.1 sets `a` and redirects to localhost, which sets `b` and redirects back.
    back = f"{other}cookies/hop?set=b=2;%20Path=/&status=302&to={base_url}cookies/read"
    result = await _sent(
        cookie_client,
        "cookies/hop",
        params={"set": "a=1; Path=/", "status": 302, "to": back},
    )

    assert result == "a=1"
    assert await _sent(cookie_client, f"{other}cookies/read") == "b=2"


def test_sync_a_cookie_set_on_one_hop_is_sent_on_the_next_only_where_it_matches(
    base_url: str, sync_cookie_client: SyncHTTPClient
) -> None:
    other = _other_origin(base_url)
    back = f"{other}cookies/hop?set=b=2;%20Path=/&status=302&to={base_url}cookies/read"
    result = _sync_sent(
        sync_cookie_client,
        "cookies/hop",
        params={"set": "a=1; Path=/", "status": 302, "to": back},
    )

    assert result == "a=1"
    assert _sync_sent(sync_cookie_client, f"{other}cookies/read") == "b=2"


async def test_an_error_responses_cookies_are_kept(cookie_client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError):
        await cookie_client.get("cookies/set", params={"set": "e=1; Path=/", "status": 500})

    assert await _sent(cookie_client) == "e=1"


def test_sync_an_error_responses_cookies_are_kept(sync_cookie_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError):
        sync_cookie_client.get("cookies/set", params={"set": "e=1; Path=/", "status": 500})

    assert _sync_sent(sync_cookie_client) == "e=1"


async def test_a_head_responses_cookies_are_kept(cookie_client: HTTPClient) -> None:
    await cookie_client.head("cookies/set", params={"set": "h=1; Path=/"})

    assert await _sent(cookie_client) == "h=1"


def test_sync_a_head_responses_cookies_are_kept(sync_cookie_client: SyncHTTPClient) -> None:
    sync_cookie_client.head("cookies/set", params={"set": "h=1; Path=/"})

    assert _sync_sent(sync_cookie_client) == "h=1"


async def test_a_streamed_responses_cookies_are_kept_and_its_redirects_hop_with_them(
    cookie_client: HTTPClient,
) -> None:
    async for _ in cookie_client.stream_get("cookies/set", params={"set": "s=1; Path=/"}):
        pass
    hopped = cookie_client.stream_get(
        "redirect-to", params={"to": "/cookies/read", "cookie": "r=1"}
    )
    body = b"".join([chunk async for chunk in hopped])

    assert json.loads(body) == {"cookie": "s=1; r=1"}


def test_sync_a_streamed_responses_cookies_are_kept_and_its_redirects_hop_with_them(
    sync_cookie_client: SyncHTTPClient,
) -> None:
    for _ in sync_cookie_client.stream_get("cookies/set", params={"set": "s=1; Path=/"}):
        pass
    hopped = sync_cookie_client.stream_get(
        "redirect-to", params={"to": "/cookies/read", "cookie": "r=1"}
    )
    body = b"".join(list(hopped))

    assert json.loads(body) == {"cookie": "s=1; r=1"}


def _failing_once(server_key: str, status: int) -> dict[str, Any]:
    """`/cookies/...`'s parameters for a request whose first attempt fails, setting `f=1`."""
    return {"key": server_key, "fail_times": 1, "status": status, "set": "f=1; Path=/"}


async def test_a_retry_sends_the_cookies_its_failed_attempt_set(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(
        base_url=base_url, cookie_store=True, max_retries=1, backoff_base=0.001
    ) as client:
        sent = await _sent(client, "cookies/retried", params=_failing_once(server_key, 503))

    assert sent == "f=1"


def test_sync_a_retry_sends_the_cookies_its_failed_attempt_set(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(
        base_url=base_url, cookie_store=True, max_retries=1, backoff_base=0.001
    ) as client:
        sent = _sync_sent(client, "cookies/retried", params=_failing_once(server_key, 503))

    assert sent == "f=1"


async def test_a_reauthenticated_request_sends_the_cookies_its_401_set(
    base_url: str, server_key: str, token_server: TokenServer
) -> None:
    async with HTTPClient(
        base_url=base_url, cookie_store=True, bearer_auth=token_server.provider()
    ) as client:
        # A key of its own: `token_server`'s endpoint counts its requests on `server_key`.
        params = _failing_once(f"{server_key}-api", 401)
        sent = await _sent(client, "cookies/reauth", params=params)

    assert sent == "f=1"


def test_sync_a_reauthenticated_request_sends_the_cookies_its_401_set(
    base_url: str, server_key: str, token_server: TokenServer
) -> None:
    with SyncHTTPClient(
        base_url=base_url, cookie_store=True, bearer_auth=token_server.sync_provider()
    ) as client:
        params = _failing_once(f"{server_key}-api", 401)
        sent = _sync_sent(client, "cookies/reauth", params=params)

    assert sent == "f=1"


def _domain_cookies() -> tuple[str, ...]:
    return (
        "s=1; Path=/; Secure",
        "h=2; Path=/",
        "d=3; Path=/; Domain=a.test",
        # A public suffix, but reqwest's jar has no public suffix list, so it's kept.
        "t=4; Path=/; Domain=test",
        "o=5; Path=/; Domain=b.test",
    )


async def test_cookies_through_a_proxy_belong_to_the_target_host_and_its_domain(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    # The proxy sends every request on to the test server, whatever host it names.
    with serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy:
        async with HTTPClient(proxy=proxy.url, cookie_store=True) as client:
            await client.get("http://a.test/cookies/set", params={"set": _domain_cookies()})

            assert await _sent(client, "http://a.test/cookies/read") == "h=2; d=3; t=4"
            assert await _sent(client, "http://sub.a.test/cookies/read") == "d=3; t=4"
            assert await _sent(client, "http://b.test/cookies/read") == "t=4"


def test_sync_cookies_through_a_proxy_belong_to_the_target_host_and_its_domain(
    base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clear_proxy_variables(monkeypatch)
    with (
        serve_http_proxy(upstream_port=urlsplit(base_url).port) as proxy,
        SyncHTTPClient(proxy=proxy.url, cookie_store=True) as client,
    ):
        client.get("http://a.test/cookies/set", params={"set": _domain_cookies()})

        assert _sync_sent(client, "http://a.test/cookies/read") == "h=2; d=3; t=4"
        assert _sync_sent(client, "http://sub.a.test/cookies/read") == "d=3; t=4"
        assert _sync_sent(client, "http://b.test/cookies/read") == "t=4"
