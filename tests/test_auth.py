import base64
from collections.abc import AsyncGenerator, Generator
from typing import Any

import pytest
from _helpers import echoed_header_values, echoed_headers

from lothc import HTTPClient, SyncHTTPClient


# Deliberately a plain class, not @dataclass (style-guide.md #1's usual preference) — confirmed
# live that zuban specifically mismatches a @dataclass-decorated class's __call__ against
# Callable[[], Awaitable[str]]/Callable[[], str] (its own error prints the "expected"/"got"
# signatures as textually identical, yet still rejects it), while mypy/ty/basedpyright all
# accept the @dataclass version fine. A plain class with the same __call__ satisfies all four.
class _CountingAuthProvider:
    def __init__(self) -> None:
        self._calls = 0

    async def __call__(self) -> str:
        self._calls += 1
        return f"token-{self._calls}"


class _SyncCountingAuthProvider:
    def __init__(self) -> None:
        self._calls = 0

    def __call__(self) -> str:
        self._calls += 1
        return f"token-{self._calls}"


@pytest.fixture(name="bearer_token_client")
async def _bearer_token_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, bearer_token="token-value") as client:
        yield client


@pytest.fixture(name="sync_bearer_token_client")
def _sync_bearer_token_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, bearer_token="token-value") as client:
        yield client


async def test_bearer_token_sends_authorization_header(bearer_token_client: HTTPClient) -> None:
    result = (await bearer_token_client.get("echo-headers", into=dict)).data

    headers = echoed_headers(result)
    assert headers["authorization"] == "Bearer token-value"


async def test_bearer_auth_callable_is_invoked_per_request(base_url: str) -> None:
    provider = _CountingAuthProvider()

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as client:
        first = (await client.get("echo-headers", into=dict)).data
        second = (await client.get("echo-headers", into=dict)).data

    first_headers = echoed_headers(first)
    second_headers = echoed_headers(second)
    assert first_headers["authorization"] == "Bearer token-1"
    assert second_headers["authorization"] == "Bearer token-2"


def test_sync_bearer_token_sends_authorization_header(
    sync_bearer_token_client: SyncHTTPClient,
) -> None:
    result = sync_bearer_token_client.get("echo-headers", into=dict).data

    headers = echoed_headers(result)
    assert headers["authorization"] == "Bearer token-value"


def test_sync_bearer_auth_callable_is_invoked_per_request(base_url: str) -> None:
    provider = _SyncCountingAuthProvider()

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as client:
        first = client.get("echo-headers", into=dict).data
        second = client.get("echo-headers", into=dict).data

    first_headers = echoed_headers(first)
    second_headers = echoed_headers(second)
    assert first_headers["authorization"] == "Bearer token-1"
    assert second_headers["authorization"] == "Bearer token-2"


async def test_skip_auth_omits_authorization_header_with_bearer_token(
    bearer_token_client: HTTPClient,
) -> None:
    result = (await bearer_token_client.get("echo-headers", into=dict, skip_auth=True)).data

    headers = echoed_headers(result)
    assert "authorization" not in headers


async def test_skip_auth_does_not_invoke_bearer_auth_callable(base_url: str) -> None:
    provider = _CountingAuthProvider()

    async with HTTPClient(base_url=base_url, bearer_auth=provider) as client:
        skipped = (await client.get("echo-headers", into=dict, skip_auth=True)).data
        # If the skipped call above had invoked `provider`, this next (non-skipped) call would
        # observe "token-2" instead of "token-1" — proving invocation without touching `provider`'s
        # own private `_calls` counter.
        subsequent = (await client.get("echo-headers", into=dict)).data

    skipped_headers = echoed_headers(skipped)
    subsequent_headers = echoed_headers(subsequent)
    assert "authorization" not in skipped_headers
    assert subsequent_headers["authorization"] == "Bearer token-1"


