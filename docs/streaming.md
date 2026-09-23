---
icon: lucide/waves
---

# Streaming

`stream_get`/`stream_post` stream the response instead of buffering the whole body in memory —
useful for large downloads, or a feed that keeps producing data over one long-lived connection.
Like `sse()`, they're async generators on `HTTPClient` and plain generators on
`SyncHTTPClient`. If you stop reading early, close the stream to release its connection straight
away, rather than whenever the generator is garbage-collected:

```python
from contextlib import aclosing

async with aclosing(client.stream_get("feed")) as chunks:
    async for chunk in chunks:
        if done(chunk):
            break  # the connection closes here
```

On `SyncHTTPClient`, use `contextlib.closing` or call `chunks.close()`.

## Raw chunks (default)

Without a decode target, chunks are yielded exactly as received off the wire — no buffering, no
newline splitting:

```python
async for chunk in client.stream_get("download/large-file"):
    handle_chunk(chunk)  # bytes
```

This is the safe default for arbitrary binary content, including bytes that happen to contain a
literal `\n` — nothing here ever inspects or splits the chunk boundaries.

## NDJSON (`response_data_type`)

Pass `response_data_type` to switch to newline-buffered decoding instead: chunks are
buffered internally and split on `\n`, and each complete line is parsed and decoded as it
arrives:

!!! warning

    Same parameter name as every other verb, but a different meaning here: on `get`/`post`/etc.
    it decodes the *whole response body* as one value; on `stream_get`/`stream_post` it decodes
    *each line* of an NDJSON stream as a separate value. The name was standardized for
    consistency across the API — keep this per-line-vs-whole-body distinction in mind when
    reading a call site.

```python
async for item in client.stream_get("stream/items", response_data_type=ItemModel):
    print(item)  # ItemModel(...), one per NDJSON line
```

Same decode targets as everywhere else — a pydantic `BaseModel`, a msgspec `Struct`,
plain `dict`, or a pydantic `TypeAdapter`/msgspec `Decoder` for a discriminated union (see
[SSE](sse.md) for the equivalent pattern). A trailing line with no final `\n` is still decoded
once the connection closes.

!!! warning

    The buffering is strictly conditional on `response_data_type` being passed — without
    it, chunks are never split on `\n`. Passing binary data through the raw path is always safe;
    it's only the NDJSON path that assumes line-delimited text.

## Streaming a request with a body — `stream_post`

`stream_post` takes the same `json`/`data`/`form`/`content` body options as `post` (at most one of
them), so you can stream the *response* to a request that itself has a body — e.g. streaming
back the results of a search:

```python
async for item in client.stream_post(
    "stream/search", json={"q": "pikachu"}, response_data_type=ItemModel
):
    print(item)
```

The raw-chunks default applies here too — omit `response_data_type` to get unbuffered
`bytes` back from a `stream_post` call.

## Timeouts

For `stream_get`, `stream_post` and `download`, the client's `timeout` is the longest the client
will wait **between chunks**, not a cap on the whole transfer. A multi-hour download or a feed
that keeps producing data works with the default 30s, and a server that goes silent for 30s (or
never sends response headers at all) fails with `HTTPTimeoutError`. Time your own code spends
between chunks isn't counted: the clock only runs while waiting on the server.

```python
async with HTTPClient(base_url="https://api.example.com/", timeout=30) as client:
    await client.download("exports/huge.csv", dest="huge.csv")  # fine at 2 hours, if data flows
```

- A per-call `timeout=` puts back a hard cap on that one call's whole transfer.
- Setting `read_timeout` on the client replaces this gap limit with pyreqwest's own.
- `sse()` has no gap limit by default, since quiet periods are normal on an event stream; set
  `read_timeout` if you want one there.

On the sync client, waiting with a limit means reading on a worker thread, since a blocking read
can't otherwise be given up on. That costs no measurable throughput. The one cost is on a genuine
stall: after the timeout fires, the stuck read's thread and socket stay parked until the server
closes the connection or the process exits (see the warning below).

## Errors

`error_for_status` (default `True`) is checked once, before the first chunk is yielded — a
4xx/5xx response raises `HTTPResponseError` immediately rather than partway through the stream. See
[Error handling](errors.md).

## Ctrl-C-interruptible streaming (sync only) — `interruptible`

On `SyncHTTPClient` only, a blocking wait for the next chunk is dead to Ctrl-C: CPython only
converts `SIGINT` into `KeyboardInterrupt` on the main thread while it's executing Python
bytecode, and the wait between chunks happens inside a blocking Rust call with no bytecode
running at all. The async client doesn't have this problem — the event loop's selector is
already signal-interruptible — so `interruptible` only exists on `stream_get`/`stream_post` on
`SyncHTTPClient` (and on `sse()`, see [SSE](sse.md#ctrl-c-interruptible-streaming-sync-only-interruptible)).

Pass `interruptible=True` to fix this: the read loop runs on a daemon worker thread instead,
and the calling thread only ever does short, signal-interruptible waits on a queue, so Ctrl-C
fires within roughly 0.2 seconds instead of never:

```python
for chunk in sync_client.stream_get("download/large-file", interruptible=True):
    handle_chunk(chunk)  # Ctrl-C now works while waiting for the next chunk
```

!!! warning "A sync stream stuck in a stalled read leaves a thread and a socket parked"

    Sync streams with a time limit (every `stream_get`/`stream_post`/`download` by default, see
    Timeouts above) or `interruptible=True` read on a worker thread, and there is no cancellation
    path for a read that's already in flight: dropping or exiting a streamed response does
    **not** cancel it, it blocks until that read resolves one way or another.

    While data is still arriving that's harmless: stop early (a `break`, or the generator being
    garbage-collected) and the worker notices at its next chunk, stops, and releases the
    connection. It hands chunks over a bounded queue, so it never reads far ahead of a slow
    consumer either. But a worker parked in a read that never returns (the server has stalled,
    whether a timeout then fired or you stopped iterating) keeps its thread and open socket until
    the peer closes the connection; process exit is what actually reclaims it.

    Ctrl-C itself is rarely the exposure here: a long-lived process has no controlling terminal
    to receive it from, and when it does, SIGINT there is usually aimed at killing the whole
    process anyway, which reclaims the leak along with everything else. The real risk is code
    that repeatedly abandons streams against a server that stalls while the process stays up.
