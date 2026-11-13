---
icon: lucide/list-checks
---

# Testing

Install the `testing` extra to pin a tested pytest version, plus `pytest-asyncio` for the async
examples below:

```
uv add 'lothc[testing]' pytest-asyncio --dev
```

## Setup

`base_url` doesn't need to point at a real server — `lothc_mocker` intercepts every request before
it reaches the network, so any URL works:

```python
import re

import pytest
import pytest_asyncio
from pydantic import BaseModel

from lothc import HTTPClient, HTTPResponseError, SyncHTTPClient
from lothc.testing import LOTHCMocker, MockRequest, MockResponse


@pytest_asyncio.fixture(name="client")
async def _client():
    async with HTTPClient(base_url="http://testserver/") as client:
        yield client


@pytest.fixture(name="sync_client")
def _sync_client():
    with SyncHTTPClient(base_url="http://testserver/") as client:
        yield client
```

## Canned responses

```python
@pytest.mark.asyncio
async def test_get_item(client: HTTPClient, lothc_mocker: LOTHCMocker) -> None:
    lothc_mocker.add_get_response(path="/items/7", data={"id": 7, "name": "item-7"})

    response = await client.get("items/7", into=dict)

    assert response.data == {"id": 7, "name": "item-7"}
```

`add_get_response`/`add_post_response`/`add_put_response`/`add_patch_response`/
`add_delete_response`/`add_head_response` (no `data=` on the `head` one) are the main entry
points, and work identically for `HTTPClient` and `SyncHTTPClient` tests. For OPTIONS, use
`lothc_mocker.mock("OPTIONS").with_status(204)`.

## Reusing your own `data=`/`headers=` classes

`data=` and `headers=` both accept the exact same types lothc's own client methods do — a plain
`dict`, or an instance of a pydantic `BaseModel`/msgspec `Struct` you already use elsewhere for
`into=`/`response_headers_type=`. Define the class once, use it on both the mocking
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

    response = await client.get("items/7", into=Item, response_headers_type=ItemHeaders)

    assert response.data == Item(id=7, name="item-7")
    assert response.typed_headers == ItemHeaders(x_total_count=1)
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

    response = await client.get("items", params={"page": "2"}, into=dict)

    assert response.data == {"page": 2}
```

`params=` — same `Params` type a real call's `params=` takes — narrows a rule to requests that
include at least those query params with those exact values. It's a subset match, not an exact
one: a real request sending extra, unlisted query params still matches. A `list[...]`/`tuple[...]`
value (a genuinely repeated query key, same convention as the real `params=`) narrows on the full,
order-sensitive list of values for that key.

`add_*_response`'s own `headers=` is not a matcher at all — it only sets the mocked *response's*
headers. Narrowing on the request's own headers (or body) needs the separate `match_header`/
`match_body_json` methods below.

## Narrowing and asserting on a mock

For anything `params=` can't express (a regex, "any value", a custom predicate, requiring no
*extra* params), or to narrow on the request's headers/body at all, every `add_*_response` call
returns a `LOTHCMock` that can be narrowed further with
`match_query`/`match_query_param`/`match_header`/`match_body_json`/`match_request` (a matcher only
narrows which requests a rule applies to — it never changes the response), and asserted on
afterwards with `assert_called`/`get_requests`/`get_call_count`. `get_requests()` returns
`MockRequest` snapshots (`method`/`path`/`query_string`/`query`/`headers`/`body`):

```python
@pytest.mark.asyncio
async def test_match_header_narrows_a_mock(client: HTTPClient, lothc_mocker: LOTHCMocker) -> None:
    mock = lothc_mocker.add_get_response(path="/items", data={"ok": True})
    mock.match_header("x-request-id", re.compile(r"^req-"))

    response = await client.get("items", headers={"x-request-id": "req-1"}, into=dict)

    assert response.data == {"ok": True}
    mock.assert_called(count=1)
    [seen] = mock.get_requests()
    assert seen.headers["x-request-id"] == "req-1"
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

Opt out with `@pytest.mark.lothc_mocker(strict=False)`, or mid-test with
`lothc_mocker.strict(enabled=False)`.

## Custom responses

For anything the `add_*_response` shape doesn't cover (a computed response, a handler that
inspects the request), use the lower-level `mock()` escape hatch — the same one `add_*_response`
is itself built on. `mock()` returns a fresh `LOTHCMock` with no canned response yet, so it's the
only place `match_request_with_response` can be used — calling it on a mock that already has a
canned response (any `add_*_response` call, or `with_status`/`with_headers`/`with_data`) raises a
`ValueError`: a mock rule is either a canned response or a custom handler, never both.

A handler (sync, or async for `HTTPClient` only) takes a `MockRequest` and returns a
`MockResponse`, or `None` to defer to a later mock. A sync handler serves both clients:

```python
def _handler(request: MockRequest) -> MockResponse:
    return MockResponse(status=200, data={"id": int(request.path.removeprefix("/items/"))})


@pytest.mark.asyncio
async def test_custom_handler_computes_response_from_the_request(
    client: HTTPClient, lothc_mocker: LOTHCMocker
) -> None:
    lothc_mocker.mock("GET").match_request_with_response(_handler)

    response = await client.get("items/42", into=dict)

    assert response.data == {"id": 42}
```

## How a request finds its mock

A request gets the response of the first mock, in the order they were registered, that matches
it. `path=` matches the URL's path and `url=` the whole URL, query included: a `str` exactly, an
`re.Pattern` anywhere in it (`re.search`). Every `match_*` call narrows the mock further, so a
request must satisfy all of them. In `match_query`/`match_query_param`, a `str` or pattern value
matches a key sent once, and a `list` every value of a repeated key, in order.
`lothc_mocker.get_requests()` lists every request its mocks answered, in the order they were sent.

When `assert_called` fails, it says what the mock is, how many requests it answered, and either
the requests past the expected count or the closest request it didn't answer, with each part that
didn't match:

```
The mock GET path='/items' answered 0 requests, expected exactly 1.
The closest request it didn't answer, GET http://testserver/items?page=2:
  query 'page': expected '1', got '2'
```

A strict mock's `AssertionError` for an unmatched request names the closest mock the same way.

## What a mock sees, and what it skips

A mock sees each request as the client built it: the full URL (`base_url` joined, `params=`
encoded), `default_headers`, auth and your own `headers=` (in the order they go out), and a
`json=`/`content=` body's bytes. It answers before the transport takes over, so the request has
none of the headers the transport adds while sending (`Accept`, `User-Agent`, `Accept-Encoding`,
cookies, `Host`), and `.body` is `None` for a `data=` or `form=` body, which the transport encodes
itself (their `Content-Type`, and a `form=` body's `Content-Length`, are there). The response a mock
gives comes back as is: a `3xx` isn't followed, a compressed body isn't decompressed, a
`Set-Cookie` isn't stored, and neither the client's `timeout` nor `max_connections` applies to it.
Retries and a `401`'s re-authentication do go through the mocks again, one request each.
