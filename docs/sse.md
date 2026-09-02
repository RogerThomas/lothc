---
icon: lucide/radio
---

# Server-Sent Events (SSE)

`sse()` opens a `GET` request with `Accept: text/event-stream` and always yields an `SSEEvent`,
as the server sends them. It's an `AsyncIterator` on `HTTPClient` and a plain `Iterator` on
`SyncHTTPClient` — breaking out of the loop closes the underlying connection.

```python
@dataclass(kw_only=True)
class SSEEvent[TData, TId = str]:
    id: TId
    event: str = "message"
    data: TData
```

`SSEEvent` is generic in `TData` — `response_data_type` controls what `.data` becomes (raw
`str`, or decoded into a typed object). `.event` is always a plain `str`, never `None` — per the
SSE spec, an event with no `event:` field on the wire is treated as type `"message"`, so there's
always a value. `.id` is genuinely optional per spec (a server can choose never to send `id:`),
which is what `id_type` (below) is about.

## Raw events (default)

If you don't pass `response_data_type`, `.data` is exactly the text the server sent in the
event's `data:` line(s) (multiple `data:` lines get joined with `\n`) — untouched, no JSON
parsing, no model construction, just a plain `str`:

```python
async for event in client.sse("events"):
    print(event.data, event.event, event.id)  # SSEEvent[str, str]
```

## The `id` field — `id_type` and `allow_missing_id`

`id:` is always literal text on the wire, but it's frequently used to encode an integer, a
`uuid.UUID`, or anything else with a single-argument `str`-taking constructor. Two independent
knobs control it: `id_type` picks what `.id` becomes, and `allow_missing_id` picks whether a
missing `id:` raises or becomes `None`:

```python
async for event in client.sse("events"):
    print(event.id)  # str — required, guaranteed present (id_type defaults to str)

async for event in client.sse("events", id_type=int):
    print(event.id)  # int — required, coerced

async for event in client.sse("events", id_type=int, allow_missing_id=True):
    print(event.id)  # int | None — optional, coerced when present

async for event in client.sse("events", allow_missing_id=True):
    print(event.id)  # str | None — optional, never coerced
```

- **`id_type=str`** (the default) — `.id` stays plain `str`.
- **a bare type** (`id_type=int`, `id_type=uuid.UUID`) — `.id` coerced via `id_type(raw_id)`.
- **`allow_missing_id=False`** (the default) — a missing `id:` raises.
- **`allow_missing_id=True`** — a missing `id:` becomes `None` instead of raising; the coercion
  type still applies when `id:` *is* present.

"Missing" follows the spec's *last event ID buffer* semantics, not "this record had no `id:`
line": once the server has sent an `id:`, every later event inherits it until the server sends a
new one, and only an explicit empty `id:` line clears it. So a server that sends `id:` on some
events but not others never trips `allow_missing_id=False` — only a stream that has sent no `id:`
at all yet does. The same buffer is what goes out as `Last-Event-ID` on a reconnect (below).

A conversion failure (e.g. `int("not-a-number")`) propagates as whatever exception that type's
constructor raises — it isn't wrapped, same as every other decode-library error in lothc.

## Decoding events

Pass `response_data_type` to decode `.data` into a typed object instead of leaving it as `str` —
`.event`/`.id` come along for the ride unchanged. Same decode targets as everywhere else:

=== "msgspec"

    ```python
    from msgspec import Struct


    class ItemModel(Struct):
        id: int
        name: str


    async for event in client.sse("events", response_data_type=ItemModel):
        print(event.data)  # e.g. ItemModel(id=25, name='pikachu')
        print(event.event)  # still populated, e.g. 'tick'
    ```

=== "pydantic"

    ```python
    from pydantic import BaseModel


    class ItemModel(BaseModel):
        id: int
        name: str


    async for event in client.sse("events", response_data_type=ItemModel):
        print(event.data)  # e.g. id=25 name='pikachu'
        print(event.event)  # still populated, e.g. 'tick'
    ```

=== "dict"

    ```python
    async for event in client.sse("events", response_data_type=dict):
        print(event.data)  # e.g. {'id': 25, 'name': 'pikachu', ...}
        print(event.event)  # still populated, e.g. 'tick'
    ```

## Discriminated-union event streams

`response_data_type` also accepts a pydantic `TypeAdapter` or a prebuilt msgspec `Decoder`, so a
stream mixing different event shapes decodes natively:

