//! Proxies, picked and spoken as pyreqwest 0.15 (reqwest 0.13.5, hyper-util 0.1.21) does.
//!
//! Which proxy a request takes is hyper-util's own `Matcher`, built once when the client opens, as
//! reqwest builds it in `ClientBuilder::build`: lothc's `proxy=`/`no_proxy=` are reqwest's
//! `Proxy::all(..).no_proxy(..)`, which turns the environment's proxies off; without them the
//! environment's apply (`Matcher::from_system`, which is environment variables only without
//! hyper-util's `client-proxy-system`, as in pyreqwest's build: `ALL_PROXY`, `HTTP_PROXY`,
//! `HTTPS_PROXY`, `NO_PROXY`, uppercase first, and none of them when `REQUEST_METHOD` is set).
//!
//! How a connection goes through it (reqwest's `connect.rs`): an `http://` target through an HTTP
//! proxy is sent to the proxy in absolute form, with the proxy URL's credentials as
//! `Proxy-Authorization` (`request.rs`); an `https://` target goes through a `CONNECT` tunnel
//! (hyper-util's `tunnel.rs`, byte for byte), then TLS to the origin; a SOCKS5 proxy does
//! hyper-util's (non-optimistic) SOCKS5 handshake, the target resolved locally for `socks5://`
//! and by the proxy for `socks5h://`. The handshake runs on the connection's own socket before TLS,
//! for both pools (`Handshake::run`), driven by the same readiness as hyper's I/O.
//!
//! An `https://` proxy (TLS to the proxy itself) and SOCKS4 aren't ported: a request that would use
//! one is a `NotImplementedError`, never sent some other way.
use std::future::poll_fn;
use std::io;
use std::net::{IpAddr, SocketAddr};
use std::os::fd::RawFd;
use std::task::Poll;

use http::header::HeaderValue;
use http::Uri;
use hyper_util::client::proxy::matcher::{Intercept, Matcher};
use pyo3::exceptions::{PyNotImplementedError, PyValueError};
use pyo3::PyResult;
use rustls::pki_types::ServerName;
use url::Url;

use crate::io::{recv, send, Readiness};
use crate::request::server_name;

/// The variables hyper-util's `Matcher::from_env` reads a proxy from: with none set it can never
/// intercept anything, so a client without proxies never consults it.
const PROXY_VARIABLES: [&str; 6] = ["ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"];

/// hyper-util's tunnel reads a `CONNECT` response into this much, at most.
const TUNNEL_RESPONSE_LIMIT: usize = 8192;
/// hyper-util's `httparse` header limit for a `CONNECT` response.
const TUNNEL_MAX_HEADERS: usize = 100;
/// hyper-util's SOCKS5 send buffer, shared by the whole handshake (`BytesMut::with_capacity(262)`).
const SOCKS_SEND_LIMIT: usize = 262;
/// hyper-util's SOCKS5 receive buffer (`BytesMut::with_capacity(513)`), read 513 bytes at a time.
const SOCKS_RECEIVE_LIMIT: usize = 513;

/// A client's proxy rules, fixed when it opens.
pub(crate) struct Proxies {
    /// `None` when nothing can be intercepted: every request is direct without asking.
    matcher: Option<Matcher>,
}

/// Where a proxy listens.
pub(crate) struct Endpoint {
    /// Without an IPv6 address's brackets, ready to connect to.
    pub(crate) host: String,
    pub(crate) port: u16,
}

/// How a request's connection is made.
pub(crate) enum Route {
    Direct,
    /// An `http://` target through an HTTP proxy: absolute-form requests to the proxy.
    Forward { proxy: Endpoint, auth: Option<HeaderValue> },
    /// An `https://` target through an HTTP proxy: a `CONNECT` tunnel, then TLS to the origin.
    Tunnel { proxy: Endpoint, auth: Option<HeaderValue> },
    /// Through a SOCKS5 proxy, which resolves the target itself when `remote_dns` (`socks5h://`).
    Socks5 { proxy: Endpoint, auth: Option<(String, String)>, remote_dns: bool },
}

