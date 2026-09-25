"""`HTTPClient`/`SyncHTTPClient` (and the other async/sync pairs) are mirrored by hand, so these
tests keep them in step, at two levels:

- **Public API**: every public method (and constructor, context-manager dunder, `__call__` and
  property) exists on both sides with the same overloads, parameter names, kinds, defaults and
  annotations, once the async vocabulary is mapped onto the sync one (`_desync`).
- **Implementation**: every method body (private ones included, and module-level function pairs
  like `_send`/`_send_sync`) is the same once `async`/`await` are stripped and the same vocabulary
  is mapped. This reads lothc's *source text* via `ast`, never calls or imports a private name.

Anything that legitimately differs is listed in an allowlist below with the reason, and an
allowlist entry that no longer differs fails too, so the lists can't silently go stale.
"""

import ast
import difflib
import inspect
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import get_overloads, overload

import pytest

from lothc import (
    AuthProvider,
    HTTPClient,
    OAuthProvider,
    SyncAuthProvider,
    SyncHTTPClient,
    SyncOAuthProvider,
)


@dataclass(slots=True)
class _ImplPair:
    label: str
    async_source: str
    sync_source: str


def _sync_only_parameters() -> dict[str, frozenset[str]]:
    # `interruptible=` runs a blocking sync read on a worker thread so Ctrl-C can land; an async
    # read is already cancellable, so there's nothing for the async client to opt into.
    interruptible = frozenset({"interruptible"})
    return {"sse": interruptible, "stream_get": interruptible, "stream_post": interruptible}


def _differing_implementations() -> dict[str, str]:
    reads_through_sync_stream_chunks = (
        "sync reads through `_sync_stream_chunks` (a worker thread whenever a stall must be "
        "interruptible or bounded); async wraps each inline read in `asyncio.timeout` instead"
    )
    lazy_loop_lock = (
        "async creates its `asyncio.Lock` lazily per running loop (a loop-bound lock breaks "
        "across `asyncio.run()` calls); sync just holds a `threading.Lock`"
    )
    return {
        "HTTPClient._download": reads_through_sync_stream_chunks,
        "HTTPClient._line_stream": reads_through_sync_stream_chunks,
        "HTTPClient._sse_connection": reads_through_sync_stream_chunks,
        "OAuthProvider.__init__": lazy_loop_lock,
        "OAuthProvider.__call__": lazy_loop_lock,
    }


def _one_sided_methods() -> dict[str, str]:
    return {"OAuthProvider._get_lock": "the lazy per-loop `asyncio.Lock` (see `__call__`)"}


def _strip_awaitable(text: str) -> str:
    """`Awaitable[X]` -> `X`, bracket-matched so `X` may itself be subscripted, and taking any
    module qualifier (`collections.abc.Awaitable[...]`, from a `repr`) with it."""
    while (match := re.search(r"\b(?:[\w.]+\.)?Awaitable\[", text)) is not None:
        depth = 1
        end = match.end()
        while depth:
            depth += {"[": 1, "]": -1}.get(text[end], 0)
            end += 1
        text = text[: match.start()] + text[match.end() : end - 1] + text[end:]
    return text


def _desync(text: str) -> str:
    """Map both halves of a pair onto one vocabulary: drop the async markers and the `Sync`/`sync`
    naming markers, so `SyncHTTPClient`, `RawSyncResponse`, `_send_sync` and `__aenter__` read the
    same as their async twins."""
    replacements = [
        (r"\bAsync(Iterator|Generator|Iterable|ExitStack)\b", r"\1"),
        (r"\bAbstractAsyncContextManager\b", "AbstractContextManager"),
        (r"\basyncio\.sleep\b", "time.sleep"),
        (r"\baclos(ing|e)\b", r"clos\1"),
        (r"\benter_async_context\b", "enter_context"),
        (r"\b__a(enter|exit|iter|next)__\b", r"__\1__"),
        (r"\basync (def|for|with)\b", r"\1"),
        (r"\bawait ", ""),
        (r"Sync(?=[A-Z])", ""),
        (r"_sync_", "_"),
        (r"\bsync_", ""),
        (r"_sync\b", ""),
    ]
    text = _strip_awaitable(text)
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text


def _paired_members(cls: type) -> dict[str, object]:
    """The members parity is about: public names plus the dunders a caller actually uses, keyed
    by their desynced name so `__aenter__` pairs with `__enter__`."""
    dunders = {"__init__", "__call__", "__aenter__", "__aexit__", "__enter__", "__exit__"}
    return {
        _desync(name): member
        for name, member in vars(cls).items()
        if not name.startswith("_") or name in dunders
    }


