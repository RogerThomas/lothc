from lothc import HTTPClient


async def test_cookie_store_disabled_by_default(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        await client.get("set-cookie")
        result = (await client.get("read-cookie", response_data_type=dict)).data

    assert result["cookie"] is None


async def test_cookie_store_persists_cookies_across_requests(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, cookie_store=True) as client:
        await client.get("set-cookie")
        result = (await client.get("read-cookie", response_data_type=dict)).data

    assert result["cookie"] == "session=abc123"
