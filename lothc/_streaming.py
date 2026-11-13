"""Streaming shared by both transports: the stream-source seam a transport implements, idle
limits, line splitting, atomic downloads, the sync chunk worker, and `stream_get`/`stream_post`/
`download` themselves (`sse()` is in `_sse.py`)."""

import asyncio
import queue
import secrets
import threading
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Buffer,
    Callable,
    Generator,
    Iterator,
)
from contextlib import (
    AsyncExitStack,
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import KW_ONLY, dataclass
from io import BufferedWriter
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Literal,
    Protocol,
)

from ._decode import _decode_json_line, _decoder_for, _Into, _TextDecoder
from ._errors import HTTPResponseError, HTTPTimeoutError, HTTPTransportError, _validate_raises
from ._headers import CaseInsensitiveDict
from ._response import RequestInfo, Response
from ._types import Form, Headers, JSONPayload, Params


@contextmanager
def _atomic_download_file(dest: Path) -> Generator[BufferedWriter]:
    """A file to stream a download into that only becomes `dest` once the body is complete.

    Writes go to a hidden sibling of `dest` (same directory, so the final rename is atomic),
    which replaces `dest` on success and is deleted on any failure, including cancellation. A
    download that dies halfway therefore never leaves a truncated file where a caller would take
    it for the real thing. Opened with `"xb"` rather than via `tempfile.mkstemp`, which would
    create it `0600` and so silently change the finished file's permissions from the umask
    default the old direct `dest.open("wb")` gave it.

    Writes stay on the calling thread, even for the async client: each chunk write is
    microseconds (measured ~10us for 64KB), and moving them onto a thread made a 200MB download's
    writes 2.5-3.8x slower for no real gain in event-loop responsiveness.
    """
    part = dest.with_name(f".{dest.name}.{secrets.token_hex(8)}.part")
    try:
        with part.open("xb") as file:
            yield file
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _take_completed_lines(pending: bytearray, chunk: Buffer) -> list[bytearray]:
    """Append `chunk` to `pending` and return every line it completes, leaving only the
    still-unterminated tail in `pending`.

    Linear in the bytes received. The old `line, buffer = buffer.split(b"\\n", 1)` loop copied the
    whole remainder once per line (a 4MB chunk took ~4.7s; this takes ~15ms), and only the newly
    appended bytes are searched for a newline, since everything before the last one has already
    been consumed; rescanning all of `pending` made one long line quadratic instead.
    """
    start = len(pending)
    pending += chunk
    end = pending.rfind(b"\n", start)
    if end == -1:
        return []
    lines = pending[:end].split(b"\n")
    del pending[: end + 1]
    return lines


def _complete_ndjson_lines(pending: bytearray, chunk: Buffer) -> list[bytearray]:
    """The non-blank NDJSON lines `chunk` completes (see `_take_completed_lines`). A line that is
    only whitespace is skipped rather than decoded, which covers the `\\r` a CRLF-terminated
    stream leaves on its blank lines; a JSON decoder tolerates the `\\r` ending any other line."""
    return [line for line in _take_completed_lines(pending, chunk) if line.strip()]


def _strip_carriage_return(line: bytearray) -> bytearray:
    return line[:-1] if line.endswith(b"\r") else line


def _complete_text_lines(pending: bytearray, chunk: Buffer) -> list[bytearray]:
    """The raw text lines `chunk` completes, for `into=str` (see `_take_completed_lines`): every
    one, blank lines included, with one trailing `\\r` (from a CRLF terminator) stripped."""
    return [_strip_carriage_return(line) for line in _take_completed_lines(pending, chunk)]


def _final_line(pending: bytearray, *, raw_text: bool) -> bytearray | None:
    """The unterminated tail left in `pending` when a line stream ends, if it holds a line: any
    bytes at all for `into=str` (CR-stripped like the others), else only a non-blank one."""
    if raw_text:
        return _strip_carriage_return(pending) if pending else None
    return pending if pending.strip() else None


