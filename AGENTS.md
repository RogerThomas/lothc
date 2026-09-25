# lothc — agent reference

Typed HTTP client on pyreqwest. `HTTPClient` (async) / `SyncHTTPClient` (sync) — identical API,
mirror methods 1:1 (drop `await`, `async with` → `with`, async iterators → sync iterators).

## Design rule: never expose pyreqwest internals

lothc's public API (including `lothc.testing`) must never require a caller to import from
`pyreqwest` directly, or hand them a pyreqwest type to construct or receive. pyreqwest is an
implementation detail; every user-facing type is lothc's own (`Data`, `Params`, `Headers`,
`CaseInsensitiveDict`, `TlsVersion`, `MockResponse`, `MockRequest`, ...). Enforced by
`tests/test_public_api.py`, which walks every exported signature, overload and type alias in
`lothc` and `lothc.testing` and fails on any pyreqwest type. History: the clients used to be opened
via a `build()` classmethod, which left their public constructor taking a pyreqwest `Client`; in
`lothc.testing`, `MockResponse`/`MockRequest` replaced pyreqwest's `ResponseBuilder`/`Request`
(a re-export was rejected as insufficient: the class itself would still be pyreqwest's).

## Construct and open

```python
async with HTTPClient(
    base_url=None,
    bearer_token=None,  # at most one of bearer_token / bearer_auth / basic_auth
    bearer_auth=None,
    basic_auth=None,  # (username, password | None)
    default_headers=None,  # Headers: a Mapping or BaseModel/Struct, encoded like headers=
    user_agent=None,  # default "python-lothc/<version>"; a per-request headers= overrides it
    timeout=30.0,  # total cap, except a gap limit for stream_get/stream_post/download (below)
    cookie_store=False,
    follow_redirects=True,
    max_redirects=None,
    proxy=None,
    no_proxy=None,  # list/tuple of hosts that skip proxy (NO_PROXY format); needs proxy
    max_retries=0,
    retry_methods=None,  # set/frozenset/list/tuple of str, case-insensitive; empty = retry nothing
    backoff_base=0.1,
    max_retry_after=60.0,
    connect_timeout=None,
    read_timeout=None,
    root_certificates=None,
    identity_pem=None,
    min_tls_version=None,  # TlsVersion: "TLSv1.0" | "TLSv1.1" | "TLSv1.2" | "TLSv1.3"
    max_tls_version=None,
    danger_accept_invalid_certs=False,
    https_only=False,
    http2=False,  # negotiate HTTP/2 via TLS ALPN, falling back to HTTP/1.1
    resolve=None,  # {hostname: ip}: skip DNS for these hosts (the URL's port still applies)
    local_address=None,  # source IP to connect from
    tcp_keepalive=None,  # seconds; TCP keepalive probes on idle connections
    max_connections=None,
    pool_idle_timeout=None,
    pool_max_idle_per_host=None,
    pool_timeout=None,
) as client:
    ...
```

The constructor only validates and stores settings (conflicting auth, or `no_proxy` without `proxy`, raises `ValueError` here);
entering builds and opens the pyreqwest client. A client can be entered again after it exits (a
module-level client works across separate `asyncio.run()` calls); entering an already-open one, or
using one that isn't open, raises `RuntimeError`. `bearer_auth` is `Callable[[], Awaitable[str]]`
(sync client: `Callable[[], str]`), resolved fresh per request.

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
    token_cache_dir=None,  # Path: one JSON file per token_url + client_id + scope, named
    # "<host>-<client_id>-<hash>.json"; atomic replace, 0600; the dir must exist at construction
    # (FileNotFoundError); a failed write warns, never raises
    client_factory=HTTPClient,  # called with NO args per token request; partial(...) for
    # timeout/proxy/TLS. Sync: SyncHTTPClient
)
```

RFC path (no models): form-encoded `grant_type=client_credentials`/`grant_type=refresh_token`,
Basic header built by lothc with each half `quote(..., safe="")`-encoded (RFC 6749 §2.3.1).
Model path: request instance sent as `json=`, `client_auth` ignored, `token_request` +
`token_response` both or neither; `token_response` needs `.access_token: str`,
`.expires_in: int | None` — `.refresh_token` is read via `getattr(..., "refresh_token", None)`
if the model declares it. Aliased pydantic request models need `ConfigDict(validate_by_name=True)`
so lothc can construct them by field name; they encode by alias automatically (below). Renewal:
refresh if a `refresh_token` is held (a refresh response without one keeps the old one), 400 on
refresh → mint; anything else → `OAuthTokenError` (`.token_url`, original as `__cause__`) from the
user's API call. One renewal under concurrency (lock; the async lock is per-event-loop).

Revoked tokens: on a 401, the client calls `provider.invalidate(rejected_token)` and, for an
idempotent verb (GET/PUT/DELETE/HEAD), retries once with a fresh token; a POST/PATCH is never
replayed but the next call gets a fresh token; a second 401 is raised. `invalidate()` is public
and a no-op if the provider has already renewed. Any `bearer_auth` with an
`invalidate(stale_access_token=None)` method gets this; the client knows nothing about OAuth.

## Decode targets (`response_data_type`, default `bytes`)

- `bytes` — raw, default
- `dict` — plain `dict[str, Any]` for a JSON *object*; any other JSON value raises `ValueError`
- pydantic `BaseModel` subclass
- msgspec `Struct` subclass
- pydantic `TypeAdapter` / msgspec `Decoder` — any other shape, e.g. a top-level array:
  `TypeAdapter(list[Item])` → `list[Item]`
- `dict[str, Any]` (subscripted) — NOT allowed, raises `TypeError` (not a real class)

## Verbs

Every body verb returns a `Response[TData]`: `.data` (the body decoded as `response_data_type`),
`.status`, `.headers`, `.typed_headers` (set with `response_headers_type=`), `.request`,
`.http_version` (e.g. `"HTTP/1.1"`) and `.elapsed` (seconds, including retries).

- `get(path, *, params=None, headers=None, response_data_type=bytes, response_headers_type=None, error_for_status=True, error_type=None) -> Response[Data]`
- `post/put/patch(path, *, params=None, headers=None, json=None, data=None, form=None, content=None, response_data_type=bytes, response_headers_type=None, error_for_status=True, error_type=None) -> Response[Data]` —
  at most one of `json`/`data`/`form`/`content`, else `ValueError`
- `delete(path, *, params=None, headers=None, json=None, data=None, form=None, content=None, response_data_type=bytes, ...) -> Response[Data]` — a body is optional
- `head(path, *, params=None, headers=None, response_headers_type=None, error_for_status=True) -> Response[None]`
- `sse(path, *, params=None, headers=None, response_data_type=None, error_for_status=True) -> Generator[SSEEvent[TData]]` —
  yields `SSEEvent(id=, event=, data=)` (kw-only dataclass, `SSEEvent[TData]`).
  `response_data_type` controls `.data` only (default `str`); class | `TypeAdapter` | `Decoder`.
  `.event` and `.id` are always `str`, defaulting to `"message"` and `""` as the spec does.
- `stream_get(path, *, params=None, headers=None, response_data_type=None, error_for_status=True) -> Generator[bytes | TLine]` —
  raw unbuffered bytes by default; `response_data_type` switches to newline-buffered per-line decode
- `stream_post(path, *, ..., json=None, data=None, form=None, content=None, response_data_type=None, ...) -> Generator[bytes | TLine]`
  (async: `AsyncGenerator`). Generators so a caller can `close()`/`aclose()` one it stops reading early.
- `download(path, dest=None, *, params=None, headers=None, error_for_status=True) -> bytes | None` —
  large bodies: no `dest` returns `bytes` (~2/3 the peak memory of `get()`), `dest` (`str` or
  path-like) streams to a file (O(chunk size) memory), written to a hidden sibling and renamed on
  success, so a failed download never leaves a truncated file or clobbers an existing one

For `stream_get`/`stream_post`/`download` the client `timeout` is the longest gap allowed between
chunks (and before headers), not a total cap; a per-call `timeout=` is a total cap; `read_timeout`
replaces the gap limit. `sse()` has no gap limit unless `read_timeout` is set.

`params`/`headers`: `dict`/`Mapping[str, str]`, or `BaseModel`/`Struct` (`None` fields omitted).
`params`'s value may also be a `list[...]`/`tuple[...]` of `str | int | float | bool` — sends that
key once per element (`{"tag": ["a", "b"]}` → `?tag=a&tag=b`). `json`: `dict` | `list` | `BaseModel`
| `Struct`; pydantic models encode by alias (like msgspec's `rename=`) unless they set
`serialize_by_alias=False`. `data`: urlencoded body, same types as `params`. `form`
(`multipart/form-data`): `dict[str, str | int | float | bool | bytes | list | dict |
model | File | tuple[...]]` — a `tuple` repeats the field and must be homogeneous; a `list` is always
one JSON part; `bool` sends `"true"`/`"false"`. `content`: raw `str | bytes`, no `Content-Type` set.
`Response.headers` / `HTTPResponseError.headers` are a `CaseInsensitiveDict`: case-insensitive
lookups, `get_all(name)` for a repeated header's every value.

## Errors

- `HTTPResponseError` — 4xx/5xx when `error_for_status=True` (default). `.status`, `.headers`,
  `.body` (whole), `.body_start` (first 100 bytes), `.request`, `.parsed_body` (with `error_type`),
  `.parse_error`; pickles. `error_type` decoding is best-effort: a body that doesn't match gives
  `parsed_body=None` and `.parse_error`, never replaces the error.
- `HTTPTransportError` base; `HTTPTimeoutError`, `HTTPConnectionError` subclasses — no usable
  response (none at all, or the connection failed partway through one)
- Using a closed client → `RuntimeError`
- `response_data_type` validation errors (pydantic/msgspec/`json`) propagate unwrapped

## Retries

`max_retries` (default `0` = off), `retry_methods` (default `{GET,PUT,DELETE,HEAD}` — `POST`/
`PATCH` need explicit opt-in). Backoff `backoff_base * 2 ** attempt`; retries on transport error or
status in `{429,500,502,503,504}`; honours `Retry-After` up to `max_retry_after` (default 60s),
beyond which retrying stops and the 429/503 is raised. Never retried: a request that can't be built
(e.g. `https_only` against `http://`), a redirect loop, a body cut short mid-read.

