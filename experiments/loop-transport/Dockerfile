FROM rust:1-slim AS builder
WORKDIR /build
COPY client/Cargo.toml ./
RUN mkdir src && echo 'fn main(){}' > src/main.rs && cargo build --release && rm -rf src
COPY client/src ./src
RUN touch src/main.rs && cargo build --release

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /build/target/release/reqwest-bench /usr/local/bin/reqwest-bench
ENTRYPOINT ["/usr/local/bin/reqwest-bench"]