def _idle_timeout_error(idle: float) -> HTTPTimeoutError:
    return HTTPTimeoutError(
        f"No data received from the server for {idle}s. For a streaming verb the client's "
        "`timeout` is the longest allowed gap between chunks, not a cap on the whole transfer; "
        "pass a per-call `timeout=` for a total cap, or set `read_timeout` to change the gap."
    )


@asynccontextmanager
async def _within_idle_limit(idle: float | None) -> AsyncGenerator[None]:
    """Fail with `HTTPTimeoutError` if the enclosed wait takes longer than `idle` (`None`: never).

    Only ever wraps a single wait on the server (opening the stream, a status check, one chunk
    read), never a `yield` to the caller, so time a slow consumer spends between chunks is never
    mistaken for a stalled server.
    """
    if idle is None:
        yield
        return
    try:
        async with asyncio.timeout(idle):
            yield
    except TimeoutError as error:
        raise _idle_timeout_error(idle) from error


@dataclass(slots=True)
class _ResponseHead:
    """A streamed response's status line and headers, read before its body is."""

    status: int
    headers: CaseInsensitiveDict
    http_version: str


# A streamed body's reader, from `_StreamSource.open`: the next chunk, `None` at the end.
type _ChunkReader = Callable[[], Awaitable[Buffer | None]]
type _SyncChunkReader = Callable[[], Buffer | None]


@dataclass(slots=True)
class _StreamCall:
    """A streaming verb's request, as the client passed it, for a transport's stream source to
    build. `timeout` is the total cap the transport chose for it (`None`: none)."""

    params: Params | None
    headers: Headers | None
    timeout: float | None
    json: JSONPayload | None = None
    data: Params | None = None
    form: Form | None = None
    content: str | bytes | None = None
    _: KW_ONLY
    skip_auth: bool
    infer_mime_type_from_file_extension: bool = True
    error_for_status: bool
    raises: type[HTTPResponseError[Any]] | None


class _StreamSource(Protocol):
    """One streamed exchange as a transport makes it: the seam between a transport and the
    streaming verbs' one shared implementation (`_stream_lines`, `_download_stream`,
    `_sse_events`), which does everything else, both transports alike.

    `prepare` does what comes before the idle clock starts (auth, building the request). `open`
    sends it, waits for the head and checks its status (an error status's body is read and its
    status error raised), registering on `stack` whatever ends the exchange; the reader it returns
    gives the body chunk by chunk as it arrives, `None` at its end. A transport's own exceptions,
    `failures`, become lothc's through `failure`; `permanent` says whether `sse()` may reconnect
    after a transport error. `request_info` is valid once `open` has started.
    """

    failures: ClassVar[tuple[type[Exception], ...]]

    @property
    def request_info(self) -> RequestInfo: ...
    def failure(self, error: Exception, /) -> Exception: ...
    def permanent(self, error: HTTPTransportError, /) -> bool: ...
    async def prepare(self) -> None: ...
    async def open(self, stack: AsyncExitStack) -> tuple[_ResponseHead, _ChunkReader]: ...


class _SyncStreamSource(Protocol):
    """`_StreamSource` for the sync client. `open` and the reader run on whichever thread reads
    the body (the chunk worker's, see `_drain_stream_chunks`), so `stack` closes it there too."""

    failures: ClassVar[tuple[type[Exception], ...]]

    @property
    def request_info(self) -> RequestInfo: ...
    def failure(self, error: Exception, /) -> Exception: ...
    def permanent(self, error: HTTPTransportError, /) -> bool: ...
    def prepare(self) -> None: ...
    def open(self, stack: ExitStack) -> tuple[_ResponseHead, _SyncChunkReader]: ...


async def _read_chunk_within(read_chunk: _ChunkReader, idle: float | None) -> Buffer | None:
    """The next body chunk, or `HTTPTimeoutError` if none arrives within `idle` seconds.

    A plain coroutine rather than `_within_idle_limit`, since this runs once per chunk and a
    generator-based context manager costs noticeably more on a many-chunk download.
    """
    if idle is None:
        return await read_chunk()
    try:
        async with asyncio.timeout(idle):
            return await read_chunk()
    except TimeoutError as error:
        raise _idle_timeout_error(idle) from error


