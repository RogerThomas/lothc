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
    # This tool's whole point is a fast dev-loop signal, not asv's own statistical rigor, but
    # sub-millisecond operations (a local HTTP round-trip) are dominated by noise at low sample
    # counts -- especially the very first call, which pays a real TCP-connection-setup cost later
    # calls don't. Many timed samples (min-of-N, matching timeit's own approach to filtering
    # scheduler-noise spikes) fixes that -- independent of the suite class's own `repeat`
    # attribute, which is tuned for asv's full isolated-env runs, not this instant-feedback tool.
    #
    # Peak RSS, however, MUST come from exactly the first call. `ru_maxrss` is a whole-process
    # high-water mark, so repeating a large-body operation (e.g. downloading a 50MB body) many
    # times in one process inflates it via allocator fragmentation across repeated large
    # allocations, not anything the benchmarked code actually did -- confirmed live: running this
    # in a naive repeat loop pushed `peakmem_download_bytes` from ~237MB to ~1195MB with zero code
    # change. Capturing `ru_maxrss` right after call #1, before any further repeats run, avoids
    # that entirely since the metric is captured before the contaminating calls happen.
    extra_timed_calls = 49
    try:
        start = time.perf_counter()
        method(*args)
        timings: list[float] = [time.perf_counter() - start]
        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        for _ in range(extra_timed_calls):
            start = time.perf_counter()
            method(*args)
            timings.append(time.perf_counter() - start)
        elapsed = min(timings)
    finally:
        suite.teardown(*args)

    print(json.dumps({"time": elapsed, "peakmem": peak_rss}))


if __name__ == "__main__":
    main()
