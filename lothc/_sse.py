"""`SSEEvent`, the server-sent events parser (records, `id:`/`retry:`, reconnect budget), and
`sse()` itself over either transport's stream source."""

import asyncio
import codecs
import time
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AsyncExitStack, aclosing, closing
from dataclasses import dataclass
from functools import partial
from typing import Any

from ._decode import (
    _decode_json_line,
    _Decoder,
    _decoder_for,
    _Into,
    _SSEBytesDecoder,
    _TextDecoder,
)
from ._errors import HTTPResponseError, HTTPTransportError, _validate_raises
from ._streaming import _ResponseHead, _StreamSource, _sync_stream_chunks, _SyncStreamSource


@dataclass(slots=True, kw_only=True)
class SSEEvent[TData]:
    """One Server-Sent Event, as yielded by `sse()`.

    `.event` defaults to `"message"` and `.id` to `""` when the wire omits them, as the SSE spec
    (and a browser's `EventSource`) does. `.data` is decoded per `sse()`'s `into`,
    raw `str` by default.
    """

    id: str = ""
    event: str = "message"
    data: TData


@dataclass(slots=True)
class _SSERecord:
    """One parsed SSE record (the lines between two blank lines), before dispatch.

    `data` is `None` when the record carried no `data:` line at all — such a record dispatches no
    event, but its `id:`/`retry:` fields still take effect (WHATWG "process the field" rules).
    `id` is `None` when there was no `id:` line; an explicit empty `id:` is `""` (it resets the
    stream's last-event-id buffer). `retry_ms` is `None` unless a well-formed `retry:` was seen.
    """

    data: str | None
    event: str
    id: str | None
    retry_ms: int | None


@dataclass(slots=True)
class _SSEStreamState:
    """Per-`sse()`-call state that has to survive across reconnects.

    `retry_delay` starts at the caller's `reconnect_delay` and is overwritten whenever the server
    sends a `retry:` field. `last_event_id` is the spec's last-event-id buffer: it persists
    across events (an event with no `id:` line inherits it) and is sent back as
    `Last-Event-ID` on every reconnect. `status` is the most recent connection's response
    status, so the reconnect loop can tell a 204 ("stop, don't reconnect") from any other
    clean close.
    """

    retry_delay: float
    last_event_id: str = ""
    status: int | None = None


def _parse_sse_record(record: str) -> _SSERecord:
    r"""Parse one blank-line-delimited SSE record per the WHATWG event-stream field rules.

    >>> _parse_sse_record("event: tick\nid: 7\ndata: a\ndata: b")
    _SSERecord(data='a\nb', event='tick', id='7', retry_ms=None)
    >>> _parse_sse_record(": comment only\nretry: 250")
    _SSERecord(data=None, event='message', id=None, retry_ms=250)
    >>> _parse_sse_record("retry: soon\nid: bad\x00null\nid:")
    _SSERecord(data=None, event='message', id='', retry_ms=None)
    >>> _parse_sse_record("data")
    _SSERecord(data='', event='message', id=None, retry_ms=None)
    """
    data_lines: list[str] = []
    event = "message"
    event_id: str | None = None
    retry_ms: int | None = None
    for line in record.split("\n"):
        if not line or line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "data":
            data_lines.append(value)
        elif field == "event":
            # The spec dispatches an empty event type as "message", not as "".
            event = value or "message"
        elif field == "id" and "\x00" not in value:
            event_id = value
        elif field == "retry" and value.isascii() and value.isdigit():
            retry_ms = int(value)
    data = "\n".join(data_lines) if data_lines else None
    return _SSERecord(data=data, event=event, id=event_id, retry_ms=retry_ms)


def _split_sse_records(buffer: bytes) -> tuple[list[bytes], bytes]:
    r"""Split off every complete record in `buffer`, returning them plus the unconsumed tail.

    Normalizes all three line terminators the spec allows (`\r\n`, `\n`, `\r`) to `\n` first,
    so a record boundary is always exactly `\n\n` afterwards. A trailing lone `\r` is held back
    unconsumed rather than normalized — it may be the first half of a `\r\n` whose `\n` hasn't
    arrived yet, and normalizing it now would fabricate a bogus record boundary when it does.

    >>> _split_sse_records(b"data: a\n\ndata: b\r\n\r\ndata: c")
    ([b'data: a', b'data: b'], b'data: c')
    >>> _split_sse_records(b"data: a\r\rdata: b\r")
    ([b'data: a'], b'data: b\r')
    >>> _split_sse_records(b"data: b\r\n\r\n")
    ([b'data: b'], b'')
    >>> _split_sse_records(b"")
    ([], b'')
    """
    pending_cr = b"\r" if buffer.endswith(b"\r") else b""
    normalized = buffer.removesuffix(b"\r").replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    *records, tail = normalized.split(b"\n\n")
    return records, tail + pending_cr