=== "msgspec"

    ```python
    from msgspec.json import Decoder

    item_event_decoder = Decoder(ItemCreated | ItemDeleted)

    async for event in client.sse("events", response_data_type=item_event_decoder):
        match event.data:
            case ItemCreated():
                ...
            case ItemDeleted():
                ...
    ```

    msgspec dispatches on a tagged union by default — give `ItemCreated`/`ItemDeleted` a
    distinguishing `tag`/`tag_field` (see the
    [msgspec docs on tagged unions](https://jcristharif.com/msgspec/structs.html#tagged-unions))
    so `Decoder` knows which one to build.

=== "pydantic"

    ```python
    from pydantic import TypeAdapter

    item_event_adapter = TypeAdapter(ItemCreated | ItemDeleted)

    async for event in client.sse("events", response_data_type=item_event_adapter):
        match event.data:
            case ItemCreated():
                ...
            case ItemDeleted():
                ...
    ```

    pydantic can also dispatch on an explicit discriminator field instead of trying each member
    in turn — see the
    [pydantic docs on discriminated unions](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions).

## Timeouts

The client's `timeout` **does not apply to `sse()`**. It's a total-request timeout — connect
through end of body — and an SSE body is open-ended, so applying it would kill every healthy
stream on schedule (with the default `timeout=30.0`, at the 30 second mark, as `HTTPTimeoutError`).
`sse()` overrides it per request instead. Two knobs remain:

- **`read_timeout`** on `build()` — the maximum idle gap between two consecutive chunks. This is
  the right way to detect a stalled stream: a server that stops sending (without closing) trips it,
  a server that keeps sending never does, however long the stream lives. Client-level, like
  `connect_timeout` — see [Cookies, redirects, proxy & TLS](networking.md#connection-pooling).
- **`timeout=`** on the `sse()` call itself — bounds one *connection attempt*, not the whole
  stream. Rarely what you want; `read_timeout` usually is.

Either one firing is a transport error, which the reconnect logic below handles like any other
dropped connection.

## Reconnecting — `max_reconnects`, `reconnect_delay`, `reconnect_on_close`

A dropped connection (a transport error mid-stream, or a timeout) reconnects automatically,
following the [WHATWG EventSource](https://html.spec.whatwg.org/multipage/server-sent-events.html)
processing model — the caller's loop never sees the seam:

- The client waits `reconnect_delay` seconds (default `3.0`, the browser default) before
  reconnecting. If the server has sent a `retry:` field (milliseconds), that value replaces the
  caller's for the rest of the stream — the server knows its own restart time better than you do.
- The reconnect request carries a `Last-Event-ID` header with the last id seen, so a
  spec-compliant server resumes from the right place instead of replaying (or skipping) events.
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

`error_for_status` (default `True`) is checked once per connection, before its first event is
yielded — a 4xx/5xx response raises `HTTPResponseError` immediately rather than partway through
iteration, and (per spec) is never reconnected. A 2xx response whose `Content-Type` isn't
`text/event-stream` raises `ValueError` — it isn't a stream, and parsing it as one would just
silently yield nothing. Once streaming has started, a dropped connection reconnects as described
above; only once the reconnect budget is exhausted does the usual `HTTPTransportError` family
propagate. See [Error handling](errors.md).

## Ctrl-C-interruptible streaming (sync only) — `interruptible`

On `SyncHTTPClient` only, a blocking wait for the next event is dead to Ctrl-C: CPython only
converts `SIGINT` into `KeyboardInterrupt` on the main thread while it's executing Python
bytecode, and the wait between SSE events happens inside a blocking Rust call with no bytecode
running at all. `HTTPClient.sse()` (async) doesn't have this problem — the event loop's
selector is already signal-interruptible — so `interruptible` only exists on the sync client.

Pass `interruptible=True` to fix this: the read loop runs on a daemon worker thread instead,
and the calling thread only ever does short, signal-interruptible waits on a queue, so Ctrl-C
fires within roughly 0.2 seconds instead of never:

```python
for event in sync_client.sse("events", interruptible=True):
    ...  # Ctrl-C now works while waiting for the next event
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

The same flag, with the same behavior and the same caveat, is also available on
[`stream_get`/`stream_post`](streaming.md#ctrl-c-interruptible-streaming-sync-only-interruptible).
