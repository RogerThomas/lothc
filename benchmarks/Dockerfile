# Benchmark images — two separate runtime stages, run as separate services via
# benchmarks/docker-compose.yml so the JSON server and the Python client each get
# their own `cpuset`. Keeping them in one container (the old approach) meant the
# server and every client library under test were fighting over the same pinned
# cores, which is exactly the kind of contention this split is meant to remove.
#
# Build+run: task perf-build / task perf (see Taskfile.yml)

FROM rust:1-slim AS server-builder
WORKDIR /build
COPY benchmarks/json_server/Cargo.toml benchmarks/json_server/Cargo.toml
COPY benchmarks/json_server/src benchmarks/json_server/src
WORKDIR /build/benchmarks/json_server
RUN cargo build --release

FROM debian:trixie-slim AS server-runtime
COPY --from=server-builder /build/benchmarks/json_server/target/release/json-server /usr/local/bin/json-server
EXPOSE 3000
ENTRYPOINT ["/usr/local/bin/json-server"]

FROM python:3.14-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY lothc ./lothc
COPY perf.py ./perf.py
RUN uv sync --frozen --group dev

COPY benchmarks/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