def _signatures(member: object) -> list[inspect.Signature]:
    function = member.fget if isinstance(member, property) else member
    assert callable(function)
    candidates: list[Callable[..., object]] = [*get_overloads(function), function]
    return [inspect.signature(each) for each in candidates]


def _without(signature: inspect.Signature, dropped: frozenset[str]) -> inspect.Signature:
    return signature.replace(
        parameters=[p for p in signature.parameters.values() if p.name not in dropped]
    )


def _api_mismatches(
    async_cls: type, sync_cls: type, sync_only: Mapping[str, frozenset[str]]
) -> list[str]:
    """Every public-API difference between an async class and its sync twin, as readable lines."""
    async_members = _paired_members(async_cls)
    sync_members = _paired_members(sync_cls)
    problems = [
        f"only on {async_cls.__name__}: {name}"
        for name in async_members.keys() - sync_members.keys()
    ]
    problems += [
        f"only on {sync_cls.__name__}: {name}"
        for name in sync_members.keys() - async_members.keys()
    ]
    for name in sorted(async_members.keys() & sync_members.keys()):
        problems += _member_mismatches(
            name, async_members[name], sync_members[name], sync_only.get(name, frozenset())
        )
    return sorted(problems)


def _member_mismatches(
    name: str, async_member: object, sync_member: object, sync_only: frozenset[str]
) -> list[str]:
    if inspect.iscoroutinefunction(sync_member) or inspect.isasyncgenfunction(sync_member):
        return [f"{name}: the sync side is `async`"]
    async_signatures = _signatures(async_member)
    sync_signatures = _signatures(sync_member)
    if len(async_signatures) != len(sync_signatures):
        return [
            f"{name}: {len(async_signatures)} vs {len(sync_signatures)} overloads/implementation"
        ]
    problems: list[str] = []
    for async_signature, sync_signature in zip(async_signatures, sync_signatures, strict=True):
        missing = sync_only - sync_signature.parameters.keys()
        if missing:
            problems.append(f"{name}: allowlisted sync-only {sorted(missing)} no longer exists")
        expected = _desync(str(async_signature))
        actual = _desync(str(_without(sync_signature, sync_only)))
        if expected != actual:
            problems.append(f"{name}:\n  async {expected}\n  sync  {actual}")
    return problems


def _drop_parameter(tree: ast.AST, name: str) -> None:
    """Remove a keyword-only parameter, and every `name=` keyword passed along, from a tree."""
    for node in ast.walk(tree):
        if isinstance(node, ast.arguments):
            kept = [
                (arg, default)
                for arg, default in zip(node.kwonlyargs, node.kw_defaults, strict=True)
                if arg.arg != name
            ]
            node.kwonlyargs = [arg for arg, _ in kept]
            node.kw_defaults = [default for _, default in kept]
        elif isinstance(node, ast.Call):
            node.keywords = [keyword for keyword in node.keywords if keyword.arg != name]


def _drop_docstrings(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and ast.get_docstring(node) is not None
        ):
            node.body = node.body[1:] or [ast.Pass()]


def _normalized_source(definitions: list[ast.stmt]) -> str:
    """One method's overloads plus implementation, docstrings and comments dropped, desynced, and
    re-parsed so that e.g. `(await x).y` -> `(x).y` prints the same as the sync `x.y`."""
    texts: list[str] = []
    for definition in definitions:
        tree = ast.parse(ast.unparse(definition))
        _drop_docstrings(tree)
        # Where `interruptible=` may appear publicly is pinned by the API test; here it's only
        # threaded through to the worker-thread reader, so it's noise in every body it touches.
        _drop_parameter(tree, "interruptible")
        texts.append(ast.unparse(ast.parse(_desync(ast.unparse(tree)))))
    return "\n".join(texts)


def _methods(cls: ast.ClassDef) -> dict[str, list[ast.stmt]]:
    methods: dict[str, list[ast.stmt]] = {}
    for node in cls.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            methods.setdefault(_desync(node.name), []).append(node)
    return methods


