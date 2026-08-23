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

func="${PERF_FUNC:-}"
if [ -n "$func" ]; then
    # PERF_FUNC is the hyphenated, human-facing name (e.g. "single-call"); perf.py's
    # actual function is the underscored Python identifier ("single_call").
    func=$(printf '%s' "$func" | tr '-' '_')
    # "perf.py:FUNC" (not "perf.py FUNC") — yeetr treats a bare positional FUNC as
    # ambiguous here because main()'s first param is also a plain string (url).
    exec uv run --no-sync yeet "perf.py:$func" "$target_url" "$@"
else
    exec uv run --no-sync yeet perf.py "$target_url" "$@"
fi
