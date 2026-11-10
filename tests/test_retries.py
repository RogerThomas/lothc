import json
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Literal

import pytest

from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPResponseError,
    HTTPTimeoutError,
    HTTPTransportError,
    SyncHTTPClient,
)

# Every test below except the two `backoff_base_scales_the_wait_between_attempts` ones asserts
# *that* a retry happened, never how long it waited, so they all pass `backoff_base=0.001` to
# override the 0.1s default. It is deliberately not 0 — the same real sleep path still runs — and
# no assertion changes; the suite just stops paying 0.1s + 0.2s for every recovering call.


async def test_retries_recovers_from_5xx_then_succeeds(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = (
            await client.get("flaky", params={"key": server_key, "fail_times": 2}, into=dict)
        ).data

    assert result == {"attempts": 3}


async def test_retries_exhausted_raises_response_error(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("flaky", params={"key": server_key, "fail_times": 5})
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert exc_info.value.status == 503
    assert hits == {"hits": 2}  # the first attempt plus exactly `max_retries`


async def test_retries_honors_retry_after_header(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1) as client:
        result = (await client.get("retry-after", params={"key": server_key}, into=dict)).data

    assert result == {"attempts": 2}


async def test_retries_applied_to_options_by_default(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = await client.options("flaky", params={"key": server_key, "fail_times": 2})

    assert result.status == 200


async def test_retries_not_applied_to_post_by_default(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=3) as client:
        with pytest.raises(HTTPResponseError):
            await client.post("flaky", params={"key": server_key, "fail_times": 1})


async def test_retries_can_be_opted_in_for_post(base_url: str, server_key: str) -> None:
    async with HTTPClient(
        base_url=base_url,
        max_retries=2,
        retry_methods=frozenset({"POST"}),
        backoff_base=0.001,
    ) as client:
        result = (
            await client.post(
                "flaky",
                params={"key": server_key, "fail_times": 2},
                into=dict,
            )
        ).data

    assert result == {"attempts": 3}


async def test_retries_recovers_from_transport_error(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = (
            await client.get(
                "connection-flaky",
                params={"key": server_key, "fail_times": 2},
                into=dict,
            )
        ).data

    assert result == {"attempts": 3}


def test_sync_retries_recovers_from_5xx_then_succeeds(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.get("flaky", params={"key": server_key, "fail_times": 2}, into=dict).data

    assert result == {"attempts": 3}


async def test_retries_honors_http_date_retry_after_header(base_url: str, server_key: str) -> None:
    retry_after = format_datetime(datetime.now(UTC))
    async with HTTPClient(base_url=base_url, max_retries=1) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": server_key, "value": retry_after},
                into=dict,
            )
        ).data

    assert result == {"attempts": 2}


async def test_retries_falls_back_to_backoff_on_malformed_retry_after_header(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": server_key, "value": "not-a-date"},
                into=dict,
            )
        ).data

    assert result == {"attempts": 2}


async def test_retries_exhausted_via_transport_error_raises_transport_error(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("connection-flaky", params={"key": server_key, "fail_times": 5})


def test_sync_retries_applied_to_options_by_default(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.options("flaky", params={"key": server_key, "fail_times": 2})

    assert result.status == 200


def test_sync_retries_not_applied_to_post_by_default(base_url: str, server_key: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=3) as client,
        pytest.raises(HTTPResponseError),
    ):
        client.post("flaky", params={"key": server_key, "fail_times": 1})


def test_sync_retries_recovers_from_transport_error(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.get(
            "connection-flaky",
            params={"key": server_key, "fail_times": 2},
            into=dict,
        ).data

    assert result == {"attempts": 3}


def test_sync_retries_exhausted_via_transport_error_raises_transport_error(
    base_url: str, server_key: str
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client,
        pytest.raises(HTTPConnectionError),
    ):
        client.get("connection-flaky", params={"key": server_key, "fail_times": 5})


async def test_backoff_base_scales_the_wait_between_attempts(
    base_url: str, server_key: str
) -> None:
    # backoff_base * 2 ** attempt, so two retries at 0.02 wait 0.02s + 0.04s = 0.06s minimum —
    # the floor below. The ceiling is what pins the parameter down: the 0.1 default would wait
    # 0.1s + 0.2s = 0.3s, so a client that ignored `backoff_base` blows straight past 0.2s.
    # Bracketing below the default rather than flooring above it costs 0.06s a run, not 0.6s.
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.02) as client:
        started = time.monotonic()
        result = (
            await client.get("flaky", params={"key": server_key, "fail_times": 2}, into=dict)
        ).data
        elapsed = time.monotonic() - started

    assert result == {"attempts": 3}
    assert 0.06 <= elapsed < 0.2


def test_sync_backoff_base_scales_the_wait_between_attempts(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.02) as client:
        started = time.monotonic()
        result = client.get("flaky", params={"key": server_key, "fail_times": 2}, into=dict).data
        elapsed = time.monotonic() - started

    assert result == {"attempts": 3}
    assert 0.06 <= elapsed < 0.2


async def test_retry_methods_are_case_insensitive(base_url: str, server_key: str) -> None:
    # pyreqwest reports methods upper-case, so a lower-case `{"get"}` used to match nothing and
    # silently turned retries off entirely.
    async with HTTPClient(
        base_url=base_url, max_retries=2, retry_methods={"get"}, backoff_base=0.001
    ) as client:
        result = (
            await client.get(
                "flaky",
                params={"key": server_key, "fail_times": 2},
                into=dict,
            )
        ).data

    assert result == {"attempts": 3}


async def test_empty_retry_methods_retries_nothing(base_url: str, server_key: str) -> None:
    # An empty set used to fall back to the defaults (via `or`), so GET still retried.
    async with HTTPClient(
        base_url=base_url, max_retries=2, retry_methods=set(), backoff_base=0.001
    ) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("flaky", params={"key": server_key, "fail_times": 1})

    assert exc_info.value.status == 503


def test_sync_retry_methods_accept_a_list(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, max_retries=2, retry_methods=["get"], backoff_base=0.001
    ) as client:
        result = client.get("flaky", params={"key": server_key, "fail_times": 2}, into=dict).data

    assert result == {"attempts": 3}


async def test_a_retry_after_longer_than_max_retry_after_stops_retrying(
    base_url: str, server_key: str
) -> None:
    # The server asks for 120s; rather than block inside the call that long, lothc stops and
    # raises the 429 so the caller can schedule its own retry from the header.
    async with HTTPClient(base_url=base_url, max_retries=2, max_retry_after=60) as client:
        started = time.monotonic()
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("retry-after-custom", params={"key": server_key, "value": "120"})
        elapsed = time.monotonic() - started

    assert exc_info.value.status == 429
    assert exc_info.value.headers["Retry-After"] == "120"
    assert elapsed < 1.0


async def test_a_retry_after_within_max_retry_after_is_still_honoured(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, max_retry_after=60) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": server_key, "value": "0"},
                into=dict,
            )
        ).data

    assert result == {"attempts": 2}


def test_sync_a_retry_after_longer_than_max_retry_after_stops_retrying(
    base_url: str, server_key: str
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=2, max_retry_after=0.05) as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get("retry-after-custom", params={"key": server_key, "value": "1"})

    assert exc_info.value.status == 429


async def test_max_retry_after_none_honours_any_wait(base_url: str, server_key: str) -> None:
    # `None` restores the old behaviour. A 0s wait keeps the test fast while still going through
    # the no-limit branch.
    async with HTTPClient(base_url=base_url, max_retries=1, max_retry_after=None) as client:
        result = (
            await client.get(
                "retry-after-custom",
                params={"key": server_key, "value": "0"},
                into=dict,
            )
        ).data

    assert result == {"attempts": 2}


async def test_a_body_cut_off_partway_is_retried(base_url: str, server_key: str) -> None:
    # Shorter than its `Content-Length`: pyreqwest reports this as a decode error, not a transport
    # error, so it used to fail the call even with retries left.
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = (
            await client.get(
                "body-flaky",
                params={"key": server_key, "fail_times": 2},
                into=dict,
            )
        ).data

    assert result == {"attempts": 3}


def test_sync_a_body_cut_off_partway_is_retried(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.get(
            "body-flaky",
            params={"key": server_key, "fail_times": 2},
            into=dict,
        ).data

    assert result == {"attempts": 3}


async def test_a_body_cut_off_on_every_attempt_raises_a_connection_error(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("body-flaky", params={"key": server_key, "fail_times": 5})


async def test_a_body_that_cannot_be_decompressed_is_not_retried(
    base_url: str, server_key: str
) -> None:
    # Unlike a cut-off body, a corrupt one comes back the same on every attempt.
    async with HTTPClient(base_url=base_url, max_retries=3, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("corrupt-gzip", params={"key": server_key})
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 1}


def test_sync_a_body_that_cannot_be_decompressed_is_not_retried(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=3, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            client.get("corrupt-gzip", params={"key": server_key})
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert hits == {"hits": 1}


async def test_a_cut_off_body_on_a_post_is_not_retried_by_default(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.post("body-flaky", params={"key": server_key, "fail_times": 1})


def test_sync_retries_honors_retry_after_header(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1) as client:
        result = client.get("retry-after", params={"key": server_key}, into=dict).data

    assert result == {"attempts": 2}


def test_sync_retries_honors_http_date_retry_after_header(base_url: str, server_key: str) -> None:
    retry_after = format_datetime(datetime.now(UTC))
    with SyncHTTPClient(base_url=base_url, max_retries=1) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": server_key, "value": retry_after},
            into=dict,
        ).data

    assert result == {"attempts": 2}


def test_sync_retries_falls_back_to_backoff_on_malformed_retry_after_header(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": server_key, "value": "not-a-date"},
            into=dict,
        ).data

    assert result == {"attempts": 2}


def test_sync_a_retry_after_longer_than_max_retry_after_raises_with_the_header(
    base_url: str, server_key: str
) -> None:
    # Mirrors the async test with the same 120s wait, so the elapsed ceiling proves the sync client
    # stopped rather than slept.
    with SyncHTTPClient(base_url=base_url, max_retries=2, max_retry_after=60) as client:
        started = time.monotonic()
        with pytest.raises(HTTPResponseError) as exc_info:
            client.get("retry-after-custom", params={"key": server_key, "value": "120"})
        elapsed = time.monotonic() - started

    assert exc_info.value.status == 429
    assert exc_info.value.headers["Retry-After"] == "120"
    assert elapsed < 1.0


def test_sync_a_retry_after_within_max_retry_after_is_still_honoured(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, max_retry_after=60) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": server_key, "value": "0"},
            into=dict,
        ).data

    assert result == {"attempts": 2}


def test_sync_max_retry_after_none_honours_any_wait(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, max_retry_after=None) as client:
        result = client.get(
            "retry-after-custom",
            params={"key": server_key, "value": "0"},
            into=dict,
        ).data

    assert result == {"attempts": 2}


async def test_a_retry_after_in_seconds_is_waited_before_retrying(
    base_url: str, server_key: str
) -> None:
    # 1s is the shortest wait the header can ask for (whole seconds only), and the 0.001
    # `backoff_base` would retry after a millisecond, so only an honoured header explains the
    # floor. `.elapsed` spans the whole call, the wait included.
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        started = time.monotonic()
        response = await client.get(
            "retry-after-custom", params={"key": server_key, "value": "1"}, into=dict
        )
        elapsed = time.monotonic() - started

    assert response.data == {"attempts": 2}
    assert 1.0 <= elapsed < 1.5
    assert response.elapsed >= 1.0


def test_sync_a_retry_after_in_seconds_is_waited_before_retrying(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        started = time.monotonic()
        response = client.get(
            "retry-after-custom", params={"key": server_key, "value": "1"}, into=dict
        )
        elapsed = time.monotonic() - started

    assert response.data == {"attempts": 2}
    assert 1.0 <= elapsed < 1.5
    assert response.elapsed >= 1.0


async def test_an_http_date_retry_after_is_read_as_a_wait(base_url: str, server_key: str) -> None:
    # Two minutes ahead is past `max_retry_after`, so retrying stops at once. Only a parsed date
    # can do that: an unreadable header falls back to backoff and retries into the 200.
    retry_after = format_datetime(datetime.now(UTC) + timedelta(minutes=2), usegmt=True)
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("retry-after-custom", params={"key": server_key, "value": retry_after})

    assert exc_info.value.status == 429


def test_sync_an_http_date_retry_after_is_read_as_a_wait(base_url: str, server_key: str) -> None:
    retry_after = format_datetime(datetime.now(UTC) + timedelta(minutes=2), usegmt=True)
    with (
        SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get("retry-after-custom", params={"key": server_key, "value": retry_after})

    assert exc_info.value.status == 429


async def test_retry_after_wins_over_a_longer_computed_backoff(
    base_url: str, server_key: str
) -> None:
    # `backoff_base=5` alone would wait 5s before the retry; the server's `Retry-After: 0` says
    # retry now, and the header wins.
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=5) as client:
        started = time.monotonic()
        result = (
            await client.get(
                "retry-after-custom", params={"key": server_key, "value": "0"}, into=dict
            )
        ).data
        elapsed = time.monotonic() - started

    assert result == {"attempts": 2}
    assert elapsed < 1.0


def test_sync_retry_after_wins_over_a_longer_computed_backoff(
    base_url: str, server_key: str
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=5) as client:
        started = time.monotonic()
        result = client.get(
            "retry-after-custom", params={"key": server_key, "value": "0"}, into=dict
        ).data
        elapsed = time.monotonic() - started

    assert result == {"attempts": 2}
    assert elapsed < 1.0


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_every_retryable_status_is_retried(
    base_url: str, server_key: str, status: int
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = (
            await client.get(
                "status-then-ok",
                params={"key": server_key, "status": status, "fail_times": 2},
                into=dict,
            )
        ).data

    assert result["attempts"] == 3


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_sync_every_retryable_status_is_retried(
    base_url: str, server_key: str, status: int
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        result = client.get(
            "status-then-ok",
            params={"key": server_key, "status": status, "fail_times": 2},
            into=dict,
        ).data

    assert result["attempts"] == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 409, 501, 505])
async def test_other_error_statuses_are_not_retried(
    base_url: str, server_key: str, status: int
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get(
                "status-then-ok", params={"key": server_key, "status": status, "fail_times": 1}
            )
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert exc_info.value.status == status
    assert hits == {"hits": 1}


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 409, 501, 505])
def test_sync_other_error_statuses_are_not_retried(
    base_url: str, server_key: str, status: int
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.get(
                "status-then-ok", params={"key": server_key, "status": status, "fail_times": 1}
            )
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert exc_info.value.status == status
    assert hits == {"hits": 1}


type _BodylessVerb = Literal["put", "delete", "head", "patch"]


async def _send_bodyless(client: HTTPClient, verb: _BodylessVerb, key: str) -> int:
    params: dict[str, str | int] = {"key": key, "fail_times": 2}
    match verb:
        case "put":
            return (await client.put("status-then-ok", params=params)).status
        case "delete":
            return (await client.delete("status-then-ok", params=params)).status
        case "head":
            return (await client.head("status-then-ok", params=params)).status
        case "patch":
            return (await client.patch("status-then-ok", params=params)).status


def _send_bodyless_sync(client: SyncHTTPClient, verb: _BodylessVerb, key: str) -> int:
    params: dict[str, str | int] = {"key": key, "fail_times": 2}
    match verb:
        case "put":
            return client.put("status-then-ok", params=params).status
        case "delete":
            return client.delete("status-then-ok", params=params).status
        case "head":
            return client.head("status-then-ok", params=params).status
        case "patch":
            return client.patch("status-then-ok", params=params).status


@pytest.mark.parametrize("verb", ["put", "delete", "head"])
async def test_the_other_idempotent_verbs_are_retried_by_default(
    base_url: str, server_key: str, verb: _BodylessVerb
) -> None:
    # GET and OPTIONS have their own tests above. HEAD's answer has no body to count attempts
    # in, so every verb reads the count back from the server.
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        status = await _send_bodyless(client, verb, server_key)
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert status == 200
    assert hits == {"hits": 3}


@pytest.mark.parametrize("verb", ["put", "delete", "head"])
def test_sync_the_other_idempotent_verbs_are_retried_by_default(
    base_url: str, server_key: str, verb: _BodylessVerb
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        status = _send_bodyless_sync(client, verb, server_key)
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert status == 200
    assert hits == {"hits": 3}


async def test_patch_is_not_retried_by_default(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError):
            await _send_bodyless(client, "patch", server_key)
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 1}


def test_sync_patch_is_not_retried_by_default(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError):
            _send_bodyless_sync(client, "patch", server_key)
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert hits == {"hits": 1}


async def test_a_retried_post_resends_the_same_json_body(base_url: str, server_key: str) -> None:
    async with HTTPClient(
        base_url=base_url, max_retries=1, retry_methods={"POST"}, backoff_base=0.001
    ) as client:
        result = (
            await client.post(
                "status-then-ok",
                params={"key": server_key, "fail_times": 1},
                json={"a": 1},
                into=dict,
            )
        ).data

    assert result == {"attempts": 2, "bodies": ['{"a":1}', '{"a":1}']}


def test_sync_a_retried_post_resends_the_same_json_body(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, max_retries=1, retry_methods={"POST"}, backoff_base=0.001
    ) as client:
        result = client.post(
            "status-then-ok",
            params={"key": server_key, "fail_times": 1},
            json={"a": 1},
            into=dict,
        ).data

    assert result == {"attempts": 2, "bodies": ['{"a":1}', '{"a":1}']}


async def test_a_retried_put_resends_the_same_raw_body(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = (
            await client.put(
                "status-then-ok",
                params={"key": server_key, "fail_times": 1},
                content="content",
                into=dict,
            )
        ).data

    assert result == {"attempts": 2, "bodies": ["content", "content"]}


def test_sync_a_retried_put_resends_the_same_raw_body(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = client.put(
            "status-then-ok",
            params={"key": server_key, "fail_times": 1},
            content="content",
            into=dict,
        ).data

    assert result == {"attempts": 2, "bodies": ["content", "content"]}


# A multipart `form=` body is streamed, so it can't be replayed: on a retried verb it is sent once,
# the way reqwest's own retry policy treats a body it can't clone, rather than failing to send.
async def test_a_form_put_on_a_client_with_retries_is_sent(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = (
            await client.put(
                "status-then-ok", params={"key": server_key}, form={"a": "b"}, into=dict
            )
        ).data

    assert result["attempts"] == 1
    assert 'name="a"' in result["bodies"][0]


def test_sync_a_form_put_on_a_client_with_retries_is_sent(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        result = client.put(
            "status-then-ok", params={"key": server_key}, form={"a": "b"}, into=dict
        ).data

    assert result["attempts"] == 1
    assert 'name="a"' in result["bodies"][0]


async def test_a_form_put_is_not_retried(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.put(
                "status-then-ok", params={"key": server_key, "fail_times": 1}, form={"a": "b"}
            )
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert exc_info.value.status == 503
    assert hits == {"hits": 1}


def test_sync_a_form_put_is_not_retried(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.put(
                "status-then-ok", params={"key": server_key, "fail_times": 1}, form={"a": "b"}
            )
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert exc_info.value.status == 503
    assert hits == {"hits": 1}


async def test_a_timed_out_attempt_is_retried_with_its_own_timeout(base_url: str) -> None:
    # Three attempts of 0.05s each: the floor proves the timeouts were retried, the ceiling that
    # each attempt got its own `timeout` rather than one shared across the call. The server
    # sleeps far longer than any attempt waits, and nothing waits that out.
    async with HTTPClient(
        base_url=base_url, timeout=0.05, max_retries=2, backoff_base=0.001
    ) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            await client.get("slow", params={"seconds": 5})
        elapsed = time.monotonic() - started

    assert 0.15 <= elapsed < 0.6


def test_sync_a_timed_out_attempt_is_retried_with_its_own_timeout(base_url: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, timeout=0.05, max_retries=2, backoff_base=0.001
    ) as client:
        started = time.monotonic()
        with pytest.raises(HTTPTimeoutError):
            client.get("slow", params={"seconds": 5})
        elapsed = time.monotonic() - started

    assert 0.15 <= elapsed < 0.6


async def test_stream_get_retries_a_5xx_before_streaming(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        chunks = [
            chunk
            async for chunk in client.stream_get(
                "status-then-ok", params={"key": server_key, "fail_times": 2}
            )
        ]

    assert json.loads(b"".join(chunks))["attempts"] == 3


def test_sync_stream_get_retries_a_5xx_before_streaming(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        chunks = list(
            client.stream_get("status-then-ok", params={"key": server_key, "fail_times": 2})
        )

    assert json.loads(b"".join(chunks))["attempts"] == 3


async def test_download_retries_a_5xx(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        response = await client.download(
            "status-then-ok", params={"key": server_key, "fail_times": 2}
        )

    assert json.loads(response.data)["attempts"] == 3


def test_sync_download_retries_a_5xx(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        response = client.download("status-then-ok", params={"key": server_key, "fail_times": 2})

    assert json.loads(response.data)["attempts"] == 3


async def test_a_redirect_loop_is_not_retried(base_url: str, server_key: str) -> None:
    # One chain is the request plus its `max_redirects` hops; a retried loop would run four.
    async with HTTPClient(
        base_url=base_url, max_redirects=2, max_retries=3, backoff_base=0.001
    ) as client:
        with pytest.raises(HTTPTransportError):
            await client.get("redirect-loop-counted", params={"key": server_key})
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 3}


def test_sync_a_redirect_loop_is_not_retried(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(
        base_url=base_url, max_redirects=2, max_retries=3, backoff_base=0.001
    ) as client:
        with pytest.raises(HTTPTransportError):
            client.get("redirect-loop-counted", params={"key": server_key})
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert hits == {"hits": 3}


async def test_retries_exhausted_by_transport_errors_make_every_attempt(
    base_url: str, server_key: str
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client:
        with pytest.raises(HTTPConnectionError):
            await client.get("connection-flaky", params={"key": server_key, "fail_times": 5})
        hits = (await client.get("hits", params={"key": server_key}, into=dict)).data

    assert hits == {"hits": 3}


def test_sync_retries_exhausted_raises_response_error(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            client.get("flaky", params={"key": server_key, "fail_times": 5})
        hits = client.get("hits", params={"key": server_key}, into=dict).data

    assert exc_info.value.status == 503
    assert hits == {"hits": 2}


def test_sync_retries_can_be_opted_in_for_post(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(
        base_url=base_url,
        max_retries=2,
        retry_methods=frozenset({"POST"}),
        backoff_base=0.001,
    ) as client:
        result = client.post("flaky", params={"key": server_key, "fail_times": 2}, into=dict).data

    assert result == {"attempts": 3}


def test_sync_empty_retry_methods_retries_nothing(base_url: str, server_key: str) -> None:
    with (
        SyncHTTPClient(
            base_url=base_url, max_retries=2, retry_methods=set(), backoff_base=0.001
        ) as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get("flaky", params={"key": server_key, "fail_times": 1})

    assert exc_info.value.status == 503


def test_sync_a_body_cut_off_on_every_attempt_raises_a_connection_error(
    base_url: str, server_key: str
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=1, backoff_base=0.001) as client,
        pytest.raises(HTTPConnectionError),
    ):
        client.get("body-flaky", params={"key": server_key, "fail_times": 5})


def test_sync_a_cut_off_body_on_a_post_is_not_retried_by_default(
    base_url: str, server_key: str
) -> None:
    with (
        SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001) as client,
        pytest.raises(HTTPConnectionError),
    ):
        client.post("body-flaky", params={"key": server_key, "fail_times": 1})
