//! TLS as reqwest 0.13's `rustls` setup has it (aws-lc-rs, the OS trust store through
//! rustls-platform-verifier, `root_certificates` as extra roots, `identity_pem` as the client
//! cert, `min_tls_version`/`max_tls_version` filtering the protocol versions, ALPN `http/1.1`
//! since lothc is HTTP/1.1-only), over the same non-blocking sockets as plain HTTP. rustls is
//! sans-I/O: records read from the socket go into `read_tls` + `process_new_packets`, plaintext
//! comes out of `reader()`, and whatever rustls wants sent (handshake, alerts, encrypted writes)
//! goes out through `write_tls`.
use std::io::{self, Read as _, Write as _};
use std::os::fd::RawFd;
use std::pin::Pin;
use std::sync::Arc;
use std::task::{Context, Poll};

use pyo3::exceptions::PyValueError;
use pyo3::PyResult;
use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::CryptoProvider;
use rustls::pki_types::pem::{self, PemObject, SectionKind};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime};
use rustls::sign::{CertifiedKey, SingleCertAndKey};
use rustls::{
    ClientConfig, ClientConnection, DigitallySignedStruct, ProtocolVersion, SignatureScheme, SupportedProtocolVersion,
};

use crate::error::ClientBuildError;
use crate::io::{recv, send, Readiness};

/// One `recv`'s worth of TLS records: a full record plus its header and tag.
const RECORD_BUFFER: usize = 16 * 1024 + 256;

/// `send(2)` for rustls's `write_tls`.
struct FdWriter(RawFd);

impl io::Write for FdWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        send(self.0, buf)
    }

    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

pub(crate) struct TlsSocket<R> {
    fd: RawFd,
    ready: R,
    tls: ClientConnection,
    /// Records as received, before rustls decrypts them.
    rx: Box<[u8]>,
    /// Plaintext out of rustls, before it is copied into hyper's buffer. Kept, so a read doesn't
    /// zero a fresh 16KB every time.
    plain: Box<[u8]>,
}

fn tls_error(error: rustls::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}

impl<R: Readiness> TlsSocket<R> {
    pub(crate) fn new(fd: RawFd, ready: R, tls: ClientConnection) -> Self {
        ready.set_handshaking(true);
        Self {
            fd,
            ready,
            tls,
            rx: vec![0; RECORD_BUFFER].into_boxed_slice(),
            plain: vec![0; 16 * 1024].into_boxed_slice(),
        }
    }

    /// Send everything rustls has queued. `Pending` means the socket's send buffer is full and a
    /// wake-up for writability is arranged; what is left stays queued in rustls.
    fn poll_flush_tls(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        while self.tls.wants_write() {
            match self.tls.write_tls(&mut FdWriter(self.fd)) {
                Ok(_) => {}
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                    self.ready.wait_writable(cx);
                    return Poll::Pending;
                }
                Err(error) => return Poll::Ready(Err(error)),
            }
        }
        Poll::Ready(Ok(()))
    }

    /// Feed one `recv` worth of records to rustls; `Ok(0)` is the peer closing the connection.
    fn poll_fill(&mut self, cx: &mut Context<'_>) -> Poll<io::Result<usize>> {
        if !self.ready.poll_readable(cx) {
            return Poll::Pending;
        }
        let n = match recv(self.fd, &mut self.rx) {
            Ok(n) => n,
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                self.ready.wait_readable(cx);
                return Poll::Pending;
            }
            Err(error) => return Poll::Ready(Err(error)),
        };
        if n > 0 && n < self.rx.len() {
            self.ready.mark_drained();
        }
        let mut records = &self.rx[..n];
        loop {
            // An empty slice tells rustls the peer closed (EOF).
            if let Err(error) = self.tls.read_tls(&mut records) {
                return Poll::Ready(Err(error));
            }
            if let Err(error) = self.tls.process_new_packets() {
                // Send the alert rustls queued for the server; the error is what matters.
                let _ = self.tls.write_tls(&mut FdWriter(self.fd));
                return Poll::Ready(Err(tls_error(error)));
            }
            if records.is_empty() {
                if !self.tls.is_handshaking() {
                    self.ready.set_handshaking(false);
                }
                return Poll::Ready(Ok(n));
            }
        }
    }
}

impl<R: Readiness> hyper::rt::Read for TlsSocket<R> {
    fn poll_read(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        mut buf: hyper::rt::ReadBufCursor<'_>,
    ) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        if buf.remaining() == 0 {
            return Poll::Ready(Ok(()));
        }
        loop {
            let want = buf.remaining().min(this.plain.len());
            match this.tls.reader().read(&mut this.plain[..want]) {
                // Ok(0) is a clean close_notify: EOF for hyper.
                Ok(n) => {
                    buf.put_slice(&this.plain[..n]);
                    return Poll::Ready(Ok(()));
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => {}
                Err(error) => return Poll::Ready(Err(error)),
            }
            // Handshake messages may be waiting to go out; a full send buffer doesn't stop the
            // read, it only means the flush resumes when the socket is writable.
            if let Poll::Ready(Err(error)) = this.poll_flush_tls(cx) {
                return Poll::Ready(Err(error));
            }
            match this.poll_fill(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Err(error)) => return Poll::Ready(Err(error)),
                Poll::Ready(Ok(_)) => {}
            }
        }
    }
}

