//! The minimal HTTP/1.1 the explorer speaks: one request head in, one response out.
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::{collections::HashMap, io::Write, net::TcpStream, time::Duration};

#[derive(Clone, Debug)]
pub(super) struct Request {
    pub method: String,
    pub path: String,
    pub params: HashMap<String, String>,
    pub headers: HashMap<String, String>,
}
impl Request {
    pub fn param(&self, key: &str, default: &str) -> String {
        self.params
            .get(key)
            .cloned()
            .unwrap_or_else(|| default.into())
    }
}
pub(super) fn decode(s: &str) -> Result<String> {
    let mut b = vec![];
    let raw = s.as_bytes();
    let mut i = 0;
    while i < raw.len() {
        match raw[i] {
            b'+' => b.push(b' '),
            b'%' => {
                anyhow::ensure!(i + 2 < raw.len(), "invalid URL escape");
                let hex = std::str::from_utf8(&raw[i + 1..i + 3])?;
                b.push(u8::from_str_radix(hex, 16)?);
                i += 2
            }
            c => b.push(c),
        }
        i += 1
    }
    Ok(String::from_utf8(b)?)
}
pub(super) fn request(raw: &str) -> Result<Request> {
    let mut lines = raw.split("\r\n");
    let words: Vec<_> = lines.next().unwrap_or("").split_whitespace().collect();
    anyhow::ensure!(words.len() == 3, "invalid HTTP request");
    let (path, query) = words[1].split_once('?').unwrap_or((words[1], ""));
    let mut params = HashMap::new();
    for pair in query.split('&').filter(|p| !p.is_empty()) {
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        params.entry(decode(k)?).or_insert(decode(v)?);
    }
    let mut headers = HashMap::new();
    for line in lines.take_while(|s| !s.is_empty()) {
        let (k, v) = line.split_once(':').context("invalid HTTP header")?;
        let k = k.to_ascii_lowercase();
        anyhow::ensure!(!headers.contains_key(&k), "duplicate header");
        headers.insert(k, v.trim().into());
    }
    Ok(Request {
        method: words[0].into(),
        path: path.into(),
        params,
        headers,
    })
}
pub(super) struct Response {
    pub code: u16,
    pub body: Vec<u8>,
    pub headers: Vec<(String, String)>,
}
impl Response {
    pub fn json(code: u16, body: Value) -> Self {
        Self {
            code,
            body: crate::json_output::to_string(&body)
                .expect("serializing JSON value")
                .into_bytes(),
            headers: vec![(
                "Content-Type".into(),
                "application/json; charset=utf-8".into(),
            )],
        }
    }
    pub fn error(code: u16, msg: &str) -> Self {
        Self::json(code, json!({"error":msg}))
    }
    pub fn from_error(error: &anyhow::Error) -> Self {
        use rusqlite::ErrorCode;
        let (code, kind, message) = if let Some(rusqlite::Error::SqliteFailure(e, _)) =
            error.downcast_ref::<rusqlite::Error>()
        {
            match e.code {
                ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked => (
                    503,
                    "storage-unavailable",
                    "database temporarily unavailable".to_owned(),
                ),
                ErrorCode::PermissionDenied | ErrorCode::ReadOnly => {
                    (403, "permission-denied", format!("{error:#}"))
                }
                ErrorCode::DatabaseCorrupt | ErrorCode::NotADatabase => {
                    (500, "invalid-database", format!("{error:#}"))
                }
                ErrorCode::CannotOpen => (503, "database-open", format!("{error:#}")),
                _ => (500, "database-error", format!("{error:#}")),
            }
        } else if let Some(e) = error.downcast_ref::<std::io::Error>() {
            match e.kind() {
                std::io::ErrorKind::NotFound => (404, "not-found", format!("{error:#}")),
                std::io::ErrorKind::PermissionDenied => {
                    (403, "permission-denied", format!("{error:#}"))
                }
                std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut => (
                    503,
                    "source-unavailable",
                    "source temporarily unavailable".to_owned(),
                ),
                _ => (500, "io-error", format!("{error:#}")),
            }
        } else {
            (500, "request-failed", format!("{error:#}"))
        };
        Self::json(code, json!({"error":message,"error_kind":kind}))
    }
    pub fn write(&self, stream: &mut TcpStream) -> std::io::Result<()> {
        let status = match self.code {
            200 => "OK",
            204 => "No Content",
            400 => "Bad Request",
            401 => "Unauthorized",
            403 => "Forbidden",
            404 => "Not Found",
            405 => "Method Not Allowed",
            408 => "Request Timeout",
            413 => "Payload Too Large",
            500 => "Internal Server Error",
            _ => "Service Unavailable",
        };
        write!(stream,"HTTP/1.1 {} {}\r\nContent-Length: {}\r\nConnection: close\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\nReferrer-Policy: no-referrer\r\nCross-Origin-Resource-Policy: same-origin\r\nCross-Origin-Opener-Policy: same-origin\r\nPermissions-Policy: camera=(), microphone=(), geolocation=()\r\n",self.code,status,self.body.len())?;
        for (k, v) in &self.headers {
            write!(stream, "{k}: {v}\r\n")?
        }
        stream.write_all(b"\r\n")?;
        stream.write_all(&self.body)
    }
}
pub(super) const HEADER_TIMEOUT: Duration = Duration::from_secs(10);
pub(super) const HEADER_LIMIT: usize = 16_384;
pub(super) fn read_headers(
    mut read: impl FnMut(&mut [u8], Duration) -> std::io::Result<usize>,
    mut remaining: impl FnMut() -> Duration,
) -> std::io::Result<Vec<u8>> {
    let mut raw = Vec::new();
    let mut byte = [0; 1];
    while raw.len() < HEADER_LIMIT {
        let budget = remaining();
        if budget.is_zero() {
            return Err(std::io::ErrorKind::TimedOut.into());
        }
        if read(&mut byte, budget)? == 0 {
            break;
        }
        raw.push(byte[0]);
        if raw.ends_with(b"\r\n\r\n") {
            break;
        }
    }
    Ok(raw)
}
