//! Following redirects as pyreqwest 0.15 has reqwest 0.13.5 do it: tower-http 0.6.11's
//! `FollowRedirect` (`follow_redirect/mod.rs`: which statuses it follows, the method and body each
//! keeps, where `Location` points) under reqwest's `TowerRedirectPolicy` (`redirect.rs`: the limit,
//! the schemes, `https_only`, the headers dropped across origins, `Referer`, which reqwest sends
//! by default and pyreqwest never turns off).
//!
//! In reqwest the redirect layer sits inside `Decompression` and outside the connection pool, so
//! a hop's request is a copy of the previous one's headers as they left `Decompression`: reqwest's
//! defaults, the auth, an HTTP proxy's `Proxy-Authorization` (added once, for the first URL) and
//! `Accept-Encoding` included, but not hyper-util's `Host`, which each hop gets anew. That copy
//! (`Redirect::headers`) only ever loses the body's headers; the credentials dropped across origins
//! and the `Referer` apply to the hop's own copy alone, so a later hop back to the first origin
//! sends them again, as reqwest does. A redirect response being followed is dropped unread, so
//! hyper drains a small body and keeps the connection, or closes it for a larger one, as there.
//!
//! A body tower-http can't copy (a multipart one, which reqwest streams) means a redirect that
//! must resend it isn't followed: that 307/308, or 301/302 for anything but a POST (which
//! becomes a bodyless GET), is the final response, as tower-http's `BodyRepr::None` makes it.
use bytes::Bytes;
use http::header::{
    AUTHORIZATION, CONTENT_ENCODING, CONTENT_LENGTH, CONTENT_TYPE, COOKIE, LOCATION, PROXY_AUTHORIZATION, REFERER,
    TRANSFER_ENCODING, WWW_AUTHENTICATE,
};
use http::{HeaderMap, HeaderValue, Method, Uri};
use url::Url;

use crate::error::Failure;

/// The client's redirect policy, when it follows them at all (`follow_redirects=False` is
/// reqwest's `Policy::none()`, which never follows, so no request carries a `Redirect`).
#[derive(Clone, Copy)]
pub(crate) struct Policy {
    /// `max_redirects`, reqwest's 10 by default: hops a request may make before the next one fails.
    max: usize,
    https_only: bool,
}

impl Policy {
    pub(crate) fn new(follow: bool, max: Option<usize>, https_only: bool) -> Option<Self> {
        follow.then(|| Self { max: max.unwrap_or(10), https_only })
    }
}

/// Where a request goes next, from `Redirect::next`: its URL, and the headers it carries.
pub(crate) struct Hop {
    pub(crate) url: Url,
    pub(crate) headers: HeaderMap,
    pub(crate) body: Bytes,
}

/// A request's redirect state (tower-http's `ResponseFuture` plus the policy's URL list), from
/// when it is built until its final response: what the current request is, so the next one can be
/// made from it.
pub(crate) struct Redirect {
    policy: Policy,
    pub(crate) method: Method,
    /// The current request's absolute URI.
    pub(crate) uri: Uri,
    /// The headers every hop starts from: the first request's, before hyper-util's `Host`.
    headers: HeaderMap,
    /// A copy of the current request's body for the next hop (tower-http's `BodyRepr`): `None`
    /// when it couldn't be copied.
    body: Option<Bytes>,
    /// Every URL redirected from so far (`TowerRedirectPolicy::urls`).
    urls: Vec<Url>,
}

/// tower-http's `resolve_uri`: `Location` against the current URI, `None` if it can't be.
fn resolve_uri(relative: &str, base: &Uri) -> Option<Uri> {
    let base_url = Url::parse(&base.to_string()).ok()?;
    let resolved = base_url.join(relative).ok()?;
    Uri::try_from(String::from(resolved)).ok()
}

/// reqwest's `remove_sensitive_headers`: credentials stay with the host, port and scheme they
/// were meant for.
fn remove_sensitive_headers(headers: &mut HeaderMap, next: &Url, previous: &[Url]) {
    if let Some(previous) = previous.last() {
        let cross_host = next.host_str() != previous.host_str()
            || next.port_or_known_default() != previous.port_or_known_default()
            || next.scheme() != previous.scheme();
        if cross_host {
            headers.remove(AUTHORIZATION);
            headers.remove(COOKIE);
            headers.remove("cookie2");
            headers.remove(PROXY_AUTHORIZATION);
            headers.remove(WWW_AUTHENTICATE);
        }
    }
}

