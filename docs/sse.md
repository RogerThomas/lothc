---
icon: lucide/radio
---

# Server-Sent Events (SSE)

`sse()` opens a `GET` request with `Accept: text/event-stream` and always yields an `SSEEvent`,
as the server sends them. It's an async generator on `HTTPClient` and a plain generator on
`SyncHTTPClient`; to release the connection as soon as you stop reading, close it (`aclosing`/
`closing`, or `.aclose()`/`.close()`), as [streaming](streaming.md) shows.

```python
@dataclass(slots=True, kw_only=True)
class SSEEvent[TData]:
    id: str = ""
    event: str = "message"
    data: TData
```

`SSEEvent` is generic in `TData` — `into` controls what `.data` becomes (raw
`str`, or decoded into a typed object). `.event` and `.id` are always plain `str`, never `None`,
with the same defaults a browser's `EventSource` uses: an event with no `event:` field is type
`"message"`, and a stream that has sent no `id:` gives `""`.

## Raw events (default)

If you don't pass `into`, `.data` is exactly the text the server sent in the
event's `data:` line(s) (multiple `data:` lines get joined with `\n`) — untouched, no JSON
parsing, no model construction, just a plain `str`:

```python
async for event in client.sse("events"):
    print(event.data, event.event, event.id)  # SSEEvent[str]
```

## The `id` field

`.id` is the text of the server's `id:` field, or `""` when there isn't one (the spec makes it
optional, and most real servers, OpenAI/Anthropic-style streams included, never send it). To use
it as a number or a UUID, convert it yourself: `int(event.id)`.

It follows the spec's *last event ID buffer* semantics, not "this record had an `id:` line":
once the server has sent an `id:`, every later event inherits it until the server sends a new
one, and an explicit empty `id:` line resets it to `""`. The same buffer is what goes out as
`Last-Event-ID` on a reconnect (below), which is omitted while it's empty.

## Decoding events

Pass `into` to decode `.data` into a typed object instead of leaving it as `str` —
`.event`/`.id` come along for the ride unchanged. Same decode targets as everywhere else: a
msgspec `Struct`, a pydantic `BaseModel`, `dict`, or any type expression (`list[ItemModel]` for
array payloads):

=== "msgspec"

    ```python
    from msgspec import Struct


    class ItemModel(Struct):
        id: int
        name: str


    async for event in client.sse("events", into=ItemModel):
        print(event.data)  # e.g. ItemModel(id=25, name='pikachu')
        print(event.event)  # still populated, e.g. 'tick'
    ```

=== "pydantic"

    ```python
    from pydantic import BaseModel


    class ItemModel(BaseModel):
        id: int
        name: str


    async for event in client.sse("events", into=ItemModel):
        print(event.data)  # e.g. id=25 name='pikachu'
        print(event.event)  # still populated, e.g. 'tick'
    ```

=== "dict"

    ```python
    async for event in client.sse("events", into=dict):
        print(event.data)  # e.g. {'id': 25, 'name': 'pikachu', ...}
        print(event.event)  # still populated, e.g. 'tick'
    ```

## Discriminated-union event streams

`into` takes a union directly, so a stream mixing different event shapes decodes natively:

```python
async for event in client.sse("events", into=ItemCreated | ItemDeleted):
    match event.data:
        case ItemCreated():
            ...
        case ItemDeleted():
            ...
```

- msgspec needs a distinguishing `tag`/`tag_field` on each member (see the
  [msgspec docs on tagged unions](https://jcristharif.com/msgspec/structs.html#tagged-unions)).
- pydantic tries each member in turn, or dispatches on an explicit discriminator field (see the
  [pydantic docs on discriminated unions](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)).

## Timeouts

The client's `timeout` doesn't apply to `sse()`; set `read_timeout` to detect a stalled stream.
See [Timeouts](networking.md#timeouts).

## Reconnecting — `max_reconnects`, `reconnect_delay`, `reconnect_on_close`

A dropped connection (a transport error mid-stream, or a timeout) reconnects automatically,
following the [WHATWG EventSource](https://html.spec.whatwg.org/multipage/server-sent-events.html)
processing model — the caller's loop never sees the seam:

- The client waits `reconnect_delay` seconds (default `3.0`, the browser default) before
  reconnecting. If the server has sent a `retry:` field (milliseconds), that value replaces the
  caller's for the rest of the stream — the server knows its own restart time better than you do.
- The reconnect request carries a `Last-Event-ID` header with the last id seen, so a
  spec-compliant server resumes from the right place instead of replaying (or skipping) events.
  To resume a stream from an id you stored earlier, pass it yourself as
  `headers={"Last-Event-ID": ...}`: the first connection sends it as is, and once the stream has
  seen an id, a reconnect sends that one *in its place* (never both).
- `max_reconnects` (default `5`) caps **consecutive reconnects that yield no event** — the "server
  is down" case. Every event received resets the count, so a flaky server that drops the
  connection every few minutes is reconnected to indefinitely, while a dead one fails after
  `max_reconnects` attempts and the transport error is raised. `None` means unlimited (browser
  behavior); `0` disables reconnecting entirely and raises on the first drop.
- A **clean close** ends the stream — `sse()` returns and the loop finishes — unless
  `reconnect_on_close=True`, in which case a clean close reconnects too (again, browser behavior;
  a server that always closes after a burst of events is reconnected to for as long as you keep
  iterating). The same `max_reconnects` budget applies.
- A **204** response always ends the stream, regardless of `reconnect_on_close` — it's the spec's
  explicit "stop, and don't reconnect" signal.

```python
async for event in client.sse(
    "events",
    max_reconnects=None,  # keep reconnecting for as long as we iterate
    reconnect_on_close=True,  # even when the server closes cleanly
):
    ...
```

Records without a `data:` line dispatch no event, but their `id:` and `retry:` fields still take
effect — a server can prime the reconnect delay or the last-event-id before sending anything.

## Errors

A 4xx/5xx response always raises `HTTPResponseError`, before the connection's first event is
yielded rather than partway through iteration, and (per spec) is never reconnected;
`raise_for_status=False` doesn't apply here. A 2xx response whose `Content-Type` isn't
`text/event-stream` raises `ValueError` — it isn't a stream, and parsing it as one would just
silently yield nothing. Once streaming has started, a dropped connection reconnects as described
above; only once the reconnect budget is exhausted does the usual `HTTPTransportError` family
propagate. See [Error handling](errors.md).

## Ctrl-C-interruptible streaming (sync only) — `interruptible`

On `SyncHTTPClient`, `interruptible=True` lets Ctrl-C stop the wait, with the
[same caveat](streaming.md#ctrl-c-interruptible-streaming-sync-only-interruptible):

```python
for event in sync_client.sse("events", interruptible=True):
    ...  # Ctrl-C now works while waiting for the next event
```