def test_sync_skip_auth_omits_authorization_header_with_bearer_token(
    sync_bearer_token_client: SyncHTTPClient,
) -> None:
    result = sync_bearer_token_client.get("echo-headers", into=dict, skip_auth=True).data

    headers = echoed_headers(result)
    assert "authorization" not in headers


def test_sync_skip_auth_does_not_invoke_bearer_auth_callable(base_url: str) -> None:
    provider = _SyncCountingAuthProvider()

    with SyncHTTPClient(base_url=base_url, bearer_auth=provider) as client:
        skipped = client.get("echo-headers", into=dict, skip_auth=True).data
        # Same reasoning as the async version above: a "token-1" on the next, non-skipped call
        # proves the skipped call never invoked `provider`, without touching its private state.
        subsequent = client.get("echo-headers", into=dict).data

    skipped_headers = echoed_headers(skipped)
    subsequent_headers = echoed_headers(subsequent)
    assert "authorization" not in skipped_headers
    assert subsequent_headers["authorization"] == "Bearer token-1"


async def test_basic_auth_sends_authorization_header(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, basic_auth=("username", "password")) as client:
        result = (await client.get("echo-headers", into=dict)).data

    headers = echoed_headers(result)
    expected = base64.b64encode(b"username:password").decode()
    assert headers["authorization"] == f"Basic {expected}"


def test_sync_basic_auth_sends_authorization_header(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, basic_auth=("username", "password")) as client:
        result = client.get("echo-headers", into=dict).data

    headers = echoed_headers(result)
    expected = base64.b64encode(b"username:password").decode()
    assert headers["authorization"] == f"Basic {expected}"


async def test_build_raises_when_more_than_one_auth_mechanism_provided(base_url: str) -> None:
    with pytest.raises(ValueError, match="Provide at most one of"):
        async with HTTPClient(
            base_url=base_url, bearer_token="token-value", basic_auth=("username", "password")
        ):
            pass


def test_sync_build_raises_when_more_than_one_auth_mechanism_provided(base_url: str) -> None:
    with (
        pytest.raises(ValueError, match="Provide at most one of"),
        SyncHTTPClient(
            base_url=base_url, bearer_token="token-value", basic_auth=("username", "password")
        ),
    ):
        pass


async def test_basic_auth_without_a_password_sends_an_empty_one(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, basic_auth=("username", None)) as client:
        result = (await client.get("echo-headers", into=dict)).data

    expected = base64.b64encode(b"username:").decode()
    assert echoed_headers(result)["authorization"] == f"Basic {expected}"


def test_sync_basic_auth_without_a_password_sends_an_empty_one(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, basic_auth=("username", None)) as client:
        result = client.get("echo-headers", into=dict).data

    expected = base64.b64encode(b"username:").decode()
    assert echoed_headers(result)["authorization"] == f"Basic {expected}"


@pytest.mark.parametrize(
    "auth",
    [{"bearer_token": "token-value"}, {"basic_auth": ("username", "password")}],
    ids=["bearer_token", "basic_auth"],
)
async def test_a_per_request_authorization_header_replaces_the_clients_auth(
    base_url: str, auth: dict[str, Any]
) -> None:
    async with HTTPClient(base_url=base_url, **auth) as client:
        result = (
            await client.get("echo-headers", headers={"authorization": "Custom value"}, into=dict)
        ).data

    assert echoed_header_values(result, "authorization") == ["Custom value"]


@pytest.mark.parametrize(
    "auth",
    [{"bearer_token": "token-value"}, {"basic_auth": ("username", "password")}],
    ids=["bearer_token", "basic_auth"],
)
def test_sync_a_per_request_authorization_header_replaces_the_clients_auth(
    base_url: str, auth: dict[str, Any]
) -> None:
    with SyncHTTPClient(base_url=base_url, **auth) as client:
        result = client.get(
            "echo-headers", headers={"authorization": "Custom value"}, into=dict
        ).data

    assert echoed_header_values(result, "authorization") == ["Custom value"]