# Discriminated union of what a worker thread hands back to the polling consumer thread via
# the Queue in `_interruptible_chunk_iter` — a decoded chunk, EOF, or a forwarded exception.
type _StreamQueueItem = (
    tuple[Literal["chunk"], bytes]
    | tuple[Literal["eof"], None]
    | tuple[Literal["error"], Exception]
)


def _offer_stream_item(
    item_queue: queue.Queue[_StreamQueueItem], item: _StreamQueueItem, abandoned: threading.Event
) -> bool:
    """Hand `item` to the consumer, waiting for room, unless the consumer has gone.

    The queue is bounded so a consumer slower than the network gets backpressure instead of the
    worker buffering the whole body in memory, which an unbounded queue did (a slow NDJSON
    consumer would eventually hold the entire stream). Waiting in short slices, rather than one
    blocking `put`, is what lets a worker whose consumer stopped early notice and exit, which
    releases the connection; a plain `put` into a full queue would block forever instead.
    Returns whether the item was delivered.
    """
    while not abandoned.is_set():
        try:
            item_queue.put(item, timeout=0.2)
        except queue.Full:
            continue
        return True
    return False


type _HeadCheck = Callable[[_ResponseHead], None]


def _drain_stream_chunks(
    source: _SyncStreamSource,
    on_head: _HeadCheck | None,
    item_queue: queue.Queue[_StreamQueueItem],
    abandoned: threading.Event,
    head_sink: list[_ResponseHead] | None,
) -> None:
    """Worker-thread target for `_interruptible_chunk_iter` — owns the entire exchange itself
    (opening it, the status check, `on_head`, the read loop, and its own eventual close), never
    the calling thread. Confirmed via spike: closing a pyreqwest streamed response does NOT
    cancel an in-flight `read_chunk()` on another thread — it blocks the calling thread until
    that read resolves. So the thread that does the blocking reads must also be the one that
    opens and closes the exchange, sequentially, or cleanup itself becomes the new hang.

    `head_sink` gets the response's status line and headers before any chunk is queued, so the
    queue hand-off orders it ahead of the consumer's first `get()`.
    """
    try:
        with ExitStack() as stack:
            head, read_chunk = source.open(stack)
            if on_head is not None:
                on_head(head)
            if head_sink is not None:
                head_sink.append(head)
            while True:
                chunk = read_chunk()
                if chunk is None:
                    _offer_stream_item(item_queue, ("eof", None), abandoned)
                    return
                if not _offer_stream_item(item_queue, ("chunk", bytes(chunk)), abandoned):
                    return
    except Exception as error:  # noqa: BLE001 — forwarded to the consumer thread below, not swallowed — pylint: disable=broad-except
        _offer_stream_item(item_queue, ("error", error), abandoned)