/// reqwest's `make_referer`: the URL redirected from, without credentials or fragment, unless
/// that would leak an https URL to plain http.
fn make_referer(next: &Url, previous: &Url) -> Option<HeaderValue> {
    if next.scheme() == "http" && previous.scheme() == "https" {
        return None;
    }
    let mut referer = previous.clone();
    let _ = referer.set_username("");
    let _ = referer.set_password(None);
    referer.set_fragment(None);
    referer.as_str().parse().ok()
}

/// reqwest's `url_bad_scheme` error, as its `Display` chain reads.
fn bad_scheme(url: &Url) -> String {
    format!("builder error for url ({url}): URL scheme is not allowed")
}

impl Redirect {
    pub(crate) fn new(policy: Policy, method: Method, uri: Uri, headers: HeaderMap, body: Option<Bytes>) -> Self {
        Self { policy, method, uri, headers, body, urls: Vec::new() }
    }

    /// tower-http's payload headers, which a request that loses its body loses too.
    fn drop_payload_headers(&mut self) {
        for header in [CONTENT_TYPE, CONTENT_LENGTH, CONTENT_ENCODING, TRANSFER_ENCODING] {
            self.headers.remove(header);
        }
    }

    /// The current request got a response of `status` with `headers`: `Ok(None)` if that is the
    /// final response, `Ok(Some)` the hop to make (this state now being that request's), or the
    /// failure the request ends with. Only a hop or a failure lets the response go unread.
    pub(crate) fn next(&mut self, status: u16, headers: &HeaderMap) -> Result<Option<Hop>, Failure> {
        match status {
            // User agents MAY change a POST to a GET (RFC 7231 6.4.2 and 6.4.3).
            301 | 302 => {
                if self.method == Method::POST {
                    self.method = Method::GET;
                    self.body = Some(Bytes::new());
                    self.drop_payload_headers();
                }
            }
            // A GET or HEAD follows a 303 (RFC 7231 6.4.4).
            303 => {
                if self.method != Method::HEAD {
                    self.method = Method::GET;
                }
                self.body = Some(Bytes::new());
                self.drop_payload_headers();
            }
            307 | 308 => {}
            _ => return Ok(None),
        }
        let Some(body) = self.body.clone() else { return Ok(None) };
        let location = headers
            .get(LOCATION)
            .and_then(|location| resolve_uri(std::str::from_utf8(location.as_bytes()).ok()?, &self.uri));
        let Some(location) = location else { return Ok(None) };
        // `TowerRedirectPolicy::redirect`.
        let previous = Url::parse(&self.uri.to_string()).map_err(|e| Failure::Builder(format!("builder error: {e}")))?;
        let next = Url::parse(&location.to_string()).map_err(|e| Failure::Builder(format!("builder error: {e}")))?;
        self.urls.push(previous);
        // `Policy::limited`: the first URL is the request itself, not a redirect.
        if self.urls.len() > self.policy.max {
            return Err(Failure::Redirect(format!("error following redirect for url ({}): too many redirects", self.urls[self.urls.len() - 1])));
        }
        if next.scheme() != "http" && next.scheme() != "https" {
            return Err(Failure::Builder(bad_scheme(&next)));
        }
        if self.policy.https_only && next.scheme() != "https" {
            return Err(Failure::Redirect(format!("error following redirect for url ({next}): {}", bad_scheme(&next))));
        }
        self.uri = location;
        // `TowerRedirectPolicy::on_request`, on the hop's own copy.
        let mut hop_headers = self.headers.clone();
        remove_sensitive_headers(&mut hop_headers, &next, &self.urls);
        if let Some(referer) = self.urls.last().and_then(|previous| make_referer(&next, previous)) {
            hop_headers.insert(REFERER, referer);
        }
        Ok(Some(Hop { url: next, headers: hop_headers, body }))
    }
}
