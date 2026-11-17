"""A client's `retry=` rule: asked about each failure lothc would retry, its answer the wait
before the next attempt or `None` to stop."""

import json
import math
import time
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest
from _oauth_server import TokenServer

from lothc import (
    HTTPClient,
    HTTPConnectionError,
    HTTPResponseError,
    RetryAttempt,
    SyncHTTPClient,
)
from lothc.testing import LOTHCMocker


@dataclass(slots=True)
class _Rule:
    """A `retry=` rule that records each `RetryAttempt` it's asked about and answers `answer`
    (`"delay"`: the wait lothc would take on its own)."""

    answer: float | Literal["delay"] | None = "delay"
    asked: list[RetryAttempt] = field(default_factory=list[RetryAttempt])

    def __call__(self, attempt: RetryAttempt) -> float | None:
        self.asked.append(attempt)
        return attempt.delay if self.answer == "delay" else self.answer


@dataclass(slots=True)
class _BadRule:
    """A `retry=` rule whose answer is neither a number nor `None`, as a checker can't see."""

    answer: Any

    def __call__(self, _attempt: RetryAttempt) -> float | None:
        answer: float | None = self.answer
        return answer


class _RuleError(Exception):
    pass


def _raising_rule(attempt: RetryAttempt) -> float | None:
    raise _RuleError(attempt.method)


async def _async_rule(attempt: RetryAttempt) -> float | None:
    return attempt.delay


class AttemptError(HTTPResponseError[dict[str, Any]], status=503): ...


class QuotaExhaustedError(HTTPResponseError[dict[str, Any]], status=429): ...


def _documented_rule(retry: RetryAttempt) -> float | None:
    """The rule `retry=`'s own documentation shows, which every type checker must accept."""
    error = retry.error
    if isinstance(error, QuotaExhaustedError):
        return None
    if isinstance(error, HTTPResponseError):
        if error.headers.get("x-should-retry") == "false":
            return None
        if "retry-after-ms" in error.headers:
            return int(error.headers["retry-after-ms"]) / 1000
    return retry.delay


@pytest.fixture(name="rule")
def _rule() -> _Rule:
    return _Rule()


@pytest.fixture(name="rule_client")
async def _rule_client(base_url: str, rule: _Rule) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(
        base_url=base_url, max_retries=2, backoff_base=0.001, retry=rule
    ) as client:
        yield client


@pytest.fixture(name="sync_rule_client")
def _sync_rule_client(base_url: str, rule: _Rule) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001, retry=rule) as client:
        yield client


async def _hits(client: HTTPClient, key: str) -> int:
    return (await client.get("hits", params={"key": key}, into=dict)).data["hits"]


def _hits_sync(client: SyncHTTPClient, key: str) -> int:
    return client.get("hits", params={"key": key}, into=dict).data["hits"]


def _status_errors(rule: _Rule) -> list[HTTPResponseError]:
    errors: list[HTTPResponseError] = []
    for asked in rule.asked:
        assert isinstance(asked.error, HTTPResponseError)
        errors.append(asked.error)
    return errors


# --- What the rule is asked ----------------------------------------------------------------------


async def test_the_rule_is_asked_about_each_retried_status(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    response = await rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 2}, into=dict
    )
    errors = _status_errors(rule)

    assert response.data["attempts"] == 3
    assert [asked.attempt for asked in rule.asked] == [0, 1]
    assert [asked.method for asked in rule.asked] == ["GET", "GET"]
    assert [asked.delay for asked in rule.asked] == [0.001, 0.002]
    assert 0 <= rule.asked[0].elapsed <= rule.asked[1].elapsed
    assert [type(error) for error in errors] == [HTTPResponseError, HTTPResponseError]
    assert [error.status for error in errors] == [503, 503]
    assert [error.problem for error in errors] == [{"attempt": 1}, {"attempt": 2}]
    assert errors[0].request.path == "/status-then-ok"


def test_sync_the_rule_is_asked_about_each_retried_status(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    response = sync_rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 2}, into=dict
    )
    errors = _status_errors(rule)

    assert response.data["attempts"] == 3
    assert [asked.attempt for asked in rule.asked] == [0, 1]
    assert [asked.method for asked in rule.asked] == ["GET", "GET"]
    assert [asked.delay for asked in rule.asked] == [0.001, 0.002]
    assert 0 <= rule.asked[0].elapsed <= rule.asked[1].elapsed
    assert [type(error) for error in errors] == [HTTPResponseError, HTTPResponseError]
    assert [error.status for error in errors] == [503, 503]
    assert [error.problem for error in errors] == [{"attempt": 1}, {"attempt": 2}]
    assert errors[0].request.path == "/status-then-ok"


