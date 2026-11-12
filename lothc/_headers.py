"""`CaseInsensitiveDict`, the header mapping on responses and errors, and its lazy variant."""

from collections.abc import (
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
)
from typing import (
    TYPE_CHECKING,
    Any,
    Protocol,
    SupportsIndex,
    cast,
)

from pyreqwest.http import HeaderMap

if TYPE_CHECKING:
    # Stub-only (typeshed has no runtime module), and the exact shape `MutableMapping.update`
    # accepts — carrying it here is what lets `update`'s override stay LSP-compatible instead of
    # needing a `reportIncompatibleMethodOverride` suppression.
    from _typeshed import SupportsKeysAndGetItem


# What CaseInsensitiveDict's constructor and update() both accept — mirrors the shapes
# MutableMapping.update itself takes, which is what keeps the override LSP-compatible.
type HeaderSource = Mapping[str, str] | SupportsKeysAndGetItem[str, str] | Iterable[tuple[str, str]]


def _header_pairs(data: HeaderSource) -> Iterable[tuple[str, str]]:
    """Every `(key, value)` pair in `data`, repeating a key once per value it holds.

    `hasattr(data, "keys")` rather than `isinstance(data, Mapping)` is what tells the two
    non-`CaseInsensitiveDict` shapes apart, since `SupportsKeysAndGetItem` is a structural
    protocol with no runtime `isinstance` support — it's also exactly how `dict.update` itself
    decides. Each branch then needs a `cast`, because neither `hasattr` nor the `else` narrows
    the union for a checker.

    Returns an iterable rather than being a generator: handing a mapping's `.items()` straight to
    the caller's loop skips a generator resume per header (~0.35us of ~1.1us for a response's
    headers). A plain `HeaderMap` doesn't come through here at all (`_store_from_header_map`).
    """
    if isinstance(data, CaseInsensitiveDict):
        # `Mapping.items()` is single-valued, so reading another CaseInsensitiveDict through the
        # branch below would drop every repeated header's extra values — the one thing the class
        # exists to keep. Goes through the public `get_all` rather than the other instance's
        # `_store` so there's one definition of "all values for this key", not two.
        return [(key, value) for key in data for value in data.get_all(key)]
    if isinstance(data, Mapping):
        # `.items()`, never `keys()` + `[key]`: pyreqwest's `HeaderMap` is a Mapping whose
        # `keys()` repeats a duplicated name while `[name]` always returns that name's *first*
        # value, so the pair below would yield the same value once per duplicate (caught by
        # `test_get_result_headers_keep_every_value_of_a_repeated_header`). `.items()` is a live
        # view of the real pairs and gets it right.
        return cast("Mapping[str, str]", data).items()
    if hasattr(data, "keys"):
        source = cast("SupportsKeysAndGetItem[str, str]", data)
        return [(key, source[key]) for key in source.keys()]  # noqa: SIM118
    return cast("Iterable[tuple[str, str]]", data)


# `{lowercased key: (key as stored, every value)}`, `CaseInsensitiveDict`'s one field.
type _HeaderStore = dict[str, tuple[str, list[str]]]


class _HeaderItems(Protocol):
    """A response's raw headers: pyreqwest's `HeaderMap`, or the native transport's `RawHeaders`.
    Both list every value as a `(name, value)` pair, names already lowercase (the `http` crate's
    `HeaderName`)."""

    def items(self) -> Iterable[tuple[str, str]]: ...


def _store_from_header_map(headers: _HeaderItems) -> _HeaderStore:
    """`CaseInsensitiveDict`'s store for a response's raw headers (`_HeaderItems`), the shape every
    response hands over. Their names are always lowercase already (the `http` crate's `HeaderName`
    lowercases on parse), so each key is its own lookup key and the `.lower()` per header is
    skipped; with the `_header_pairs` dispatch also skipped, that's ~20% of the conversion (2.48us
    to 1.98us for 13 headers, of which 1.23us is `HeaderMap.items()` itself). `.items()`, never
    `keys()` + `[key]`, for the repeated-name reason `_header_pairs` gives.
    """
    store: _HeaderStore = {}
    for key, value in headers.items():
        if key in store:
            store[key][1].append(value)
        else:
            store[key] = (key, [value])
    return store


