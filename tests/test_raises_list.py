"""`raises=` as a list of classes keyed by `status=`: which class a 4xx/5xx is raised as, what its
`.problem` and `.parse_error` are, and the `TypeError`s for a list with an unreachable entry.

The bodies come from the mocker, which runs through each transport's own request code."""

import pickle
from collections.abc import AsyncGenerator, Generator
from types import new_class
from typing import Any, Literal, assert_type

import pytest
from msgspec import Struct, ValidationError

from lothc import HTTPClient, HTTPResponseError, Response, SyncHTTPClient
from lothc.testing import LOTHCMocker


class QuotaProblem(Struct):
    limit: int
    used: int


class Problem(Struct):
    detail: str


class JobProblem(Problem):
    job_id: str


class LockedProblem(Struct):
    code: Literal["document-locked"]
    locked_by: str


class ArchivedProblem(Struct):
    code: Literal["document-archived"]


class QuotaError(HTTPResponseError[QuotaProblem], status=429):
    pass


class RateLimitError(HTTPResponseError[Problem], status=429):
    pass


class NotFoundError(HTTPResponseError[Problem], status=404):
    pass


class GatewayError(HTTPResponseError, status=(502, 503, 504)):
    pass


class ServerError(HTTPResponseError[Problem], status=range(500, 600)):
    pass


class LockedError(HTTPResponseError[LockedProblem]):
    pass


class ArchivedError(HTTPResponseError[ArchivedProblem]):
    pass


class QuotaSubError(QuotaError):
    pass


class NotFoundBase[P: Problem = Problem](HTTPResponseError[P], status=404):
    pass


class JobNotFoundError(NotFoundBase[JobProblem]):
    pass


class NarrowGatewayError(HTTPResponseError[Problem], status=503):
    pass


class JobsError[P = object](HTTPResponseError[P]):
    pass


class JobGoneError(JobsError[Problem], status=410):
    pass


@pytest.fixture(name="jobs_client")
async def _jobs_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(
        base_url=base_url, raises=[QuotaError, RateLimitError, NotFoundError, GatewayError]
    ) as client:
        yield client


@pytest.fixture(name="sync_jobs_client")
def _sync_jobs_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(
        base_url=base_url, raises=[QuotaError, RateLimitError, NotFoundError, GatewayError]
    ) as client:
        yield client


@pytest.fixture(name="lenient_jobs_client")
async def _lenient_jobs_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(
        base_url=base_url,
        raises=[QuotaError, RateLimitError, NotFoundError, GatewayError],
        raise_for_status=False,
    ) as client:
        yield client


@pytest.fixture(name="sync_lenient_jobs_client")
def _sync_lenient_jobs_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(
        base_url=base_url,
        raises=[QuotaError, RateLimitError, NotFoundError, GatewayError],
        raise_for_status=False,
    ) as client:
        yield client


def _describe(error: HTTPResponseError[Any]) -> tuple[object, ...]:
    return (type(error), error.status, error.body, error.problem, error.request, error.headers)


def _takes_error_class(cls: type[HTTPResponseError]) -> str:
    return cls.__name__


def _takes_error(error: HTTPResponseError) -> int:
    return error.status


# `status=`


async def test_a_status_keyed_class_is_raised_for_its_status(
    jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"limit": 1, "used": 2})

    with pytest.raises(QuotaError) as exc_info:
        await jobs_client.get("jobs")

    assert type(exc_info.value) is QuotaError
    assert exc_info.value.problem == QuotaProblem(limit=1, used=2)
    assert exc_info.value.parse_error is None


def test_sync_a_status_keyed_class_is_raised_for_its_status(
    sync_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"limit": 1, "used": 2})

    with pytest.raises(QuotaError) as exc_info:
        sync_jobs_client.get("jobs")

    assert type(exc_info.value) is QuotaError
    assert exc_info.value.problem == QuotaProblem(limit=1, used=2)