async def test_the_delay_is_the_retry_after_when_one_is_sent(
    base_url: str, server_key: str
) -> None:
    rule = _Rule(answer=0)
    async with HTTPClient(base_url=base_url, max_retries=1, retry=rule) as client:
        response = await client.get(
            "retry-after-custom", params={"key": server_key, "value": "7"}, into=dict
        )

    assert response.data == {"attempts": 2}
    assert [asked.delay for asked in rule.asked] == [7.0]


def test_sync_the_delay_is_the_retry_after_when_one_is_sent(base_url: str, server_key: str) -> None:
    rule = _Rule(answer=0)
    with SyncHTTPClient(base_url=base_url, max_retries=1, retry=rule) as client:
        response = client.get(
            "retry-after-custom", params={"key": server_key, "value": "7"}, into=dict
        )

    assert response.data == {"attempts": 2}
    assert [asked.delay for asked in rule.asked] == [7.0]


async def test_the_error_is_the_clients_raises_class(base_url: str, server_key: str) -> None:
    rule = _Rule()
    async with HTTPClient(
        base_url=base_url, max_retries=1, backoff_base=0.001, raises=AttemptError, retry=rule
    ) as client:
        await client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
    error = rule.asked[0].error

    assert isinstance(error, AttemptError)
    assert error.problem == {"attempt": 1}


def test_sync_the_error_is_the_clients_raises_class(base_url: str, server_key: str) -> None:
    rule = _Rule()
    with SyncHTTPClient(
        base_url=base_url, max_retries=1, backoff_base=0.001, raises=AttemptError, retry=rule
    ) as client:
        client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
    error = rule.asked[0].error

    assert isinstance(error, AttemptError)
    assert error.problem == {"attempt": 1}


async def test_the_error_is_the_calls_raises_class(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    await rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, raises=AttemptError
    )
    error = rule.asked[0].error

    assert isinstance(error, AttemptError)
    assert error.problem == {"attempt": 1}


def test_sync_the_error_is_the_calls_raises_class(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    sync_rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, raises=AttemptError
    )
    error = rule.asked[0].error

    assert isinstance(error, AttemptError)
    assert error.problem == {"attempt": 1}


