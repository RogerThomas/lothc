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


async def test_bearer_token_sends_authorization_header(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, bearer_token="token-value") as client:
        result = await client.get("echo-headers", response_data_type=dict)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["authorization"] == "Bearer token-value"


async def test_bearer_auth_callable_is_invoked_per_request(base_url: str) -> None:
    provider = _CountingAuthProvider()

    async with HTTPClient.build(base_url=base_url, bearer_auth=provider) as client:
        first = await client.get("echo-headers", response_data_type=dict)
        second = await client.get("echo-headers", response_data_type=dict)

    first_headers = {h["name"].lower(): h["value"] for h in first["headers"]}
    second_headers = {h["name"].lower(): h["value"] for h in second["headers"]}
    assert first_headers["authorization"] == "Bearer token-1"
    assert second_headers["authorization"] == "Bearer token-2"


def test_sync_bearer_token_sends_authorization_header(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, bearer_token="token-value") as client:
        result = client.get("echo-headers", response_data_type=dict)

    headers = {h["name"].lower(): h["value"] for h in result["headers"]}
    assert headers["authorization"] == "Bearer token-value"


def test_sync_bearer_auth_callable_is_invoked_per_request(base_url: str) -> None:
    provider = _SyncCountingAuthProvider()

    with SyncHTTPClient.build(base_url=base_url, bearer_auth=provider) as client:
        first = client.get("echo-headers", response_data_type=dict)
        second = client.get("echo-headers", response_data_type=dict)

    first_headers = {h["name"].lower(): h["value"] for h in first["headers"]}
    second_headers = {h["name"].lower(): h["value"] for h in second["headers"]}
    assert first_headers["authorization"] == "Bearer token-1"
    assert second_headers["authorization"] == "Bearer token-2"