async def test_a_status_keyed_class_is_not_raised_for_another_status(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    # The body would decode as `QuotaProblem`, but a 400 isn't `QuotaError`'s status.
    lothc_mocker.add_get_response(path="/jobs", status=400, data={"limit": 1, "used": 2})

    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("jobs", raises=QuotaError)

    assert type(exc_info.value) is HTTPResponseError
    assert exc_info.value.problem == {"limit": 1, "used": 2}
    assert exc_info.value.parse_error is None


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_a_status_tuple_covers_each_of_its_statuses(
    jobs_client: HTTPClient, lothc_mocker: LOTHCMocker, status: int
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=status, data=b"<html>bad-gateway</html>")

    with pytest.raises(GatewayError) as exc_info:
        await jobs_client.get("jobs")

    assert exc_info.value.status == status
    assert exc_info.value.problem is None


@pytest.mark.parametrize("status", [500, 507, 599])
def test_sync_a_status_range_covers_each_of_its_statuses(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker, status: int
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=status, data={"detail": "detail"})

    with pytest.raises(ServerError) as exc_info:
        sync_client.get("jobs", raises=ServerError)

    assert exc_info.value.problem == Problem(detail="detail")


async def test_a_subclass_inherits_its_parents_status(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"limit": 1, "used": 2})
    lothc_mocker.add_get_response(path="/other", status=400, data={"limit": 1, "used": 2})

    with pytest.raises(QuotaSubError) as matched:
        await client.get("jobs", raises=QuotaSubError)
    with pytest.raises(HTTPResponseError) as unmatched:
        await client.get("other", raises=QuotaSubError)

    assert matched.value.problem == QuotaProblem(limit=1, used=2)
    assert type(unmatched.value) is HTTPResponseError


def test_sync_a_subclass_narrows_its_parents_pinned_type(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(
        path="/jobs/1", status=404, data={"detail": "detail", "job_id": "job-id"}
    )
    lothc_mocker.add_get_response(path="/jobs/2", status=404, data={"detail": "detail"})

    with pytest.raises(JobNotFoundError) as narrowed:
        sync_client.get("jobs/1", raises=JobNotFoundError)
    with pytest.raises(HTTPResponseError) as not_narrowed:
        sync_client.get("jobs/2", raises=JobNotFoundError)

    assert_type(narrowed.value.problem, JobProblem)
    assert narrowed.value.problem == JobProblem(detail="detail", job_id="job-id")
    # The narrowed type decodes only its own shape, and the class keeps its parent's `status=`.
    assert type(not_narrowed.value) is HTTPResponseError


async def test_an_unbound_generic_base_takes_its_bound_default(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=404, data={"detail": "detail"})

    with pytest.raises(NotFoundBase) as exc_info:
        await client.get("jobs", raises=NotFoundBase)
    # Annotated: mypy reads a bare class passed as a value (`pytest.raises(NotFoundBase)`) as
    # `NotFoundBase[Any]`, where an annotation takes the default.
    error: NotFoundBase = exc_info.value

    assert_type(error.problem, Problem)
    assert error.problem == Problem(detail="detail")


@pytest.mark.parametrize(
    "status",
    ["429", True, [429, "430"], 2.5],
    ids=["str", "bool", "list-with-str", "float"],
)
def test_a_status_that_is_not_an_int_is_a_type_error(status: Any) -> None:  # noqa: ANN401 — deliberately not a valid `status=`
    with pytest.raises(TypeError, match="status= takes an int or an iterable of ints"):
        new_class("BadError", (HTTPResponseError,), {"status": status})


@pytest.mark.parametrize(
    ("status", "match"),
    [
        (200, "takes 4xx/5xx statuses"),
        (600, "takes 4xx/5xx statuses"),
        (range(399, 401), "takes 4xx/5xx statuses"),
        ((), "names no status"),
    ],
    ids=["2xx", "600", "range-from-399", "empty"],
)
def test_a_status_outside_4xx_5xx_is_a_value_error(status: Any, match: str) -> None:  # noqa: ANN401 — deliberately not a valid `status=`
    with pytest.raises(ValueError, match=match):
        new_class("BadError", (HTTPResponseError,), {"status": status})


# Matching a list


async def test_one_class_or_a_one_class_list_raise_the_same(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"limit": 1, "used": 2})

    with pytest.raises(QuotaError) as one_class:
        await client.get("jobs", raises=QuotaError)
    with pytest.raises(QuotaError) as one_class_list:
        await client.get("jobs", raises=[QuotaError])
    with pytest.raises(QuotaError) as one_class_tuple:
        await client.get("jobs", raises=(QuotaError,))

    assert _describe(one_class.value) == _describe(one_class_list.value)
    assert _describe(one_class.value) == _describe(one_class_tuple.value)


