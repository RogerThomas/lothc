#!yeet
"""Median-of-runs table from run_matrix.sh's JSON lines."""

import json
import statistics
from collections import defaultdict
from pathlib import Path


def main(path: Path) -> None:
    groups: defaultdict[tuple[int, str], list[dict[str, float]]] = defaultdict(list)
    for line in path.read_text().splitlines():
        if not line.startswith("{"):
            continue
        r = json.loads(line)
        name = f"reqwest/{r['runtime']}/{r['body']}" if r["impl"] == "reqwest" else r["impl"]
        groups[r["concurrency"], name].append(r)
    print(f"{'conc':>4}  {'impl':<24}{'req/s':>8}{'cpu us/req':>12}{'p50 us':>9}{'p99 us':>9}")
    last = None
    for (conc, name), rs in sorted(groups.items(), key=lambda kv: kv[0][0]):
        if last is not None and conc != last:
            print()
        last = conc
        med = {
            k: statistics.median(r[k] for r in rs)
            for k in ("rps", "cpu_us_per_req", "p50_us", "p99_us")
        }
        print(
            f"{conc:>4}  {name:<24}{med['rps']:>8.0f}{med['cpu_us_per_req']:>12.1f}{med['p50_us']:>9.1f}{med['p99_us']:>9.1f}"
        )
