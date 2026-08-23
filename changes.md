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
- [x] Swap `AsyncAuthProvider`/`AuthProvider` naming to match the rest of the codebase's
      "async = bare, sync = `Sync`-prefixed" convention. `type AuthProvider = Callable[[],
      Awaitable[str]]` (async, was `AsyncAuthProvider`), `type SyncAuthProvider = Callable[[],
      str]` (sync, was bare `AuthProvider`). Updated `lothc/_client.py` (2 type statements + 4
      usage sites) and `lothc/__init__.py` exports. `docs/auth.md` and tests didn't reference
      the alias names directly, so no changes needed there. `task check`/`task test` pass.
- [x] `_decode_json_line`'s fallback error hardcoded "SSE" despite being shared by `sse()`
      AND `stream_get`/`stream_post`. Dropped "SSE" from the message in `lothc/_client.py`;
      updated the one test asserting the old wording (`tests/test_streaming.py`, which was
      itself testing `stream_get`, not SSE — a live example of the exact confusion this
      fixed). `task check`/`task test` pass.
- [x] Added `post_result`/`put_result`/`patch_result`/`delete_result` on both `HTTPClient` and
      `SyncHTTPClient`, mirroring `get_result`'s 4-overload shape (bare/`response_data_type`/
      `response_headers_type`/both), with `json`/`form`/`content` added for post/put/patch
      (delete_result has none, matching `delete`'s own shape). Added a private
      `_send_with_body_result` helper on each client (right after the existing
      `_send_with_body`), reusing `_attach_body`/`_check_status`/`_decode_body`/
      `_parse_typed_headers` — no duplicated logic. 14 new tests across `tests/test_write.py`
      (post/put/patch_result) and `tests/test_delete_head.py` (delete_result), covering status +
      decoded body, `response_headers_type` parsing, and one `error_for_status=False` case.
      Documented in `docs/verbs.md`.
- [x] `form=` silently dropping unsupported value types — **confirmed real** by reading
      `_build_form`/`_build_sync_form`'s `match` statement: no `case _`, so an unmatched value
      just fell through the loop with no error and no effect. Fixed by extracting
      `_apply_form_value`/`_apply_sync_form_value` (parameter typed `object`, same technique as
      `_validate_response_data_type`) with a `case _: raise TypeError(...)` fallback naming the
      field, value, and type. Also tightened the file-tuple case to `case (str() as filename,
      bytes() as content):` so a malformed tuple shape now raises too, instead of matching
      loosely. Found and fixed a pre-existing test (`tests/test_write.py`) that had encoded the
      old silent-drop as expected behavior — inverted its assertion to expect the raise, and
      added a sync mirror.

- [x] `Form`/`File` redesign — added multipart content-type control and repeated part names
      (see `form-file-plan.md`/`idea.py`, both deleted now that this landed). `File` gained two
      new tuple shapes (`(filename, content, content_type)` explicit override, `(Path,
      content_type)` keeps the path's auto-derived filename but overrides mime) plus
      `io.BufferedIOBase` support; a new `post`/`put`/`patch`/`stream_post` param
      `infer_mime_type_from_file_extension: bool = True` controls stdlib-`mimetypes`-based
      auto-inference from the filename (never sniffs byte content) — set `False` to send no
      `Content-Type` at all unless one's given explicitly. `Form`'s value type now also accepts
      `list[Any]`/`dict`/`BaseModel`/`Struct` (JSON-encoded as that one part's body,
      `application/json`) and a `tuple` of any supported value (repeats that part name once per
      element — the outer `list` vs `tuple` distinction is what keeps "JSON-array body" and
      "repeat this name" from colliding). Kept public names `File`/`Form` unchanged (no
      `FormFile`/`FormValue` export) and kept bare `bytes`/`BufferedIOBase` support exactly as
      before — both confirmed via `AskUserQuestion` rather than assumed. New private helpers:
      `_guess_form_part_mime`, `_encode_json_form_part`, `_finish_file_part`,
      `_build_file_part`/`_build_sync_file_part`, `_buffered_io_filename`. `tests/_server.py`'s
      `/upload` handler gained a `parts` list (every part in wire order + its own Content-Type
      header) alongside the existing `fields`/`files`, additive — no existing assertion changed.
      19 new tests in `tests/test_write.py`; the one pre-existing test asserting `[1, 2, 3]` was
      unsupported was updated to use a `set` instead (a list is now a valid, JSON-encoded `Form`
      value) — same pattern as the earlier `form=` silent-drop fix. `docs/verbs.md`'s multipart
      section rewritten to document all of the above. `task check`/`task test` both pass (181
      tests total).

Final state after all of the above: `task check`/`task test` both pass, 181 tests total.

## Decided: not worth fixing

- [x] `TypedHeaders` vs `Headers` naming hygiene — grounded against the code: `TypedHeaders`
      is a legitimate narrowing of `Headers` (drops the raw `Mapping` option), the real mismatch
      is `TypedHeaders` vs `Data` (same conceptual role, different naming philosophy) — but every
      alternative name is worse than the status quo. Leaving as-is.
