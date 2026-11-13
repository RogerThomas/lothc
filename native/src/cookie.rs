//! `cookie_store=True` as reqwest 0.13.5's `cookies` feature has it: a `Jar` (`cookie.rs`) of the
//! `cookie_store` crate's `CookieStore`, which the client's `CookieService` (`cookie.rs`'s
//! `service`) reads for each request and fills from each response's `Set-Cookie`s.
//!
//! The service sits inside reqwest's redirect layer and outside the connection pool, so it runs
//! for every hop of a followed redirect, after that layer copied the hop's headers: a hop's own
//! `Cookie` comes from the jar for the hop's URL (unless the copy kept the caller's), and the
//! cookies a redirect response sets apply to the next hop. A request with a `Cookie` of its own
//! (a header, or a default header) gets none from the jar.
//!
//! The jar is a pool's, so it lives as long as one entry of the client, as pyreqwest's client is
//! rebuilt, jar and all, on each entry.
use std::sync::{Arc, PoisonError, RwLock};

use bytes::Bytes;
use http::header::{Entry, COOKIE, SET_COOKIE};
use http::{HeaderMap, HeaderValue, Uri};
use url::Url;

/// reqwest's `cookie::Jar`: the store behind a lock, since a sync pool's requests run on any
/// thread. A poisoned lock is still used, as no write leaves the store half-changed.
#[derive(Default)]
pub(crate) struct Jar(RwLock<cookie_store::CookieStore>);

impl Jar {
    /// `Jar::cookies`: the `Cookie` value the store has for `url`, its cookies' `name=value`s
    /// joined by `; `, if there are any.
    fn cookies(&self, url: &Url) -> Option<HeaderValue> {
        let value = self
            .0
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .get_request_values(url)
            .map(|(name, value)| format!("{name}={value}"))
            .collect::<Vec<_>>()
            .join("; ");
        if value.is_empty() {
            return None;
        }
        HeaderValue::from_maybe_shared(Bytes::from(value)).ok()
    }

    /// `Jar::set_cookies`: every `Set-Cookie` value that parses, stored as received from `url`
    /// (the rest are skipped).
    fn set_cookies(&self, headers: &HeaderMap, url: &Url) {
        let mut values = headers.get_all(SET_COOKIE).iter().peekable();
        // `ResponseFuture::poll` calls the jar only for a response that sets something.
        if values.peek().is_none() {
            return;
        }
        let cookies = values.filter_map(|value| {
            std::str::from_utf8(value.as_bytes())
                .map_err(cookie::ParseError::from)
                .and_then(cookie::Cookie::parse)
                .map(cookie::Cookie::into_owned)
                .ok()
        });
        self.0.write().unwrap_or_else(PoisonError::into_inner).store_response_cookies(cookies, url);
    }
}

/// One request's use of its client's jar (`CookieService`'s per-request state): the URL its
/// response's cookies are stored against.
#[derive(Clone)]
pub(crate) struct Cookies {
    jar: Arc<Jar>,
    url: Url,
}

impl Cookies {
    /// `CookieService::call` for a request to `uri` (absolute) with `headers`: the jar's cookies
    /// for it added last, unless it has a `Cookie` already. `None` if `uri` isn't a URL, which
    /// reqwest's service would panic on (every request URI here came from one).
    pub(crate) fn add(jar: &Arc<Jar>, uri: &Uri, headers: &mut HeaderMap) -> Option<Self> {
        let url = Url::parse(&uri.to_string()).ok()?;
        if let Ok(Entry::Vacant(entry)) = headers.try_entry(COOKIE) {
            if let Some(value) = jar.cookies(&url) {
                // Only a map at its size limit refuses it, as `HeaderMap::insert` would panic.
                let _ = entry.try_insert(value);
            }
        }
        Some(Self { jar: jar.clone(), url })
    }

    /// `ResponseFuture::poll`: the response's head arrived with `headers`.
    pub(crate) fn store(&self, headers: &HeaderMap) {
        self.jar.set_cookies(headers, &self.url);
    }
}
