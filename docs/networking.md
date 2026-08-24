---
icon: lucide/network
---

# Cookies, redirects & proxy

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
