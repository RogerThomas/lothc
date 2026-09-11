---
name: write-python-code
description: Python code-style conventions for this repo (lothc) — load before writing or editing any Python code here (lothc/*.py, tests/*.py, examples/*.py, etc.). Covers dataclass/DTO shape, no-nested-defs, private-helper ordering, testing conventions, and when to read style-guide.md in full.
---

# Writing Python code in lothc

Before editing `lothc/_client.py` specifically (or any file whose conventions you're unsure of),
**read `./style-guide.md` in full first** — its rules (overload-pairs-over-casts, the type alias
vocabulary, naming rules) are load-bearing; deviating from them silently reintroduces bugs this
project has already paid to fix once. Tell the user you've read `CLAUDE.md` and
`./style-guide.md` before starting code changes on that file.

The rules below are the ones that come up everywhere in this repo, summarized so you don't have to
re-derive them each time. `style-guide.md` has the full detail and worked examples for each.

## Structure

- **Prefer `@dataclass` for classes**, even ones that aren't pure data containers — reduced
  boilerplate wins over dataclass "purity". Construct with positional args
  (`Controller(service1, service2)`), not `service1=service1`.
- **`@dataclass(slots=True)` for internal DTOs** (data passed between layers/helpers, not
  validated at an I/O boundary) — faster and smaller than a plain dataclass. Reach for pydantic
  only at real I/O boundaries (request/response models, external payloads, config).
- **Don't reach for `frozen=True`** on a dataclass that has any mutable-typed field (a `dict`,
  `list`, etc.) — it can't actually deliver immutability or hashability for that field, just a
  confusing partial version of both. A plain mutable `@dataclass(slots=True)` is more honest than
  a `frozen=True` that doesn't fully work.
- **No nested (`def` inside `def`) functions, ever** — not even a small closure like a worker loop
  or a one-off wrapper. Pull it out to a module-level `_`-prefixed function and pass in whatever
  state it needs as explicit parameters. If you need to bind some of those parameters ahead of
  time (e.g. to match a callback signature a library expects), use `functools.partial`, not a
  closure.
- **`_`-prefix every private/module-internal helper.** Only the actual public entry points
  (a class's public methods, a script's `main`) stay unprefixed.
- **Place private methods/functions before the public ones that call them** — all of a class's
  privates in one block, before any public method (not just "somewhere above its one caller").
  Interleaving public/private/public is exactly what this rule exists to prevent.
- **A public method never calls another public method on the same class.** Route shared behavior
  through a private method that both call — keeps each public method's behavior independent of
  its siblings (mocking/overriding one can't silently change another).
- **No `UPPER_CASE` module-level globals for lookup tables** (e.g. a dict mapping a CLI choice to
  a function) — build them as local lowercase dicts inside the function that uses them. Plain
  lowercase module-level *state* (a shared client instance, a cache) is fine; the objection is to
  `UPPER_CASE` constants/tables specifically, not to all globals.
- **`dict.get` only for a genuinely optional field with a real default.** If the code can't
  meaningfully continue without a key, index directly (`d["key"]`) and let a missing key raise
  `KeyError` — don't manually re-raise a more "helpful" error, it just swallows the real traceback.
- **Keep `try`/`except` blocks to the minimum lines that can actually raise** — never let
  surrounding code that can't raise sit inside the block, so a bug there doesn't get misreported
  as the exception you meant to catch. If the risky code can't be reduced to a line or two,
  extract it into a helper and wrap the call, not the inline code.

## Testing

- **Use `create_autospec(Thing, spec_set=True, instance=True)`** for mocks, never bare
  `MagicMock()` — catches typos in attribute/method names and wrong call signatures at test time.
- **Never test private methods/functions directly** — no calling one on an instance, no patching a
  private attribute to observe it, no importing a private module-level function to unit-test in
  isolation. Every test goes through the public interface and asserts on externally-observable
  outcomes. If a private method's logic is complex enough to want its own tests, that's a sign it
  should be its own public class/function, not a reason to reach into the private one.
- **Simple, obvious test values, not pseudo-realistic ones** (`"first-name"`, not `"John"`; a
  plain string, not a UUID, unless the code actually validates the shape). Convention: the
  kebab-case form of the variable name (`token_id` → `"token-id"`). Inline these directly at the
  call site rather than extracting them into variables/fixtures — reserve fixtures for values that
  are genuinely non-trivial to construct or shared across many tests.
- **Prefer passing a fixture name (`str`) over raw bytes** when a test failure would otherwise
  dump a huge byte blob into the pytest output — load the fixture value inside the test body via
  `request.getfixturevalue(fixture_name)`.

## Project-specific (lothc's own, beyond general Python style)

- **`lothc`'s public API (including `lothc.testing`) never exposes a pyreqwest type** — no
  pyreqwest class a caller has to import, construct, or receive, and no builder pattern for
  anything public-facing. Wrap it in a plain lothc-native dataclass instead (see `MockRequest`/
  `MockResponse` in `lothc/testing.py` for the pattern). See `CLAUDE.md`'s dev notes for the full
  reasoning and history.
- **Overload-pairs over a single generic-with-cast signature** for the main client's verb methods
  — see `style-guide.md` and `CLAUDE.md`'s "Architecture" section for why.
- **basedpyright strict mode is the contract.** Every change must pass `task typecheck` with zero
  errors and, ideally, zero new `cast(...)` calls. `tests/` additionally needs to type-check
  cleanly under mypy, ty, and zuban — see `CLAUDE.md`'s Testing section for the bar on when a
  checker-specific `ignore` comment is actually justified versus a real fix being required.
