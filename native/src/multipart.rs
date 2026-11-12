//! A multipart `form=` body, built as pyreqwest 0.15's `FormBuilder` has reqwest 0.13.5's
//! `multipart::Form` build it (`async_impl/multipart.rs`). lothc's `_form_parts` describes the
//! parts in Python (`_NativeForm`); each becomes a reqwest `Part` here, in order, as pyreqwest's
//! builder makes them:
//!
//! - `Part::text`, `Part::bytes`, or `Part::file`, which opens the file now (its errors are
//!   pyo3's `OSError`s, as pyreqwest raises them) and takes the path's file name and
//!   `mime_guess`'s type for its extension (`application/octet-stream` if none);
//! - then its `file_name`, and its `mime`, parsed by `mime` as pyreqwest's `MimeType` parses it
//!   (a `ValueError` with its message);
//! - each part's headers as reqwest's `PercentEncoding::PathSegment` (pyreqwest's default, which
//!   lothc never changes) encodes them (`encode_headers`);
//! - the body as `Form::into_stream` lays it out, and its length as `compute_length` counts it:
//!   known (so sent as `Content-Length`) unless a file's metadata couldn't be read.
//!
//! reqwest keeps its encoder inside its `multipart` module, behind tokio, so this is a port of
//! it, over the crates it uses (`percent-encoding`, `mime`, `mime_guess`). A file part is read
//! as the body is written (`body.rs`), as reqwest streams one.
use std::borrow::Cow;
use std::collections::VecDeque;
use std::fs::File;
use std::path::PathBuf;
use std::str::FromStr;

use bytes::{Bytes, BytesMut};
use mime::Mime;
use percent_encoding::{utf8_percent_encode, AsciiSet, CONTROLS};
use pyo3::exceptions::PyValueError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};

use crate::body::{RequestBody, Segment, Segments};

// reqwest's sets, from the URL spec: https://url.spec.whatwg.org/#fragment-percent-encode-set
const FRAGMENT_ENCODE_SET: &AsciiSet = &CONTROLS.add(b' ').add(b'"').add(b'<').add(b'>').add(b'`');
// https://url.spec.whatwg.org/#path-percent-encode-set
const PATH_ENCODE_SET: &AsciiSet = &FRAGMENT_ENCODE_SET.add(b'#').add(b'?').add(b'{').add(b'}');
const PATH_SEGMENT_ENCODE_SET: &AsciiSet = &PATH_ENCODE_SET.add(b'/').add(b'%');

/// An in-memory value at least this long is a segment of its own rather than copied into its
/// neighbours' buffer.
const OWN_SEGMENT: usize = 64 * 1024;

enum Value {
    Bytes(Bytes),
    /// `len` is the file's metadata length when it was opened (`None` if that couldn't be read).
    File { file: File, len: Option<u64> },
}

struct Part {
    mime: Option<Mime>,
    file_name: Option<String>,
    value: Value,
}

impl Part {
    fn new(value: Value) -> Self {
        Self { mime: None, file_name: None, value }
    }

    /// reqwest's `Part::file`.
    fn file(path: PathBuf) -> PyResult<Self> {
        let file_name = path.file_name().map(|name| name.to_string_lossy().into_owned());
        let ext = path.extension().and_then(|ext| ext.to_str()).unwrap_or("");
        let mime = mime_guess::from_ext(ext).first_or_octet_stream();
        let file = File::open(&path)?;
        let len = file.metadata().map(|metadata| metadata.len()).ok();
        Ok(Self { mime: Some(mime), file_name, value: Value::File { file, len } })
    }

    /// reqwest's `PartProps::value_len`.
    fn value_len(&self) -> Option<u64> {
        match &self.value {
            Value::Bytes(bytes) => Some(bytes.len() as u64),
            Value::File { len, .. } => *len,
        }
    }

    /// reqwest's `PercentEncoding::PathSegment.encode_headers` (a part has no custom headers).
    fn encode_headers(&self, name: &str) -> Vec<u8> {
        let mut buf = Vec::new();
        buf.extend_from_slice(b"Content-Disposition: form-data; ");
        match Cow::from(utf8_percent_encode(name, PATH_SEGMENT_ENCODE_SET)) {
            Cow::Borrowed(value) => {
                buf.extend_from_slice(b"name=\"");
                buf.extend_from_slice(value.as_bytes());
                buf.extend_from_slice(b"\"");
            }
            Cow::Owned(value) => {
                buf.extend_from_slice(b"name*=utf-8''");
                buf.extend_from_slice(value.as_bytes());
            }
        }
        // RFC 7578 section 4.2 rules out `filename*=`.
        if let Some(filename) = &self.file_name {
            buf.extend_from_slice(b"; filename=\"");
            let legal_filename = filename.replace('\\', "\\\\").replace('"', "\\\"").replace('\r', "\\\r").replace('\n', "\\\n");
            buf.extend_from_slice(legal_filename.as_bytes());
            buf.extend_from_slice(b"\"");
        }
        if let Some(mime) = &self.mime {
            buf.extend_from_slice(b"\r\nContent-Type: ");
            buf.extend_from_slice(mime.as_ref().as_bytes());
        }
        buf
    }
}