async def test_the_first_class_in_list_order_that_decodes_wins(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    # Unknown fields are ignored, so this body decodes as both `QuotaProblem` and `Problem`.
    lothc_mocker.add_get_response(
        path="/jobs", status=429, data={"detail": "detail", "limit": 1, "used": 2}
    )

    with pytest.raises(QuotaError) as quota_first:
        await client.get("jobs", raises=[QuotaError, RateLimitError])
    with pytest.raises(RateLimitError) as rate_limit_first:
        await client.get("jobs", raises=[RateLimitError, QuotaError])

    assert type(quota_first.value) is QuotaError
    assert type(rate_limit_first.value) is RateLimitError


def test_sync_a_pinned_class_that_does_not_decode_falls_through_to_the_next(
    sync_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    # `QuotaError` is tried first for a 429, but this body is `RateLimitError`'s shape.
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"detail": "detail"})

    with pytest.raises(RateLimitError) as exc_info:
        sync_jobs_client.get("jobs")

    assert exc_info.value.problem == Problem(detail="detail")
    assert exc_info.value.parse_error is None


async def test_no_class_that_decodes_raises_plain_with_the_first_failure(
    jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"other": "other"})

    with pytest.raises(HTTPResponseError) as exc_info:
        await jobs_client.get("jobs")

    assert type(exc_info.value) is HTTPResponseError
    assert exc_info.value.problem == {"other": "other"}
    # `QuotaError`'s failure, the first tried: `RateLimitError` failed too. msgspec's stubs don't
    # declare its errors `ValueError`s, so `type(...) is` comes last (zuban reads the rest as
    # unreachable).
    assert "limit" in str(exc_info.value.parse_error)
    assert type(exc_info.value.parse_error) is ValidationError


def test_sync_a_status_no_class_names_raises_plain_with_no_parse_error(
    sync_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=400, data={"detail": "detail"})

    with pytest.raises(HTTPResponseError) as exc_info:
        sync_jobs_client.get("jobs")

    assert type(exc_info.value) is HTTPResponseError
    assert exc_info.value.problem == {"detail": "detail"}
    assert exc_info.value.parse_error is None


