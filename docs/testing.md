---
icon: lucide/list-checks
---

# Testing

Install the `testing` extra to pin a tested pytest version, plus `pytest-asyncio` for the async
examples below:

```
uv add 'lothc[testing]' pytest-asyncio --dev
```

This gets you the `lothc_mocker` pytest fixture. It intercepts every request a `HTTPClient` or
`SyncHTTPClient` sends during a test — no real network call happens, and no changes are needed to
how the client under test is built. Every example on this page is a complete, runnable file —
paste the setup block below into a `test_lothc_mocker.py` (or your `conftest.py` for just the
fixtures), then any test function under it, and `pytest` will run it as-is.

!!! note

    The `lothc_mocker` fixture is registered as a pytest plugin and auto-loads in **any** project
    that has both `lothc` and pytest installed — not only when the `testing` extra is installed.
    Standard Python packaging has no way to make a `pytest11` entry point conditional on an extra
    (unlike a regular dependency). The extra above only pins pytest's version; it isn't a gate.

## Setup

`base_url` doesn't need to point at a real server — `lothc_mocker` intercepts every request before
it reaches the network, so any URL works:

```python
import re

import pytest
import pytest_asyncio
from pydantic import BaseModel
from pyreqwest.request import Request

from lothc import HTTPClient, HTTPConnectionError, HTTPResponseError, SyncHTTPClient
from lothc.testing import LOTHCMocker, MockResponse


@pytest_asyncio.fixture(name="client")
async def _client():
    async with HTTPClient.build(base_url="http://testserver/") as client:
        yield client


@pytest.fixture(name="sync_client")
def _sync_client():
    with SyncHTTPClient.build(base_url="http://testserver/") as client:
        yield client
```

Every test function below assumes `client`/`sync_client` come from this setup, and uses
`@pytest.mark.asyncio` on each `async def test_...` explicitly rather than relying on your own
project's `pytest-asyncio` mode setting — copy them in as shown and they'll run regardless of how
your `pyproject.toml`/`pytest.ini` configures `asyncio_mode`.

## Canned responses

```python
@pytest.mark.asyncio
async def test_get_item(client: HTTPClient, lothc_mocker: LOTHCMocker) -> None:
    lothc_mocker.add_get_response(path="/items/7", data={"id": 7, "name": "item-7"})

    item = await client.get("items/7", response_data_type=dict)

    assert item == {"id": 7, "name": "item-7"}
```

`add_get_response`/`add_post_response`/`add_put_response`/`add_patch_response`/
`add_delete_response`/`add_head_response` — one per lothc verb (no `data=` on the `head` one,
since `head()` never decodes a body) — are the main entry points, and they work identically for
`HTTPClient` and `SyncHTTPClient` tests: the same `lothc_mocker` fixture, the same methods, no
separate sync/async mock API to learn.

## Reusing your own `data=`/`headers=` classes

`data=` and `headers=` both accept the exact same types lothc's own client methods do — a plain
`dict`, or an instance of a pydantic `BaseModel`/msgspec `Struct` you already use elsewhere for
`response_data_type=`/`response_headers_type=`. Define the class once, use it on both the mocking
side and the assertion side:

```python
class Item(BaseModel):
    id: int
    name: str


class ItemHeaders(BaseModel):
    x_total_count: int


@pytest.mark.asyncio
async def test_reuses_typed_data_and_headers_classes(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(
        path="/items/7",
        data=Item(id=7, name="item-7"),
        headers=ItemHeaders(x_total_count=1),
    )

    result = await client.get_result(
        "items/7", response_data_type=Item, response_headers_type=ItemHeaders
    )

    assert result.data == Item(id=7, name="item-7")
    assert result.typed_headers == ItemHeaders(x_total_count=1)
```

Both are encoded exactly the way a real request would encode them (field names with `_` become
`-`, non-`None` values only), so a mocked response round-trips through `response_headers_type=`
the same way a real one does.

## Status and params

```python
@pytest.mark.asyncio
async def test_add_get_response_with_status(client: HTTPClient, lothc_mocker: LOTHCMocker) -> None:
    lothc_mocker.add_get_response(path="/items/7", status=404, data=b"not found")

    with pytest.raises(HTTPResponseError) as exc_info:
        await client.get("items/7")

    assert exc_info.value.status == 404


@pytest.mark.asyncio
async def test_add_get_response_matches_on_params(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.add_get_response(path="/items", params={"page": "2"}, data={"page": 2})

    item = await client.get("items", params={"page": "2"}, response_data_type=dict)

    assert item == {"page": 2}
```

