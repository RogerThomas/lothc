# API findings — tracking (scratch, delete when done)

From the API-consistency audit + coverage-review forks earlier this session.

## Done

- [x] Rename `Json` type alias → `JSONPayload`. Leave `class JSON` unchanged.
      29 usages renamed across both clients; `lothc/__init__.py` updated; `task check`/
      `task test` pass.
- [x] Fix `RedirectError` exception leak — widened `_translate_transport_error` +
      all 8 catch sites to also catch `pyreqwest.exceptions.RedirectError`, mapped to
      `HTTPTransportError` (no new exception class added). New tests in
      `tests/test_transport_errors.py` (async + sync) via a `/redirect-loop` route.

## Queued (same file — run after the above finishes to avoid edit collisions)

- [ ] Swap `AsyncAuthProvider`/`AuthProvider` naming to match the rest of the codebase's
      "async = bare, sync = `Sync`-prefixed" convention (`HTTPClient`/`SyncHTTPClient`):
      - `AsyncAuthProvider = Callable[[], Awaitable[str]]` → `AuthProvider` (bare, async)
      - `AuthProvider = Callable[[], str]` → `SyncAuthProvider` (sync, prefixed)
      Touches `lothc/_client.py:60-61,456,465,1224,1233`, `lothc/__init__.py` exports,
      `docs/auth.md` if it names these types.

## Queued (small, low-risk — fold into next batch)

- [ ] `_decode_json_line`'s fallback error hardcodes "SSE" despite being shared by
      `sse()` AND `stream_get`/`stream_post` (`lothc/_client.py:313`, called from
      692/1463 for SSE and 799/801/1570/1572 for the non-SSE streaming verbs). Fix:
      drop "SSE" from the message — `f"Unsupported response_data_type: {response_data_type!r}"`.

## Not yet actioned — needs a decision

- [ ] `TypedHeaders` vs `Headers` naming hygiene — lower confidence finding.
- [ ] Only `get`/`head` have a status+headers-returning variant (`get_result`);
      `post`/`put`/`patch`/`delete` don't — lower confidence, possibly intentional scope.
- [ ] `form=` silently drops unsupported value types instead of raising — reported by the
      coverage fork, not yet independently verified.