def _may_complete_sse_record(buffer: bytearray, new_from: int) -> bool:
    r"""Whether the bytes appended to `buffer` from `new_from` on can have completed a record.

    A record only ends at a blank line, so if none arrived with the latest chunk there is nothing
    for `_split_sse_records` to find, and calling it anyway re-normalized the whole buffer once
    per chunk: quadratic for one large event (a 4MB event in 16KB chunks took ~470ms). Looks back
    3 bytes, since the longest boundary, `\r\n\r\n`, can straddle the previous chunk by that
    much. A false positive only costs one needless split; a false negative can't happen.

    >>> _may_complete_sse_record(bytearray(b"data: a\n\n"), 0)
    True
    >>> _may_complete_sse_record(bytearray(b"data: a\ndata: b"), 8)
    False
    >>> _may_complete_sse_record(bytearray(b"data: a\r\n\r\n"), 10)
    True
    """
    window = bytes(buffer[max(0, new_from - 3) :])
    return b"\n\n" in window.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _verify_sse_response(state: _SSEStreamState, status: int, content_type: str | None) -> None:
    """Record the connection's status and enforce the spec's "must be text/event-stream" rule.

    Only a 2xx that isn't a 204 is held to that rule: a 204 is the spec's explicit "stop
    reconnecting" signal and has no body to type, and anything else that isn't a 2xx (a 4xx/5xx
    never gets here: the stream source raises it) simply yields no events.
    """
    state.status = status
    if status == 204 or not 200 <= status < 300:
        return
    media_type = (content_type or "").partition(";")[0].strip().lower()
    if media_type != "text/event-stream":
        raise ValueError(
            f"SSE response has Content-Type {content_type!r}, expected 'text/event-stream'"
        )


def _verify_sse_head(state: _SSEStreamState, head: _ResponseHead) -> None:
    """`_verify_sse_response` as the sync chunk iterators' head hook, run on whichever thread
    reads the body (see `_drain_stream_chunks`)."""
    _verify_sse_response(state, head.status, head.headers.get("content-type"))


def _sse_data_decoder(into: _Into) -> _Decoder:
    """The decoder for each SSE event's `data`, resolved once per stream. Never `None`, unlike
    `_decoder_for`'s: `data` arrives as text, so the default (`into=None`) keeps it as text and
    `into=bytes` encodes it back."""
    if into is bytes:
        return _SSEBytesDecoder()
    decoder = _decoder_for(into)
    return _TextDecoder() if decoder is None else decoder


def _dispatch_sse_record(
    record: bytes, state: _SSEStreamState, decoder: _Decoder
) -> SSEEvent[Any] | None:
    """Apply one record's `retry:`/`id:` fields to `state`, then build its event (if it has data).

    `.id` comes from the *persisted* last-event-id buffer, not this record alone — per spec an
    event with no `id:` line carries the most recent id seen on the stream, and the buffer is
    only cleared by an explicit empty `id:` line.
    """
    # UTF-8 with replacement, as the spec's own decode step does, rather than raising.
    parsed_record = _parse_sse_record(record.decode(errors="replace"))
    if parsed_record.retry_ms is not None:
        state.retry_delay = parsed_record.retry_ms / 1000
    if parsed_record.id is not None:
        state.last_event_id = parsed_record.id
    if parsed_record.data is None:
        return None
    data = _decode_json_line(parsed_record.data, decoder)
    return SSEEvent(id=state.last_event_id, event=parsed_record.event, data=data)


def _sse_reconnect_budget_exhausted(
    max_reconnects: int | None, consecutive_reconnects: int
) -> bool:
    return max_reconnects is not None and consecutive_reconnects >= max_reconnects


async def _sse_connection(
    source: _StreamSource, state: _SSEStreamState, decoder: _Decoder
) -> AsyncGenerator[SSEEvent[Any]]:
    """One connection's worth of an SSE stream: yields its events, returns on clean EOF."""
    try:
        async with AsyncExitStack() as stack:
            head, read_chunk = await source.open(stack)
            _verify_sse_response(state, head.status, head.headers.get("content-type"))
            buffer = bytearray()
            strip_bom = True
            while True:
                chunk = await read_chunk()
                if chunk is None:
                    return
                new_from = len(buffer)
                buffer += chunk
                if strip_bom and len(buffer) >= len(codecs.BOM_UTF8):
                    buffer = buffer.removeprefix(codecs.BOM_UTF8)
                    strip_bom = False
                    new_from = 0
                if not _may_complete_sse_record(buffer, new_from):
                    continue
                records, tail = _split_sse_records(bytes(buffer))
                buffer = bytearray(tail)
                for record in records:
                    event = _dispatch_sse_record(record, state, decoder)
                    if event is not None:
                        yield event
    except source.failures as error:
        raise source.failure(error) from error


