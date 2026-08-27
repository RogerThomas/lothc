---
icon: lucide/network
---

# Cookies, redirects, proxy & TLS

```python
async with HTTPClient.build(
    base_url="https://api.example.com/",
    cookie_store=True,  # in-memory cookie jar, sent automatically on subsequent requests
    follow_redirects=True,  # default
    max_redirects=10,  # None (default) leaves pyreqwest's own limit in place
    proxy="http://localhost:8080",
) as client:
    ...
```

Unlike `timeout` and auth (see [Authentication](auth.md)), none of these three take a per-call
override — pyreqwest's `RequestBuilder` has no `.follow_redirects()`/`.max_redirects()`/`.proxy()`/
`.default_cookie_store()` at all, only its `ClientBuilder` does. That's not a gap lothc could
close: redirect policy, proxy, and the cookie jar are all client/connection-level constructs in
the underlying Rust `reqwest` crate itself, not per-request ones — the proxy decides which TCP
connection gets opened, the cookie jar is shared state, and the redirect policy governs the
transport loop before a response ever reaches request-level code. If one call genuinely needs
different redirect/proxy/cookie behavior than the rest, build a second client with that setting
rather than looking for a per-verb kwarg.

## TLS & mTLS

```python
async with HTTPClient.build(
    base_url="https://internal-api.example.com/",
    root_certificates=[Path("internal-ca.pem").read_bytes()],  # trust a custom/internal CA
    identity_pem=Path("client-identity.pem").read_bytes(),  # mTLS: cert + private key, one PEM
    min_tls_version="TLSv1.2",
    max_tls_version="TLSv1.3",
    https_only=True,  # refuse plain HTTP entirely
) as client:
    ...
```

`root_certificates` takes any number of PEM-encoded certificates — each one is trusted in
addition to (not instead of) the system's own root store. `identity_pem` is a single PEM buffer
containing both the client certificate and its private key concatenated, exactly as reqwest's own
`Identity::from_pem` expects.

There's also `danger_accept_invalid_certs: bool = False`, which disables certificate validation
entirely — insecure, and only ever appropriate against a local/test endpoint you control, never
in production.

Like `timeout`, these are all client-level only — set once at `build()`, no per-call override,
since a TLS/connection identity belongs to the underlying connection, not a single request.

## Connection pooling

```python
async with HTTPClient.build(
    base_url="https://api.example.com/",
    connect_timeout=5.0,  # bounds only the TCP connect phase, separate from `timeout`
    max_connections=50,
    pool_idle_timeout=30.0,  # None (default) leaves pyreqwest's own 90s default in place
    pool_max_idle_per_host=10,
    pool_timeout=2.0,  # max wait for a free connection slot
) as client:
    ...
```

`connect_timeout` is distinct from `timeout` — it only bounds the initial TCP connect, not the
whole request, so it's useful for "fail fast on a dead host" without capping how long a slow
(but alive) download is allowed to take.
