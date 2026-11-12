#!/bin/sh
# Usage: ./run_matrix.sh OUT.jsonl  (SERVER_CPUSET/CLIENT_CPUSET from env)
set -eu
out="$1"; : > "$out"
docker compose up -d --force-recreate json-server >/dev/null 2>&1
for c in 1 10 50 100; do
  for rt in current multi; do for body in bytes json; do
    docker compose run --rm reqwest-bench --runtime $rt --body $body --concurrency $c 2>/dev/null >> "$out"
  done; done
  for impl in pyreqwest-bytes pyreqwest-json lothc-bytes lothc-dict lothc-msgspec; do
    docker compose run --rm py-bench $impl --concurrency $c 2>/dev/null >> "$out"
  done
done
