from lothc import JSON, HTTPClient


async def test_follow_redirects_true_by_default(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url) as client:
        result = await client.get("redirect", response_data_type=JSON)

    assert result == {"id": 7, "name": "item-7"}


async def test_follow_redirects_false_returns_the_redirect_itself(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, follow_redirects=False) as client:
        result = await client.get_result("redirect")

    assert result.status == 302
    assert result.headers["location"] == "/items/7"
