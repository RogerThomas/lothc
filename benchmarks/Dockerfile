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
# lothc's version is dynamic (hatch-vcs, [tool.hatch.version] source = "vcs") — it normally reads
# the version from git metadata, but this build context has no .git (and the base image has no
# `git` binary either), so pin a pretend version instead of copying .git in just for this: the
# version string is irrelevant here, this image only runs perf.py against lothc's code, and
# copying .git would also bust the uv sync layer cache on every single commit. Must be the
# generic SETUPTOOLS_SCM_PRETEND_VERSION, not the per-package _FOR_<NAME> variant — hatch-vcs
# doesn't pass a dist name through to setuptools-scm, so the namespaced form is never matched
# (verified locally: _FOR_LOTHC silently has no effect, the bare var works).
ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
# asv (regression-benchmark tracking of lothc's own history, unrelated to this cross-library
# perf.py comparison) needs a C compiler to build from source and this slim image has none —
# excluded here since perf.py never imports it; task perf doesn't need it installed.
RUN uv sync --frozen --group dev --no-install-package asv

COPY benchmarks/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
