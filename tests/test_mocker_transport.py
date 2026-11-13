"""What `lothc_mocker` sees of a request and does with a mocked response, which depends on where
it sits in the transport: as pyreqwest's mocker sees one, on either transport (each test runs on
the native one too, under `task test-native`). A mock answers a request as the client built it,
before the transport's sending adds anything or follows, retries, decompresses or times anything
out; lothc's own retries and re-authentication sit above it, so they go through the mocks again.
"""

import asyncio
import gzip
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lothc import HTTPClient, HTTPResponseError, HTTPTransportError, SyncHTTPClient
from lothc.testing import LOTHCMocker, MockRequest, MockResponse

# reqwest's boundary: four random 64-bit numbers, 16 hex digits each.
_multipart_content_type = re.compile(
    r"multipart/form-data; boundary=[0-9a-f]{16}(-[0-9a-f]{16}){3}"
)


class _Tokens:
    """A `bearer_auth` that can `invalidate()`, so a 401 re-authenticates."""

    def __init__(self) -> None:
        self.issued = 0
        self.invalidated: list[str | None] = []

    async def __call__(self) -> str:
        self.issued += 1
        return f"token-{self.issued}"

    def invalidate(self, stale_access_token: str | None = None) -> None:
        self.invalidated.append(stale_access_token)


class _SyncTokens:
    def __init__(self) -> None:
        self.issued = 0
        self.invalidated: list[str | None] = []

    def __call__(self) -> str:
        self.issued += 1
        return f"token-{self.issued}"

    def invalidate(self, stale_access_token: str | None = None) -> None:
        self.invalidated.append(stale_access_token)


async def _slow_handler(_request: MockRequest) -> MockResponse:
    await asyncio.sleep(0.05)
    return MockResponse(data=b"slow")


def _sync_slow_handler(_request: MockRequest) -> MockResponse:
    time.sleep(0.05)
    return MockResponse(data=b"slow")


async def _never(_request: MockRequest) -> bool:
    return False


def _sync_never(_request: MockRequest) -> bool:
    return False


# --- What a mock sees -----------------------------------------------------------------------------