`params=` — same `Params` type a real call's `params=` takes — matches a rule only against
requests sent with those exact query params, the same way `path=`/`url=` narrow by URL.

## Narrowing and asserting on a mock

For anything `params=`/`headers=` can't express as an exact match (a regex, a custom predicate),
every `add_*_response` call returns a `LOTHCMock` that can be narrowed further with
`match_query`/`match_query_param`/`match_header`/`match_body_json`/`match_request` (a matcher only
narrows which requests a rule applies to — it never changes the response), and asserted on
afterwards with `assert_called`/`get_requests`/`get_call_count`:

```python
@pytest.mark.asyncio
async def test_match_header_narrows_a_mock(client: HTTPClient, lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_get_response(path="/items", data={"ok": True})
    mock.match_header("x-request-id", re.compile(r"^req-"))

    item = await client.get("items", headers={"x-request-id": "req-1"}, response_data_type=dict)

    assert item == {"ok": True}
    mock.assert_called(count=1)
```

`lothc_mocker` is **strict by default** — any request that matches no registered mock raises
`AssertionError` immediately, rather than falling through to a real network call:

```python
@pytest.mark.asyncio
async def test_strict_by_default_raises_for_unmatched_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    with pytest.raises(AssertionError):
        await client.get("items/7")
```

Opt out per-test with the `@pytest.mark.lothc_mocker(strict=False)` marker — standard pytest, no
special import needed — for the rare test that genuinely wants an unmocked request to fall
through to the real network instead of raising. Since `client`'s `base_url` here isn't a real
server (see [Setup](#setup)), falling through raises a connection error rather than succeeding —
which is itself the point: with strict mode off, that's a real `HTTPConnectionError` from actually
trying the network, not lothc's own `AssertionError`:

```python
@pytest.mark.lothc_mocker(strict=False)
@pytest.mark.asyncio
async def test_lothc_mocker_marker_disables_strict(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    with pytest.raises(HTTPConnectionError):
        await client.get("items/7")
```

Or call `lothc_mocker.strict(enabled=False)` mid-test instead, if you only want it off for part of
a test rather than the whole thing.

## Custom responses

For anything the `add_*_response` shape doesn't cover (a computed response, a handler that
inspects the request), use the lower-level `mock()` escape hatch — the same one `add_*_response`
is itself built on. `mock()` returns a fresh `LOTHCMock` with no canned response yet, so it's the
only place `match_request_with_response` can be used — calling it on a mock that already has a
canned response (any `add_*_response` call, or `with_status`/`with_headers`/`with_data`) raises a
`ValueError`: a mock rule is either a canned response or a custom handler, never both.

A custom handler has to either be an `async def` (for a `HTTPClient` test) or a plain function
(for a `SyncHTTPClient` test) — the same reason `HTTPClient`/`SyncHTTPClient` themselves are two
separate classes throughout lothc, not one. What the handler *returns* isn't split, though: a
plain `MockResponse` (`data`/`headers`/`status`, same fields and same encoding as
`add_*_response`) either way, or `None` to decline and let a later mock try to match instead —
lothc never hands you pyreqwest's own response types to build this, `MockResponse` is a plain
dataclass, no builder.

This is also the one place `request` is genuinely useful — reading it is the reason to reach for a
custom handler instead of `add_*_response`, whose `data=` is fixed once at registration time. Here
the handler computes its response from `request.url.path` instead of returning a hardcoded value,
so it works for *any* item ID without registering a separate mock per ID:

```python
async def handler(request: Request) -> MockResponse:
    item_id = int(request.url.path.removeprefix("/items/"))
    return MockResponse(status=200, data={"id": item_id})


@pytest.mark.asyncio
async def test_custom_handler_computes_response_from_the_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(handler)

    item = await client.get("items/42", response_data_type=dict)

    assert item == {"id": 42}


def sync_handler(request: Request) -> MockResponse:
    item_id = int(request.url.path.removeprefix("/items/"))
    return MockResponse(status=200, data={"id": item_id})


def test_sync_custom_handler_computes_response_from_the_request(
    sync_client: SyncHTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(sync_handler)

    item = sync_client.get("items/42", response_data_type=dict)

    assert item == {"id": 42}
```

`request` also exposes `.method`, `.headers` (e.g. `request.headers.get("x-request-id")`), and
`.body` — anything a real handler would need to branch on.