def _definition_pairs(source: str) -> Iterator[tuple[str, ast.stmt, ast.stmt]]:
    """Every top-level (async name, async node, sync node) whose sync name desyncs to the other."""
    module = ast.parse(source)
    definitions = {
        node.name: node
        for node in module.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    for name, node in definitions.items():
        twin = _desync(name)
        if twin != name and twin in definitions:
            yield twin, definitions[twin], node


def _implementation_pairs(source: str) -> tuple[list[_ImplPair], list[str]]:
    """Normalized (async, sync) source for every paired function and paired class method, plus
    the labels of methods that exist on only one side of a class pair."""
    pairs: list[_ImplPair] = []
    one_sided: list[str] = []
    for name, async_node, sync_node in _definition_pairs(source):
        if isinstance(async_node, ast.ClassDef) and isinstance(sync_node, ast.ClassDef):
            async_methods, sync_methods = _methods(async_node), _methods(sync_node)
            one_sided += [f"{name}.{m}" for m in async_methods.keys() ^ sync_methods.keys()]
            pairs += [
                _ImplPair(
                    f"{name}.{method}",
                    _normalized_source(async_methods[method]),
                    _normalized_source(sync_methods[method]),
                )
                for method in sorted(async_methods.keys() & sync_methods.keys())
            ]
        else:
            pairs.append(
                _ImplPair(name, _normalized_source([async_node]), _normalized_source([sync_node]))
            )
    return pairs, one_sided


def _lothc_sources() -> list[str]:
    # A list, not a set: parametrize ids must come out in the same order on every run.
    modules = [inspect.getmodule(HTTPClient), inspect.getmodule(OAuthProvider)]
    return [inspect.getsource(module) for module in modules if module is not None]


def _collect_lothc_implementation_pairs() -> tuple[list[_ImplPair], list[str]]:
    pairs: list[_ImplPair] = []
    one_sided: list[str] = []
    for source in _lothc_sources():
        module_pairs, module_one_sided = _implementation_pairs(source)
        pairs += module_pairs
        one_sided += module_one_sided
    return pairs, sorted(one_sided)


lothc_implementation_pairs, lothc_one_sided_methods = _collect_lothc_implementation_pairs()


# --- Public API ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("async_cls", "sync_cls"),
    [
        (HTTPClient, SyncHTTPClient),
        (OAuthProvider, SyncOAuthProvider),
    ],
    ids=lambda cls: cls.__name__,
)
def test_public_api_matches_its_sync_twin(async_cls: type, sync_cls: type) -> None:
    sync_only = _sync_only_parameters() if async_cls is HTTPClient else {}

    assert _api_mismatches(async_cls, sync_cls, sync_only) == []


def test_auth_provider_aliases_match() -> None:
    assert _desync(repr(AuthProvider.__value__)) == _desync(repr(SyncAuthProvider.__value__))


# --- Implementation -----------------------------------------------------------------------------


def test_implementation_pairs_were_discovered() -> None:
    # Guards the discovery itself: a naming change that stopped pairs being found would otherwise
    # make every implementation test below vanish rather than fail.
    labels = {pair.label for pair in lothc_implementation_pairs}

    assert {
        "HTTPClient.get",
        "HTTPClient.__enter__",
        "HTTPClient.post",
        "HTTPClient._send_with_body",
        "_RetryMiddleware.__call__",
        "_ReauthMiddleware.__call__",
        "OAuthProvider._renew",
        "_send",
        "_attach_body",
    } <= labels


def test_paired_classes_define_the_same_methods() -> None:
    assert lothc_one_sided_methods == sorted(_one_sided_methods())


@pytest.mark.parametrize("pair", lothc_implementation_pairs, ids=lambda pair: pair.label)
def test_implementation_matches_its_sync_twin(pair: _ImplPair) -> None:
    if pair.label in _differing_implementations():
        assert pair.async_source != pair.sync_source, (
            f"{pair.label} now matches its sync twin: drop it from _differing_implementations()"
        )
    else:
        diff = difflib.unified_diff(
            pair.async_source.splitlines(), pair.sync_source.splitlines(), "async", "sync", n=1
        )
        assert pair.async_source == pair.sync_source, "\n".join(diff)


def test_every_allowlisted_implementation_exists() -> None:
    labels = {pair.label for pair in lothc_implementation_pairs}

    assert _differing_implementations().keys() <= labels


# --- The comparison helpers catch drift (toy classes, not lothc) --------------------------------


@pytest.mark.parametrize(
    ("async_text", "sync_text"),
    [
        ("AsyncIterator[SSEEvent[str]]", "Iterator[SSEEvent[str]]"),
        (
            "Callable[[], collections.abc.Awaitable[dict[str, int]]]",
            "Callable[[], dict[str, int]]",
        ),
        ("_RetryMiddleware(RawResponse)", "_SyncRetryMiddleware(RawSyncResponse)"),
        ("_stream_chunks(_build_form(x))", "_sync_stream_chunks(_build_sync_form(x))"),
        ("await _read_body(x)", "_read_body_sync(x)"),
        (
            "async with AsyncExitStack() as s: await asyncio.sleep(1)",
            "with ExitStack() as s: time.sleep(1)",
        ),
    ],
)
def test_desync_maps_both_vocabularies_onto_one(async_text: str, sync_text: str) -> None:
    assert _desync(async_text) == _desync(sync_text)


