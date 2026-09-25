"""How `base_url` joins with a verb's path. lothc hands `base_url` to pyreqwest unchanged, which
joins per RFC 3986 (the WHATWG URL parser's `join`), and requires the base to end in `/`.

The test server has no route that echoes the request path, so every assertion reads
`.request.path`/`.url`, the `RequestInfo` lothc records before sending, from a `Response` on a 200
or from the `HTTPResponseError` on a 404 (anything under `/prefix/` doesn't exist on the server).
"""

import pytest

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


async def test_base_with_trailing_slash_joins_a_relative_path(base_url: str) -> None:
    async with HTTPClient(base_url=base_url) as client:
        result = await client.get("items/7")

    assert result.request.url == f"{base_url}items/7"


def test_sync_base_with_trailing_slash_joins_a_relative_path(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url) as client:
        result = client.get("items/7")

    assert result.request.url == f"{base_url}items/7"


async def test_bare_origin_base_without_trailing_slash_joins_a_relative_path(
    base_url: str,
) -> None:
    # An origin with no path at all parses with path `/`, so the trailing-slash rule is met.
    async with HTTPClient(base_url=base_url.removesuffix("/")) as client:
        result = await client.get("items/7")

    assert result.request.url == f"{base_url}items/7"


async def test_base_path_prefix_is_kept_for_a_relative_path(base_url: str) -> None:
    async with HTTPClient(base_url=f"{base_url}prefix/") as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("items/7")

    assert exc_info.value.status == 404
    assert exc_info.value.request.url == f"{base_url}prefix/items/7"


def test_sync_base_path_prefix_is_kept_for_a_relative_path(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=f"{base_url}prefix/") as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get("items/7")

    assert exc_info.value.request.url == f"{base_url}prefix/items/7"


async def test_a_leading_slash_path_replaces_the_base_path_prefix(base_url: str) -> None:
    # RFC 3986: a path starting with `/` is absolute-path, so the base's `/prefix/` is dropped.
    async with HTTPClient(base_url=f"{base_url}prefix/") as client:
        result = await client.get("/items/7")

    assert result.request.path == "/items/7"


def test_sync_a_leading_slash_path_replaces_the_base_path_prefix(base_url: str) -> None:
    with SyncHTTPClient(base_url=f"{base_url}prefix/") as client:
        result = client.get("/items/7")

    assert result.request.path == "/items/7"


async def test_an_absolute_url_overrides_base_url(base_url: str) -> None:
    async with HTTPClient(base_url=f"{base_url}prefix/") as client:
        result = await client.get(f"{base_url}items/7")

    assert result.request.url == f"{base_url}items/7"


def test_sync_an_absolute_url_overrides_base_url(base_url: str) -> None:
    with SyncHTTPClient(base_url=f"{base_url}prefix/") as client:
        result = client.get(f"{base_url}items/7")

    assert result.request.url == f"{base_url}items/7"


async def test_base_path_without_trailing_slash_is_rejected(base_url: str) -> None:
    # Rather than silently dropping `prefix` (what plain RFC 3986 joining would do), pyreqwest
    # refuses the base. It surfaces on entering the client, since that's when lothc builds it.
    with pytest.raises(ValueError, match="trailing slash"):
        async with HTTPClient(base_url=f"{base_url}prefix"):
            pass


def test_sync_base_path_without_trailing_slash_is_rejected(base_url: str) -> None:
    with (
        pytest.raises(ValueError, match="trailing slash"),
        SyncHTTPClient(base_url=f"{base_url}prefix"),
    ):
        pass