/// pyreqwest's `MimeType` extraction of a `str`, then reqwest's `mime_str`.
fn parse_mime(mime: &Bound<'_, PyAny>) -> PyResult<Mime> {
    Mime::from_str(mime.extract::<&str>()?).map_err(|e| PyValueError::new_err(e.to_string()))
}

fn part_name(part: &Bound<'_, PyAny>) -> PyResult<String> {
    part.getattr(intern!(part.py(), "name"))?.extract()
}

/// One described part as pyreqwest builds it: a text part through `FormBuilder.text(name,
/// value)`, which extracts the name first; any other through `PartBuilder.from_bytes` or
/// `from_file`, then `file_name`, `mime` and `FormBuilder.part(name, part)`, in that order.
fn build_part(part: &Bound<'_, PyAny>) -> PyResult<(String, Part)> {
    let py = part.py();
    let content = part.getattr(intern!(py, "content"))?;
    if let Ok(text) = content.cast::<PyString>() {
        let name = part_name(part)?;
        return Ok((name, Part::new(Value::Bytes(Bytes::from(text.to_str()?.to_owned())))));
    }
    let mut built = match content.cast::<PyBytes>() {
        Ok(bytes) => Part::new(Value::Bytes(Bytes::copy_from_slice(bytes.as_bytes()))),
        Err(_) => Part::file(content.extract::<PathBuf>()?)?,
    };
    if let Some(file_name) = part.getattr(intern!(py, "filename"))?.extract::<Option<String>>()? {
        built.file_name = Some(file_name);
    }
    let mime = part.getattr(intern!(py, "mime"))?;
    if !mime.is_none() {
        built.mime = Some(parse_mime(&mime)?);
    }
    Ok((part_name(part)?, built))
}

/// A multipart body, from `_NativeForm` (`body_kind` 4).
pub(crate) struct Form {
    boundary: String,
    fields: Vec<(String, Part)>,
}

/// A form ready to send: what reqwest's `RequestBuilder::multipart` sets.
pub(crate) struct Encoded {
    pub(crate) content_type: String,
    /// `None` when a part's length is unknown, so it goes out chunked.
    pub(crate) length: Option<u64>,
    pub(crate) body: RequestBody,
}

impl Form {
    /// Build every part `form.parts()` describes, in order, each before the next is described.
    pub(crate) fn extract(form: &Bound<'_, PyAny>) -> PyResult<Self> {
        let py = form.py();
        let boundary = form.getattr(intern!(py, "boundary"))?.extract::<String>()?;
        let mut fields = Vec::new();
        for part in form.call_method0(intern!(py, "parts"))?.try_iter()? {
            fields.push(build_part(&part?)?);
        }
        Ok(Self { boundary, fields })
    }

    /// Whether it has no parts: reqwest sends an empty body then, which it can copy for a
    /// redirect or a retry (it streams any other).
    pub(crate) fn is_empty(&self) -> bool {
        self.fields.is_empty()
    }

    /// Its `Content-Type`, `Content-Length` and body (reqwest's `compute_length` and
    /// `into_stream`, an empty body for no parts).
    pub(crate) fn encode(self) -> Encoded {
        let content_type = format!("multipart/form-data; boundary={}", self.boundary);
        if self.fields.is_empty() {
            return Encoded { content_type, length: Some(0), body: RequestBody::full(Bytes::new()) };
        }
        let boundary = self.boundary.as_bytes();
        let boundary_len = boundary.len() as u64;
        let mut length = Some(0u64);
        let mut segments = VecDeque::new();
        let mut pending = BytesMut::new();
        for (name, part) in self.fields {
            let headers = part.encode_headers(&name);
            length = length
                .zip(part.value_len())
                .map(|(total, value_len)| total + 2 + boundary_len + 2 + headers.len() as u64 + 4 + value_len + 2);
            pending.extend_from_slice(b"--");
            pending.extend_from_slice(boundary);
            pending.extend_from_slice(b"\r\n");
            pending.extend_from_slice(&headers);
            pending.extend_from_slice(b"\r\n\r\n");
            match part.value {
                Value::Bytes(bytes) if bytes.len() < OWN_SEGMENT => pending.extend_from_slice(&bytes),
                Value::Bytes(bytes) => {
                    segments.push_back(Segment::Bytes(pending.split().freeze()));
                    segments.push_back(Segment::Bytes(bytes));
                }
                Value::File { file, len } => {
                    segments.push_back(Segment::Bytes(pending.split().freeze()));
                    segments.push_back(Segment::File { file, expected: len.unwrap_or(0) });
                }
            }
            pending.extend_from_slice(b"\r\n");
        }
        pending.extend_from_slice(b"--");
        pending.extend_from_slice(boundary);
        pending.extend_from_slice(b"--\r\n");
        let length = length.map(|total| total + 2 + boundary_len + 4);
        let body = if segments.is_empty() {
            RequestBody::full(pending.freeze())
        } else {
            segments.push_back(Segment::Bytes(pending.freeze()));
            RequestBody::Segments(Segments::new(segments))
        };
        Encoded { content_type, length, body }
    }
}
