import pytest
from msgspec import Struct
from msgspec.json import Decoder
from pydantic import BaseModel, TypeAdapter

from lothc import HTTPClient, HTTPConnectionError, SSEEvent, SyncHTTPClient


class TickEvent(Struct):
    msg: str
    now: int


async def test_sse_yields_raw_events(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events")]

    assert len(events) == 25
    assert all(isinstance(event, SSEEvent) for event in events)
    assert events[0].event == "tick"


async def test_sse_yields_typed_events_via_decoder(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", response_data_type=Decoder(TickEvent))]

    assert events[0].data == TickEvent(msg="hello 0", now=0)
    assert events[-1].data == TickEvent(msg="hello 24", now=2400)
    assert events[0].event == "tick"


def test_sync_sse_yields_raw_events(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("events"))

    assert len(events) == 25
    assert events[0].event == "tick"


async def test_sse_default_id_type_raises_when_id_missing(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="missing required 'id'"):
        [event async for event in client.sse("events", params={"omit": "id"})]


async def test_sse_id_type_none_allows_missing_id(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", params={"omit": "id"}, id_type=None)]

    assert events[0].id is None


def test_sync_sse_default_id_type_raises_when_id_missing(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(ValueError, match="missing required 'id'"):
        list(sync_client.sse("events", params={"omit": "id"}))


async def test_sse_id_type_coerces_id(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", id_type=int)]

    assert events[0].id == 0
    assert events[-1].id == 24


def test_sync_sse_id_type_coerces_id(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("events", id_type=int))

    assert events[0].id == 0


async def test_sse_id_type_optional_union_coerces_when_present(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", id_type=int | None)]

    assert events[0].id == 0


async def test_sse_id_type_optional_union_allows_missing_id(client: HTTPClient) -> None:
    events = [
        event async for event in client.sse("events", params={"omit": "id"}, id_type=int | None)
    ]

    assert events[0].id is None


async def test_sse_skips_comment_only_and_unrecognized_field_records(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events-weird", id_type=None)]

    assert len(events) == 1
    assert events[0].data == "hello"
    assert events[0].event == "message"


async def test_sse_error_for_status_false_suppresses_raise(client: HTTPClient) -> None:
    events = [event async for event in client.sse("boom", error_for_status=False)]

    assert events == []


async def test_sse_yields_typed_events_via_struct_class(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", response_data_type=TickEvent)]

    assert events[0].data == TickEvent(msg="hello 0", now=0)


class TickModel(BaseModel):
    msg: str
    now: int


async def test_sse_yields_typed_events_via_pydantic_model(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", response_data_type=TickModel)]

    assert events[0].data == TickModel(msg="hello 0", now=0)


async def test_sse_yields_typed_events_via_pydantic_type_adapter(client: HTTPClient) -> None:
    events = [
        event async for event in client.sse("events", response_data_type=TypeAdapter(TickModel))
    ]

    assert events[0].data == TickModel(msg="hello 0", now=0)


async def test_sse_transport_error_mid_stream_raises_connection_error(client: HTTPClient) -> None:
    with pytest.raises(HTTPConnectionError):
        [event async for event in client.sse("truncated")]


def test_sync_sse_skips_comment_only_and_unrecognized_field_records(
    sync_client: SyncHTTPClient,
) -> None:
    events = list(sync_client.sse("events-weird", id_type=None))

    assert len(events) == 1
    assert events[0].data == "hello"


def test_sync_sse_error_for_status_false_suppresses_raise(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("boom", error_for_status=False))

    assert events == []


def test_sync_sse_yields_typed_events_via_struct_class(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("events", response_data_type=TickEvent))

    assert events[0].data == TickEvent(msg="hello 0", now=0)


def test_sync_sse_transport_error_mid_stream_raises_connection_error(
    sync_client: SyncHTTPClient,
) -> None:
    with pytest.raises(HTTPConnectionError):
        list(sync_client.sse("truncated"))
