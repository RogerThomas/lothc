from lothc import HTTPClient, SyncHTTPClient


async def test_response_get_request_info(base_url: str, client: HTTPClient) -> None:
    result = await client.get("items/7", into=dict)

    assert result.request.method == "GET"
    assert result.request.path == "/items/7"
    assert result.request.url == f"{base_url}items/7"
    assert result.request.host == "127.0.0.1"


async def test_response_post_request_info(client: HTTPClient) -> None:
    result = await client.post("items", json={"id": 1, "name": "item-1"})

    assert result.request.method == "POST"
    assert result.request.path == "/items"


async def test_head_request_info(client: HTTPClient) -> None:
    result = await client.head("items/7")

    assert result.request.method == "HEAD"
    assert result.request.path == "/items/7"


def test_sync_get_result_request_info(base_url: str, sync_client: SyncHTTPClient) -> None:
    result = sync_client.get("items/7", into=dict)

    assert result.request.method == "GET"
    assert result.request.path == "/items/7"
    assert result.request.url == f"{base_url}items/7"
    assert result.request.host == "127.0.0.1"


def test_sync_post_result_request_info(sync_client: SyncHTTPClient) -> None:
    result = sync_client.post("items", json={"id": 1, "name": "item-1"})

    assert result.request.method == "POST"
    assert result.request.path == "/items"


def test_sync_head_request_info(sync_client: SyncHTTPClient) -> None:
    result = sync_client.head("items/7")

    assert result.request.method == "HEAD"
    assert result.request.path == "/items/7"