## Testing (`lothc[testing]`, `lothc/testing.py`)

`lothc_mocker: LOTHCMocker` pytest fixture (auto-registered `pytest11` entry point — no
`conftest.py` wiring, works identically for `HTTPClient`/`SyncHTTPClient`). Thin adapter over
pyreqwest's own `client_mocker` plugin — see the design rule above: no pyreqwest type is ever part
of this fixture's public surface, not even re-exported.

- `add_get_response`/`add_post_response`/`add_put_response`/`add_patch_response`/
  `add_delete_response`/`add_head_response(*, path=None, url=None, params=None, data=b"",
  headers=None, status=200) -> LOTHCMock` — `params`/`data`/`headers` are lothc's own
  `Params`/`Data`/`Headers` types, encoded exactly the way a real request would be (a model in
  `data=` encodes by alias, as a real `json=` does). `params=`'s match values come from
  pyreqwest's own real encoder, so a `bool` becomes lowercase `true`/`false` and an invalid value
  (e.g. `None`) raises the same `ValueError` a real request would. `params=` narrows by exact
  query-param match — a `list[...]`/`tuple[...]` value narrows on the full, order-sensitive list of
  values for that key; no `data=` on `add_head_response`. `url=` is `str | re.Pattern[str]` only.
- `LOTHCMock`: `.match_query`/`.match_query_param`/`.match_header`/`.match_body_json`/
  `.match_request(predicate)` narrow further (chainable); `.assert_called(count=/min_count=/
  max_count=)`, `.get_requests() -> list[MockRequest]`, `.get_call_count()`, `.reset_requests()`.
