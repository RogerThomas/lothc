from lothc import HTTPClient, SyncHTTPClient


async def test_get_result_request_info(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url) as client:
        result = await client.get_result("items/7", response_data_type=dict)

    assert result.request.method == "GET"
    assert result.request.path == "/items/7"
    assert result.request.url == f"{base_url}items/7"
    assert result.request.host == "127.0.0.1"


async def test_post_result_request_info(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url) as client:
        result = await client.post_result("items", json={"id": 1, "name": "item-1"})

    assert result.request.method == "POST"
    assert result.request.path == "/items"


async def test_head_request_info(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url) as client:
        result = await client.head("items/7")

    assert result.request.method == "HEAD"
    assert result.request.path == "/items/7"


def test_sync_get_result_request_info(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url) as client:
        result = client.get_result("items/7", response_data_type=dict)

    assert result.request.method == "GET"
    assert result.request.path == "/items/7"
    assert result.request.url == f"{base_url}items/7"
    assert result.request.host == "127.0.0.1"


def test_sync_post_result_request_info(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url) as client:
        result = client.post_result("items", json={"id": 1, "name": "item-1"})

    assert result.request.method == "POST"
    assert result.request.path == "/items"


def test_sync_head_request_info(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url) as client:
        result = client.head("items/7")

    assert result.request.method == "HEAD"
    assert result.request.path == "/items/7"