async def test_a_literal_field_selects_a_class_with_no_status(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(
        path="/locked", status=409, data={"code": "document-locked", "locked_by": "locked-by"}
    )
    lothc_mocker.add_get_response(path="/archived", status=409, data={"code": "document-archived"})

    with pytest.raises(LockedError) as locked:
        await client.get("locked", raises=[LockedError, ArchivedError])
    with pytest.raises(ArchivedError) as archived:
        await client.get("archived", raises=[LockedError, ArchivedError])

    assert locked.value.problem == LockedProblem(code="document-locked", locked_by="locked-by")
    assert archived.value.problem == ArchivedProblem(code="document-archived")


def test_sync_a_literal_field_selects_a_class_with_no_status(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/archived", status=500, data={"code": "document-archived"})

    with pytest.raises(ArchivedError) as exc_info:
        sync_client.get("archived", raises=[LockedError, ArchivedError])

    assert exc_info.value.problem == ArchivedProblem(code="document-archived")


async def test_an_unpinned_base_listed_last_is_the_fallback_with_no_parse_error(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/gone", status=410, data={"detail": "detail"})
    lothc_mocker.add_get_response(path="/other", status=410, data=b"<html>gone</html>")
    lothc_mocker.add_get_response(path="/teapot", status=418, data={"other": "other"})

    with pytest.raises(JobGoneError) as gone:
        await client.get("gone", raises=[JobGoneError, JobsError])
    with pytest.raises(JobsError) as other:
        await client.get("other", raises=[JobGoneError, JobsError])
    with pytest.raises(JobsError) as teapot:
        await client.get("teapot", raises=[JobGoneError, JobsError])

    assert gone.value.problem == Problem(detail="detail")
    assert type(other.value) is JobsError
    assert other.value.problem is None
    # Matched, by the fallback, so no parse error even though `JobGoneError` failed first.
    assert other.value.parse_error is None
    assert type(teapot.value) is JobsError
    assert teapot.value.problem == {"other": "other"}


def test_sync_an_unpinned_base_listed_last_is_the_fallback(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/other", status=410, data=b"<html>gone</html>")

    with pytest.raises(JobsError) as exc_info:
        sync_client.get("other", raises=[JobGoneError, JobsError])

    assert type(exc_info.value) is JobsError
    assert exc_info.value.parse_error is None


async def test_problem_is_a_dict_for_a_json_object_else_none(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/object", status=400, data={"detail": "detail"})
    lothc_mocker.add_get_response(path="/array", status=400, data=b"[1, 2]")
    lothc_mocker.add_get_response(path="/html", status=400, data=b"<html>bad-request</html>")

    with pytest.raises(HTTPResponseError) as unrelated:
        # Not a status `GatewayError` is for, so plain.
        await client.get("object", raises=GatewayError)
    with pytest.raises(HTTPResponseError) as array:
        await client.get("array")
    with pytest.raises(HTTPResponseError) as html:
        await client.get("html")

    assert unrelated.value.problem == {"detail": "detail"}
    assert array.value.problem is None
    assert html.value.problem is None


# `raises=` per call


async def test_a_per_call_list_replaces_the_clients(
    jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=404, data={"detail": "detail"})

    with pytest.raises(HTTPResponseError) as replaced:
        await jobs_client.get("jobs", raises=[QuotaError])
    with pytest.raises(NotFoundError) as extended:
        await jobs_client.get("jobs", raises=[JobGoneError, NotFoundError])

    # The client's `NotFoundError` isn't consulted: the call's list replaced it.
    assert type(replaced.value) is HTTPResponseError
    assert type(extended.value) is NotFoundError


def test_sync_a_per_call_class_replaces_the_clients(
    sync_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=404, data={"detail": "detail"})

    with pytest.raises(JobsError) as exc_info:
        sync_jobs_client.get("jobs", raises=JobsError)

    assert type(exc_info.value) is JobsError


async def test_a_later_change_to_the_list_does_not_change_the_client(
    base_url: str, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"limit": 1, "used": 2})
    classes: list[type[HTTPResponseError[Any]]] = [QuotaError]

    async with HTTPClient(base_url=base_url, raises=classes) as client:
        classes.clear()
        with pytest.raises(QuotaError):
            await client.get("jobs")


# Unreachable entries


@pytest.mark.parametrize(
    ("raises", "match"),
    [
        ([QuotaError, QuotaError], "listed twice"),
        ([JobsError, NotFoundError], "pins no problem type"),
        ([GatewayError, ServerError, LockedError], ""),
        ([HTTPResponseError, QuotaError], "pins no problem type"),
        ([NotFoundError, NotFoundBase], "pins the same problem type"),
        ([ServerError, GatewayError, NotFoundError, NotFoundBase], "pins the same problem type"),
    ],
    ids=[
        "duplicate",
        "after-an-unpinned-class-for-every-status",
        "reachable",
        "after-plain-http-response-error",
        "same-type-and-statuses",
        "same-type-and-statuses-later",
    ],
)
def test_an_unreachable_entry_is_a_type_error_at_construction(
    raises: list[type[HTTPResponseError[Any]]], match: str
) -> None:
    if not match:
        HTTPClient(raises=raises)
        SyncHTTPClient(raises=raises)
        return
    with pytest.raises(TypeError, match=f"raises= can never raise .*{match}"):
        HTTPClient(raises=raises)
    with pytest.raises(TypeError, match=f"raises= can never raise .*{match}"):
        SyncHTTPClient(raises=raises)


def test_an_entry_after_an_unpinned_class_covering_its_statuses_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="can never raise NarrowGatewayError"):
        HTTPClient(raises=[GatewayError, NarrowGatewayError])
    # The other way round, the unpinned class is the fallback for 503 and the only class for
    # 502 and 504.
    HTTPClient(raises=[NarrowGatewayError, GatewayError])


async def test_an_unreachable_per_call_entry_fails_before_sending(client: HTTPClient) -> None:
    # No mock: a request that went out would fail on strict mocking instead.
    with pytest.raises(TypeError, match="listed twice"):
        await client.get("jobs", raises=[QuotaError, QuotaError])


def test_sync_an_unreachable_per_call_entry_fails_before_sending(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="pins the same problem type"):
        sync_client.get("jobs", raises=[NotFoundError, NotFoundBase])


# `raise_for_status=False`


async def test_response_error_is_the_matched_error_kept_across_reads(
    lenient_jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"limit": 1, "used": 2})

    response = await lenient_jobs_client.get("jobs", into=dict)

    assert isinstance(response.error, QuotaError)
    assert response.error is response.error
    assert_type(response.error.problem, QuotaProblem)
    assert response.error.problem == QuotaProblem(limit=1, used=2)
    with pytest.raises(QuotaError) as first:
        _ = response.data
    with pytest.raises(QuotaError) as second:
        _ = response.data
    assert first.value is not second.value
    assert first.value is not response.error
    assert _describe(first.value) == _describe(response.error)
    assert _describe(second.value) == _describe(response.error)


def test_sync_response_error_is_the_matched_error_kept_across_reads(
    sync_lenient_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=404, data={"detail": "detail"})

    response = sync_lenient_jobs_client.get("jobs", into=dict)

    assert isinstance(response.error, NotFoundError)
    assert response.error is response.error
    with pytest.raises(NotFoundError) as exc_info:
        _ = response.data
    assert exc_info.value is not response.error
    assert _describe(exc_info.value) == _describe(response.error)


async def test_response_error_is_plain_for_an_unmatched_non_json_body(
    lenient_jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data=b"<html>slow-down</html>")

    response = await lenient_jobs_client.get("jobs")

    assert response.error is not None
    assert type(response.error) is HTTPResponseError
    assert response.error.problem is None
    assert isinstance(response.error.parse_error, ValueError)
    assert response.error.body == b"<html>slow-down</html>"


def test_sync_response_error_is_none_on_a_success(
    sync_lenient_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", data={"id": 1})

    response = sync_lenient_jobs_client.get("jobs", into=dict)

    assert response.error is None
    assert response.data == {"id": 1}


async def test_a_match_on_response_error_narrows_to_each_class(
    lenient_jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/quota", status=429, data={"limit": 1, "used": 2})
    lothc_mocker.add_get_response(path="/busy", status=503, data=b"busy")
    lothc_mocker.add_get_response(path="/teapot", status=418, data=b"teapot")
    lothc_mocker.add_get_response(path="/ok", data={"id": 1})
    outcomes: list[object] = []

    for path in ["quota", "busy", "teapot", "ok"]:
        response = await lenient_jobs_client.get(path, into=dict)
        match response.error:
            case QuotaError() as quota:
                assert_type(quota.problem, QuotaProblem)
                outcomes.append(quota.problem.used)
            case HTTPResponseError(status=503):
                outcomes.append("unavailable")
            case HTTPResponseError() as other:
                outcomes.append(other.status)
            case None:
                outcomes.append(response.data)

    assert outcomes == [2, "unavailable", 418, {"id": 1}]


def test_sync_isinstance_narrows_response_error(
    sync_lenient_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=404, data={"detail": "detail"})

    response: Response[dict[str, Any]] = sync_lenient_jobs_client.get("jobs", into=dict)
    error = response.error

    assert isinstance(error, NotFoundError)
    assert_type(error.problem, Problem)
    assert error.problem.detail == "detail"


# Streaming verbs


async def test_stream_get_raises_the_matched_class(
    jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"detail": "detail"})

    with pytest.raises(RateLimitError) as exc_info:
        async for _ in jobs_client.stream_get("jobs"):
            pass

    assert exc_info.value.problem == Problem(detail="detail")


def test_sync_download_returns_the_matched_error(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=404, data={"detail": "detail"})

    response = sync_client.download(
        "jobs", raises=[QuotaError, NotFoundError], raise_for_status=False
    )

    assert isinstance(response.error, NotFoundError)
    with pytest.raises(NotFoundError):
        _ = response.data


# Pickling


def test_status_keyed_errors_pickle_matched_and_unmatched(
    sync_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/quota", status=429, data={"limit": 1, "used": 2})
    lothc_mocker.add_get_response(path="/other", status=429, data={"other": "other"})
    with pytest.raises(QuotaError) as matched:
        sync_jobs_client.get("quota")
    with pytest.raises(HTTPResponseError) as unmatched:
        sync_jobs_client.get("other")

    restored_matched = pickle.loads(pickle.dumps(matched.value))  # noqa: S301 — our own object
    restored_unmatched = pickle.loads(pickle.dumps(unmatched.value))  # noqa: S301 — our own object

    assert _describe(restored_matched) == _describe(matched.value)
    assert _describe(restored_unmatched) == _describe(unmatched.value)
    # Not carried: pydantic's `ValidationError` can't be pickled.
    assert restored_unmatched.parse_error is None


async def test_an_error_response_with_a_list_pickles_before_and_after_reading_error(
    lenient_jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=503, data={"detail": "detail"})
    unread = await lenient_jobs_client.get("jobs")
    read = await lenient_jobs_client.get("jobs")
    assert read.error is not None

    restored_unread = pickle.loads(pickle.dumps(unread))  # noqa: S301 — our own object
    restored_read = pickle.loads(pickle.dumps(read))  # noqa: S301 — our own object

    assert restored_unread == unread
    assert restored_read == read
    assert type(restored_unread.error) is GatewayError
    assert type(restored_read.error) is GatewayError
    assert restored_read.error.problem == {"detail": "detail"}


# Typing: what a caller writes, checked by all four type checkers.


async def test_except_types_problem_exactly(
    jobs_client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/quota", status=429, data={"limit": 1, "used": 2})
    lothc_mocker.add_get_response(path="/other", status=400, data={"detail": "detail"})
    seen: list[object] = []

    for path in ["quota", "other"]:
        try:
            await jobs_client.get(path)
        except QuotaError as e:
            assert_type(e.problem, QuotaProblem)
            seen.append(e.problem.limit)
        except (NotFoundError, RateLimitError) as e:
            assert_type(e.problem, Problem)
            seen.append(e.problem.detail)
        except HTTPResponseError as e:
            seen.append(e.problem)

    assert seen == [1, {"detail": "detail"}]


def test_a_pinned_subclass_is_accepted_where_the_base_is_expected(
    sync_jobs_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/jobs", status=429, data={"limit": 1, "used": 2})

    with pytest.raises(QuotaError) as exc_info:
        sync_jobs_client.get(
            "jobs", raises=[QuotaError, NotFoundError, GatewayError, JobGoneError, JobsError]
        )
    base: HTTPResponseError = exc_info.value

    assert _takes_error_class(QuotaError) == "QuotaError"
    assert _takes_error_class(JobGoneError) == "JobGoneError"
    assert _takes_error(exc_info.value) == 429
    assert base.status == 429
