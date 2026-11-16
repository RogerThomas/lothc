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

## NDJSON (`into`)

Pass `into` to switch to newline-buffered decoding instead: chunks are
buffered internally and split on `\n`, and each complete line is parsed and decoded as it
arrives. Unlike on other verbs, `into` here decodes each line, not the whole body:

```python
async for item in client.stream_get("stream/items", into=ItemModel):
    print(item)  # ItemModel(...), one per NDJSON line
```

Same decode targets as everywhere else — any type expression: a pydantic `BaseModel`, a msgspec
`Struct`, plain `dict`, `list[ItemModel]` for lines that are arrays, or a union such as
`ItemCreated | ItemDeleted` for a discriminated union (see [SSE](sse.md) for the equivalent
pattern). A pre-built pydantic `TypeAdapter`/msgspec `Decoder` works too. A trailing line with no
final `\n` is still decoded once the connection closes. Blank lines are skipped, as NDJSON allows.

`into=str` is the exception: it means raw text lines, not NDJSON, so every line is yielded,
blank ones as `""`, with one trailing `\r` stripped from a CRLF line (the final unterminated
line too).

## Streaming a request with a body — `stream_post`

`stream_post` takes the same `json`/`data`/`form`/`content` body options as `post` (at most one of
them), so you can stream the *response* to a request that itself has a body — e.g. streaming
back the results of a search:

```python
async for item in client.stream_post("stream/search", json={"q": "pikachu"}, into=ItemModel):
    print(item)
```

## Timeouts

The client's `timeout` is the longest wait between chunks here, not a cap on the whole transfer;
see [Timeouts](networking.md#timeouts).

```python
async with HTTPClient(base_url="https://api.example.com/", timeout=30) as client:
    await client.download("exports/huge.csv", dest="huge.csv")  # fine at 2 hours, if data flows
```

## Errors

A 4xx/5xx response always raises `HTTPResponseError`, before the first chunk is yielded rather
than partway through the stream; `raise_for_status=False` doesn't apply here. See
[Error handling](errors.md).

## Ctrl-C-interruptible streaming (sync only) — `interruptible`

Ctrl-C can't interrupt a sync wait for the next chunk. Pass `interruptible=True` to fix this: the read loop runs on a daemon worker thread instead,
and the calling thread only ever does short, signal-interruptible waits on a queue, so Ctrl-C
fires within roughly 0.2 seconds instead of never:

```python
for chunk in sync_client.stream_get("download/large-file", interruptible=True):
    handle_chunk(chunk)  # Ctrl-C now works while waiting for the next chunk
```

!!! warning "A sync stream stuck in a stalled read leaves a thread and a socket parked"

    Sync streams with a time limit (every `stream_get`/`stream_post`/`download` by default, see
    [Timeouts](networking.md#timeouts)) or `interruptible=True` read on a worker thread, and
    there is no cancellation path for a read that's already in flight: dropping or exiting a
    streamed response does **not** cancel it, it blocks until that read resolves one way or
    another.

    While data is still arriving that's harmless: stop early (a `break`, or the generator being
    garbage-collected) and the worker notices at its next chunk, stops, and releases the
    connection. It hands chunks over a bounded queue, so it never reads far ahead of a slow
    consumer either. But a worker parked in a read that never returns (the server has stalled,
    whether a timeout then fired or you stopped iterating) keeps its thread and open socket until
    the peer closes the connection; process exit is what actually reclaims it.