impl<R: Readiness> hyper::rt::Write for TlsSocket<R> {
    fn poll_write(self: Pin<&mut Self>, cx: &mut Context<'_>, buf: &[u8]) -> Poll<io::Result<usize>> {
        let this = self.get_mut();
        loop {
            // Before the handshake finishes rustls buffers plaintext and sends it once it can.
            let n = match this.tls.writer().write(buf) {
                Ok(n) => n,
                Err(error) => return Poll::Ready(Err(error)),
            };
            if n > 0 || buf.is_empty() {
                // Push out what the socket takes now; the rest stays queued in rustls.
                if let Poll::Ready(Err(error)) = this.poll_flush_tls(cx) {
                    return Poll::Ready(Err(error));
                }
                return Poll::Ready(Ok(n));
            }
            // rustls's buffer is full: drain it to the socket first, and if the handshake is
            // what holds the plaintext back, read the server's side of it.
            match this.poll_flush_tls(cx) {
                Poll::Pending => return Poll::Pending,
                Poll::Ready(Err(error)) => return Poll::Ready(Err(error)),
                Poll::Ready(Ok(())) => {}
            }
            if this.tls.is_handshaking() {
                match this.poll_fill(cx) {
                    Poll::Pending => return Poll::Pending,
                    Poll::Ready(Err(error)) => return Poll::Ready(Err(error)),
                    Poll::Ready(Ok(0)) => {
                        return Poll::Ready(Err(io::Error::new(
                            io::ErrorKind::UnexpectedEof,
                            "the server closed the connection during the TLS handshake",
                        )))
                    }
                    Poll::Ready(Ok(_)) => {}
                }
            }
        }
    }

    fn poll_flush(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.get_mut().poll_flush_tls(cx)
    }

    fn poll_shutdown(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        let this = self.get_mut();
        this.tls.send_close_notify();
        this.poll_flush_tls(cx)
    }
}

/// `danger_accept_invalid_certs=True`: any certificate, signatures still checked.
#[derive(Debug)]
struct AcceptAnyCert(Arc<CryptoProvider>);

