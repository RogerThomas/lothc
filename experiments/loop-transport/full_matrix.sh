#!/bin/sh
# Everything in one session, so the numbers are comparable. Usage: ./full_matrix.sh OUT.jsonl
set -eu
out="$1"; : > "$out"
export SERVER_CPUSET="${SERVER_CPUSET:-2,3,6}" CLIENT_CPUSET="${CLIENT_CPUSET:-4}"
docker compose up -d --force-recreate json-server >/dev/null 2>&1
tag() { sed "s/\"impl\": *\"\([^\"]*\)\"/\"impl\": \"$1\1\"/"; }
for round in 1 2; do
  for c in 1 10 100; do
    docker compose run --rm reqwest-bench --runtime current --body json --concurrency $c --runs 3 2>/dev/null >> "$out"
    for impl in pyreqwest-bytes lothc-msgspec lothc-msgspec-params; do
      docker compose run --rm py-bench $impl --concurrency $c --runs 3 2>/dev/null >> "$out"
    done
    for impl in poc-msgspec poc-msgspec-params; do
      docker compose run --rm poc-bench $impl --concurrency $c --runs 3 2>/dev/null >> "$out"
      docker compose run --rm poc-pgo $impl --concurrency $c --runs 3 2>/dev/null | tag "pgo:" >> "$out"
    done
  done
  for t in 1 4 16; do
    docker compose run --rm --entrypoint uv py-bench run --no-sync yeet /bench/sync_bench.py lothc-sync --threads $t --runs 3 2>/dev/null | tag "sync:" >> "$out"
    docker compose run --rm --entrypoint uv poc-bench run --no-sync yeet /bench/sync_bench.py poc-sync --threads $t --runs 3 2>/dev/null | tag "sync:" >> "$out"
    docker compose run --rm --entrypoint uv poc-pgo run --no-sync yeet /bench/sync_bench.py poc-sync --threads $t --runs 3 2>/dev/null | tag "sync:pgo:" >> "$out"
  done
done
