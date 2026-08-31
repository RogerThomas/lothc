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

## Errors

`error_for_status` (default `True`) is checked once, before the first event is yielded — a
4xx/5xx response raises `HTTPResponseError` immediately rather than partway through iteration. Once
streaming has started, a dropped connection raises the usual `HTTPTransportError` family. See
[Error handling](errors.md).

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
