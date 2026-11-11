//! How both pools set up a connection's socket, as reqwest 0.13's connector does through
//! hyper-util 0.1.21's `HttpConnector` (`client/legacy/connect/http.rs`), and the two timeouts
//! that bound connecting and reading:
//!
//! - `resolve=` (reqwest's `DnsResolverWithOverrides`): a host named there, lowercased, takes its
//!   addresses instead of the system resolver's, whatever the connection connects to (the origin,
//!   or the proxy) and for a `socks4://` or `socks5://` proxy's target, which reqwest resolves
//!   with the same resolver. An IP address is never looked up, and the port is always the URL's (or the
//!   scheme's), since lothc gives each override port 0. The URL keeps its host, so `Host`, SNI and
//!   the certificate check still use the name;
//! - `local_address=`: each socket is bound to it, port 0, before connecting, but only for an
//!   address of its family, and the others are never tried (`split_by_preference`). A bind that
//!   fails ends the connect at once, without trying the next address;
//! - TCP keepalive on every socket, reqwest's defaults: 15s idle (or `tcp_keepalive=`), probes
//!   every 15s, 3 of them (socket2's `set_tcp_keepalive`, whose failure is only ever ignored);
//! - `connect_timeout=`, reqwest's connector timeout: one deadline for the whole connect, DNS,
//!   TCP, a proxy's handshake and TLS's, plus hyper-util's own on each TCP attempt, the timeout
//!   divided by how many addresses of that family there are. Either is a timeout error;
//! - `read_timeout=` (reqwest's `PendingRequest` and `ReadTimeoutBody`): until a response's head
//!   arrives, a fixed deadline from the request's start (connect and every redirect hop
//!   included), then a limit on each gap in the body, from the head or the last read.
//!
//! The async pool connects its sockets in Python (`lothc/_native_loop.py`, which mirrors the
//! address rules), the sync pool here (`connect_blocking`).
use std::collections::HashMap;
use std::io;
use std::net::{IpAddr, SocketAddr, TcpStream, ToSocketAddrs};
use std::os::fd::{BorrowedFd, RawFd};
use std::sync::Arc;
use std::time::{Duration, Instant};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use socket2::{Domain, Protocol, SockAddr, SockRef, Socket, TcpKeepalive, Type};

use crate::error::{error_chain, Failure};

/// reqwest's `ClientBuilder` defaults: `tcp_keepalive` 15s, `tcp_keepalive_interval` 15s,
/// `tcp_keepalive_retries` 3.
const KEEPALIVE: Duration = Duration::from_secs(15);
const KEEPALIVE_INTERVAL: Duration = Duration::from_secs(15);
const KEEPALIVE_RETRIES: u32 = 3;

/// The connection settings both pools share, built once per client.
pub(crate) struct ConnectSettings {
    overrides: HashMap<String, Vec<IpAddr>>,
    local_address: Option<IpAddr>,
    keepalive: TcpKeepalive,
    pub(crate) connect_timeout: Option<Duration>,
    pub(crate) read_timeout: Option<Duration>,
}

/// pyreqwest's `IpAddr::from_str` with its `ValueError`.
fn parse_ip(text: &str) -> PyResult<IpAddr> {
    text.parse().map_err(|e: std::net::AddrParseError| PyValueError::new_err(e.to_string()))
}

impl ConnectSettings {
    /// The address overrides for `host` (a connection's host, an IPv6 address unbracketed): its
    /// own address if it is one, else its `resolve=` override, else `None` (resolve it).
    pub(crate) fn addresses(&self, host: &str) -> Option<Vec<IpAddr>> {
        match host.parse::<IpAddr>() {
            Ok(ip) => Some(vec![ip]),
            Err(_) => self.override_for(host).cloned(),
        }
    }

    /// `host`'s `resolve=` override, if it has one: never an IP address's, which isn't looked up.
    pub(crate) fn override_for(&self, host: &str) -> Option<&Vec<IpAddr>> {
        if host.parse::<IpAddr>().is_ok() {
            return None;
        }
        self.overrides.get(host)
    }

    /// The `resolve=` override for a `socks4://` or `socks5://` proxy's target (as the URL writes
    /// its host).
    pub(crate) fn socks_override(&self, host: &str) -> Option<IpAddr> {
        self.overrides.get(host).and_then(|addresses| addresses.first().copied())
    }

    /// The local address to bind a socket to `address` from, if any.
    fn local_for(&self, address: &SocketAddr) -> Option<SocketAddr> {
        self.local_address.filter(|local| local.is_ipv4() == address.is_ipv4()).map(|local| SocketAddr::new(local, 0))
    }

    /// The addresses to try, in order, each with its own connect timeout: hyper-util's
    /// `ConnectingTcp`, its preferred family first (the first address's, or `local_address`'s,
    /// whose other family is dropped) then the rest, each family dividing `connect_timeout`
    /// among its addresses. (hyper-util races the rest against the preferred after 300ms; here
    /// they are tried in turn.)
    pub(crate) fn attempts(&self, addresses: Vec<SocketAddr>) -> Vec<(SocketAddr, Option<Duration>)> {
        let (preferred, fallback): (Vec<SocketAddr>, Vec<SocketAddr>) = match self.local_address {
            Some(local) => (addresses.into_iter().filter(|a| a.is_ipv4() == local.is_ipv4()).collect(), Vec::new()),
            None => {
                let v6 = addresses.first().is_some_and(SocketAddr::is_ipv6);
                addresses.into_iter().partition(|a| a.is_ipv6() == v6)
            }
        };
        let share = |count: usize| self.connect_timeout.and_then(|t| t.checked_div(u32::try_from(count).unwrap_or(u32::MAX)));
        let (first, rest) = (share(preferred.len()), share(fallback.len()));
        preferred.into_iter().map(|a| (a, first)).chain(fallback.into_iter().map(|a| (a, rest))).collect()
    }