async def test_the_rule_is_asked_about_a_transport_failure(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    response = await rule_client.get(
        "connection-flaky", params={"key": server_key, "fail_times": 1}, into=dict
    )

    assert response.data == {"attempts": 2}
    assert len(rule.asked) == 1
    assert isinstance(rule.asked[0].error, HTTPConnectionError)
    assert (rule.asked[0].attempt, rule.asked[0].method) == (0, "GET")
    assert rule.asked[0].delay == 0.001


def test_sync_the_rule_is_asked_about_a_transport_failure(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    response = sync_rule_client.get(
        "connection-flaky", params={"key": server_key, "fail_times": 1}, into=dict
    )

    assert response.data == {"attempts": 2}
    assert len(rule.asked) == 1
    assert isinstance(rule.asked[0].error, HTTPConnectionError)
    assert (rule.asked[0].attempt, rule.asked[0].method) == (0, "GET")
    assert rule.asked[0].delay == 0.001


# --- What its answer does ------------------------------------------------------------------------


async def test_the_rules_wait_is_taken(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = 0.2
    started = time.monotonic()
    await rule_client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
    took = time.monotonic() - started

    assert 0.19 <= took < 1.5


def test_sync_the_rules_wait_is_taken(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = 0.2
    started = time.monotonic()
    sync_rule_client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
    took = time.monotonic() - started

    assert 0.19 <= took < 1.5


async def test_none_stops_retrying_and_raises_the_error_the_rule_saw(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = None
    with pytest.raises(HTTPResponseError) as exc_info:
        await rule_client.get("status-then-ok", params={"key": server_key, "fail_times": 1})

    assert exc_info.value is rule.asked[0].error
    assert await _hits(rule_client, server_key) == 1


def test_sync_none_stops_retrying_and_raises_the_error_the_rule_saw(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = None
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_rule_client.get("status-then-ok", params={"key": server_key, "fail_times": 1})

    assert exc_info.value is rule.asked[0].error
    assert _hits_sync(sync_rule_client, server_key) == 1


async def test_none_returns_the_error_the_rule_saw_without_raise_for_status(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = None
    response = await rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, raise_for_status=False
    )

    assert response.status == 503
    assert response.error is rule.asked[0].error
    assert await _hits(rule_client, server_key) == 1


def test_sync_none_returns_the_error_the_rule_saw_without_raise_for_status(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = None
    response = sync_rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, raise_for_status=False
    )

    assert response.status == 503
    assert response.error is rule.asked[0].error
    assert _hits_sync(sync_rule_client, server_key) == 1


async def test_none_stops_retrying_a_transport_failure(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = None
    with pytest.raises(HTTPConnectionError) as exc_info:
        await rule_client.get("connection-flaky", params={"key": server_key, "fail_times": 1})

    assert exc_info.value is rule.asked[0].error


def test_sync_none_stops_retrying_a_transport_failure(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = None
    with pytest.raises(HTTPConnectionError) as exc_info:
        sync_rule_client.get("connection-flaky", params={"key": server_key, "fail_times": 1})

    assert exc_info.value is rule.asked[0].error


async def test_a_wait_above_max_retry_after_stops_retrying(base_url: str, server_key: str) -> None:
    rule = _Rule(answer=0.05)
    async with HTTPClient(
        base_url=base_url, max_retries=2, max_retry_after=0.01, retry=rule
    ) as client:
        with pytest.raises(HTTPResponseError):
            await client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
        hits = await _hits(client, server_key)

    assert hits == 1
    assert len(rule.asked) == 1


def test_sync_a_wait_above_max_retry_after_stops_retrying(base_url: str, server_key: str) -> None:
    rule = _Rule(answer=0.05)
    with SyncHTTPClient(
        base_url=base_url, max_retries=2, max_retry_after=0.01, retry=rule
    ) as client:
        with pytest.raises(HTTPResponseError):
            client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
        hits = _hits_sync(client, server_key)

    assert hits == 1
    assert len(rule.asked) == 1


async def test_max_retry_after_none_takes_any_wait_the_rule_gives(
    base_url: str, server_key: str
) -> None:
    rule = _Rule(answer=0.05)
    async with HTTPClient(
        base_url=base_url, max_retries=1, max_retry_after=None, retry=rule
    ) as client:
        response = await client.get(
            "status-then-ok", params={"key": server_key, "fail_times": 1}, into=dict
        )

    assert response.data["attempts"] == 2


def test_sync_max_retry_after_none_takes_any_wait_the_rule_gives(
    base_url: str, server_key: str
) -> None:
    rule = _Rule(answer=0.05)
    with SyncHTTPClient(
        base_url=base_url, max_retries=1, max_retry_after=None, retry=rule
    ) as client:
        response = client.get(
            "status-then-ok", params={"key": server_key, "fail_times": 1}, into=dict
        )

    assert response.data["attempts"] == 2


async def test_the_rule_may_retry_sooner_than_an_over_long_retry_after(
    base_url: str, server_key: str
) -> None:
    rule = _Rule(answer=0)
    async with HTTPClient(base_url=base_url, max_retries=1, retry=rule) as client:
        response = await client.get(
            "retry-after-custom", params={"key": server_key, "value": "3600"}, into=dict
        )

    assert response.data == {"attempts": 2}
    assert [asked.delay for asked in rule.asked] == [3600.0]


def test_sync_the_rule_may_retry_sooner_than_an_over_long_retry_after(
    base_url: str, server_key: str
) -> None:
    rule = _Rule(answer=0)
    with SyncHTTPClient(base_url=base_url, max_retries=1, retry=rule) as client:
        response = client.get(
            "retry-after-custom", params={"key": server_key, "value": "3600"}, into=dict
        )

    assert response.data == {"attempts": 2}
    assert [asked.delay for asked in rule.asked] == [3600.0]


async def test_a_negative_wait_retries_at_once(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = -5
    response = await rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, into=dict
    )

    assert response.data["attempts"] == 2


def test_sync_a_negative_wait_retries_at_once(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = -5
    response = sync_rule_client.get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, into=dict
    )

    assert response.data["attempts"] == 2


def _bad_answers() -> list[tuple[object, type[Exception], str]]:
    return [
        (False, TypeError, "seconds to wait or None to stop, not False"),
        ("1", TypeError, "seconds to wait or None to stop, not '1'"),
        (math.nan, ValueError, "finite number of seconds, not nan"),
        (math.inf, ValueError, "finite number of seconds, not inf"),
    ]


@pytest.mark.parametrize(
    ("answer", "error", "match"), _bad_answers(), ids=["false", "str", "nan", "inf"]
)
async def test_an_answer_that_is_not_a_wait_is_raised(
    base_url: str, server_key: str, answer: object, error: type[Exception], match: str
) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, retry=_BadRule(answer)) as client:
        with pytest.raises(error, match=match):
            await client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
        hits = await _hits(client, server_key)

    assert hits == 1


@pytest.mark.parametrize(
    ("answer", "error", "match"), _bad_answers(), ids=["false", "str", "nan", "inf"]
)
def test_sync_an_answer_that_is_not_a_wait_is_raised(
    base_url: str, server_key: str, answer: object, error: type[Exception], match: str
) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, retry=_BadRule(answer)) as client:
        with pytest.raises(error, match=match):
            client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
        hits = _hits_sync(client, server_key)

    assert hits == 1


async def test_an_exception_the_rule_raises_propagates(base_url: str, server_key: str) -> None:
    async with HTTPClient(base_url=base_url, max_retries=2, retry=_raising_rule) as client:
        with pytest.raises(_RuleError, match="GET"):
            await client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
        hits = await _hits(client, server_key)

    assert hits == 1


def test_sync_an_exception_the_rule_raises_propagates(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(base_url=base_url, max_retries=2, retry=_raising_rule) as client:
        with pytest.raises(_RuleError, match="GET"):
            client.get("status-then-ok", params={"key": server_key, "fail_times": 1})
        hits = _hits_sync(client, server_key)

    assert hits == 1


# --- When it isn't asked -------------------------------------------------------------------------


async def test_the_rule_is_not_asked_about_a_status_outside_retry_statuses(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    params: dict[str, str | int] = {"key": server_key, "fail_times": 1, "status": 404}
    with pytest.raises(HTTPResponseError):
        await rule_client.get("status-then-ok", params=params)

    assert rule.asked == []


def test_sync_the_rule_is_not_asked_about_a_status_outside_retry_statuses(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    params: dict[str, str | int] = {"key": server_key, "fail_times": 1, "status": 404}
    with pytest.raises(HTTPResponseError):
        sync_rule_client.get("status-then-ok", params=params)

    assert rule.asked == []


async def test_the_rule_is_not_asked_about_a_verb_outside_retry_methods(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    with pytest.raises(HTTPResponseError):
        await rule_client.post("status-then-ok", params={"key": server_key, "fail_times": 1})

    assert rule.asked == []


def test_sync_the_rule_is_not_asked_about_a_verb_outside_retry_methods(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    with pytest.raises(HTTPResponseError):
        sync_rule_client.post("status-then-ok", params={"key": server_key, "fail_times": 1})

    assert rule.asked == []


async def test_the_rule_is_not_asked_about_a_form_upload(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    with pytest.raises(HTTPResponseError):
        await rule_client.put(
            "status-then-ok", params={"key": server_key, "fail_times": 1}, form={"a": "b"}
        )

    assert rule.asked == []


def test_sync_the_rule_is_not_asked_about_a_form_upload(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    with pytest.raises(HTTPResponseError):
        sync_rule_client.put(
            "status-then-ok", params={"key": server_key, "fail_times": 1}, form={"a": "b"}
        )

    assert rule.asked == []


async def test_the_rule_is_not_asked_about_a_401_that_reauthentication_recovered(
    base_url: str, token_server: TokenServer
) -> None:
    rule = _Rule()
    async with HTTPClient(
        base_url=base_url,
        bearer_auth=token_server.provider(),
        max_retries=1,
        retry_statuses={401, 503},
        retry=rule,
    ) as client:
        response = await client.get("rejects-first-token", into=dict)

    assert response.data == {"authorization": "Bearer token-2"}
    assert rule.asked == []


def test_sync_the_rule_is_not_asked_about_a_401_that_reauthentication_recovered(
    base_url: str, token_server: TokenServer
) -> None:
    rule = _Rule()
    with SyncHTTPClient(
        base_url=base_url,
        bearer_auth=token_server.sync_provider(),
        max_retries=1,
        retry_statuses={401, 503},
        retry=rule,
    ) as client:
        response = client.get("rejects-first-token", into=dict)

    assert response.data == {"authorization": "Bearer token-2"}
    assert rule.asked == []


async def test_the_rule_is_not_asked_once_retries_run_out(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await rule_client.get("status-then-ok", params={"key": server_key, "fail_times": 5})

    assert [asked.attempt for asked in rule.asked] == [0, 1]
    assert exc_info.value.problem == {"attempt": 3}


def test_sync_the_rule_is_not_asked_once_retries_run_out(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_rule_client.get("status-then-ok", params={"key": server_key, "fail_times": 5})

    assert [asked.attempt for asked in rule.asked] == [0, 1]
    assert exc_info.value.problem == {"attempt": 3}


# --- A call's own `max_retries` -----------------------------------------------------------------


async def test_a_call_max_retries_asks_the_rule_for_a_verb_outside_retry_methods(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    response = await rule_client.post(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, json={}, max_retries=1
    )

    assert response.status == 200
    assert [(asked.attempt, asked.method) for asked in rule.asked] == [(0, "POST")]


def test_sync_a_call_max_retries_asks_the_rule_for_a_verb_outside_retry_methods(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    response = sync_rule_client.post(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, json={}, max_retries=1
    )

    assert response.status == 200
    assert [(asked.attempt, asked.method) for asked in rule.asked] == [(0, "POST")]


async def test_a_call_max_retries_bounds_how_often_the_rule_is_asked(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    params: dict[str, str | int] = {"key": server_key, "fail_times": 9}
    with pytest.raises(HTTPResponseError):
        await rule_client.get("status-then-ok", params=params, max_retries=3)
    with pytest.raises(HTTPResponseError):
        await rule_client.get("status-then-ok", params=params, max_retries=0)

    assert [asked.attempt for asked in rule.asked] == [0, 1, 2]


def test_sync_a_call_max_retries_bounds_how_often_the_rule_is_asked(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    params: dict[str, str | int] = {"key": server_key, "fail_times": 9}
    with pytest.raises(HTTPResponseError):
        sync_rule_client.get("status-then-ok", params=params, max_retries=3)
    with pytest.raises(HTTPResponseError):
        sync_rule_client.get("status-then-ok", params=params, max_retries=0)

    assert [asked.attempt for asked in rule.asked] == [0, 1, 2]


# --- The other verbs ----------------------------------------------------------------------------


@pytest.mark.parametrize("verb", ["head", "options"])
async def test_head_and_options_ask_the_rule(
    server_key: str, verb: Literal["head", "options"], rule: _Rule, rule_client: HTTPClient
) -> None:
    params: dict[str, str | int] = {"key": server_key, "fail_times": 1}
    if verb == "head":
        response = await rule_client.head("status-then-ok", params=params)
    else:
        response = await rule_client.options("status-then-ok", params=params)

    assert response.status == 200
    assert [asked.method for asked in rule.asked] == [verb.upper()]
    assert [error.status for error in _status_errors(rule)] == [503]


@pytest.mark.parametrize("verb", ["head", "options"])
def test_sync_head_and_options_ask_the_rule(
    server_key: str,
    verb: Literal["head", "options"],
    rule: _Rule,
    sync_rule_client: SyncHTTPClient,
) -> None:
    params: dict[str, str | int] = {"key": server_key, "fail_times": 1}
    if verb == "head":
        response = sync_rule_client.head("status-then-ok", params=params)
    else:
        response = sync_rule_client.options("status-then-ok", params=params)

    assert response.status == 200
    assert [asked.method for asked in rule.asked] == [verb.upper()]
    assert [error.status for error in _status_errors(rule)] == [503]


async def test_download_asks_the_rule(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    response = await rule_client.download(
        "status-then-ok", params={"key": server_key, "fail_times": 1}
    )

    assert json.loads(response.data)["attempts"] == 2
    assert [error.problem for error in _status_errors(rule)] == [{"attempt": 1}]


def test_sync_download_asks_the_rule(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    response = sync_rule_client.download(
        "status-then-ok", params={"key": server_key, "fail_times": 1}
    )

    assert json.loads(response.data)["attempts"] == 2
    assert [error.problem for error in _status_errors(rule)] == [{"attempt": 1}]


async def test_download_raises_the_error_the_rule_stopped_on(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = None
    with pytest.raises(HTTPResponseError) as exc_info:
        await rule_client.download("status-then-ok", params={"key": server_key, "fail_times": 1})

    assert exc_info.value is rule.asked[0].error
    assert exc_info.value.problem == {"attempt": 1}


def test_sync_download_raises_the_error_the_rule_stopped_on(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = None
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_rule_client.download("status-then-ok", params={"key": server_key, "fail_times": 1})

    assert exc_info.value is rule.asked[0].error
    assert exc_info.value.problem == {"attempt": 1}


async def test_download_returns_the_body_the_rule_stopped_on_without_raise_for_status(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = None
    response = await rule_client.download(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, raise_for_status=False
    )

    assert response.status == 503
    assert response.error is not None
    assert response.error.problem == {"attempt": 1}


def test_sync_download_returns_the_body_the_rule_stopped_on_without_raise_for_status(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = None
    response = sync_rule_client.download(
        "status-then-ok", params={"key": server_key, "fail_times": 1}, raise_for_status=False
    )

    assert response.status == 503
    assert response.error is not None
    assert response.error.problem == {"attempt": 1}


async def test_stream_get_asks_the_rule(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    chunks = rule_client.stream_get("status-then-ok", params={"key": server_key, "fail_times": 1})

    assert json.loads(b"".join([chunk async for chunk in chunks]))["attempts"] == 2
    assert [error.problem for error in _status_errors(rule)] == [{"attempt": 1}]


def test_sync_stream_get_asks_the_rule(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    chunks = sync_rule_client.stream_get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}
    )

    assert json.loads(b"".join(chunks))["attempts"] == 2
    assert [error.problem for error in _status_errors(rule)] == [{"attempt": 1}]


async def test_stream_get_raises_the_error_the_rule_stopped_on(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    rule.answer = None
    chunks = rule_client.stream_get("status-then-ok", params={"key": server_key, "fail_times": 1})
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = [chunk async for chunk in chunks]

    assert exc_info.value is rule.asked[0].error
    assert exc_info.value.problem == {"attempt": 1}


def test_sync_stream_get_raises_the_error_the_rule_stopped_on(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    rule.answer = None
    chunks = sync_rule_client.stream_get(
        "status-then-ok", params={"key": server_key, "fail_times": 1}
    )
    with pytest.raises(HTTPResponseError) as exc_info:
        _ = list(chunks)

    assert exc_info.value is rule.asked[0].error
    assert exc_info.value.problem == {"attempt": 1}


async def test_stream_post_asks_the_rule(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    lines = rule_client.stream_post(
        "status-then-ok",
        params={"key": server_key, "fail_times": 1},
        content="content",
        into=dict,
        max_retries=1,
    )

    assert [line async for line in lines] == [{"attempts": 2, "bodies": ["content", "content"]}]
    assert [(asked.attempt, asked.method) for asked in rule.asked] == [(0, "POST")]


def test_sync_stream_post_asks_the_rule(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    lines = sync_rule_client.stream_post(
        "status-then-ok",
        params={"key": server_key, "fail_times": 1},
        content="content",
        into=dict,
        max_retries=1,
    )

    assert list(lines) == [{"attempts": 2, "bodies": ["content", "content"]}]
    assert [(asked.attempt, asked.method) for asked in rule.asked] == [(0, "POST")]


async def _collect_sse_events(client: HTTPClient, key: str, events: list[str]) -> None:
    async for event in client.sse(
        "events-then-status", params={"key": key, "status": 503}, reconnect_delay=0.001
    ):
        events.append(event.data)


# `sse()` reconnects by its own loop, which never asks the rule: a connection that breaks
# mid-stream is reconnected as before. Each connection is opened through the retry loop (the
# client's `max_retries` for GET), which asks the rule about a retryable status or transport
# failure on opening, exactly as for `stream_get`.
async def test_sse_asks_the_rule_only_when_opening_a_connection(
    server_key: str, rule: _Rule, rule_client: HTTPClient
) -> None:
    events: list[str] = []
    with pytest.raises(HTTPResponseError) as exc_info:
        await _collect_sse_events(rule_client, server_key, events)

    assert events == ["first"]
    assert [(asked.attempt, asked.method) for asked in rule.asked] == [(0, "GET"), (1, "GET")]
    assert [error.problem for error in _status_errors(rule)] == [{"refused": True}] * 2
    assert exc_info.value.status == 503


def test_sync_sse_asks_the_rule_only_when_opening_a_connection(
    server_key: str, rule: _Rule, sync_rule_client: SyncHTTPClient
) -> None:
    events: list[str] = []
    with pytest.raises(HTTPResponseError) as exc_info:
        events.extend(
            event.data
            for event in sync_rule_client.sse(
                "events-then-status",
                params={"key": server_key, "status": 503},
                reconnect_delay=0.001,
            )
        )

    assert events == ["first"]
    assert [(asked.attempt, asked.method) for asked in rule.asked] == [(0, "GET"), (1, "GET")]
    assert [error.problem for error in _status_errors(rule)] == [{"refused": True}] * 2
    assert exc_info.value.status == 503


# --- The mocker ---------------------------------------------------------------------------------


async def test_a_mocked_retry_asks_the_rule(base_url: str, lothc_mocker: LOTHCMocker) -> None:
    lothc_mocker.add_get_response(path="/items", status=503, data={"a": 1})
    rule = _Rule()

    async with HTTPClient(
        base_url=base_url, max_retries=2, backoff_base=0.001, retry=rule
    ) as client:
        with pytest.raises(HTTPResponseError) as exc_info:
            await client.get("items")

    assert [asked.attempt for asked in rule.asked] == [0, 1]
    assert [error.problem for error in _status_errors(rule)] == [{"a": 1}, {"a": 1}]
    assert exc_info.value.status == 503


def test_sync_a_mocked_retry_asks_the_rule(base_url: str, lothc_mocker: LOTHCMocker) -> None:
    lothc_mocker.add_get_response(path="/items", status=503, data={"a": 1})
    rule = _Rule()

    with (
        SyncHTTPClient(base_url=base_url, max_retries=2, backoff_base=0.001, retry=rule) as client,
        pytest.raises(HTTPResponseError) as exc_info,
    ):
        client.get("items")

    assert [asked.attempt for asked in rule.asked] == [0, 1]
    assert [error.problem for error in _status_errors(rule)] == [{"a": 1}, {"a": 1}]
    assert exc_info.value.status == 503


# --- Construction and typing --------------------------------------------------------------------


def test_an_async_rule_is_refused_at_construction() -> None:
    settings: dict[str, Any] = {"retry": _async_rule}

    with pytest.raises(TypeError, match="not an async one: _async_rule"):
        HTTPClient(**settings)
    with pytest.raises(TypeError, match="not an async one: _async_rule"):
        SyncHTTPClient(**settings)


def test_a_rule_that_is_not_callable_is_refused_at_construction() -> None:
    settings: dict[str, Any] = {"retry": 1.5}

    with pytest.raises(TypeError, match=r"takes a function of a RetryAttempt, not 1\.5"):
        HTTPClient(**settings)
    with pytest.raises(TypeError, match=r"takes a function of a RetryAttempt, not 1\.5"):
        SyncHTTPClient(**settings)


async def test_the_documented_rule_retries(base_url: str, server_key: str) -> None:
    async with HTTPClient(
        base_url=base_url,
        max_retries=3,
        backoff_base=0.001,
        raises=[QuotaExhaustedError],
        retry=_documented_rule,
    ) as client:
        response = await client.get(
            "status-then-ok", params={"key": server_key, "fail_times": 1}, into=dict
        )

    assert response.data["attempts"] == 2


def test_sync_the_documented_rule_retries(base_url: str, server_key: str) -> None:
    with SyncHTTPClient(
        base_url=base_url,
        max_retries=3,
        backoff_base=0.001,
        raises=[QuotaExhaustedError],
        retry=_documented_rule,
    ) as client:
        response = client.get(
            "status-then-ok", params={"key": server_key, "fail_times": 1}, into=dict
        )

    assert response.data["attempts"] == 2


def test_a_lambda_rule_is_accepted() -> None:
    HTTPClient(retry=lambda attempt: None if attempt.elapsed > 1 else attempt.delay)
    SyncHTTPClient(retry=lambda attempt: None if attempt.elapsed > 1 else attempt.delay)