/// What a SOCKS5 `CONNECT` asks for.
pub(crate) enum SocksAddress {
    Ip(SocketAddr),
    Domain(String, u16),
}

/// What a new connection's socket does before TLS and HTTP: a proxy's handshake.
pub(crate) enum Handshake {
    /// The `CONNECT` request, written out whole.
    Tunnel(Vec<u8>),
    Socks5 { auth: Option<(String, String)>, address: SocksAddress },
}

fn unported(feature: &str) -> pyo3::PyErr {
    PyNotImplementedError::new_err(format!(
        "{feature} is not supported by the native transport yet (native=True)"
    ))
}

/// `proxy=` as pyreqwest hands it to reqwest: parsed as a URL (pyreqwest's `UrlType`, its error a
/// `ValueError`), then reqwest's `IntoProxy` (a URL without a host is retried with `http://` in
/// front, as a bare `host:port` parses as a scheme and a path; SOCKS without a port gets 1080),
/// then serialized, as `Proxy::into_matcher` passes it to hyper-util.
fn explicit_proxy(proxy: &str) -> PyResult<String> {
    let url = Url::parse(proxy).map_err(|e| PyValueError::new_err(e.to_string()))?;
    if !url.has_host() {
        return Url::parse(&format!("http://{}", url.as_str()))
            .ok()
            .filter(Url::has_host)
            .map(String::from)
            .ok_or_else(|| PyValueError::new_err("Invalid proxy: builder error"));
    }
    let mut url = url;
    if url.port().is_none() && matches!(url.scheme(), "socks4" | "socks4a" | "socks5" | "socks5h") {
        // Neither fails on a URL with a host.
        let _ = url.set_port(Some(1080));
    }
    Ok(String::from(url))
}

impl Endpoint {
    /// hyper-util's `HttpConnector`: the URI's host, and its port or the scheme's (443 for https,
    /// 80 for anything else, SOCKS from the environment included).
    fn of(uri: &Uri) -> Self {
        let host = bare_host(uri.host().unwrap_or_default());
        let port = uri.port_u16().unwrap_or(if uri.scheme_str() == Some("https") { 443 } else { 80 });
        Self { host: host.to_owned(), port }
    }
}

impl Proxies {
    /// No proxies at all.
    pub(crate) const fn none() -> Self {
        Self { matcher: None }
    }

    /// `proxy`/`no_proxy` are lothc's `proxy=` and `no_proxy=` (joined with commas); without
    /// `proxy`, the environment's proxies, read now.
    pub(crate) fn new(proxy: Option<&str>, no_proxy: Option<&str>) -> PyResult<Self> {
        let matcher = match proxy {
            Some(proxy) => Some(Matcher::builder().all(explicit_proxy(proxy)?).no(no_proxy.unwrap_or_default()).build()),
            None => PROXY_VARIABLES.iter().any(|name| std::env::var_os(name).is_some()).then(Matcher::from_system),
        };
        Ok(Self { matcher })
    }

    /// The route to `uri` (a request's whole URI): direct unless a proxy intercepts it. A proxy
    /// this transport can't speak is a `NotImplementedError`.
    pub(crate) fn route(&self, uri: &Uri) -> PyResult<Route> {
        let Some(intercept) = self.matcher.as_ref().and_then(|matcher| matcher.intercept(uri)) else {
            return Ok(Route::Direct);
        };
        Self::route_through(&intercept, uri.scheme_str() == Some("https"))
    }