class CaseInsensitiveDict(MutableMapping[str, str]):
    """A mapping whose keys compare case-insensitively, as HTTP field names do (RFC 9110 §5.1).

    `Response.headers` and `lothc.testing`'s `MockRequest.headers` are this, so
    `headers["Content-Type"]` and `headers["content-type"]` are the same lookup. Iteration,
    `.keys()` and `dict(...)` yield keys with the casing they were stored under; only lookups,
    `in`, and `==` ignore case — the same split `requests`/`niquests` make.

    Deliberately a `MutableMapping`, not a `dict` subclass: a `dict` subclass can only override
    the Python-level lookups, so `{**headers}` and `dict(headers)` would silently fall back to
    exact-match keys. None of requests, niquests, httpx or httpx2 subclass `dict` here either.
    """

    # Not a @dataclass, unlike most classes here (style-guide.md §1): the one field is a
    # normalized `{lowercased key: (key as stored, every value)}` store rather than anything a
    # caller passes in, and a generated __init__ can't express that translation.
    def __init__(self, data: HeaderSource | None = None) -> None:
        if isinstance(data, HeaderMap):  # not subclassable, so no `.items()` override to fear
            self._store: _HeaderStore = _store_from_header_map(data)
            return
        self._store = {}
        if data is not None:
            # Appending rather than overwriting is what keeps every value of a repeated header
            # reachable via `get_all`. Iterating pairs is required for that — `dict(data)` would
            # throw the extras away — while appending in arrival order keeps `__getitem__` on the
            # *first* value, which is what a plain `dict(raw_response.headers)` always returned.
            store = self._store
            for key, value in _header_pairs(data):
                lowered = key.lower()
                if lowered in store:
                    store[lowered][1].append(value)
                else:
                    store[lowered] = (key, [value])

    def __getitem__(self, key: str) -> str:
        return self._store[key.lower()][1][0]

    def __setitem__(self, key: str, value: str) -> None:
        # Replaces every value for that name, matching pyreqwest `HeaderMap.__setitem__`'s own
        # documented behaviour rather than quietly appending to a name that already has values.
        self._store[key.lower()] = (key, [value])

    def __delitem__(self, key: str) -> None:
        del self._store[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def __eq__(self, other: object) -> bool:
        # Against another CaseInsensitiveDict, every value counts — two responses whose only
        # difference is a dropped `set-cookie` must not compare equal. Against any other mapping
        # only the single-valued view can be compared, since that's all the other side holds.
        if isinstance(other, CaseInsensitiveDict):
            mine = {key: values for key, (_, values) in self._store.items()}
            theirs = {key: values for key, (_, values) in other._store.items()}
            return mine == theirs
        if not isinstance(other, Mapping):
            return NotImplemented
        other_items = cast("Mapping[str, str]", other).items()
        lowered = {str(key).lower(): value for key, value in other_items}
        return {key: values[0] for key, (_, values) in self._store.items()} == lowered

    def __repr__(self) -> str:
        # Falls back to the pair-list form (which the constructor also accepts, so either form
        # round-trips) as soon as any name is repeated — a repr rendering three `set-cookie`
        # values as one dict entry would hide exactly what this class exists to keep.
        if any(len(values) > 1 for _, values in self._store.values()):
            return f"{type(self).__name__}({list(_header_pairs(self))!r})"
        return f"{type(self).__name__}({dict(self.items())!r})"

    def update(
        self,
        data: HeaderSource = (),
        /,
        **kwargs: str,
    ) -> None:
        """Merge `data` in, replacing every value of each name it carries and keeping the rest.

        Overridden because `MutableMapping.update`'s own mixin assigns one value per key, so
        merging another `CaseInsensitiveDict` through it would drop a repeated header's extra
        values — the same trap `__init__` has to avoid.
        """
        replaced: set[str] = set()
        for key, value in _header_pairs(data):
            lowered = key.lower()
            if lowered in replaced:
                self._store[lowered][1].append(value)
            else:
                replaced.add(lowered)
                self._store[lowered] = (key, [value])
        for key, value in kwargs.items():
            self._store[key.lower()] = (key, [value])

    def copy(self) -> "CaseInsensitiveDict":
        """An independent copy, repeated header values included.

        Present because swapping a plain `dict` for a `MutableMapping` otherwise takes
        `dict.copy` away, and it's the natural way to get a mutable snapshot — `requests`'s own
        `CaseInsensitiveDict` carries it for the same reason. `dict`'s `|` merge operator has no
        equivalent here (nor in requests); build a new one from `_header_pairs` if you need it.
        """
        return CaseInsensitiveDict(self)

    def get_all(self, key: str) -> list[str]:
        """Every value sent under `key`, in arrival order — `[]` if the header wasn't sent.

        `headers[key]` gives only the first, which is the wrong answer for a genuinely repeatable
        field like `set-cookie`. The returned list is a copy, so mutating it changes nothing here.
        """
        lowered = key.lower()
        if lowered not in self._store:
            return []
        return list(self._store[lowered][1])


class _LazyHeaders(CaseInsensitiveDict):
    """`Response.headers` as handed out: a `CaseInsensitiveDict` holding the response's raw headers
    (pyreqwest's `HeaderMap`, or the native transport's `RawHeaders`) until something first reads
    them, since most callers never do and converting cost ~15% of throughput (see CLAUDE.md).

    `_store` is deliberately left unset, so the first method that touches it, whichever it is,
    lands in `__getattr__`, which builds it and turns this object into a plain
    `CaseInsensitiveDict` for good: from then on there's no laziness left to pay for, and
    `type()`, `repr()`, pickling and copying are exactly a `CaseInsensitiveDict`'s. The object
    itself never changes, so repeated `.headers` reads are the same object and mutations stick.

    Holding the `HeaderMap` keeps only the headers alive, not the response, which caches its body
    (measured over 10MB bodies: holding 40 or 150 maps peaked at ~100MB RSS against ~85MB for
    holding nothing, while holding 40 responses peaked at ~470MB). It reads fine from any thread
    and after the client has closed.
    """

    def __init__(self, raw_headers: _HeaderItems) -> None:  # pylint: disable=super-init-not-called
        # No `super().__init__()`: building `_store` is precisely the work being deferred.
        self._raw_headers = raw_headers

    def __getattr__(self, name: str) -> _HeaderStore:
        # Only reached for an attribute normal lookup didn't find, so it costs nothing once
        # `_store` exists. Atomic `__dict__` operations, in this order (store set before the
        # source goes), so two threads making the first read at once return the same store.
        if name != "_store":
            raise AttributeError(name)
        state = self.__dict__
        raw_headers = state.get("_raw_headers")
        if raw_headers is not None:
            state.setdefault("_store", _store_from_header_map(raw_headers))
            state.pop("_raw_headers", None)
            object.__setattr__(self, "__class__", CaseInsensitiveDict)
        return state["_store"]

    def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[Any, ...]:
        # `pickle`/`copy` look this up on the class, which `__getattr__` never sees, so
        # materialize first: they then capture a plain `CaseInsensitiveDict`, never the
        # `HeaderMap` (`object`'s, since `super()` no longer applies once the class has changed).
        _ = self._store
        return object.__reduce_ex__(self, protocol)
