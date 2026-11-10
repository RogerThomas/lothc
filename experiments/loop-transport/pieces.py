#!yeet
"""Per-request cost of each piece the POC added, timed in isolation (ns/op, min of 7)."""

import asyncio
import timeit

from msgspec import Struct

from lothc import RequestInfo, Response
from lothc._decode import _decoder_for
from lothc._errors import _validate_raises
from lothc._headers import (
    CaseInsensitiveDict,
    _LazyHeaders,
)

BODY = b'{"status":"success","timestamp":"2026-08-19T12:34:56Z","user":{"id":42}}'


class _Resp(Struct):
    status: str


class _Items:
    def items(self) -> list[tuple[str, str]]:
        return [("content-type", "application/json"), ("content-length", "600"), ("date", "x")]


def _ns(stmt, n: int = 200_000) -> float:
    return min(timeit.repeat(stmt, number=n, repeat=7)) / n * 1e9


async def main() -> None:
    loop = asyncio.get_running_loop()
    dec = _decoder_for(_Resp)
    ri = RequestInfo("GET", "http://h/", "/", "h")
    raw = _Items()
    store = {"content-type": ("content-type", ["application/json"])}
    rows = {
        "loop.create_future()": lambda: loop.create_future(),
        "fut.set_result + done() check": lambda: (
            f := loop.create_future(),
            f.done(),
            f.set_result(1),
        ),
        "call_later(30, cb) + cancel()": lambda: loop.call_later(30.0, print).cancel(),
        "_validate_raises(None) + _decoder_for(T) + .decode": lambda: (
            _validate_raises(None),
            _decoder_for(_Resp).decode,
        ),
        "msgspec decode of body": lambda: dec.decode(BODY),
        "RequestInfo(...) + url format": lambda: RequestInfo("GET", f"http://h{'/'}", "/", "h"),
        "_LazyHeaders(raw)  [Python __init__]": lambda: _LazyHeaders(raw),
        "_LazyHeaders.__new__ + __dict__ set": lambda: (
            o := _LazyHeaders.__new__(_LazyHeaders),
            o.__dict__.__setitem__("_raw_headers", raw),
        ),
        "headers read once (materialize 3)": lambda: _LazyHeaders(raw)["content-type"],
        "eager CaseInsensitiveDict(store) via __new__": lambda: (
            o := CaseInsensitiveDict.__new__(CaseInsensitiveDict),
            o.__dict__.__setitem__("_store", dict(store)),
        ),
        "Response(7 args)  [dataclass __init__]": lambda: Response(
            1, 200, raw, None, ri, "HTTP/1.1", 0.1
        ),
        "Response.__new__ + __dict__.update": lambda: (
            o := Response.__new__(Response),
            o.__dict__.update(
                data=1,
                status=200,
                headers=raw,
                typed_headers=None,
                request=ri,
                http_version="HTTP/1.1",
                elapsed=0.1,
            ),
        ),
        "baseline: empty lambda": lambda: None,
    }
    for name, stmt in rows.items():
        print(f"{_ns(stmt):8.0f} ns  {name}")
