import time
from concurrent.futures import ThreadPoolExecutor

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

    assert len(events) == 10
    assert all(isinstance(event, SSEEvent) for event in events)
    assert events[0].event == "tick"


async def test_sse_yields_typed_events_via_decoder(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", response_data_type=Decoder(TickEvent))]

    assert events[0].data == TickEvent(msg="hello 0", now=0)
    assert events[-1].data == TickEvent(msg="hello 9", now=900)
    assert events[0].event == "tick"


@pytest.mark.parametrize("interruptible", [False, True])
def test_sync_sse_yields_raw_events(sync_client: SyncHTTPClient, *, interruptible: bool) -> None:
    events = list(sync_client.sse("events", interruptible=interruptible))

    assert len(events) == 10
    assert events[0].event == "tick"


async def test_sse_default_id_type_raises_when_id_missing(client: HTTPClient) -> None:
    with pytest.raises(ValueError, match="missing required 'id'"):
        [event async for event in client.sse("events", params={"omit": "id"})]


async def test_sse_allow_missing_id_allows_missing_id(client: HTTPClient) -> None:
    events = [
        event async for event in client.sse("events", params={"omit": "id"}, allow_missing_id=True)
    ]

    assert events[0].id is None


def test_sync_sse_default_id_type_raises_when_id_missing(sync_client: SyncHTTPClient) -> None:
    with pytest.raises(ValueError, match="missing required 'id'"):
        list(sync_client.sse("events", params={"omit": "id"}))


async def test_sse_id_type_coerces_id(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", id_type=int)]

    assert events[0].id == 0
    assert events[-1].id == 9


def test_sync_sse_id_type_coerces_id(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("events", id_type=int))

    assert events[0].id == 0


async def test_sse_id_type_with_allow_missing_id_coerces_when_present(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events", id_type=int, allow_missing_id=True)]

    assert events[0].id == 0


async def test_sse_id_type_with_allow_missing_id_allows_missing_id(client: HTTPClient) -> None:
    events = [
        event
        async for event in client.sse(
            "events", params={"omit": "id"}, id_type=int, allow_missing_id=True
        )
    ]

    assert events[0].id is None


async def test_sse_skips_comment_only_and_unrecognized_field_records(client: HTTPClient) -> None:
    events = [event async for event in client.sse("events-weird", allow_missing_id=True)]

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


async def test_sse_events_arrive_incrementally_not_buffered_until_stream_end(
    client: HTTPClient,
) -> None:
    """Regression test for pyreqwest's `streamed_read_buffer_limit` default (64KB, see the
    `RequestInfo`-adjacent dev note in CLAUDE.md) — it withholds every received byte until either
    that fills or the stream ends, so a small-record SSE stream would arrive as one
    microseconds-scale burst right at EOF instead of as the server actually writes it (one event
    per `time.sleep(0.001)` in `tests/_server.py`).
    """
    previous = time.perf_counter()
    deltas: list[float] = []
    async for _ in client.sse("events"):
        now = time.perf_counter()
        deltas.append(now - previous)
        previous = now

    assert len(deltas) == 10
    # Buffered-until-EOF collapses nearly every delta to microseconds (all released together at
    # stream end), with the stream's whole real duration dumped into a single outlier instead.
    # Real incremental delivery keeps most per-event gaps close to the server's real
    # `time.sleep(0.001)` pace — checking the majority (not every delta, not just the max) keeps
    # this from flaking on a single slow scheduler wakeup.
    assert sum(delta > 0.0003 for delta in deltas) >= len(deltas) // 2


def test_sync_sse_skips_comment_only_and_unrecognized_field_records(
    sync_client: SyncHTTPClient,
) -> None:
    events = list(sync_client.sse("events-weird", allow_missing_id=True))

    assert len(events) == 1
    assert events[0].data == "hello"


def test_sync_sse_error_for_status_false_suppresses_raise(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("boom", error_for_status=False))

    assert events == []


def test_sync_sse_yields_typed_events_via_struct_class(sync_client: SyncHTTPClient) -> None:
    events = list(sync_client.sse("events", response_data_type=TickEvent))

    assert events[0].data == TickEvent(msg="hello 0", now=0)


@pytest.mark.parametrize("interruptible", [False, True])
def test_sync_sse_transport_error_mid_stream_raises_connection_error(
    sync_client: SyncHTTPClient, *, interruptible: bool
) -> None:
    with pytest.raises(HTTPConnectionError):
        list(sync_client.sse("truncated", interruptible=interruptible))


def test_sync_sse_interruptible_error_for_status_false_suppresses_raise(
    sync_client: SyncHTTPClient,
) -> None:
    events = list(sync_client.sse("boom", error_for_status=False, interruptible=True))

    assert events == []


def _consume_one_event_then_break(sync_client: SyncHTTPClient) -> None:
    for _ in sync_client.sse("events", interruptible=True):
        break


def test_sync_sse_interruptible_early_break_does_not_hang(sync_client: SyncHTTPClient) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_consume_one_event_then_break, sync_client).result(timeout=5)


def test_sync_sse_events_arrive_incrementally_not_buffered_until_stream_end(
    sync_client: SyncHTTPClient,
) -> None:
    """Sync mirror of `test_sse_events_arrive_incrementally_not_buffered_until_stream_end`."""
    previous = time.perf_counter()
    deltas: list[float] = []
    for _ in sync_client.sse("events"):
        now = time.perf_counter()
        deltas.append(now - previous)
        previous = now

    assert len(deltas) == 10
    assert sum(delta > 0.0003 for delta in deltas) >= len(deltas) // 2