    fn route_through(intercept: &Intercept, https: bool) -> PyResult<Route> {
        let proxy = intercept.uri();
        let endpoint = Endpoint::of(proxy);
        let raw_auth = || intercept.raw_auth().map(|(user, pass)| (user.to_owned(), pass.to_owned()));
        match proxy.scheme_str().unwrap_or_default() {
            "http" if https => Ok(Route::Tunnel { proxy: endpoint, auth: intercept.basic_auth().cloned() }),
            "http" => Ok(Route::Forward { proxy: endpoint, auth: intercept.basic_auth().cloned() }),
            "socks5" => Ok(Route::Socks5 { proxy: endpoint, auth: raw_auth(), remote_dns: false }),
            "socks5h" => Ok(Route::Socks5 { proxy: endpoint, auth: raw_auth(), remote_dns: true }),
            "https" => Err(unported(&format!("an https:// proxy ({proxy}, TLS to the proxy itself)"))),
            scheme => Err(unported(&format!("a {scheme}:// proxy ({proxy})"))),
        }
    }
}

/// hyper-util's `strip_ipv6_brackets`.
fn bare_host(host: &str) -> &str {
    host.strip_prefix('[').and_then(|h| h.strip_suffix(']')).unwrap_or(host)
}

impl Route {
    /// Where a connection to the origin at `origin` (host, port) connects: there, or its proxy.
    pub(crate) fn connect_address(&self, origin: (String, u16)) -> (String, u16) {
        match self {
            Self::Direct => origin,
            Self::Forward { proxy, .. } | Self::Tunnel { proxy, .. } | Self::Socks5 { proxy, .. } => {
                (proxy.host.clone(), proxy.port)
            }
        }
    }

    pub(crate) fn is_forward(&self) -> bool {
        matches!(self, Self::Forward { .. })
    }

    /// The name an https origin's certificate is verified against. Through a tunnel or SOCKS,
    /// reqwest takes it from the URI's host as is (`ServerName::try_from`), so an IPv6 address,
    /// which keeps its brackets there, is refused, unlike on a direct connection.
    pub(crate) fn server_name(&self, url: &Url) -> Result<ServerName<'static>, String> {
        match self {
            Self::Tunnel { .. } | Self::Socks5 { .. } => {
                ServerName::try_from(url.host_str().unwrap_or_default().to_owned()).map_err(|_| "Invalid Server Name".to_owned())
            }
            Self::Direct | Self::Forward { .. } => server_name(url),
        }
    }

    /// The host a `socks5://` proxy wants resolved here before connecting to it, if any: the
    /// target's (`host` as a URL serializes it), unless it is an IPv4 address already. reqwest
    /// resolves the URI's host as written, so an IPv6 address goes to the resolver in its
    /// brackets, which no resolver takes: such a request fails, as it does with pyreqwest.
    pub(crate) fn resolve_locally(&self, host: &str) -> Option<String> {
        let Self::Socks5 { remote_dns: false, .. } = self else { return None };
        host.parse::<IpAddr>().is_err().then(|| host.to_owned())
    }

    /// The handshake a new connection to the origin `host` (as a URL serializes it, an IPv6
    /// address in brackets) and `port` needs, given what `resolve_locally` asked for (`resolved`:
    /// the first address, or `None` if there was none). `user_agent` goes on a `CONNECT`, as
    /// reqwest's connector sends its client's user agent there.
    pub(crate) fn handshake(&self, host: &str, port: u16, user_agent: Option<&HeaderValue>, resolved: Option<IpAddr>) -> Result<Option<Handshake>, String> {
        match self {
            Self::Direct | Self::Forward { .. } => Ok(None),
            Self::Tunnel { auth, .. } => Ok(Some(Handshake::Tunnel(connect_request(host, port, user_agent, auth.as_ref())))),
            Self::Socks5 { auth, remote_dns, .. } => {
                let host = bare_host(host);
                let address = match (host.parse::<IpAddr>(), resolved) {
                    (Ok(ip), _) => SocksAddress::Ip(SocketAddr::new(ip, port)),
                    (Err(_), Some(ip)) if !remote_dns => SocksAddress::Ip(SocketAddr::new(ip, port)),
                    (Err(_), _) if host.len() <= 255 => SocksAddress::Domain(host.to_owned(), port),
                    (Err(_), _) => return Err(socks_error("host address is more than 255 characters")),
                };
                Ok(Some(Handshake::Socks5 { auth: auth.clone(), address }))
            }
        }
    }
}