def _interruptible_chunk_iter(
    source: _SyncStreamSource,
    on_head: _HeadCheck | None,
    *,
    idle_timeout: float | None = None,
    head_sink: list[_ResponseHead] | None = None,
    poll_timeout: float = 0.2,
    max_buffered_chunks: int = 64,
) -> Iterator[bytes]:
    """Yield a streamed response's chunks with Ctrl-C-interruptible waits between them.

    Also the only way the sync client can bound how long it waits on the server: a transport's
    blocking read takes no timeout, but these timed `Queue.get()`s can give up after
    `idle_timeout` seconds with no chunk (covering the wait for headers too, since the worker
    opens the response). Time the caller spends between chunks isn't counted: the clock
    restarts when the caller asks for the next one.

    That read (pyreqwest's `read_chunk()`, the native pool's `poll(2)`) blocks the calling thread
    with no way to interrupt it from Python — CPython only converts SIGINT into
    `KeyboardInterrupt` on the main thread while it's executing Python bytecode. Running the
    read loop on a daemon worker thread (via `_drain_stream_chunks`) and having the main thread
    only ever do timed `Queue.get()` calls keeps the main thread in interruptible Python bytecode
    the whole time, so Ctrl-C fires within roughly `poll_timeout`.

    Abandoning iteration (an early `break`, or the generator being garbage-collected) leaves
    the worker thread parked in its read — there is no cancellation path, see
    `_drain_stream_chunks`'s docstring. This is a deliberate, documented leak: one thread and
    one open socket per abandonment, reclaimed only when the peer closes the connection, a
    timeout fires, or the process exits.
    """
    item_queue: queue.Queue[_StreamQueueItem] = queue.Queue(maxsize=max_buffered_chunks)
    abandoned = threading.Event()
    worker = threading.Thread(
        target=_drain_stream_chunks,
        args=(source, on_head, item_queue, abandoned, head_sink),
        daemon=True,
    )
    worker.start()
    # A quarter of the idle limit (never more than `poll_timeout`, which Ctrl-C needs): the
    # limit is only checked when a wait times out, so a coarse poll would let a stall run up
    # to one extra poll past it, and would make one poll the whole window, so a clock left
    # running across the caller's own pause between chunks could never be told apart.
    wait = poll_timeout if idle_timeout is None else min(poll_timeout, idle_timeout / 4)
    waiting_since = time.monotonic()
    try:
        while True:
            try:
                item = item_queue.get(timeout=wait)
            except queue.Empty:
                if idle_timeout is not None and time.monotonic() - waiting_since > idle_timeout:
                    raise _idle_timeout_error(idle_timeout) from None
                continue
            match item:
                case ("chunk", bytes_chunk):
                    yield bytes_chunk
                    waiting_since = time.monotonic()
                case ("eof", None):
                    return
                case ("error", error):
                    raise error.with_traceback(error.__traceback__)
    finally:
        # However iteration ends (exhausted, an error, an early `break`, garbage collection),
        # tell the worker to stop rather than leave it waiting to hand over more chunks.
        abandoned.set()


def _sync_stream_chunks(
    source: _SyncStreamSource,
    on_head: _HeadCheck | None,
    *,
    interruptible: bool,
    idle_timeout: float | None = None,
    head_sink: list[_ResponseHead] | None = None,
) -> Iterator[bytes]:
    """Yield a streamed response's chunks, on a worker thread when a wait must be bounded.

    `on_head` runs on the response's head, where it's read (`sse()` checks its content type).
    Pass `head_sink` to have the head appended to it once the response opens, since the response
    itself never leaves this generator.
    """
    # An idle limit needs the worker-thread path too: it's the only one that can stop waiting.
    if interruptible or idle_timeout is not None:
        yield from _interruptible_chunk_iter(
            source, on_head, idle_timeout=idle_timeout, head_sink=head_sink
        )
        return
    with ExitStack() as stack:
        head, read_chunk = source.open(stack)
        if on_head is not None:
            on_head(head)
        if head_sink is not None:
            head_sink.append(head)
        while True:
            chunk = read_chunk()
            if chunk is None:
                return
            yield bytes(chunk)


async def _stream_lines(
    source: _StreamSource,
    into: _Into,
    raises: type[HTTPResponseError[Any]] | None,
    idle: float | None,
) -> AsyncGenerator[Any]:
    """`stream_get`/`stream_post` over either transport: raw chunks, or one decoded value per
    line with `into`. `idle` is the longest gap allowed between chunks (`None`: no limit)."""
    _validate_raises(raises)
    # Resolved before connecting, so an unusable `into` fails without a request.
    decoder = _decoder_for(into)
    await source.prepare()
    try:
        async with AsyncExitStack() as stack:
            async with _within_idle_limit(idle):
                _, read_chunk = await source.open(stack)
            if decoder is None:
                while True:
                    chunk = await _read_chunk_within(read_chunk, idle)
                    if chunk is None:
                        return
                    yield bytes(chunk)
            raw_text = isinstance(decoder, _TextDecoder)
            complete_lines = _complete_text_lines if raw_text else _complete_ndjson_lines
            pending = bytearray()
            while True:
                chunk = await _read_chunk_within(read_chunk, idle)
                if chunk is None:
                    break
                for line in complete_lines(pending, chunk):
                    yield _decode_json_line(line, decoder)
            final_line = _final_line(pending, raw_text=raw_text)
            if final_line is not None:
                yield _decode_json_line(final_line, decoder)
    except source.failures as error:
        raise source.failure(error) from error


