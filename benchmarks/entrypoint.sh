#!/bin/sh
set -eu

cd /app

target_url="${PERF_TARGET_URL:-http://127.0.0.1:3000}"

for _ in $(seq 1 50); do
    if uv run --no-sync python -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=0.5)" "$target_url/" 2>/dev/null; then
        break
    fi
    sleep 0.1
done

exec uv run --no-sync yeet perf.py "$target_url" "$@"