/// hyper-util's `tunnel`: the request line and `Host`, then the extra headers lowercase. reqwest
/// adds the proxy's credentials first and its user agent after, merged so the user agent comes
/// first (`Tunnel::with_auth`, then `with_headers`, which keeps an existing
/// `proxy-authorization`).
fn connect_request(host: &str, port: u16, user_agent: Option<&HeaderValue>, auth: Option<&HeaderValue>) -> Vec<u8> {
    let mut request = format!("CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n").into_bytes();
    let mut header = |name: &[u8], value: &HeaderValue| {
        request.extend_from_slice(name);
        request.extend_from_slice(b": ");
        request.extend_from_slice(value.as_bytes());
        request.extend_from_slice(b"\r\n");
    };
    match (user_agent, auth) {
        (Some(user_agent), auth) => {
            header(b"user-agent", user_agent);
            if let Some(auth) = auth {
                header(b"proxy-authorization", auth);
            }
        }
        (None, Some(auth)) => header(b"Proxy-Authorization", auth),
        (None, None) => {}
    }
    request.extend_from_slice(b"\r\n");
    request
}

fn socks_error(message: &str) -> String {
    format!("error connecting to socks proxy: SOCKS error: {message}")
}

/// One `send` of `buf`'s start, waiting for the socket to be writable.
async fn write_some<R: Readiness>(fd: RawFd, ready: &R, buf: &[u8]) -> io::Result<usize> {
    poll_fn(|cx| match send(fd, buf) {
        Ok(n) => Poll::Ready(Ok(n)),
        Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
            ready.wait_writable(cx);
            Poll::Pending
        }
        Err(error) => Poll::Ready(Err(error)),
    })
    .await
}

async fn write_all<R: Readiness>(fd: RawFd, ready: &R, mut buf: &[u8]) -> io::Result<()> {
    while !buf.is_empty() {
        match write_some(fd, ready, buf).await? {
            0 => return Err(io::ErrorKind::WriteZero.into()),
            n => buf = &buf[n..],
        }
    }
    Ok(())
}

/// One `recv` into `buf`, waiting for the socket to be readable; `Ok(0)` is the peer closing.
async fn read<R: Readiness>(fd: RawFd, ready: &R, buf: &mut [u8]) -> io::Result<usize> {
    poll_fn(|cx| {
        if !ready.poll_readable(cx) {
            return Poll::Pending;
        }
        match recv(fd, buf) {
            Ok(n) => {
                if n > 0 && n < buf.len() {
                    ready.mark_drained();
                }
                Poll::Ready(Ok(n))
            }
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                ready.wait_readable(cx);
                Poll::Pending
            }
            Err(error) => Poll::Ready(Err(error)),
        }
    })
    .await
}

/// hyper-util's `tunnel`: send the `CONNECT`, then read until the response's head parses; only a
/// 200 opens the tunnel.
async fn tunnel<R: Readiness>(fd: RawFd, ready: &R, request: &[u8]) -> Result<(), String> {
    let io_error = |e: io::Error| format!("tunnel error: io error establishing tunnel: {e}");
    write_all(fd, ready, request).await.map_err(io_error)?;
    let mut buf = [0; TUNNEL_RESPONSE_LIMIT];
    let mut pos = 0;
    loop {
        let n = read(fd, ready, &mut buf[pos..]).await.map_err(io_error)?;
        if n == 0 {
            return Err("tunnel error: unexpected end of file".to_owned());
        }
        pos += n;
        let mut headers = [httparse::EMPTY_HEADER; TUNNEL_MAX_HEADERS];
        let mut response = httparse::Response::new(&mut headers);
        match response.parse(&buf[..pos]) {
            Ok(httparse::Status::Complete(_)) => {
                return match response.code {
                    Some(200) => Ok(()),
                    Some(407) => Err("tunnel error: proxy authorization required".to_owned()),
                    _ => Err("tunnel error: unsuccessful".to_owned()),
                };
            }
            Ok(httparse::Status::Partial) if pos == buf.len() => {
                return Err("tunnel error: proxy response headers too long".to_owned());
            }
            Ok(httparse::Status::Partial) => {}
            Err(_) => return Err("tunnel error: unsuccessful".to_owned()),
        }
    }
}

