"""Runs ONE benchmark method from an asv_bench/ suite in complete isolation (its own process, so
`ru_maxrss` reflects only this one call) and prints its `{"time": ..., "peakmem": ...}` as JSON.
Invoked as a subprocess by `quick_check.py` — not meant to be run directly.
"""

import importlib
import json
import resource
import sys
import time


def main() -> None:
    module_name, class_name, method_name = sys.argv[1], sys.argv[2], sys.argv[3]
    suite_cls = getattr(importlib.import_module(module_name), class_name)
    suite = suite_cls()

    if hasattr(suite_cls, "setup_cache"):
        cache = suite.setup_cache()
        args: tuple[object, ...] = (cache,)
        suite.setup(cache)
    else:
        args = ()
        suite.setup()

    method = getattr(suite, method_name)
    repeat = getattr(suite_cls, "repeat", 1) or 1
    try:
        timings: list[float] = []
        for _ in range(repeat):
            start = time.perf_counter()
            method(*args)
            timings.append(time.perf_counter() - start)
        elapsed = min(timings)
    finally:
        suite.teardown(*args)

    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps({"time": elapsed, "peakmem": peak_rss}))


if __name__ == "__main__":
    main()
