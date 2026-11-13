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

# `uv sync` builds lothc's Rust extension (`lothc._native`, from native/), so it runs in a stage
# with a Rust toolchain and a C compiler (aws-lc-sys compiles C); the runtime stage gets the
# finished /app, venv included. Same base image in both, so the venv's interpreter path holds.
FROM python:3.14-slim AS runtime-builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
COPY --from=rust:1-slim /usr/local/cargo /usr/local/cargo
COPY --from=rust:1-slim /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo RUSTUP_HOME=/usr/local/rustup PATH=/usr/local/cargo/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY native ./native
COPY lothc ./lothc
COPY perf.py ./perf.py
# asv (regression-benchmark tracking of lothc's own history, unrelated to this cross-library
# perf.py comparison) builds from source; excluded since perf.py never imports it and task perf
# doesn't need it installed.
RUN uv sync --frozen --group dev --group perf --no-install-package asv

FROM python:3.14-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY --from=runtime-builder /app /app

COPY benchmarks/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