/// Why a SOCKS5 reply didn't parse, as hyper-util's `ParsingError`.
enum Parse {
    Incomplete,
    Other,
}

/// Parses one message off the front of what has arrived: it, and how many bytes it took.
type Parser<T> = fn(&[u8]) -> Result<(T, usize), Parse>;

/// hyper-util's `read_message`: parse the next message from what has arrived, reading more while
/// it's incomplete; what's left over stays for the next message.
async fn read_message<R: Readiness, T>(
    fd: RawFd,
    ready: &R,
    received: &mut Vec<u8>,
    parse: Parser<T>,
) -> Result<T, String> {
    let mut chunk = [0; SOCKS_RECEIVE_LIMIT];
    loop {
        match parse(received) {
            Ok((message, used)) => {
                received.drain(..used);
                return Ok(message);
            }
            Err(Parse::Other) => return Err(socks_error("failed parsing server response")),
            Err(Parse::Incomplete) => {}
        }
        let n = read(fd, ready, &mut chunk).await.map_err(|e| socks_error(&format!("io error during SOCKS handshake: {e}")))?;
        if n == 0 {
            return Err(if received.len() >= SOCKS_RECEIVE_LIMIT {
                socks_error("failed parsing server response")
            } else {
                socks_error("io error during SOCKS handshake: unexpected eof")
            });
        }
        received.extend_from_slice(&chunk[..n]);
    }
}

/// A method selection: the version, then the method (0x00 none, 0x02 username/password, 0xFF
/// none acceptable; anything else doesn't parse).
fn negotiation(buf: &[u8]) -> Result<(u8, usize), Parse> {
    match buf {
        [0x05, method @ (0x00 | 0x02 | 0xFF), ..] => Ok((*method, 2)),
        [_, _, ..] => Err(Parse::Other),
        _ => Err(Parse::Incomplete),
    }
}

/// A username/password status: accepted or not.
fn authentication(buf: &[u8]) -> Result<(bool, usize), Parse> {
    match buf {
        [0x01, status, ..] => Ok((*status == 0, 2)),
        [_, _, ..] => Err(Parse::Other),
        _ => Err(Parse::Incomplete),
    }
}

/// hyper-util's SOCKS5 `Status` messages, by reply code.
fn reply_status(code: u8) -> Option<&'static str> {
    Some(match code {
        0x00 => "success",
        0x01 => "general server failure",
        0x02 => "connection not allowed",
        0x03 => "network unreachable",
        0x04 => "host unreachable",
        0x05 => "connection refused",
        0x06 => "ttl expired",
        0x07 => "command not supported",
        0x08 => "address type not supported",
        _ => return None,
    })
}

/// A `CONNECT` reply: version, reply code, reserved, then the bound address (which must parse,
/// though nothing uses it). `Ok(None)` is success, `Ok(Some(status))` a refusal.
fn reply(buf: &[u8]) -> Result<(Option<&'static str>, usize), Parse> {
    let [version, code, reserved, rest @ ..] = buf else { return Err(Parse::Incomplete) };
    if *version != 0x05 {
        return Err(Parse::Other);
    }
    let status = reply_status(*code).ok_or(Parse::Other)?;
    if *reserved != 0x00 {
        return Err(Parse::Other);
    }
    let address_len = match rest {
        [] | [_] => return Err(Parse::Incomplete),
        [0x01, ..] => 1 + 4 + 2,
        [0x03, 0, ..] => return Err(Parse::Other),
        [0x03, len, ..] => 2 + usize::from(*len) + 2,
        [0x04, ..] => 1 + 16 + 2,
        _ => return Err(Parse::Other),
    };
    if rest.len() < address_len {
        return Err(Parse::Incomplete);
    }
    if let [0x03, len, domain @ ..] = rest {
        std::str::from_utf8(&domain[..usize::from(*len)]).map_err(|_| Parse::Other)?;
    }
    Ok(((*code != 0x00).then_some(status), 3 + address_len))
}

