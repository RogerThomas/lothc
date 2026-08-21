#!yeet
"""No git, no commit, no isolated-env build: run every `time_*` benchmark in `asv_bench/`
against whatever is on disk RIGHT NOW (dirty working tree included), diff each result against
whatever was saved the last time this ran, then overwrite that saved baseline with today's
numbers. Edit code, run this, see the diff; edit again, run again, see the diff against *that*
run. Each benchmark still runs in its own subprocess (`_quick_check_worker.py`) so `peakmem`
stays a correct per-benchmark reading, not a whole-run high-water mark.

This is deliberately separate from `asv run`/`asv continuous` (which track lothc's history across
real commits, see CLAUDE.md's "Regression benchmarking (asv)" section) — this script has no
concept of commits at all, on purpose.
"""

import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import cast


def _discover_targets(bench_dir: Path) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    for bench_file in sorted(bench_dir.glob("bench_*.py")):
        module = importlib.import_module(f"asv_bench.{bench_file.stem}")
        for name, obj in vars(module).items():
            if not inspect.isclass(obj) or obj.__module__ != module.__name__:
                continue
            for method_name in vars(obj):
                if method_name.startswith("time_"):
                    targets.append((module.__name__, name, method_name))
    return targets


def _run_one(module_name: str, class_name: str, method_name: str) -> dict[str, float]:
    worker = Path(__file__).parent / "_quick_check_worker.py"
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(worker), module_name, class_name, method_name],
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, float], json.loads(proc.stdout))


def _print_row(label: str, before: dict[str, float] | None, after: dict[str, float]) -> None:
    time_str = f"{after['time'] * 1000:.2f}ms"
    mem_str = f"{after['peakmem'] / (1024 * 1024):.1f}MB"
    if before is None:
        print(f"{label:<40} time={time_str:>10}  peakmem={mem_str:>10}  (no baseline yet)")
        return
    time_ratio = after["time"] / before["time"] if before["time"] else float("inf")
    mem_ratio = after["peakmem"] / before["peakmem"] if before["peakmem"] else float("inf")
    print(
        f"{label:<40} time={time_str:>10} ({time_ratio:.2f}x)  "
        f"peakmem={mem_str:>10} ({mem_ratio:.2f}x)"
    )


def main(baseline_file: Path = Path(__file__).parent / ".quick_check_baseline.json") -> None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    targets = _discover_targets(Path(__file__).parent)
    baseline: dict[str, dict[str, float]] = (
        json.loads(baseline_file.read_text()) if baseline_file.exists() else {}
    )

    results: dict[str, dict[str, float]] = {}
    for module_name, class_name, method_name in targets:
        label = f"{class_name}.{method_name}"
        after = _run_one(module_name, class_name, method_name)
        results[label] = after
        _print_row(label, baseline.get(label), after)

    baseline_file.write_text(json.dumps(results, indent=2))
