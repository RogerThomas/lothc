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
