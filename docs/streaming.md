---
icon: lucide/waves
---

# Streaming

`stream_get`/`stream_post` stream the response instead of buffering the whole body in memory —
useful for large downloads, or a feed that keeps producing data over one long-lived connection.
Like `sse()`, they're `AsyncIterator`s on `HTTPClient` and plain `Iterator`s on
`SyncHTTPClient`; breaking out of the loop closes the underlying connection.

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

`stream_post` takes the same `json`/`form`/`content` body options as `post` (at most one of
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

!!! warning "Abandoning an interruptible stream before EOF always leaks a thread and a socket"

    There is no cancellation path — dropping or exiting a streamed response does **not** cancel
    an in-flight read on the worker thread, it blocks until that read resolves one way or
    another. So abandoning an interruptible stream before it reaches EOF — an early `break`, or
    the generator getting garbage-collected — always leaves the worker thread and its open
    socket parked until the peer closes the connection or a timeout fires; process exit is what
    actually reclaims it.

    Ctrl-C itself is rarely the exposure here: a long-lived process has no controlling terminal
    to receive it from, and when it does, SIGINT there is usually aimed at killing the whole
    process anyway, which reclaims the leak along with everything else. The real risk is code
    that repeatedly breaks out of an interruptible stream early while the process stays up —
    pair that with a short `timeout`/`connect_timeout` so each leaked connection is bounded,
    rather than assuming avoiding Ctrl-C is the mitigation.
