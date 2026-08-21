from dataclasses import dataclass, field

from lothc import JSON, HTTPClient, SyncHTTPClient


@dataclass
class _CountingAuthProvider:
    calls: int = field(default=0, init=False)

    async def __call__(self) -> str:
        self.calls += 1
        return f"token-{self.calls}"


@dataclass
class _SyncCountingAuthProvider:
    calls: int = field(default=0, init=False)

    def __call__(self) -> str:
        self.calls += 1
        return f"token-{self.calls}"


async def test_bearer_token_sends_authorization_header(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, bearer_token="token-value") as client:
        result = await client.get("echo-headers", response_data_type=JSON)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["authorization"] == "Bearer token-value"


async def test_bearer_auth_callable_is_invoked_per_request(base_url: str) -> None:
    provider = _CountingAuthProvider()

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as client:
        first = await client.get("echo-headers", response_data_type=JSON)
        second = await client.get("echo-headers", response_data_type=JSON)

    first_headers = {h["name"].lower(): h["value"] for h in first["headers"]}
    second_headers = {h["name"].lower(): h["value"] for h in second["headers"]}
    assert first_headers["authorization"] == "Bearer token-1"
    assert second_headers["authorization"] == "Bearer token-2"


def test_sync_bearer_token_sends_authorization_header(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, bearer_token="token-value") as client:
        result = client.get("echo-headers", response_data_type=JSON)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["authorization"] == "Bearer token-value"


def test_sync_bearer_auth_callable_is_invoked_per_request(base_url: str) -> None:
    provider = _SyncCountingAuthProvider()

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as client:
        first = client.get("echo-headers", response_data_type=JSON)
        second = client.get("echo-headers", response_data_type=JSON)

    first_headers = {h["name"].lower(): h["value"] for h in first["headers"]}
    second_headers = {h["name"].lower(): h["value"] for h in second["headers"]}
    assert first_headers["authorization"] == "Bearer token-1"
    assert second_headers["authorization"] == "Bearer token-2"
