# lothc — agent reference

Typed HTTP client on pyreqwest. `HTTPClient` (async) / `SyncHTTPClient` (sync) — identical API,
mirror methods 1:1 (drop `await`, `async with` → `with`, async iterators → sync iterators).

## Design rule: never expose pyreqwest internals

lothc's public API (including `lothc.testing`) must never require a caller to import from
`pyreqwest` directly, or hand them a pyreqwest type to construct or receive. pyreqwest is an
implementation detail; every user-facing type is lothc's own (`Data`, `Params`, `Headers`,
`MockResponse`, `MockRequest`, ...). This is now a hard, fully-enforced rule in `lothc.testing`,
not just an aspiration — two rounds of fixes:
(1) `match_request_with_response`'s custom-handler escape hatch used to require building a
response via pyreqwest's own `ResponseBuilder().status(...).body_json(...).build()`/
`.build_sync()` — fixed by adding `MockResponse` (a plain dataclass, no builder pattern — this
project doesn't use that pattern anywhere else) that the handler returns instead; lothc builds the
real pyreqwest response internally.
(2) `Request` (the handler/matcher's parameter type, and `get_requests()`'s return type) and
`Url` (the `url=` matcher's type) were still pyreqwest's own types, re-exported rather than
wrapped — rejected as insufficient (a re-export still means the *class itself* is pyreqwest's,
even if the import path isn't). Fixed properly: `MockRequest` (`lothc/testing.py`) is a plain,
frozen, slotted dataclass (`method`/`path`/`query_string`/`headers`/`body`) built by
`_mock_request_from` from pyreqwest's real `Request` at the one point a mock actually receives
one — `match_request`/`match_request_with_response` handlers get a `MockRequest`, `get_requests()`
returns `list[MockRequest]`, and `url=` matching is narrowed to `str | re.Pattern[str]` only
(pyreqwest's `Url`-object alternative is dropped entirely — a `str` already matches the exact URL,
so nothing real is lost, and there's no `URL` type to wrap or re-export at all). No pyreqwest type
appears anywhere in `lothc.testing`'s public API now, not even as a re-export.

## Build

```python
async with HTTPClient.build(
    base_url=None,
    bearer_token=None,
    bearer_auth=None,
    default_headers=None,
    timeout=30.0,
    cookie_store=False,
    follow_redirects=True,
    max_redirects=None,
    proxy=None,
    max_retries=0,
    retry_methods=None,
) as client:
    ...
```

`bearer_token: str` (static) xor `bearer_auth: Callable[[], Awaitable[str]]` (sync client:
`Callable[[], str]`) — resolved fresh per request, at most one of the two (see OAuth below for a
ready-made `bearer_auth`). `default_headers`
sent on every request. `cookie_store=True` = in-memory jar. `proxy: str | None`.

## OAuth 2 client credentials (`lothc/_oauth.py`)

`OAuthProvider` (async) / `SyncOAuthProvider` (sync) are ready-made `bearer_auth` providers —
pass an instance as `bearer_auth=`, no new client parameter. All keyword-only:

```python
OAuthProvider(
    token_url=...,
    client_id=...,
    client_secret=...,
    scope=None,  # RFC path only
    client_auth="basic",  # "basic" (HTTP Basic header, RFC default) | "body" (form fields)
    token_request=None,  # non-RFC APIs: class constructible as Cls(client_id=, client_secret=)
    token_refresh_request=None,  # optional, Cls(refresh_token=); without it renewal always mints
    token_response=None,  # class with .access_token / .expires_in; .refresh_token read if present, not required
    refresh_leeway=300.0,  # renew once fewer than this many seconds remain (clamped to expires_in / 2)
    default_expires_in=None,  # lifetime to assume when the response has no expires_in; else error
    token_cache_path=None,  # Path: JSON, atomic replace, 0600, keyed on token_url + client_id + scope;
    # parent dir must exist at construction (FileNotFoundError)
    client_factory=HTTPClient.build,  # called with NO args per token request; partial(...) for
    # timeout/proxy/TLS. Sync: SyncHTTPClient.build
)
```

RFC path (no models): form-encoded `grant_type=client_credentials`/`grant_type=refresh_token`,
Basic header built by lothc with each half `quote(..., safe="")`-encoded (RFC 6749 §2.3.1).
Model path: request instance sent as `json=`, `client_auth` ignored, `token_request` +
`token_response` both or neither; `token_response` needs `.access_token: str`,
`.expires_in: int | None` — `.refresh_token` is read via `getattr(..., "refresh_token", None)`
if the model declares it, but an API that never issues one needs no field for it at all.
Aliased pydantic request models need
`ConfigDict(validate_by_name=True, serialize_by_alias=True)` (lothc's `json=` dumps without
`by_alias`); msgspec `field(name=...)` needs nothing. Renewal: refresh if a `refresh_token` is
held and refreshing is possible (a refresh response without one keeps the old one), 400 on
refresh → mint; anything else (401/403/429, 5xx, transport, decode, malformed payload) →
`OAuthTokenError` (`.token_url`, original as `__cause__`) from the user's API call. One renewal
under concurrency (lock; the async lock is per-event-loop, so a provider survives repeated
`asyncio.run()`).

## Decode targets (`response_data_type`, default `bytes`)

- `bytes` — raw, default
- `dict` — plain `dict[str, Any]`, zero validation
- pydantic `BaseModel` subclass
- msgspec `Struct` subclass
- `dict[str, Any]` (subscripted) — NOT allowed, raises `TypeError` (not a real class)

## Verbs

- `get(path, *, params=None, headers=None, response_data_type=bytes, error_for_status=True) -> Data`
- `get_result(path, *, params=None, headers=None, response_data_type=bytes, response_headers_type=None, error_for_status=True) -> Result` —
  `.data .status .headers .typed_headers`
- `post/put/patch(path, *, params=None, headers=None, json=None, form=None, content=None, response_data_type=bytes, error_for_status=True) -> Data` —
  at most one of `json`/`form`/`content`, else `ValueError`
- `delete(path, *, params=None, headers=None, response_data_type=bytes, error_for_status=True) -> Data`
- `head(path, *, params=None, headers=None, response_headers_type=None, error_for_status=True) -> Result[None]`
- `sse(path, *, params=None, headers=None, response_data_type=None, id_type=str, allow_missing_id=False, error_for_status=True) -> Iterator[SSEEvent[TData, TId]]` —
  always yields `SSEEvent(id=, event=, data=)` (kw-only, `SSEEvent[TData, TId=str]`).
  `response_data_type` controls `.data`'s type only (default `str`); class | pydantic
  `TypeAdapter` | msgspec `Decoder`. `.event` is always `str`, never `None` (spec defaults it to
  `"message"` when absent from the wire). `.id` is genuinely `str | None` per spec — two
  independent knobs control it: `id_type` (a bare type, default `str`, coerced via
  `id_type(raw)`) and `allow_missing_id` (default `False` — missing `id:` raises; `True` — `.id`
  becomes `None` instead, coercion type still applies when present)
- `stream_get(path, *, params=None, headers=None, response_data_type=None, error_for_status=True) -> Iterator[bytes | TLine]` —
  raw unbuffered bytes by default (safe for binary); `response_data_type` switches to
  newline-buffered per-line decode
- `stream_post(path, *, params=None, headers=None, json=None, form=None, content=None, response_data_type=None, error_for_status=True) -> Iterator[bytes | TLine]`
- `download(path, dest=None, *, params=None, headers=None, error_for_status=True) -> bytes | None` —
  memory-efficient GET for large bodies; no `dest` streams into one buffer and returns `bytes`
  (~1/3 the peak memory of `get()`), `dest: Path` streams straight to a file instead (`None`
  return, O(chunk size) memory regardless of body size)

`params`/`headers`: `dict`/`Mapping[str, str]`, or `BaseModel`/`Struct` (`None` fields omitted).
`json`: `dict` | `BaseModel` | `Struct`. `form`: `dict[str, int | bytes | str | File]`, `File =
tuple[str, bytes] | Path | BufferedIOBase`. `content`: raw `str | bytes` body.

## Errors

- `HTTPResponseError(status, body_start)` — 4xx/5xx, raised when `error_for_status=True` (default)
- `HTTPTransportError` base; `HTTPTimeoutError`, `HTTPConnectionError` subclasses — no response received
- pydantic/msgspec validation errors propagate unwrapped (not translated)

## Retries

`max_retries` (default `0` = off), `retry_methods` (default `{GET,PUT,DELETE,HEAD}` — `POST`/
`PATCH` need explicit opt-in). Exponential backoff; retries on transport error or status in
`{429,500,502,503,504}`; honors `Retry-After`.

## Testing (`lothc[testing]`, `lothc/testing.py`)

`lothc_mocker: LOTHCMocker` pytest fixture (auto-registered `pytest11` entry point — no
`conftest.py` wiring, works identically for `HTTPClient`/`SyncHTTPClient`). Thin adapter over
pyreqwest's own `client_mocker` plugin — see the design rule above: no pyreqwest type is ever part
of this fixture's public surface, not even re-exported.

- `add_get_response`/`add_post_response`/`add_put_response`/`add_patch_response`/
  `add_delete_response`/`add_head_response(*, path=None, url=None, params=None, data=b"",
  headers=None, status=200) -> LOTHCMock` — `params`/`data`/`headers` are lothc's own
  `Params`/`Data`/`Headers` types (`dict` or `BaseModel`/`Struct`, same class you'd reuse for
  `response_data_type=`/`response_headers_type=` on the real call), encoded exactly the way a real
  request would be — `params=`'s match values are derived from pyreqwest's own real encoder
  (`_query_param_match_values`, via `Url.parse_with_params(...).query_dict_multi_value`), not
  hand-reimplemented, so a `bool` becomes lowercase `true`/`false` (not Python's `str(True)`) and
  an invalid value (e.g. `None`) raises the same `ValueError` a real request would rather than
  silently registering an unreachable mock. `params=` narrows by exact query-param match; no
  `data=` on `add_head_response`. `url=` is `str | re.Pattern[str]` only.
- `LOTHCMock`: `.match_query`/`.match_query_param`/`.match_header`/`.match_body_json`/
  `.match_request(predicate)` narrow further (chainable); `.assert_called(count=/min_count=/
  max_count=)`, `.get_requests() -> list[MockRequest]`, `.get_call_count()`, `.reset_requests()`.
- `LOTHCMocker.mock(method=None, *, path=None, url=None) -> LOTHCMock` — bare rule, no canned
  response yet; the only way to reach `.match_request_with_response(handler)` (a canned response
  and a custom handler are mutually exclusive on one rule — raises `ValueError` either order, via
  `LOTHCMock._commit`). `handler`: `async def` (for `HTTPClient`) or plain `def` (for
  `SyncHTTPClient`) — dispatch is via `inspect.iscoroutinefunction`, not overloads — taking
  `MockRequest`, returning `MockResponse | None` (`None` = decline, fall through to the next
  mock). `match_request`'s predicate takes `MockRequest` too, same async/plain split.
- `MockRequest` (slotted, **not** frozen — a mutable `headers` dict field means `frozen=True`
  couldn't deliver real immutability/hashability anyway): `.method`/`.path`/`.query_string`/
  `.headers` (`Mapping[str, str]`, first-value-only for a repeated header, matching
  `Result.headers`'s own convention elsewhere)/`.body` (`bytes | None`) — lothc's own snapshot of
  the request a handler/predicate/`get_requests()` sees, built by `_mock_request_from`. Never
  pyreqwest's `Request`.
- `LOTHCMocker.strict(enabled=True)` raises `AssertionError` on an unmatched request — this is the
  fixture's *default* (pyreqwest's own `client_mocker` defaults to silent passthrough instead; the
  `lothc_mocker` fixture explicitly overrides that). Opt out per-test with
  `@pytest.mark.lothc_mocker(strict=False)` (registered via this module's own `pytest_configure`
  hook — a positional `@pytest.mark.lothc_mocker(False)` also works) or call
  `.strict(enabled=False)` mid-test. `.clear()`, `.get_requests() -> list[MockRequest]`,
  `.get_call_count()`, `.reset_requests()`.
