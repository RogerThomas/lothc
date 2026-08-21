#!yeet
"""No git, no commit, no isolated-env build: run every `time_*` benchmark in `asv_bench/`
against whatever is on disk RIGHT NOW (dirty working tree included). Results are recorded keyed
by a content hash of `lothc/`'s own source (not a git sha — no commit involved), so you choose
what to compare against instead of always diffing against the immediately preceding run:

    task bench-check                       # records the current state, prints its hash, no table
    # ... edit code ...
    task bench-check -- --against=<hash>   # runs again, prints ITS OWN new hash, diffs vs <hash>
    # ... edit more ...
    task bench-check -- --against=<hash>   # compare against the ORIGINAL hash again
    task bench-check -- --against=<newer-hash>  # or against the run right before this one instead

With no `--against`, nothing is printed but the current hash — there is nothing to diff against
until you actually pass one. Each benchmark still runs in its own subprocess
(`_quick_check_worker.py`) so `peakmem` stays a correct per-benchmark reading, not a whole-run
high-water mark.

This is deliberately separate from `asv run`/`asv continuous` (which track lothc's history across
real commits, see CLAUDE.md's "Regression benchmarking (asv)" section) — this script has no
concept of commits at all, on purpose.
"""

import hashlib
import importlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import cast

from rich.console import Console
from rich.table import Table

_IMPROVED_THRESHOLD = 0.98
_REGRESSED_THRESHOLD = 1.02


def _hash_lothc_source(lothc_dir: Path) -> str:
    digest = hashlib.sha256()
    for source_file in sorted(lothc_dir.rglob("*.py")):
        digest.update(source_file.relative_to(lothc_dir).as_posix().encode())
        digest.update(source_file.read_bytes())
    return digest.hexdigest()[:12]


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


def _format_ratio(ratio: float | None) -> str:
    if ratio is None:
        return "[dim]—[/dim]"
    if ratio < _IMPROVED_THRESHOLD:
        return f"[bold green]{ratio:.2f}x[/bold green]"
    if ratio > _REGRESSED_THRESHOLD:
        return f"[bold red]{ratio:.2f}x[/bold red]"
    return f"{ratio:.2f}x"


def _add_row(
    table: Table, label: str, before: dict[str, float] | None, after: dict[str, float]
) -> None:
    time_str = f"{after['time'] * 1000:.2f}ms"
    mem_str = f"{after['peakmem'] / (1024 * 1024):.1f}MB"
    time_ratio = after["time"] / before["time"] if before and before["time"] else None
    mem_ratio = after["peakmem"] / before["peakmem"] if before and before["peakmem"] else None
    table.add_row(label, time_str, _format_ratio(time_ratio), mem_str, _format_ratio(mem_ratio))


def main(
    *,
    against: str | None = None,
    history_file: Path = Path(__file__).parent / ".quick_check_history.json",
) -> None:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    console = Console()

    lothc_dir = Path(__file__).parent.parent / "lothc"
    current_hash = _hash_lothc_source(lothc_dir)

    history: dict[str, dict[str, dict[str, float]]] = (
        json.loads(history_file.read_text()) if history_file.exists() else {}
    )

    if against is not None and against not in history:
        console.print(
            f"[bold red]No recorded run for --against={against}[/bold red] — "
            f"known hashes: {', '.join(history) or '(none yet)'}"
        )
        return

    targets = _discover_targets(Path(__file__).parent)
    results: dict[str, dict[str, float]] = {}
    for module_name, class_name, method_name in targets:
        label = f"{class_name}.{method_name}"
        results[label] = _run_one(module_name, class_name, method_name)

    history[current_hash] = results
    history_file.write_text(json.dumps(history, indent=2))

    if against is None:
        console.print(f"lothc hash: [bold cyan]{current_hash}[/bold cyan]")
        return

    before = history[against]
    table = Table(title=f"bench-check: {against} → {current_hash}")
    table.add_column("Benchmark", style="cyan")
    table.add_column("Time", justify="right")
    table.add_column("Time Δ", justify="right")
    table.add_column("Peak Mem", justify="right")
    table.add_column("Mem Δ", justify="right")
    for label, after in results.items():
        _add_row(table, label, before.get(label), after)
    console.print(f"lothc hash: [bold cyan]{current_hash}[/bold cyan]")
    console.print(table)
