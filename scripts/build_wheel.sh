#!/usr/bin/env bash
# Builds one release wheel of lothc for one interpreter, PGO-optimised by the recipe
# experiments/loop-transport/Dockerfile.pgo measured: an instrumented wheel, scripts/pgo_train.py
# run on it, its profiles merged, then the wheel rebuilt from them. It runs on the platform the
# wheel is for (natively, or in the manylinux/musllinux image on Linux), so training does too.
#
#   scripts/build_wheel.sh <python> <out-dir> [maturin build args, e.g. --compatibility ...]
#
# <python> is anything `uv python find` takes (`3.14t`, a path). Needs uv, and for PGO rustup's
# llvm-tools. PGO=0 builds a plain release wheel instead: Windows' stub module has nothing to
# train, and it's the baseline PGO is measured against.
set -euo pipefail

# `--system`: never a virtual environment's, which the repo's own `.venv` would be.
python="$(uv python find --system "$1")"
out="$2"
shift 2
cd "$(dirname "$0")/.."
maturin=(uv tool run --from maturin==1.16.0 maturin build --release --interpreter "$python" "$@")

if [[ "${PGO:-1}" == "0" ]]; then
  exec "${maturin[@]}" --out "$out"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# A target directory of its own, so neither build reuses the other's (or a dev build's) objects.
export CARGO_TARGET_DIR="$work/target"
# llvm-tools' own llvm-profdata, whose LLVM is the rustc's: a profile only merges with its own.
llvm_profdata="$(rustc --print sysroot)/lib/rustlib/$(rustc -vV | sed -n 's/^host: //p')/bin/llvm-profdata"

echo "::group::Instrumented build"
RUSTFLAGS="-Cprofile-generate=$work/profiles" "${maturin[@]}" --out "$work/instrumented"
echo "::endgroup::"

echo "::group::Training"
uv venv --quiet --python "$python" "$work/venv"
uv pip install --quiet --python "$work/venv" "$work"/instrumented/*.whl msgspec yeetr
# Any GIL-enabled interpreter: trustme's cryptography has no free-threaded 3.15 wheel.
uv run --quiet --isolated --no-project --python 3.13 --with trustme --with yeetr \
  yeet ./scripts/pgo_certs.py "$work/certs"
"$work/venv/bin/yeet" ./scripts/pgo_train.py --certs "$work/certs"
"$llvm_profdata" merge -o "$work/lothc.profdata" "$work/profiles"
echo "::endgroup::"
"$llvm_profdata" show "$work/lothc.profdata"

echo "::group::Optimised build"
RUSTFLAGS="-Cprofile-use=$work/lothc.profdata -Cllvm-args=-pgo-warn-missing-function" \
  "${maturin[@]}" --out "$out" 2>&1 | tee "$work/optimised.log"
echo "::endgroup::"
# The profile is used: a function it has no data for warns, so one that didn't match (another
# LLVM, other sources) would warn for nearly every function it counted above.
missing="$(grep -c 'no profile data' "$work/optimised.log" || true)"
echo "Functions without profile data: $missing"
# Matched profiles leave 0-50 (measured per platform); an unused one leaves over 1,000.
if (( missing > 200 )); then
  echo "::error::The PGO profile wasn't used: $missing functions without profile data"
  exit 1
fi