def _sse_connection_sync(
    source: _SyncStreamSource,
    state: _SSEStreamState,
    decoder: _Decoder,
    *,
    interruptible: bool,
) -> Generator[SSEEvent[Any]]:
    """Sync mirror of `_sse_connection`, reading through `_sync_stream_chunks`."""
    try:
        buffer = bytearray()
        strip_bom = True
        for chunk in _sync_stream_chunks(
            source, partial(_verify_sse_head, state), interruptible=interruptible
        ):
            new_from = len(buffer)
            buffer += chunk
            if strip_bom and len(buffer) >= len(codecs.BOM_UTF8):
                buffer = buffer.removeprefix(codecs.BOM_UTF8)
                strip_bom = False
                new_from = 0
            if not _may_complete_sse_record(buffer, new_from):
                continue
            records, tail = _split_sse_records(bytes(buffer))
            buffer = bytearray(tail)
            for record in records:
                event = _dispatch_sse_record(record, state, decoder)
                if event is not None:
                    yield event
    except source.failures as error:
        raise source.failure(error) from error


async def _sse_events(
    connect: Callable[[str], _StreamSource],
    into: _Into,
    raises: type[HTTPResponseError[Any]] | None,
    *,
    max_reconnects: int | None,
    reconnect_delay: float,
    reconnect_on_close: bool,
) -> AsyncGenerator[SSEEvent[Any]]:
    """`sse()` over either transport: `connect(last_event_id)` gives each connection's source
    (sending `Last-Event-ID` when there is one), reconnected per the WHATWG EventSource model."""
    _validate_raises(raises)
    # Resolved once per stream, not per event or per reconnect.
    decoder = _sse_data_decoder(into)
    state = _SSEStreamState(retry_delay=reconnect_delay)
    consecutive_reconnects = 0
    while True:
        source = connect(state.last_event_id)
        await source.prepare()
        connection = _sse_connection(source, state, decoder)
        try:
            # `aclosing` so an early `break` by the caller closes this connection right
            # here, not whenever the event loop's async-generator finalizer gets around to it.
            async with aclosing(connection) as events:
                async for event in events:
                    consecutive_reconnects = 0
                    yield event
        except HTTPTransportError as error:
            # A permanent failure can't be reconnected away, so it propagates on the first
            # attempt instead of burning the whole budget sleeping between retries that
            # cannot possibly succeed.
            if source.permanent(error) or _sse_reconnect_budget_exhausted(
                max_reconnects, consecutive_reconnects
            ):
                raise
        else:
            # A clean close: a 204 is the spec's explicit "stop, don't reconnect"; any other
            # clean close ends the stream too unless the caller opted into browser-style
            # reconnect-on-close — which still respects the reconnect budget.
            if state.status == 204 or not reconnect_on_close:
                return
            if _sse_reconnect_budget_exhausted(max_reconnects, consecutive_reconnects):
                return
        consecutive_reconnects += 1
        await asyncio.sleep(state.retry_delay)


def _sse_events_sync(
    connect: Callable[[str], _SyncStreamSource],
    into: _Into,
    raises: type[HTTPResponseError[Any]] | None,
    *,
    max_reconnects: int | None,
    reconnect_delay: float,
    reconnect_on_close: bool,
    interruptible: bool,
) -> Generator[SSEEvent[Any]]:
    """Sync mirror of `_sse_events`."""
    _validate_raises(raises)
    # Resolved once per stream, not per event or per reconnect.
    decoder = _sse_data_decoder(into)
    state = _SSEStreamState(retry_delay=reconnect_delay)
    consecutive_reconnects = 0
    while True:
        source = connect(state.last_event_id)
        source.prepare()
        connection = _sse_connection_sync(source, state, decoder, interruptible=interruptible)
        try:
            # `closing` so an early `break` by the caller closes this connection right here,
            # not whenever the generator happens to be garbage-collected.
            with closing(connection) as events:
                for event in events:
                    consecutive_reconnects = 0
                    yield event
        except HTTPTransportError as error:
            # A permanent failure can't be reconnected away, so it propagates on the first
            # attempt instead of burning the whole budget sleeping between retries that
            # cannot possibly succeed.
            if source.permanent(error) or _sse_reconnect_budget_exhausted(
                max_reconnects, consecutive_reconnects
            ):
                raise
        else:
            # See the matching comment in `_sse_events`.
            if state.status == 204 or not reconnect_on_close:
                return
            if _sse_reconnect_budget_exhausted(max_reconnects, consecutive_reconnects):
                return
        consecutive_reconnects += 1
        time.sleep(state.retry_delay)
