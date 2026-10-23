"""`raises=`: the `HTTPResponseError` subclass a 4xx/5xx is raised as, whose pinned type
parameter decodes the body onto `.error`."""

import pickle
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any, Literal, assert_type

import pytest
from msgspec import Struct, ValidationError
from pydantic import BaseModel

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient


class NotFound(Struct, tag_field="type", tag="not-found"):
    title: str
    status: int


class NotAuthorized(Struct, tag_field="type", tag="not-authorized"):
    title: str
    status: int


class NotAuthorizedModel(BaseModel):
    title: str


type APIError = NotFound | NotAuthorized


class MyAPIError(HTTPResponseError[APIError]):
    pass


class SubAPIError(MyAPIError):
    pass


class AuthError(HTTPResponseError[NotAuthorized]):
    pass


class GenericError[T](HTTPResponseError[T]):
    pass


class ChainedError(GenericError[NotFound]):
    pass


class DefaultedError[T = NotFound](HTTPResponseError[T]):
    pass


class DefaultedChildError(DefaultedError):
    pass


class OverriddenDefaultError(DefaultedError[NotAuthorized]):
    pass


class MixedError(HTTPResponseError[NotFound | NotAuthorizedModel]):
    pass


@pytest.fixture(name="api_client")
async def _api_client(base_url: str) -> AsyncGenerator[HTTPClient]:
    async with HTTPClient(base_url=base_url, raises=MyAPIError) as client:
        yield client


@pytest.fixture(name="sync_api_client")
def _sync_api_client(base_url: str) -> Generator[SyncHTTPClient]:
    with SyncHTTPClient(base_url=base_url, raises=MyAPIError) as client:
        yield client


