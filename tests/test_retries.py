from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from lothc import JSON, HTTPClient, HTTPConnectionError, HTTPResponseError, SyncHTTPClient


async def test_retries_recovers_from_5xx_then_succeeds(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, max_retries=2) as client:
        result = await client.get(
            "flaky", params={"key": "flaky-recovers", "fail_times": 2}, response_data_type=JSON
        )

    assert result == {"attempts": 3}


async def test_retries_exhausted_raises_response_error(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, max_retries=1) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("flaky", params={"key": "flaky-exhausted", "fail_times": 5})

    assert exc_info.value.status == 503


async def test_retries_honors_retry_after_header(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, max_retries=1) as client:
        result = await client.get(
            "retry-after", params={"key": "retry-after-1"}, response_data_type=JSON
        )

    assert result == {"attempts": 2}


async def test_retries_not_applied_to_post_by_default(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, max_retries=3) as client:
        with pytest.raises(HTTPResponseError):
            await client.post("flaky", params={"key": "flaky-post-default", "fail_times": 1})


async def test_retries_can_be_opted_in_for_post(base_url: str) -> None:
    async with HTTPClient.build(
        base_url=base_url, max_retries=2, retry_methods=frozenset({"POST"})
    ) as client:
        result = await client.post(
            "flaky", params={"key": "flaky-post-optin", "fail_times": 2}, response_data_type=JSON
        )

    assert result == {"attempts": 3}


async def test_retries_recovers_from_transport_error(base_url: str) -> None:
    async with HTTPClient.build(base_url=base_url, max_retries=2) as client:
        result = await client.get(
            "connection-flaky",
            params={"key": "conn-flaky-1", "fail_times": 2},
            response_data_type=JSON,
        )

    assert result == {"attempts": 3}


def test_sync_retries_recovers_from_5xx_then_succeeds(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, max_retries=2) as client:
        result = client.get(
            "flaky", params={"key": "sync-flaky-recovers", "fail_times": 2}, response_data_type=JSON
        )

    assert result == {"attempts": 3}


async def test_retries_honors_http_date_retry_after_header(base_url: str) -> None:
    retry_after = format_datetime(datetime.now(UTC))
    async with HTTPClient.build(base_url=base_url, max_retries=1) as client:
        result = await client.get(
            "retry-after-custom",
            params={"key": "retry-after-http-date", "value": retry_after},
            response_data_type=JSON,
        )

    assert result == {"attempts": 2}


async def test_retries_falls_back_to_backoff_on_malformed_retry_after_header(
    base_url: str,
) -> None:
    async with HTTPClient.build(base_url=base_url, max_retries=1) as client:
        result = await client.get(
            "retry-after-custom",
            params={"key": "retry-after-malformed", "value": "not-a-date"},
            response_data_type=JSON,
        )

    assert result == {"attempts": 2}


async def test_retries_exhausted_via_transport_error_raises_transport_error(
    base_url: str,
) -> None:
    async with HTTPClient.build(base_url=base_url, max_retries=1) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get(
                "connection-flaky", params={"key": "conn-flaky-exhausted", "fail_times": 5}
            )


def test_sync_retries_not_applied_to_post_by_default(base_url: str) -> None:
    with (
        SyncHTTPClient.build(base_url=base_url, max_retries=3) as client,
        pytest.raises(HTTPResponseError),
    ):
        client.post("flaky", params={"key": "sync-flaky-post-default", "fail_times": 1})


def test_sync_retries_recovers_from_transport_error(base_url: str) -> None:
    with SyncHTTPClient.build(base_url=base_url, max_retries=2) as client:
        result = client.get(
            "connection-flaky",
            params={"key": "sync-conn-flaky-1", "fail_times": 2},
            response_data_type=JSON,
        )

    assert result == {"attempts": 3}


def test_sync_retries_exhausted_via_transport_error_raises_transport_error(
    base_url: str,
) -> None:
    with (
        SyncHTTPClient.build(base_url=base_url, max_retries=1) as client,
        pytest.raises(HTTPConnectionError),
    ):
        client.get("connection-flaky", params={"key": "sync-conn-flaky-exhausted", "fail_times": 5})