def _stream_lines_sync(
    source: _SyncStreamSource,
    into: _Into,
    raises: type[HTTPResponseError[Any]] | None,
    idle: float | None,
    *,
    interruptible: bool,
) -> Generator[Any]:
    """Sync mirror of `_stream_lines`, reading through `_sync_stream_chunks`."""
    _validate_raises(raises)
    # Resolved before connecting, so an unusable `into` fails without a request.
    decoder = _decoder_for(into)
    source.prepare()
    try:
        chunks = _sync_stream_chunks(source, None, interruptible=interruptible, idle_timeout=idle)
        if decoder is None:
            yield from chunks
            return
        raw_text = isinstance(decoder, _TextDecoder)
        complete_lines = _complete_text_lines if raw_text else _complete_ndjson_lines
        pending = bytearray()
        for chunk in chunks:
            for line in complete_lines(pending, chunk):
                yield _decode_json_line(line, decoder)
        final_line = _final_line(pending, raw_text=raw_text)
        if final_line is not None:
            yield _decode_json_line(final_line, decoder)
    except source.failures as error:
        raise source.failure(error) from error


async def _download_stream(
    source: _StreamSource, dest: Path | None, idle: float | None
) -> Response[bytes] | Response[Path]:
    """`download()` over either transport: the body into memory, or into `dest` atomically."""
    await source.prepare()
    buffer = bytearray()
    started = time.monotonic()
    try:
        async with AsyncExitStack() as stack:
            async with _within_idle_limit(idle):
                head, read_chunk = await source.open(stack)
            if dest is None:
                while True:
                    chunk = await _read_chunk_within(read_chunk, idle)
                    if chunk is None:
                        break
                    buffer += chunk
            else:
                with _atomic_download_file(dest) as file:
                    while True:
                        chunk = await _read_chunk_within(read_chunk, idle)
                        if chunk is None:
                            break
                        file.write(chunk)
    except source.failures as error:
        raise source.failure(error) from error
    elapsed = time.monotonic() - started
    request_info = source.request_info
    if dest is None:
        data = bytes(buffer)
        return Response(
            data, head.status, head.headers, None, request_info, head.http_version, elapsed
        )
    return Response(dest, head.status, head.headers, None, request_info, head.http_version, elapsed)


def _download_stream_sync(
    source: _SyncStreamSource, dest: Path | None, idle: float | None
) -> Response[bytes] | Response[Path]:
    """Sync mirror of `_download_stream`, reading through `_sync_stream_chunks`."""
    source.prepare()
    started = time.monotonic()
    # The response itself stays inside `_sync_stream_chunks` (on a worker thread, when an idle
    # limit applies), so its status line and headers come back through this instead.
    head_sink: list[_ResponseHead] = []
    buffer = bytearray()
    try:
        chunks = _sync_stream_chunks(
            source, None, interruptible=False, idle_timeout=idle, head_sink=head_sink
        )
        if dest is None:
            for chunk in chunks:
                buffer += chunk
        else:
            with _atomic_download_file(dest) as file:
                for chunk in chunks:
                    file.write(chunk)
    except source.failures as error:
        raise source.failure(error) from error
    elapsed = time.monotonic() - started
    head = head_sink[0]
    request_info = source.request_info
    if dest is None:
        data = bytes(buffer)
        return Response(
            data, head.status, head.headers, None, request_info, head.http_version, elapsed
        )
    return Response(dest, head.status, head.headers, None, request_info, head.http_version, elapsed)
