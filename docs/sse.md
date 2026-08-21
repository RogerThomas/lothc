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

## The `id` field — `id_type`

`id:` is always literal text on the wire, but it's frequently used to encode an integer, a
`uuid.UUID`, or anything else with a single-argument `str`-taking constructor. `id_type` is the
single knob for both *whether `id` is required* and *what type it becomes* — pass the type you
want, and union it with `None` yourself when it's optional, the same way you'd write
`response_data_type=ItemCreated | ItemDeleted` for a discriminated union elsewhere:

```python
async for event in client.sse("events"):
    print(event.id)  # str — required, guaranteed present (id_type defaults to str)

async for event in client.sse("events", id_type=int):
    print(event.id)  # int — required, coerced

async for event in client.sse("events", id_type=int | None):
    print(event.id)  # int | None — optional, coerced when present

async for event in client.sse("events", id_type=None):
    print(event.id)  # str | None — optional, never coerced
```

- **`id_type=str`** (the default) — required, `.id` stays plain `str`.
- **a bare type** (`id_type=int`, `id_type=uuid.UUID`) — required, `.id` coerced via
  `id_type(raw_id)`.
- **a type unioned with `None`** (`id_type=int | None`) — optional; coerced when present, `None`
  when the event has no `id:`.
- **`id_type=None`** (bare) — optional, `.id` stays `str | None`, never coerced.

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

=== "TypedDict"

    ```python
    from typing import TypedDict


    class ItemModel(TypedDict):
        id: int
        name: str


    async for event in client.sse("events", response_data_type=ItemModel):
        print(event.data)  # e.g. {'id': 25, 'name': 'pikachu'}
        print(event.event)  # still populated, e.g. 'tick'
    ```

    !!! warning

        Install the `typeguard` extra to get the `TypedDict` fields validated at runtime — same
        caveat as everywhere else `TypedDict` is used as a decode target.

=== "JSON"

    ```python
    from lothc import JSON

    async for event in client.sse("events", response_data_type=JSON):
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