async def test_a_mock_sees_default_headers_auth_and_headers_in_the_order_they_go_out(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items")

    async with HTTPClient(
        base_url=base_url, default_headers={"x-default": "d", "x-over": "default"}, bearer_token="t"
    ) as client:
        await client.get("items", headers={"x-over": "own", "X-Own": "o"})

    [seen] = mock.get_requests()
    # Nothing the transport adds while sending: no accept, user-agent, accept-encoding or host.
    assert list(seen.headers.items()) == [
        ("x-default", "d"),
        ("authorization", "Bearer t"),
        ("x-over", "own"),
        ("x-own", "o"),
    ]


def test_sync_a_mock_sees_default_headers_auth_and_headers_in_the_order_they_go_out(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items")

    with SyncHTTPClient(
        base_url=base_url, default_headers={"x-default": "d", "x-over": "default"}, bearer_token="t"
    ) as client:
        client.get("items", headers={"x-over": "own", "X-Own": "o"})

    [seen] = mock.get_requests()
    assert list(seen.headers.items()) == [
        ("x-default", "d"),
        ("authorization", "Bearer t"),
        ("x-over", "own"),
        ("x-own", "o"),
    ]


async def test_a_mock_sees_the_query_as_it_is_encoded(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items")

    await client.get("items?page=1", params={"tag": ["a b", "c&d"], "flag": True, "n": 2})

    [seen] = mock.get_requests()
    assert seen.query_string == "page=1&tag=a+b&tag=c%26d&flag=true&n=2"


def test_sync_a_mock_sees_the_query_as_it_is_encoded(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items")

    sync_client.get("items?page=1", params={"tag": ["a b", "c&d"], "flag": True, "n": 2})

    [seen] = mock.get_requests()
    assert seen.query_string == "page=1&tag=a+b&tag=c%26d&flag=true&n=2"


async def test_a_mock_sees_a_json_body_as_it_is_encoded(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_post_response(path="/items")

    await client.post("items", json={"b": 1, "a": [1, 2]}, headers={"content-type": "text/plain"})

    [seen] = mock.get_requests()
    assert seen.body == b'{"a":[1,2],"b":1}'
    assert seen.headers.get_all("content-type") == ["application/json"]


def test_sync_a_mock_sees_a_json_body_as_it_is_encoded(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_post_response(path="/items")

    sync_client.post("items", json={"b": 1, "a": [1, 2]}, headers={"content-type": "text/plain"})

    [seen] = mock.get_requests()
    assert seen.body == b'{"a":[1,2],"b":1}'
    assert seen.headers.get_all("content-type") == ["application/json"]


async def test_a_mock_sees_a_form_body_s_headers_but_not_its_body(
    client: HTTPClient, lothc_mocker: LOTHCMocker, tmp_path: Path
) -> None:
    (tmp_path / "f.txt").write_bytes(b"file-bytes")
    mock = lothc_mocker.add_post_response(path="/upload")

    await client.post("upload", form={"field": "v", "file": tmp_path / "f.txt"})
    await client.post("upload", data={"x": "1 2"})

    multipart, urlencoded = mock.get_requests()
    assert _multipart_content_type.fullmatch(multipart.headers["content-type"])
    assert multipart.headers["content-length"] == "369"
    assert urlencoded.headers["content-type"] == "application/x-www-form-urlencoded"
    assert (multipart.body, urlencoded.body) == (None, None)


def test_sync_a_mock_sees_a_form_body_s_headers_but_not_its_body(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker, tmp_path: Path
) -> None:
    (tmp_path / "f.txt").write_bytes(b"file-bytes")
    mock = lothc_mocker.add_post_response(path="/upload")

    sync_client.post("upload", form={"field": "v", "file": tmp_path / "f.txt"})
    sync_client.post("upload", data={"x": "1 2"})

    multipart, urlencoded = mock.get_requests()
    assert _multipart_content_type.fullmatch(multipart.headers["content-type"])
    assert multipart.headers["content-length"] == "369"
    assert urlencoded.headers["content-type"] == "application/x-www-form-urlencoded"
    assert (multipart.body, urlencoded.body) == (None, None)


@pytest.mark.lothc_mocker(strict=False)
async def test_a_mock_sees_no_cookies_and_keeps_none_it_sets(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/mocked-set", headers={"set-cookie": "mocked=1; Path=/"})
    mock = lothc_mocker.add_get_response(path="/mocked")

    async with HTTPClient(base_url=base_url, cookie_store=True) as client:
        await client.get("mocked-set")
        await client.get("set-cookie")  # a real one, from the server
        await client.get("mocked")
        result = (await client.get("read-cookie", into=dict)).data

    [seen] = mock.get_requests()
    assert "cookie" not in seen.headers
    assert result == {"cookie": "session=abc123"}


@pytest.mark.lothc_mocker(strict=False)
def test_sync_a_mock_sees_no_cookies_and_keeps_none_it_sets(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/mocked-set", headers={"set-cookie": "mocked=1; Path=/"})
    mock = lothc_mocker.add_get_response(path="/mocked")

    with SyncHTTPClient(base_url=base_url, cookie_store=True) as client:
        client.get("mocked-set")
        client.get("set-cookie")
        client.get("mocked")
        result = client.get("read-cookie", into=dict).data

    [seen] = mock.get_requests()
    assert "cookie" not in seen.headers
    assert result == {"cookie": "session=abc123"}


async def test_a_url_without_a_host_fails_before_any_mock_sees_it(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.mock()

    with pytest.raises(HTTPTransportError, match="URL scheme is not allowed"):
        await client.get("unix:/items")

    mock.assert_called(count=0)


def test_sync_a_url_without_a_host_fails_before_any_mock_sees_it(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.mock()

    with pytest.raises(HTTPTransportError, match="URL scheme is not allowed"):
        sync_client.get("unix:/items")

    mock.assert_called(count=0)


async def test_a_mock_answers_before_https_only_is_checked(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items", data=b"mocked")

    async with HTTPClient(base_url=base_url, https_only=True) as client:
        response = await client.get("items")

    assert response.data == b"mocked"


def test_sync_a_mock_answers_before_https_only_is_checked(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items", data=b"mocked")

    with SyncHTTPClient(base_url=base_url, https_only=True) as client:
        response = client.get("items")

    assert response.data == b"mocked"


# --- What a mocked response skips -----------------------------------------------------------------


async def test_a_mocked_redirect_is_not_followed(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/start", status=302, headers={"location": "/items"})

    response = await client.get("start")

    assert (response.status, response.headers["location"]) == (302, "/items")
    mock.assert_called(count=1)


def test_sync_a_mocked_redirect_is_not_followed(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/start", status=302, headers={"location": "/items"})

    response = sync_client.get("start")

    assert (response.status, response.headers["location"]) == (302, "/items")
    mock.assert_called(count=1)


async def test_a_mocked_compressed_body_is_not_decompressed(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    body = gzip.compress(b"data", mtime=0)
    lothc_mocker.add_get_response(path="/items", data=body, headers={"content-encoding": "gzip"})

    response = await client.get("items")

    assert response.data == body


def test_sync_a_mocked_compressed_body_is_not_decompressed(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    body = gzip.compress(b"data", mtime=0)
    lothc_mocker.add_get_response(path="/items", data=body, headers={"content-encoding": "gzip"})

    response = sync_client.get("items")

    assert response.data == body


async def test_a_mocked_request_ignores_the_client_timeout(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_slow_handler)

    async with HTTPClient(base_url=base_url, timeout=0.01) as client:
        response = await client.get("items")

    assert response.data == b"slow"


def test_sync_a_mocked_request_ignores_the_client_timeout(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_sync_slow_handler)

    with SyncHTTPClient(base_url=base_url, timeout=0.01) as client:
        response = client.get("items")

    assert response.data == b"slow"


async def test_a_mocked_request_takes_none_of_max_connections(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_slow_handler)

    async with HTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.01) as client:
        responses = await asyncio.gather(client.get("a"), client.get("b"))

    assert [response.data for response in responses] == [b"slow", b"slow"]


def test_sync_a_mocked_request_takes_none_of_max_connections(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_sync_slow_handler)

    with (
        SyncHTTPClient(base_url=base_url, max_connections=1, pool_timeout=0.01) as client,
        ThreadPoolExecutor(2) as threads,
    ):
        responses = list(threads.map(client.get, ["a", "b"]))

    assert [response.data for response in responses] == [b"slow", b"slow"]


async def test_a_mocked_error_status_raises_from_a_stream(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/ndjson", status=503, data=b"down")

    with pytest.raises(HTTPResponseError) as exc_info:
        _ = [line async for line in client.stream_get("ndjson")]

    assert (exc_info.value.status, exc_info.value.body_start) == (503, b"down")


def test_sync_a_mocked_error_status_raises_from_a_stream(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/ndjson", status=503, data=b"down")

    with pytest.raises(HTTPResponseError) as exc_info:
        _ = list(sync_client.stream_get("ndjson"))

    assert (exc_info.value.status, exc_info.value.body_start) == (503, b"down")


@pytest.mark.lothc_mocker(strict=False)
async def test_an_unmatched_stream_goes_to_the_server_when_not_strict(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lines = [line async for line in client.stream_get("ndjson", params={"count": 2}, into=dict)]

    assert lines == [{"i": 0}, {"i": 1}]
    assert lothc_mocker.get_call_count() == 0


@pytest.mark.lothc_mocker(strict=False)
def test_sync_an_unmatched_stream_goes_to_the_server_when_not_strict(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lines = list(sync_client.stream_get("ndjson", params={"count": 2}, into=dict))

    assert lines == [{"i": 0}, {"i": 1}]
    assert lothc_mocker.get_call_count() == 0


# --- lothc's own retries and re-authentication go through the mocks again ------------------------


async def test_a_retry_asks_the_mocks_again(base_url: str, lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_get_response(path="/items", status=503)

    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError):
            await client.get("items")

    mock.assert_called(count=3)


def test_sync_a_retry_asks_the_mocks_again(base_url: str, lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_get_response(path="/items", status=503)

    with (
        SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client,
        pytest.raises(HTTPResponseError),
    ):
        client.get("items")

    mock.assert_called(count=3)


async def test_a_mocked_401_reauthenticates_through_the_mocks(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", status=401)
    tokens = _Tokens()

    async with HTTPClient(base_url=base_url, bearer_auth=tokens) as client:
        with pytest.raises(HTTPResponseError):
            await client.get("items")

    assert [seen.headers["authorization"] for seen in mock.get_requests()] == [
        "Bearer token-1",
        "Bearer token-2",
    ]
    assert tokens.invalidated == ["token-1"]


def test_sync_a_mocked_401_reauthenticates_through_the_mocks(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", status=401)
    tokens = _SyncTokens()

    with (
        SyncHTTPClient(base_url=base_url, bearer_auth=tokens) as client,
        pytest.raises(HTTPResponseError),
    ):
        client.get("items")

    assert [seen.headers["authorization"] for seen in mock.get_requests()] == [
        "Bearer token-1",
        "Bearer token-2",
    ]
    assert tokens.invalidated == ["token-1"]


async def test_an_unmatched_request_fails_once_even_with_retries(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items").match_request(_never)

    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(AssertionError) as exc_info:
            await client.get("items", params={"a": "b"})

    assert str(exc_info.value) == (
        f"No mock answered GET {base_url}items?a=b\n"
        "The closest mock, GET path='/items':\n"
        "  match_request(_never) returned False\n"
        "lothc_mocker is strict: mock the request, or let unmocked requests through with "
        "@pytest.mark.lothc_mocker(strict=False)."
    )
    mock.assert_called(count=0)
    with pytest.raises(AssertionError, match=r"\n  match_request\(_never\) returned False$"):
        mock.assert_called()


def test_sync_an_unmatched_request_fails_once_even_with_retries(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items").match_request(_sync_never)

    with (
        SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client,
        pytest.raises(AssertionError) as exc_info,
    ):
        client.get("items", params={"a": "b"})

    assert str(exc_info.value) == (
        f"No mock answered GET {base_url}items?a=b\n"
        "The closest mock, GET path='/items':\n"
        "  match_request(_sync_never) returned False\n"
        "lothc_mocker is strict: mock the request, or let unmocked requests through with "
        "@pytest.mark.lothc_mocker(strict=False)."
    )
    mock.assert_called(count=0)
    with pytest.raises(AssertionError, match=r"\n  match_request\(_sync_never\) returned False$"):
        mock.assert_called()


async def test_assert_called_shows_the_closest_request_it_did_not_answer(
    base_url: str, client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", params={"page": "1"})
    lothc_mocker.add_get_response(path="/items")

    await client.get("items", params={"page": "2"})

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value) == (
        "The mock GET path='/items' answered 0 requests, expected exactly 1.\n"
        f"The closest request it didn't answer, GET {base_url}items?page=2:\n"
        "  query 'page': expected '1', got '2'"
    )


def test_sync_assert_called_shows_the_closest_request_it_did_not_answer(
    base_url: str, sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    mock = lothc_mocker.add_get_response(path="/items", params={"page": "1"})
    lothc_mocker.add_get_response(path="/items")

    sync_client.get("items", params={"page": "2"})

    with pytest.raises(AssertionError) as exc_info:
        mock.assert_called()
    assert str(exc_info.value) == (
        "The mock GET path='/items' answered 0 requests, expected exactly 1.\n"
        f"The closest request it didn't answer, GET {base_url}items?page=2:\n"
        "  query 'page': expected '1', got '2'"
    )


# --- Which clients are mocked ---------------------------------------------------------------------


async def test_a_client_opened_inside_the_test_is_mocked(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items", data=b"mocked")

    async with HTTPClient(base_url=base_url) as client:
        response = await client.get("items")

    assert response.data == b"mocked"


def test_sync_a_client_opened_inside_the_test_is_mocked(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items", data=b"mocked")

    with SyncHTTPClient(base_url=base_url) as client:
        response = client.get("items")

    assert response.data == b"mocked"