- `LOTHCMocker.mock(method=None, *, path=None, url=None) -> LOTHCMock` — bare rule, no canned
  response yet; the only way to reach `.match_request_with_response(handler)` (a canned response
  and a custom handler are mutually exclusive on one rule — raises `ValueError` either order).
  `handler`: `async def` (for `HTTPClient`) or plain `def` (for `SyncHTTPClient`), taking
  `MockRequest`, returning `MockResponse | None` (`None` = decline, fall through to the next
  mock). `match_request`'s predicate takes `MockRequest` too, same async/plain split.
- `MockRequest` (slotted, **not** frozen — its mutable `headers` field means `frozen=True`
  couldn't deliver real immutability anyway): `.method`/`.path`/`.query_string`/`.query`/
  `.headers` (a `CaseInsensitiveDict`, like `Response.headers`: case-insensitive, `get_all` for a
  repeated header)/`.body` (`bytes | None`). Never pyreqwest's `Request`.
- `LOTHCMocker.strict(enabled=True)` raises `AssertionError` on an unmatched request — this is the
  fixture's *default* (pyreqwest's own `client_mocker` defaults to silent passthrough). Opt out
  per-test with `@pytest.mark.lothc_mocker(strict=False)` or call `.strict(enabled=False)`
  mid-test. `.clear()`, `.get_requests() -> list[MockRequest]`, `.get_call_count()`,
  `.reset_requests()`.