    /// Set TCP keepalive on socket `fd` as hyper-util does, ignoring a failure as it does.
    pub(crate) fn keepalive(&self, fd: RawFd) {
        // SAFETY: `fd` is an open socket its owner keeps open for the length of this call.
        let fd = unsafe { BorrowedFd::borrow_raw(fd) };
        let _ = SockRef::from(&fd).set_tcp_keepalive(&self.keepalive);
    }

    /// Where a `socks4://` or `socks5://` proxy's target `host` resolves, first address only: its
    /// override, or the system resolver's.
    pub(crate) fn resolve_first(&self, host: &str) -> Result<Option<IpAddr>, Failure> {
        if let Some(ip) = self.socks_override(host) {
            return Ok(Some(ip));
        }
        let mut addresses = (host, 0).to_socket_addrs().map_err(|e| Failure::Transport(format!("error resolving for socks proxy: {e}")))?;
        Ok(addresses.next().map(|address| address.ip()))
    }

    /// A blocking, connected socket to `host:port`, resolved and set up as above, each attempt
    /// also bounded by `deadline`.
    pub(crate) fn connect_blocking(&self, host: &str, port: u16, deadline: Option<Instant>) -> Result<TcpStream, Failure> {
        let addresses: Vec<SocketAddr> = match self.addresses(host) {
            Some(ips) => ips.into_iter().map(|ip| SocketAddr::new(ip, port)).collect(),
            None => (host, port).to_socket_addrs().map_err(|e| connect_error(&e))?.collect(),
        };
        let mut first_error = None;
        for (address, timeout) in self.attempts(addresses) {
            let socket = self.socket_for(&address)?;
            let left = match deadline {
                None => None,
                Some(deadline) => match deadline.checked_duration_since(Instant::now()).filter(|left| !left.is_zero()) {
                    Some(left) => Some(left.max(Duration::from_millis(1))),
                    None => return Err(Failure::Timeout),
                },
            };
            let attempt = match [timeout, left].into_iter().flatten().min() {
                Some(limit) => socket.connect_timeout(&SockAddr::from(address), limit),
                None => socket.connect(&SockAddr::from(address)),
            };
            match attempt {
                Ok(()) => return Ok(socket.into()),
                Err(error) => {
                    first_error.get_or_insert(error);
                }
            }
        }
        Err(match first_error {
            Some(error) => connect_error(&error),
            None => Failure::Transport("tcp connect error: Network unreachable".to_owned()),
        })
    }

    /// A new socket for `address`: keepalive set, and bound to `local_address` if it applies.
    fn socket_for(&self, address: &SocketAddr) -> Result<Socket, Failure> {
        let socket = Socket::new(Domain::for_address(*address), Type::STREAM, Some(Protocol::TCP))
            .map_err(|e| Failure::Transport(format!("tcp open error: {}", error_chain(&e))))?;
        let _ = socket.set_tcp_keepalive(&self.keepalive);
        if let Some(local) = self.local_for(address) {
            socket.bind(&SockAddr::from(local)).map_err(|e| Failure::Transport(format!("tcp bind local error: {}", error_chain(&e))))?;
        }
        Ok(socket)
    }
}

/// A connect error: a timeout (hyper-util's own, or the system's) is one, as reqwest's
/// `is_timeout` sees an `io::ErrorKind::TimedOut`.
pub(crate) fn connect_error(error: &io::Error) -> Failure {
    if error.kind() == io::ErrorKind::TimedOut {
        Failure::Timeout
    } else {
        Failure::Transport(error_chain(error))
    }
}

/// A client's connection settings as Python hands them over, validated as pyreqwest's builder
/// validates them: an address that isn't one is a `ValueError` with Rust's message, and each
/// duration is converted from a `timedelta` as pyreqwest's are (a negative one is a
/// `ValueError`).
#[pyclass(frozen, module = "lothc._native")]
pub(crate) struct ConnectOptions {
    pub(crate) settings: Arc<ConnectSettings>,
}

#[pymethods]
impl ConnectOptions {
    #[new]
    #[pyo3(signature = (resolve, local_address, tcp_keepalive, connect_timeout, read_timeout))]
    fn new(
        resolve: Vec<(String, String)>,
        local_address: Option<&str>,
        tcp_keepalive: Option<Duration>,
        connect_timeout: Option<Duration>,
        read_timeout: Option<Duration>,
    ) -> PyResult<Self> {
        let mut overrides = HashMap::new();
        for (host, ip) in resolve {
            overrides.insert(host.to_ascii_lowercase(), vec![parse_ip(&ip)?]);
        }
        let local_address = local_address.map(parse_ip).transpose()?;
        let keepalive = TcpKeepalive::new()
            .with_time(tcp_keepalive.unwrap_or(KEEPALIVE))
            .with_interval(KEEPALIVE_INTERVAL)
            .with_retries(KEEPALIVE_RETRIES);
        let settings = ConnectSettings { overrides, local_address, keepalive, connect_timeout, read_timeout };
        Ok(Self { settings: Arc::new(settings) })
    }

    /// `local_address`, as Rust writes it, for the async pool's Python side.
    #[getter]
    fn local_address(&self) -> Option<String> {
        self.settings.local_address.map(|ip| ip.to_string())
    }

    /// `connect_timeout` in seconds, for the async pool's Python side.
    #[getter]
    fn connect_timeout(&self) -> Option<f64> {
        self.settings.connect_timeout.map(|t| t.as_secs_f64())
    }

    fn __repr__(&self) -> &'static str {
        "<lothc._native.ConnectOptions>"
    }
}
