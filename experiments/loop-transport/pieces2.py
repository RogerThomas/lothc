#!yeet
"""What params/headers cost on the Python side today (ns/op, min of 7)."""

import timeit

from lothc._encode import (
    _encode_headers,
    _encode_params,
    _query_pairs,
)


def _ns(stmt, n: int = 300_000) -> float:
    return min(timeit.repeat(stmt, number=n, repeat=7)) / n * 1e9


def main() -> None:
    params, headers = {"page": 1, "tag": "a"}, {"x-id": "1"}
    print(
        f"{_ns(lambda: _query_pairs(_encode_params(params))):6.0f} ns  _query_pairs(_encode_params(params))"
    )
    print(f"{_ns(lambda: _encode_headers(headers)):6.0f} ns  _encode_headers(headers)")
    print(
        f"{_ns(lambda: type(params) is dict and type(headers) is dict):6.0f} ns  type(x) is dict checks"
    )
