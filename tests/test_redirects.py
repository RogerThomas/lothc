from urllib.parse import urlsplit

import pytest
from _helpers import echoed_headers

from lothc import HTTPClient, HTTPTransportError, SyncHTTPClient


async def test_follow_redirects_true_by_default(client: HTTPClient) -> None:
    result = (await client.get("redirect", into=dict)).data

    assert result == {"id": 7, "name": "item-7"}


async def test_follow_redirects_false_returns_the_redirect_itself(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, follow_redirects=False) as client:
        result = await client.get("redirect")

    assert result.status == 302
    assert result.headers["location"] == "/items/7"


def test_sync_follow_redirects_true_by_default(sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("redirect", into=dict).data

    assert result == {"id": 7, "name": "item-7"}


def test_sync_follow_redirects_false_returns_the_redirect_itself(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, follow_redirects=False) as client:
        result = client.get("redirect")

    assert result.status == 302
    assert result.headers["location"] == "/items/7"


async def test_the_response_request_is_the_original_target_after_a_redirect(
    base_url: str, client: HTTPClient
) -> None:
    response = await client.get("redirect")

    assert response.request.url == f"{base_url}redirect"
    assert response.request.path == "/redirect"


def test_sync_the_response_request_is_the_original_target_after_a_redirect(
    base_url: str, sync_client: SyncHTTPClient
) -> None:
    response = sync_client.get("redirect")

    assert response.request.url == f"{base_url}redirect"
    assert response.request.path == "/redirect"


def _other_host_url(base_url: str, path: str) -> str:
    # Another host name for the same test server (the clients below `resolve` it to 127.0.0.1),
    # so a redirect there is cross-origin without a second server.
    return f"http://other.test:{urlsplit(base_url).port}/{path}"


async def test_a_redirect_to_another_host_drops_credentials_but_keeps_other_headers(
    base_url: str,
) -> None:
    async with HTTPClient(
        base_url=base_url, bearer_token="token-value", resolve={"other.test": "127.0.0.1"}
    ) as client:
        result = (
            await client.get(
                "redirect-to",
                params={"to": _other_host_url(base_url, "echo-headers")},
                headers={"cookie": "name=value", "x-custom": "custom"},
                into=dict,
            )
        ).data

    headers = echoed_headers(result)
    assert headers["host"].startswith("other.test")
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert headers["x-custom"] == "custom"


def test_sync_a_redirect_to_another_host_drops_credentials_but_keeps_other_headers(
    base_url: str,
) -> None:
    with SyncHTTPClient(
        base_url=base_url, bearer_token="token-value", resolve={"other.test": "127.0.0.1"}
    ) as client:
        result = client.get(
            "redirect-to",
            params={"to": _other_host_url(base_url, "echo-headers")},
            headers={"cookie": "name=value", "x-custom": "custom"},
            into=dict,
        ).data

    headers = echoed_headers(result)
    assert headers["host"].startswith("other.test")
    assert "authorization" not in headers
    assert "cookie" not in headers
    assert headers["x-custom"] == "custom"


async def test_a_redirect_on_the_same_host_keeps_credentials(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, bearer_token="token-value") as client:
        result = (
            await client.get(
                "redirect-to",
                params={"to": "/echo-headers"},
                headers={"cookie": "name=value"},
                into=dict,
            )
        ).data

    headers = echoed_headers(result)
    assert headers["authorization"] == "Bearer token-value"
    assert headers["cookie"] == "name=value"


def test_sync_a_redirect_on_the_same_host_keeps_credentials(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, bearer_token="token-value") as client:
        result = client.get(
            "redirect-to",
            params={"to": "/echo-headers"},
            headers={"cookie": "name=value"},
            into=dict,
        ).data

    headers = echoed_headers(result)
    assert headers["authorization"] == "Bearer token-value"
    assert headers["cookie"] == "name=value"


@pytest.mark.parametrize("status", [301, 302, 303])
async def test_a_post_redirected_by_301_302_or_303_becomes_a_get_without_its_body(
    client: HTTPClient, status: int
) -> None:
    result = (
        await client.post(
            "redirect-to",
            params={"to": "/echo-method", "status": status},
            json={"a": 1},
            into=dict,
        )
    ).data

    assert result == {"method": "GET", "body": "", "content_type": None}


@pytest.mark.parametrize("status", [301, 302, 303])
def test_sync_a_post_redirected_by_301_302_or_303_becomes_a_get_without_its_body(
    sync_client: SyncHTTPClient, status: int
) -> None:
    result = sync_client.post(
        "redirect-to",
        params={"to": "/echo-method", "status": status},
        json={"a": 1},
        into=dict,
    ).data

    assert result == {"method": "GET", "body": "", "content_type": None}


@pytest.mark.parametrize("status", [307, 308])
async def test_a_post_redirected_by_307_or_308_keeps_its_method_and_body(
    client: HTTPClient, status: int
) -> None:
    result = (
        await client.post(
            "redirect-to",
            params={"to": "/echo-method", "status": status},
            json={"a": 1},
            into=dict,
        )
    ).data

    assert result == {"method": "POST", "body": '{"a":1}', "content_type": "application/json"}


@pytest.mark.parametrize("status", [307, 308])
def test_sync_a_post_redirected_by_307_or_308_keeps_its_method_and_body(
    sync_client: SyncHTTPClient, status: int
) -> None:
    result = sync_client.post(
        "redirect-to",
        params={"to": "/echo-method", "status": status},
        json={"a": 1},
        into=dict,
    ).data

    assert result == {"method": "POST", "body": '{"a":1}', "content_type": "application/json"}


@pytest.mark.parametrize(
    ("status", "method"),
    [(301, "PUT"), (302, "PUT"), (303, "GET"), (307, "PUT"), (308, "PUT")],
)
async def test_a_put_becomes_a_get_only_on_a_303(
    client: HTTPClient, status: int, method: str
) -> None:
    result = (
        await client.put(
            "redirect-to",
            params={"to": "/echo-method", "status": status},
            content="content",
            into=dict,
        )
    ).data

    assert result["method"] == method
    assert result["body"] == ("" if method == "GET" else "content")


@pytest.mark.parametrize(
    ("status", "method"),
    [(301, "PUT"), (302, "PUT"), (303, "GET"), (307, "PUT"), (308, "PUT")],
)
def test_sync_a_put_becomes_a_get_only_on_a_303(
    sync_client: SyncHTTPClient, status: int, method: str
) -> None:
    result = sync_client.put(
        "redirect-to",
        params={"to": "/echo-method", "status": status},
        content="content",
        into=dict,
    ).data

    assert result["method"] == method
    assert result["body"] == ("" if method == "GET" else "content")


# A multipart form with parts is streamed, so a redirect that would have to send it again isn't
# followed (tower-http can't copy the body): its 307 or 308, or a PUT's 301 or 302, is the response.
@pytest.mark.parametrize("status", [307, 308])
async def test_a_form_post_redirected_by_307_or_308_is_not_followed(
    client: HTTPClient, status: int
) -> None:
    response = await client.post(
        "redirect-to", params={"to": "/echo-method", "status": status}, form={"a": "b"}
    )

    assert response.status == status


@pytest.mark.parametrize("status", [307, 308])
def test_sync_a_form_post_redirected_by_307_or_308_is_not_followed(
    sync_client: SyncHTTPClient, status: int
) -> None:
    response = sync_client.post(
        "redirect-to", params={"to": "/echo-method", "status": status}, form={"a": "b"}
    )

    assert response.status == status


@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_a_form_put_redirected_other_than_by_303_is_not_followed(
    client: HTTPClient, status: int
) -> None:
    response = await client.put(
        "redirect-to", params={"to": "/echo-method", "status": status}, form={"a": "b"}
    )

    assert response.status == status


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_sync_a_form_put_redirected_other_than_by_303_is_not_followed(
    sync_client: SyncHTTPClient, status: int
) -> None:
    response = sync_client.put(
        "redirect-to", params={"to": "/echo-method", "status": status}, form={"a": "b"}
    )

    assert response.status == status


@pytest.mark.parametrize("status", [301, 302, 303])
async def test_a_form_post_redirected_by_301_302_or_303_becomes_a_get_without_it(
    client: HTTPClient, status: int
) -> None:
    result = (
        await client.post(
            "redirect-to",
            params={"to": "/echo-method", "status": status},
            form={"a": "b"},
            into=dict,
        )
    ).data

    assert result == {"method": "GET", "body": "", "content_type": None}


@pytest.mark.parametrize("status", [301, 302, 303])
def test_sync_a_form_post_redirected_by_301_302_or_303_becomes_a_get_without_it(
    sync_client: SyncHTTPClient, status: int
) -> None:
    result = sync_client.post(
        "redirect-to", params={"to": "/echo-method", "status": status}, form={"a": "b"}, into=dict
    ).data

    assert result == {"method": "GET", "body": "", "content_type": None}


# An empty form is an empty body, which can be sent again.
@pytest.mark.parametrize("status", [307, 308])
async def test_an_empty_form_post_redirected_by_307_or_308_is_followed(
    client: HTTPClient, status: int
) -> None:
    result = (
        await client.post(
            "redirect-to", params={"to": "/echo-method", "status": status}, form={}, into=dict
        )
    ).data

    assert (result["method"], result["body"]) == ("POST", "")
    assert result["content_type"].startswith("multipart/form-data; boundary=")


@pytest.mark.parametrize("status", [307, 308])
def test_sync_an_empty_form_post_redirected_by_307_or_308_is_followed(
    sync_client: SyncHTTPClient, status: int
) -> None:
    result = sync_client.post(
        "redirect-to", params={"to": "/echo-method", "status": status}, form={}, into=dict
    ).data

    assert (result["method"], result["body"]) == ("POST", "")
    assert result["content_type"].startswith("multipart/form-data; boundary=")


async def test_max_redirects_stops_a_longer_chain(base_url: str) -> None:
    # A finite two-hop chain, so reqwest's own default limit (10) can't be what stops it.
    async with HTTPClient(base_url=base_url, max_redirects=1) as client:
        with pytest.raises(HTTPTransportError):
            await client.get("redirect-chain", params={"hops": 2})


def test_sync_max_redirects_stops_a_longer_chain(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_redirects=1) as client,
        pytest.raises(HTTPTransportError),
    ):
        client.get("redirect-chain", params={"hops": 2})


async def test_max_redirects_follows_a_chain_exactly_that_long(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_redirects=2) as client:
        result = (await client.get("redirect-chain", params={"hops": 2}, into=dict)).data

    assert result == {"hops": "done"}


def test_sync_max_redirects_follows_a_chain_exactly_that_long(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_redirects=2) as client:
        result = client.get("redirect-chain", params={"hops": 2}, into=dict).data

    assert result == {"hops": "done"}


async def test_a_redirect_to_a_non_http_scheme_raises_transport_error(client: HTTPClient) -> None:
    with pytest.raises(HTTPTransportError):
        await client.get("redirect-to", params={"to": "ftp://127.0.0.1/file"})


def test_sync_a_redirect_to_a_non_http_scheme_raises_transport_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPTransportError):
        sync_client.get("redirect-to", params={"to": "ftp://127.0.0.1/file"})