impl ServerCertVerifier for AcceptAnyCert {
    fn verify_server_cert(
        &self,
        _end_entity: &CertificateDer<'_>,
        _intermediates: &[CertificateDer<'_>],
        _server_name: &ServerName<'_>,
        _ocsp: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls12_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        rustls::crypto::verify_tls13_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// `min_tls_version`/`max_tls_version` as pyreqwest parses them (`parse_tls_version`, same
/// message), ranked so they compare like reqwest's `tls::Version`.
fn tls_version_rank(version: &str) -> PyResult<u8> {
    match version {
        "TLSv1.0" => Ok(0),
        "TLSv1.1" => Ok(1),
        "TLSv1.2" => Ok(2),
        "TLSv1.3" => Ok(3),
        _ => Err(PyValueError::new_err("Invalid TLS version. Use 'TLSv1.0', 'TLSv1.1', 'TLSv1.2', or 'TLSv1.3'")),
    }
}

/// reqwest's `tls::Version::from_rustls`: `None` for a version it doesn't know.
fn rustls_version_rank(version: ProtocolVersion) -> Option<u8> {
    match version {
        ProtocolVersion::TLSv1_0 => Some(0),
        ProtocolVersion::TLSv1_1 => Some(1),
        ProtocolVersion::TLSv1_2 => Some(2),
        ProtocolVersion::TLSv1_3 => Some(3),
        _ => None,
    }
}

/// The protocol versions rustls is allowed, filtered as reqwest's `ClientBuilder::build` filters
/// `rustls::ALL_VERSIONS` (TLS 1.2 and 1.3): a version reqwest doesn't know stays in under a
/// minimum and drops out under a maximum, and an empty result is a builder error. So a TLS 1.0 or
/// 1.1 minimum allows everything rustls has, and a TLS 1.0 or 1.1 maximum fails.
fn protocol_versions(min: Option<u8>, max: Option<u8>) -> PyResult<Vec<&'static SupportedProtocolVersion>> {
    let versions: Vec<_> = rustls::ALL_VERSIONS
        .iter()
        .copied()
        .filter(|supported| {
            let rank = rustls_version_rank(supported.version);
            let above_min = min.is_none_or(|min| rank.is_none_or(|rank| rank >= min));
            let below_max = max.is_none_or(|max| rank.is_some_and(|rank| rank <= max));
            above_min && below_max
        })
        .collect();
    if versions.is_empty() {
        return Err(ClientBuildError::new_err("builder error: empty supported tls versions"));
    }
    Ok(versions)
}

/// The cert chain and key in an `identity_pem`, parsed as reqwest's `Identity::from_pem` parses
/// them: every certificate in order, the last private key (PKCS#8, PKCS#1 or SEC1), and any other
/// PEM section is an error. Its messages are reqwest's; pyreqwest's `ValueError` says only
/// "builder error", which ours starts with.
fn parse_identity_pem(pem: &[u8]) -> PyResult<(Vec<CertificateDer<'static>>, PrivateKeyDer<'static>)> {
    let invalid = |message: &str| PyValueError::new_err(format!("builder error: {message}"));
    let mut reader = io::Cursor::new(pem);
    let mut keys = Vec::new();
    let mut certs = Vec::new();
    while let Some((kind, data)) = pem::from_buf(&mut reader).map_err(|_| invalid("Invalid identity PEM file"))? {
        match kind {
            SectionKind::Certificate => certs.push(data.into()),
            SectionKind::PrivateKey => keys.push(PrivateKeyDer::Pkcs8(data.into())),
            SectionKind::RsaPrivateKey => keys.push(PrivateKeyDer::Pkcs1(data.into())),
            SectionKind::EcPrivateKey => keys.push(PrivateKeyDer::Sec1(data.into())),
            _ => return Err(invalid("No valid certificate was found")),
        }
    }
    match (keys.pop(), certs.is_empty()) {
        (Some(key), false) => Ok((certs, key)),
        _ => Err(invalid("private key or certificate not found")),
    }
}

/// The client's TLS settings. The rustls config is only built on the first HTTPS connection (an
/// http-only client never loads the trust store), but everything that can be wrong with them is
/// checked at construction, in the order pyreqwest's builder checks it, so a bad one fails when
/// the client is opened, as it does with pyreqwest, even on a client that only ever speaks HTTP.
pub(crate) struct TlsSettings {
    roots: Vec<CertificateDer<'static>>,
    danger_accept_invalid_certs: bool,
    versions: Vec<&'static SupportedProtocolVersion>,
    /// `identity_pem`'s chain and key, loaded and checked against each other (rustls's
    /// `CertifiedKey::from_der`, which `with_client_auth_cert` runs inside reqwest's `build()`).
    identity: Option<Arc<CertifiedKey>>,
}

impl TlsSettings {
    pub(crate) fn new(
        root_certificates: &[Vec<u8>],
        identity_pem: Option<&[u8]>,
        min_tls_version: Option<&str>,
        max_tls_version: Option<&str>,
        danger_accept_invalid_certs: bool,
    ) -> PyResult<Self> {
        let mut roots = Vec::new();
        for pem in root_certificates {
            for cert in CertificateDer::pem_slice_iter(pem) {
                roots.push(cert.map_err(|e| PyValueError::new_err(format!("invalid root certificate: {e}")))?);
            }
        }
        let identity = identity_pem.map(parse_identity_pem).transpose()?;
        let min = min_tls_version.map(tls_version_rank).transpose()?;
        let max = max_tls_version.map(tls_version_rank).transpose()?;
        let versions = protocol_versions(min, max)?;
        let identity = match identity {
            None => None,
            Some((certs, key)) => {
                let provider = rustls::crypto::aws_lc_rs::default_provider();
                let certified = CertifiedKey::from_der(certs, key, &provider)
                    .map_err(|e| PyValueError::new_err(format!("builder error: {e}")))?;
                Some(Arc::new(certified))
            }
        };
        Ok(Self { roots, danger_accept_invalid_certs, versions, identity })
    }

    pub(crate) fn client_config(&self) -> Result<Arc<ClientConfig>, String> {
        let provider = Arc::new(rustls::crypto::aws_lc_rs::default_provider());
        let builder = ClientConfig::builder_with_provider(provider.clone())
            .with_protocol_versions(&self.versions)
            .map_err(|e| e.to_string())?
            .dangerous();
        let builder = if self.danger_accept_invalid_certs {
            builder.with_custom_certificate_verifier(Arc::new(AcceptAnyCert(provider)))
        } else {
            let verifier = rustls_platform_verifier::Verifier::new_with_extra_roots(self.roots.clone(), provider)
                .map_err(|e| e.to_string())?;
            builder.with_custom_certificate_verifier(Arc::new(verifier))
        };
        let mut config = match &self.identity {
            None => builder.with_no_client_auth(),
            Some(identity) => builder.with_client_cert_resolver(Arc::new(SingleCertAndKey::from(identity.clone()))),
        };
        config.alpn_protocols = vec![b"http/1.1".to_vec()];
        Ok(Arc::new(config))
    }
}