/// What a SOCKS5 `CONNECT` asks for: the address type, the address, then the port.
fn socks_address(address: &SocksAddress, buf: &mut Vec<u8>) {
    match address {
        SocksAddress::Ip(SocketAddr::V4(v4)) => {
            buf.push(0x01);
            buf.extend_from_slice(&v4.ip().octets());
            buf.extend_from_slice(&v4.port().to_be_bytes());
        }
        SocksAddress::Ip(SocketAddr::V6(v6)) => {
            buf.push(0x04);
            buf.extend_from_slice(&v6.ip().octets());
            buf.extend_from_slice(&v6.port().to_be_bytes());
        }
        SocksAddress::Domain(domain, port) => {
            buf.push(0x03);
            // At most 255 bytes, checked when the address was chosen.
            buf.push(domain.len() as u8);
            buf.extend_from_slice(domain.as_bytes());
            buf.extend_from_slice(&port.to_be_bytes());
        }
    }
}

/// hyper-util's `SocksConfig::execute`, without optimistic sending (reqwest's setting): offer one
/// method, authenticate if it was username/password, then `CONNECT`. hyper-util writes every
/// message into one 262-byte buffer, so a handshake that doesn't fit fails to serialize.
async fn socks5<R: Readiness>(fd: RawFd, ready: &R, auth: Option<&(String, String)>, address: &SocksAddress) -> Result<(), String> {
    let io_error = |e: io::Error| socks_error(&format!("io error during SOCKS handshake: {e}"));
    let method = if auth.is_some() { 0x02 } else { 0x00 };
    let mut received = Vec::with_capacity(SOCKS_RECEIVE_LIMIT);
    let mut sent = 3;
    write_all(fd, ready, &[0x05, 0x01, method]).await.map_err(io_error)?;
    match read_message(fd, ready, &mut received, negotiation).await? {
        0xFF => return Err(socks_error("server does not support user/pass authentication")),
        selected if selected != method => return Err(socks_error("server implements authentication incorrectly")),
        _ => {}
    }
    if let Some((username, password)) = auth {
        sent += 3 + username.len() + password.len();
        if sent > SOCKS_SEND_LIMIT {
            return Err(socks_error("failed serialize request"));
        }
        let mut message = vec![0x01];
        // Truncated to a byte, as hyper-util's `as u8` does (the budget above lets one reach 256).
        message.push(username.len() as u8);
        message.extend_from_slice(username.as_bytes());
        message.push(password.len() as u8);
        message.extend_from_slice(password.as_bytes());
        write_all(fd, ready, &message).await.map_err(io_error)?;
        if !read_message(fd, ready, &mut received, authentication).await? {
            return Err(socks_error("credentials not accepted"));
        }
    }
    let mut message = vec![0x05, 0x01, 0x00];
    socks_address(address, &mut message);
    sent += message.len();
    if sent > SOCKS_SEND_LIMIT {
        return Err(socks_error("failed serialize request"));
    }
    write_all(fd, ready, &message).await.map_err(io_error)?;
    match read_message(fd, ready, &mut received, reply).await? {
        None => Ok(()),
        Some(status) => Err(socks_error(status)),
    }
}

impl Handshake {
    /// Run the handshake on connection socket `fd`, waiting on `ready` as hyper's I/O does, so
    /// it is the same code on the asyncio loop and on a sync caller's thread. `Err` is the
    /// connection error's message.
    pub(crate) async fn run<R: Readiness>(self, fd: RawFd, ready: R) -> Result<(), String> {
        match &self {
            Self::Tunnel(request) => tunnel(fd, &ready, request).await,
            Self::Socks5 { auth, address } => socks5(fd, &ready, auth.as_ref(), address).await,
        }
    }
}
