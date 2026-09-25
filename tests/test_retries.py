import time
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from lothc import HTTPClient, HTTPConnectionError, HTTPResponseError, SyncHTTPClient

# Every test below except the two `backoff_base_scales_the_wait_between_attempts` ones asserts
# *that* a retry happened, never how long it waited, so they all pass `backoff_base=0.001` to
# override the 0.1s default. It is deliberately not 0 — the same real sleep path still runs — and
# no assertion changes; the suite just stops paying 0.1s + 0.2s for every recovering call.


async def test_retries_recovers_from_5xx_then_succeeds(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = (
            await client.get(
                "flaky", params={"key": "flaky-recovers", "fail_times": 2}, response_data_type=dict
            )
        ).data

    assert result == {"attempts": 3}


async def test_retries_exhausted_raises_response_error(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("flaky", params={"key": "flaky-exhausted", "fail_times": 5})

    assert exc_info.value.status == 503


async def test_retries_honors_retry_after_header(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1) as client:
        result = (
            await client.get(
                "retry-after", params={"key": "retry-after-1"}, response_data_type=dict
            )
        ).data

    assert result == {"attempts": 2}


async def test_retries_not_applied_to_post_by_default(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=3) as client:
        with pytest.raises(HTTPResponseError):
            await client.post("flaky", params={"key": "flaky-post-default", "fail_times": 1})


async def test_retries_can_be_opted_in_for_post(base_url: str) -> None:
    async with HTTPClient(
        base_url=base_url,
        max_retries=2,
        retry_methods=frozenset({"POST"}),
        backoff_base=0.001,
    ) as client:
        result = (
            await client.post(
                "flaky",
                params={"key": "flaky-post-optin", "fail_times": 2},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 3}


async def test_retries_recovers_from_transport_error(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = (
            await client.get(
                "connection-flaky",
                params={"key": "conn-flaky-1", "fail_times": 2},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 3}


def test_sync_retries_recovers_from_5xx_then_succeeds(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.get(
            "flaky", params={"key": "sync-flaky-recovers", "fail_times": 2}, response_data_type=dict
        ).data

    assert result == {"attempts": 3}


async def test_retries_honors_http_date_retry_after_header(base_url: str) -> None:
    retry_after = format_datetime(datetime.now(UTC))
    async with HTTPClient(base_url=base_url, max_retries=1) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": "retry-after-http-date", "value": retry_after},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 2}


async def test_retries_falls_back_to_backoff_on_malformed_retry_after_header(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": "retry-after-malformed", "value": "not-a-date"},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 2}


async def test_retries_exhausted_via_transport_error_raises_transport_error(
    base_url: str,
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get(
                "connection-flaky", params={"key": "conn-flaky-exhausted", "fail_times": 5}
            )


def test_sync_retries_not_applied_to_post_by_default(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=3) as client,
        pytest.raises(HTTPResponseError),
    ):
        client.post("flaky", params={"key": "sync-flaky-post-default", "fail_times": 1})


def test_sync_retries_recovers_from_transport_error(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.get(
            "connection-flaky",
            params={"key": "sync-conn-flaky-1", "fail_times": 2},
            response_data_type=dict,
        ).data

    assert result == {"attempts": 3}


def test_sync_retries_exhausted_via_transport_error_raises_transport_error(
    base_url: str,
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client,
        pytest.raises(HTTPConnectionError),
    ):
        client.get("connection-flaky", params={"key": "sync-conn-flaky-exhausted", "fail_times": 5})


async def test_backoff_base_scales_the_wait_between_attempts(base_url: str) -> None:
    # backoff_base * 2 ** attempt, so two retries at 0.02 wait 0.02s + 0.04s = 0.06s minimum —
    # the floor below. The ceiling is what pins the parameter down: the 0.1 default would wait
    # 0.1s + 0.2s = 0.3s, so a client that ignored `backoff_base` blows straight past 0.2s.
    # Bracketing below the default rather than flooring above it costs 0.06s a run, not 0.6s.
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.02) as client:
        started = time.monotonic()
        result = (
            await client.get(
                "flaky", params={"key": "backoff-base", "fail_times": 2}, response_data_type=dict
            )
        ).data
        elapsed = time.monotonic() - started

    assert result == {"attempts": 3}
    assert 0.06 <= elapsed < 0.2


def test_sync_backoff_base_scales_the_wait_between_attempts(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.02) as client:
        started = time.monotonic()
        result = client.get(
            "flaky", params={"key": "sync-backoff-base", "fail_times": 2}, response_data_type=dict
        ).data
        elapsed = time.monotonic() - started

    assert result == {"attempts": 3}
    assert 0.06 <= elapsed < 0.2


async def test_retry_methods_are_case_insensitive(base_url: str) -> None:
    # pyreqwest reports methods upper-case, so a lower-case `{"get"}` used to match nothing and
    # silently turned retries off entirely.
    async with HTTPClient(
        base_url=base_url, max_retries=2, retry_methods={"get"}, backoff_base=0.001
    ) as client:
        result = (
            await client.get(
                "flaky",
                params={"key": "lowercase-methods", "fail_times": 2},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 3}


async def test_empty_retry_methods_retries_nothing(base_url: str) -> None:
    # An empty set used to fall back to the defaults (via `or`), so GET still retried.
    async with HTTPClient(
        base_url=base_url, max_retries=2, retry_methods=set(), backoff_base=0.001
    ) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("flaky", params={"key": "empty-methods", "fail_times": 1})

    assert exc_info.value.status == 503


def test_sync_retry_methods_accept_a_list(base_url: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, max_retries=2, retry_methods=["get"], backoff_base=0.001
    ) as client:
        result = client.get(
            "flaky", params={"key": "sync-list-methods", "fail_times": 2}, response_data_type=dict
        ).data

    assert result == {"attempts": 3}


async def test_a_retry_after_longer_than_max_retry_after_stops_retrying(base_url: str) -> None:
    # The server asks for 120s; rather than block inside the call that long, lothc stops and
    # raises the 429 so the caller can schedule its own retry from the header.
    async with HTTPClient(base_url=base_url, max_retries=2, max_retry_after=60) as client:
        started = time.monotonic()
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get(
                "retry-after-custom", params={"key": "retry-after-too-long", "value": "120"}
            )
        elapsed = time.monotonic() - started

    assert exc_info.value.status == 429
    assert exc_info.value.headers["Retry-After"] == "120"
    assert elapsed < 1.0


async def test_a_retry_after_within_max_retry_after_is_still_honoured(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, max_retry_after=60) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": "retry-after-within", "value": "0"},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 2}


def test_sync_a_retry_after_longer_than_max_retry_after_stops_retrying(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=2, max_retry_after=0.05) as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get("retry-after-custom", params={"key": "sync-retry-after-too-long", "value": "1"})

    assert exc_info.value.status == 429


async def test_max_retry_after_none_honours_any_wait(base_url: str) -> None:
    # `None` restores the old behaviour. A 0s wait keeps the test fast while still going through
    # the no-limit branch.
    async with HTTPClient(base_url=base_url, max_retries=1, max_retry_after=None) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": "retry-after-no-limit", "value": "0"},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 2}


async def test_a_body_cut_off_partway_is_retried(base_url: str) -> None:
    # Shorter than its `Content-Length`: pyreqwest reports this as a decode error, not a transport
    # error, so it used to fail the call even with retries left.
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = (
            await client.get(
                "body-flaky",
                params={"key": "body-flaky-1", "fail_times": 2},
                response_data_type=dict,
            )
        ).data

    assert result == {"attempts": 3}


def test_sync_a_body_cut_off_partway_is_retried(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.get(
            "body-flaky",
            params={"key": "sync-body-flaky-1", "fail_times": 2},
            response_data_type=dict,
        ).data

    assert result == {"attempts": 3}


async def test_a_body_cut_off_on_every_attempt_raises_a_connection_error(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("body-flaky", params={"key": "body-flaky-exhausted", "fail_times": 5})


async def test_a_body_that_cannot_be_decompressed_is_not_retried(base_url: str) -> None:
    # Unlike a cut-off body, a corrupt one comes back the same on every attempt.
    async with HTTPClient(base_url=base_url, max_retries=3, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("corrupt-gzip", params={"key": "corrupt-gzip-1"})
        hits = (
            await client.get("hits", params={"key": "corrupt-gzip-1"}, response_data_type=dict)
        ).data

    assert hits == {"hits": 1}


def test_sync_a_body_that_cannot_be_decompressed_is_not_retried(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=3, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            client.get("corrupt-gzip", params={"key": "sync-corrupt-gzip-1"})
        hits = client.get(
            "hits", params={"key": "sync-corrupt-gzip-1"}, response_data_type=dict
        ).data

    assert hits == {"hits": 1}


async def test_a_cut_off_body_on_a_post_is_not_retried_by_default(base_url: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.post("body-flaky", params={"key": "body-flaky-post", "fail_times": 1})


def test_sync_retries_honors_retry_after_header(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1) as client:
        result = client.get(
            "retry-after", params={"key": "sync-retry-after-1"}, response_data_type=dict
        ).data

    assert result == {"attempts": 2}


def test_sync_retries_honors_http_date_retry_after_header(base_url: str) -> None:
    retry_after = format_datetime(datetime.now(UTC))
    with SyncHTTPClient(base_url=base_url, max_retries=1) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": "sync-retry-after-http-date", "value": retry_after},
            response_data_type=dict,
        ).data

    assert result == {"attempts": 2}


def test_sync_retries_falls_back_to_backoff_on_malformed_retry_after_header(
    base_url: str,
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": "sync-retry-after-malformed", "value": "not-a-date"},
            response_data_type=dict,
        ).data

    assert result == {"attempts": 2}


def test_sync_a_retry_after_longer_than_max_retry_after_raises_with_the_header(
    base_url: str,
) -> None:
    # Mirrors the async test with the same 120s wait, so the elapsed ceiling proves the sync client
    # stopped rather than slept.
    with SyncHTTPClient(base_url=base_url, max_retries=2, max_retry_after=60) as client:
        started = time.monotonic()
        with pytest.raises(HTTPResponseError) as exc_info:
            client.get("retry-after-custom", params={"key": "sync-retry-after-120", "value": "120"})
        elapsed = time.monotonic() - started

    assert exc_info.value.status == 429
    assert exc_info.value.headers["Retry-After"] == "120"
    assert elapsed < 1.0


def test_sync_a_retry_after_within_max_retry_after_is_still_honoured(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, max_retry_after=60) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": "sync-retry-after-within", "value": "0"},
            response_data_type=dict,
        ).data

    assert result == {"attempts": 2}


def test_sync_max_retry_after_none_honours_any_wait(base_url: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, max_retry_after=None) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": "sync-retry-after-no-limit", "value": "0"},
            response_data_type=dict,
        ).data

    assert result == {"attempts": 2}
