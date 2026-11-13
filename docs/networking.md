---
icon: lucide/network
---

# Networking

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    cookie_store=True,  # in-memory cookie jar, sent automatically on subsequent requests
    follow_redirects=True,  # default
    max_redirects=10,  # None (default) is 10
    proxy="http://localhost:8080",
    no_proxy=["localhost", "internal.example.com"],  # hosts that skip the proxy
) as client:
    ...
```

Every setting on this page is client-level, except `timeout`, which a verb can override for one
call (see [Timeouts](#timeouts)). For different redirect, proxy or cookie behaviour on one call,
use a second client.

`no_proxy` entries use the same format as the `NO_PROXY` environment variable. It only
narrows `proxy`, so passing it without `proxy` raises `ValueError`.

## User-Agent and HTTP/2

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    user_agent="my-app/1.2",  # sent on every request; a per-request `headers=` still overrides it
    http2=True,  # negotiate HTTP/2 where the server supports it
) as client:
    ...
```

Without `user_agent`, requests identify as `python-lothc/<version>`.

`http2=True` negotiates HTTP/2 during the TLS handshake (ALPN) and falls back to HTTP/1.1 when
the server doesn't offer it, so it's safe to turn on for a mixed set of servers. A plain `http://`
connection stays on HTTP/1.1.

## TLS & mTLS

```python
async with HTTPClient(
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
addition to (not instead of) the system's own root store. `identity_pem` is one PEM buffer
holding both the client certificate and its private key.

There's also `danger_accept_invalid_certs: bool = False`, which disables certificate validation
entirely — insecure, and only ever appropriate against a local/test endpoint you control, never
in production.

lothc uses rustls: TLS 1.2/1.3 only. An empty version range raises `ValueError` when the client
opens, as does an unusable `identity_pem` or a `user_agent` that isn't a valid header value.

## DNS overrides, source address and keepalive

```python
async with HTTPClient(
    resolve={"api.example.com": "10.0.0.5"},  # skip DNS for this host; the URL's port still applies
    local_address="10.0.0.2",  # the source IP connections come from
    tcp_keepalive=60.0,  # seconds before TCP keepalive probes start on an idle connection
) as client:
    ...
```

`resolve` is handy for pointing a real hostname at a staging box or a local server without
editing `/etc/hosts`; TLS still validates the certificate against the hostname in the URL.

## Connection pooling

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    max_connections=50,
    pool_idle_timeout=30.0,  # None (default) is 90s
    pool_max_idle_per_host=10,
    pool_timeout=2.0,  # max wait for a free connection slot
) as client:
    ...
```

## Timeouts

```python
async with HTTPClient(
    base_url="https://api.example.com/",
    timeout=5.0,  # the whole request; default 30.0, None disables it
    connect_timeout=5.0,  # only the TCP connect
    read_timeout=60.0,  # the longest idle gap between two body chunks
) as client:
    await client.get("items/7")  # 5s
    await client.get("exports/large-file.csv", timeout=60.0)  # this call gets 60s
```

- `timeout` caps the whole request, raising `HTTPTimeoutError` (see [Error handling](errors.md)).
  A verb's own `timeout=` overrides it for that call, but can't remove it: for no limit, build the
  client with `timeout=None`.
- `connect_timeout` fails fast on a dead host without capping a slow download.
- `read_timeout` trips on a body that stalls, never on one that keeps trickling.
- On `stream_get`, `stream_post` and `download`, `timeout` is the longest wait **between chunks**,
  not a cap on the whole transfer, and only time spent waiting on the server counts. A per-call
  `timeout=` puts back a hard cap; `read_timeout` replaces the gap limit.
- `sse()` ignores the client's `timeout`, since an event stream is open-ended, and has no gap
  limit unless you set `read_timeout`, the way to detect a stalled stream. Its own `timeout=`
  bounds one connection attempt. Either firing reconnects like any dropped connection (see
  [SSE](sse.md#reconnecting-max_reconnects-reconnect_delay-reconnect_on_close)).

## Native transport

```python
async with HTTPClient(base_url="https://api.example.com/", native="off") as client:
    ...
```

On Linux and macOS, a client uses lothc's own native transport by default, about 2x faster on
`get(into=Struct)`; see [Benchmarks](benchmarks.md). `native=` picks the transport:

- `"auto"` (the default): native, except on Windows, where it isn't available, and with
  `http2=True`, which it doesn't support yet; both use pyreqwest instead.
- `"off"`: always pyreqwest.
- `"on"`: always native; on Windows or with `http2=True`, constructing the client raises
  `NotImplementedError`.

`lothc.testing`'s mocker intercepts requests on either transport, the same way.