async def test_a_404_decodes_as_its_member_of_the_pinned_union(api_client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        await api_client.get("missing")

    assert exc_info.value.error == NotFound(title="Not found", status=404)
    assert exc_info.value.parse_error is None


async def test_a_401_decodes_as_its_member_of_the_pinned_union(api_client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        await api_client.get("unauthorized")

    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


def test_sync_a_404_and_a_401_decode_as_their_members_of_the_pinned_union(
    sync_api_client: SyncHTTPClient,
) -> None:
    with pytest.raises(MyAPIError) as not_found:
        sync_api_client.get("missing")
    with pytest.raises(MyAPIError) as not_authorized:
        sync_api_client.get("unauthorized")

    assert not_found.value.error == NotFound(title="Not found", status=404)
    assert not_authorized.value.error == NotAuthorized(title="Not authorized", status=401)


async def test_a_json_body_outside_the_union_is_a_dict_with_a_parse_error(
    api_client: HTTPClient,
) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        await api_client.get("boom")

    assert exc_info.value.error == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }
    # msgspec's stubs don't declare its `DecodeError` a `ValueError`, though at runtime it is, so
    # an `isinstance` here reads as unreachable to a checker.
    assert type(exc_info.value.parse_error) is ValidationError


def test_sync_a_json_body_outside_the_union_is_a_dict_with_a_parse_error(
    sync_api_client: SyncHTTPClient,
) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        sync_api_client.get("boom")

    assert exc_info.value.error == {
        "type": "internal-server-error",
        "title": "Internal server error",
        "status": 500,
    }
    # msgspec's stubs don't declare its `DecodeError` a `ValueError`, though at runtime it is, so
    # an `isinstance` here reads as unreachable to a checker.
    assert type(exc_info.value.parse_error) is ValidationError


async def test_a_non_json_body_is_none_with_the_body_intact(api_client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        await api_client.get("invalid-json-error")

    assert exc_info.value.status == 502
    assert exc_info.value.error is None
    assert isinstance(exc_info.value.parse_error, ValueError)
    assert exc_info.value.body == b"<html><body>502 Bad Gateway</body></html>"


async def test_without_raises_a_non_json_body_is_none_with_no_parse_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("invalid-json-error")

    assert exc_info.value.error is None
    assert exc_info.value.parse_error is None
    assert exc_info.value.body.startswith(b"<html>")


async def test_without_raises_a_json_object_body_is_a_dict(client: HTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("missing")

    assert type(exc_info.value) is HTTPResponseError
    assert exc_info.value.error == {"type": "not-found", "title": "Not found", "status": 404}


def test_sync_without_raises_a_json_object_body_is_a_dict(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        sync_client.get("missing")

    assert type(exc_info.value) is HTTPResponseError
    assert exc_info.value.error == {"type": "not-found", "title": "Not found", "status": 404}


async def test_a_per_call_raises_overrides_the_clients(api_client: HTTPClient) -> None:
    with pytest.raises(AuthError) as exc_info:
        await api_client.get("unauthorized", raises=AuthError)

    assert type(exc_info.value) is AuthError
    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


def test_sync_a_per_call_raises_overrides_the_clients(sync_api_client: SyncHTTPClient) -> None:
    with pytest.raises(AuthError) as exc_info:
        sync_api_client.get("unauthorized", raises=AuthError)

    assert type(exc_info.value) is AuthError


async def test_a_per_call_raises_on_a_plain_client(client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        await client.get("missing", raises=MyAPIError)

    assert exc_info.value.error == NotFound(title="Not found", status=404)


async def test_a_subclass_of_a_pinned_subclass_decodes_the_same(client: HTTPClient) -> None:
    with pytest.raises(SubAPIError) as exc_info:
        await client.get("missing", raises=SubAPIError)

    assert exc_info.value.error == NotFound(title="Not found", status=404)


def test_sync_a_subclass_of_a_pinned_subclass_decodes_the_same(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(SubAPIError) as exc_info:
        sync_client.get("unauthorized", raises=SubAPIError)

    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


async def test_a_type_parameter_bound_further_down_the_chain_is_resolved(
    client: HTTPClient,
) -> None:
    with pytest.raises(ChainedError) as exc_info:
        await client.get("missing", raises=ChainedError)

    assert exc_info.value.error == NotFound(title="Not found", status=404)


async def test_an_unbound_type_parameter_falls_back_to_its_default(client: HTTPClient) -> None:
    # Checkers read a bare `DefaultedError` as `DefaultedError[NotFound]`, so lothc does too.
    with pytest.raises(DefaultedError) as exc_info:
        await client.get("missing", raises=DefaultedError)

    assert exc_info.value.error == NotFound(title="Not found", status=404)


def test_sync_an_unbound_type_parameter_falls_back_to_its_default(base_url: str) -> None:
    with (
        SyncHTTPClient(base_url=base_url, raises=DefaultedChildError) as client,
        pytest.raises(DefaultedChildError) as exc_info,
    ):
        client.get("missing")

    assert exc_info.value.error == NotFound(title="Not found", status=404)


async def test_a_bound_type_parameter_overrides_its_default(client: HTTPClient) -> None:
    with pytest.raises(OverriddenDefaultError) as exc_info:
        await client.get("unauthorized", raises=OverriddenDefaultError)

    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


@pytest.mark.parametrize(
    ("raises", "match"),
    [
        (GenericError, "leaves HTTPResponseError's error type unbound"),
        (int, "must be HTTPResponseError or a subclass"),
        (HTTPResponseError[NotFound], "must be HTTPResponseError or a subclass"),
        (MixedError, "mixes msgspec Structs and pydantic BaseModels"),
    ],
    ids=["unbound", "not-a-subclass", "generic-alias", "mixed-libraries"],
)
def test_a_bad_raises_fails_at_construction(raises: Any, match: str) -> None:  # noqa: ANN401 — deliberately not a valid `raises=`
    with pytest.raises(TypeError, match=match):
        HTTPClient(raises=raises)
    with pytest.raises(TypeError, match=match):
        SyncHTTPClient(raises=raises)


# The verbs each `raises=` validation test calls (the sync one uses `put` where async uses `post`).
type Verb = Literal["get", "post", "head", "download", "stream_get", "stream_post", "sse"]
type SyncVerb = Literal["get", "put", "head", "download", "stream_get", "stream_post", "sse"]


async def _call_with_raises(
    client: HTTPClient,
    verb: Verb,
    raises: Any,  # noqa: ANN401 — deliberately not a valid `raises=`
    tmp_path: Path,
) -> None:
    match verb:
        case "get":
            await client.get("items/7", raises=raises)
        case "post":
            await client.post("items", json={"a": 1}, raises=raises)
        case "head":
            await client.head("items/7", raises=raises)
        case "download":
            await client.download("binary", tmp_path / "file", raises=raises)
        case "stream_get":
            async for _ in client.stream_get("binary", raises=raises):
                pass
        case "stream_post":
            async for _ in client.stream_post("ndjson-echo", json={"n": 1}, raises=raises):
                pass
        case "sse":
            async for _ in client.sse("events", raises=raises):
                pass


def _sync_call_with_raises(
    sync_client: SyncHTTPClient,
    verb: SyncVerb,
    raises: Any,  # noqa: ANN401 — deliberately not a valid `raises=`
    tmp_path: Path,
) -> None:
    match verb:
        case "get":
            sync_client.get("items/7", raises=raises)
        case "put":
            sync_client.put("items/7", json={"name": "name"}, raises=raises)
        case "head":
            sync_client.head("items/7", raises=raises)
        case "download":
            sync_client.download("binary", tmp_path / "file", raises=raises)
        case "stream_get":
            for _ in sync_client.stream_get("binary", raises=raises):
                pass
        case "stream_post":
            for _ in sync_client.stream_post("ndjson-echo", json={"n": 1}, raises=raises):
                pass
        case "sse":
            for _ in sync_client.sse("events", raises=raises):
                pass


@pytest.mark.parametrize(
    ("raises", "match"),
    [
        (GenericError, "leaves HTTPResponseError's error type unbound"),
        (int, "must be HTTPResponseError or a subclass"),
    ],
    ids=["unbound", "not-a-subclass"],
)
@pytest.mark.parametrize(
    "verb", ["get", "post", "head", "download", "stream_get", "stream_post", "sse"]
)
async def test_a_bad_per_call_raises_fails_even_on_a_200(
    client: HTTPClient,
    tmp_path: Path,
    verb: Verb,
    raises: Any,  # noqa: ANN401 — deliberately not a valid `raises=`
    match: str,
) -> None:
    # Checked before sending, so it can't hide until a real 4xx/5xx and replace that error.
    with pytest.raises(TypeError, match=match):
        await _call_with_raises(client, verb, raises, tmp_path)


@pytest.mark.parametrize(
    ("raises", "match"),
    [
        (GenericError, "leaves HTTPResponseError's error type unbound"),
        (int, "must be HTTPResponseError or a subclass"),
    ],
    ids=["unbound", "not-a-subclass"],
)
@pytest.mark.parametrize(
    "verb", ["get", "put", "head", "download", "stream_get", "stream_post", "sse"]
)
def test_sync_a_bad_per_call_raises_fails_even_on_a_200(
    sync_client: SyncHTTPClient,
    tmp_path: Path,
    verb: SyncVerb,
    raises: Any,  # noqa: ANN401 — deliberately not a valid `raises=`
    match: str,
) -> None:
    with pytest.raises(TypeError, match=match):
        _sync_call_with_raises(sync_client, verb, raises, tmp_path)


async def test_a_per_call_raises_mixing_struct_and_basemodel_raises_type_error(
    client: HTTPClient,
) -> None:
    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic BaseModels"):
        await client.get("missing", raises=MixedError)


def test_sync_a_per_call_raises_mixing_struct_and_basemodel_raises_type_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(TypeError, match="mixes msgspec Structs and pydantic BaseModels"):
        sync_client.get("missing", raises=MixedError)


async def test_except_http_response_error_still_catches_a_raises_subclass(
    api_client: HTTPClient,
) -> None:
    with pytest.raises(HTTPResponseError) as exc_info:
        await api_client.get("missing")

    assert isinstance(exc_info.value, MyAPIError)


async def test_pickling_keeps_the_raises_subclass(api_client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        await api_client.get("missing")

    restored = pickle.loads(pickle.dumps(exc_info.value))  # noqa: S301 — round-tripping our own object

    assert type(restored) is MyAPIError
    assert restored.status == 404
    assert restored.error == NotFound(title="Not found", status=404)
    assert str(restored) == str(exc_info.value)


async def test_stream_get_raises_the_selected_class(api_client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        async for _ in api_client.stream_get("missing"):
            pass

    assert exc_info.value.error == NotFound(title="Not found", status=404)


async def test_stream_post_raises_the_per_call_class(client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        async for _ in client.stream_post("missing", json={"a": 1}, raises=MyAPIError):
            pass

    assert exc_info.value.error == NotFound(title="Not found", status=404)


async def test_sse_raises_the_selected_class(api_client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        async for _ in api_client.sse("unauthorized"):
            pass

    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


async def test_download_raises_the_selected_class(api_client: HTTPClient, tmp_path: Path) -> None:
    with pytest.raises(MyAPIError) as in_memory:
        await api_client.download("missing")
    with pytest.raises(MyAPIError) as to_file:
        await api_client.download("missing", tmp_path / "file")

    assert in_memory.value.error == NotFound(title="Not found", status=404)
    assert to_file.value.error == NotFound(title="Not found", status=404)


async def test_head_raises_the_per_call_class(client: HTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        await client.head("missing", raises=MyAPIError)

    # A HEAD response has no body to decode.
    assert exc_info.value.error is None
    assert exc_info.value.body == b""


def test_sync_stream_get_raises_the_selected_class(sync_api_client: SyncHTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        for _ in sync_api_client.stream_get("missing"):
            pass

    assert exc_info.value.error == NotFound(title="Not found", status=404)


def test_sync_stream_post_raises_the_per_call_class(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(MyAPIError):
        for _ in sync_client.stream_post("missing", json={"a": 1}, raises=MyAPIError):
            pass


def test_sync_sse_raises_the_selected_class(sync_api_client: SyncHTTPClient) -> None:
    with pytest.raises(MyAPIError) as exc_info:
        for _ in sync_api_client.sse("unauthorized"):
            pass

    assert exc_info.value.error == NotAuthorized(title="Not authorized", status=401)


def test_sync_download_raises_the_selected_class(
    sync_api_client: SyncHTTPClient, tmp_path: Path
) -> None:
    with pytest.raises(MyAPIError):
        sync_api_client.download("missing")
    with pytest.raises(MyAPIError) as to_file:
        sync_api_client.download("missing", tmp_path / "file")

    assert to_file.value.error == NotFound(title="Not found", status=404)


def test_sync_head_raises_the_per_call_class(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(MyAPIError):
        sync_client.head("missing", raises=MyAPIError)


async def test_retries_exhausted_raise_the_selected_class(base_url: str, server_key: str) -> None:
    async with HTTPClient(
        base_url=base_url, max_retries=1, backoff_base=0.001, raises=MyAPIError
    ) as client:
        with pytest.raises(MyAPIError) as exc_info:
            await client.get("flaky", params={"key": server_key, "fail_times": 5})

    assert exc_info.value.status == 503
    assert exc_info.value.error == {
        "type": "service-unavailable",
        "title": "Service unavailable",
        "status": 503,
    }


def test_sync_retries_exhausted_raise_the_selected_class(base_url: str, server_key: str) -> None:
    with (
        SyncHTTPClient(
            base_url=base_url, max_retries=1, backoff_base=0.001, raises=MyAPIError
        ) as client,
        pytest.raises(MyAPIError) as exc_info,
    ):
        client.get("flaky", params={"key": server_key, "fail_times": 5})

    assert exc_info.value.status == 503


async def test_except_a_pinned_subclass_types_error_as_the_union(client: HTTPClient) -> None:
    # The static half of the design: `except MyAPIError as e` gives `e.error` the pinned type,
    # checked by all four type checkers (`assert_type` is a no-op at runtime).
    errors: list[object] = []
    try:
        await client.get("missing", raises=MyAPIError)
    except MyAPIError as e:
        assert_type(e.error, NotFound | NotAuthorized | dict[str, Any] | None)
        errors.append(e.error)
    try:
        await client.get("missing", raises=SubAPIError)
    except SubAPIError as e:
        assert_type(e.error, NotFound | NotAuthorized | dict[str, Any] | None)
        errors.append(e.error)

    assert errors == [NotFound(title="Not found", status=404)] * 2