class _ToyClient:
    @overload
    async def get(self, path: str) -> bytes: ...
    @overload
    async def get(self, path: str, *, as_text: bool) -> str: ...
    async def get(self, path: str, *, as_text: bool = False) -> bytes | str:
        return path if as_text else path.encode()

    async def post(self, path: str, *, timeout: float = 30.0) -> Iterator[bytes]:
        return iter([f"{path}:{timeout}".encode()])


class _SyncToyClient:
    @overload
    def get(self, path: str) -> bytes: ...
    @overload
    def get(self, path: str, *, as_text: bool) -> str: ...
    def get(self, path: str, *, as_text: bool = False) -> bytes | str:
        return path if as_text else path.encode()

    def post(self, path: str, *, timeout: float = 30.0) -> Iterator[bytes]:
        return iter([f"{path}:{timeout}".encode()])


class _SyncToyClientMissingParameter:
    @overload
    def get(self, path: str) -> bytes: ...
    @overload
    def get(self, path: str, *, as_text: bool) -> str: ...
    def get(self, path: str, *, as_text: bool = False) -> bytes | str:
        return path if as_text else path.encode()

    def post(self, path: str) -> Iterator[bytes]:
        return iter([path.encode()])


class _SyncToyClientChangedDefault:
    @overload
    def get(self, path: str) -> bytes: ...
    @overload
    def get(self, path: str, *, as_text: bool) -> str: ...
    def get(self, path: str, *, as_text: bool = False) -> bytes | str:
        return path if as_text else path.encode()

    def post(self, path: str, *, timeout: float = 60.0) -> Iterator[bytes]:
        return iter([f"{path}:{timeout}".encode()])


class _SyncToyClientMissingMethod:
    @overload
    def get(self, path: str) -> bytes: ...
    @overload
    def get(self, path: str, *, as_text: bool) -> str: ...
    def get(self, path: str, *, as_text: bool = False) -> bytes | str:
        return path if as_text else path.encode()


def test_api_comparison_accepts_a_faithful_twin() -> None:
    assert _api_mismatches(_ToyClient, _SyncToyClient, {}) == []


@pytest.mark.parametrize(
    ("sync_cls", "expected"),
    [
        (_SyncToyClientMissingParameter, "post:"),
        (_SyncToyClientChangedDefault, "timeout: float = 60.0"),
        (_SyncToyClientMissingMethod, "only on _ToyClient: post"),
    ],
    ids=["missing-parameter", "changed-default", "missing-method"],
)
def test_api_comparison_catches_drift(sync_cls: type, expected: str) -> None:
    problems = _api_mismatches(_ToyClient, sync_cls, {})

    assert len(problems) == 1
    assert expected in problems[0]


def test_api_comparison_flags_a_stale_sync_only_allowlist_entry() -> None:
    problems = _api_mismatches(_ToyClient, _SyncToyClient, {"post": frozenset({"interruptible"})})

    assert problems == ["post: allowlisted sync-only ['interruptible'] no longer exists"]


_toy_async_source = """
class Client:
    async def fetch(self, path, *, interruptible=None):
        '''Async docstring.'''
        async with self._open(path) as response:  # a comment
            await asyncio.sleep(0.1)
            return (await response.read()).decode()
"""


_toy_sync_source = """
class SyncClient:
    def fetch(self, path, *, interruptible=False):
        '''Sync docstring, worded differently.'''
        with self._open(path) as response:
            time.sleep(0.1)
            return response.read().decode()
"""


def test_implementation_comparison_accepts_a_faithful_twin() -> None:
    (pair,), one_sided = _implementation_pairs(_toy_async_source + _toy_sync_source)

    assert one_sided == []
    assert pair.label == "Client.fetch"
    assert pair.async_source == pair.sync_source


def test_implementation_comparison_catches_a_drifted_body() -> None:
    drifted = _toy_sync_source.replace("time.sleep(0.1)", "time.sleep(0.2)")

    (pair,), _ = _implementation_pairs(_toy_async_source + drifted)

    assert pair.async_source != pair.sync_source


def test_implementation_comparison_reports_a_method_missing_from_one_side() -> None:
    extra = "    def close(self):\n        pass\n"

    _, one_sided = _implementation_pairs(_toy_async_source + _toy_sync_source + extra)

    assert one_sided == ["Client.close"]
