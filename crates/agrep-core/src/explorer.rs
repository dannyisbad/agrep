//! Small authenticated loopback HTTP server. All history comes from shared core APIs.
mod api;
use crate::{
    data::DataRoot,
    live::{self, Service},
    query::{self, Filters, Options},
    store,
};
use anyhow::{Context, Result};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream},
    sync::{Arc, Mutex},
    time::Duration,
};
const APP: &str = include_str!("../../../web/app.html");
fn token() -> Result<String> {
    let mut b = [0u8; 32];
    getrandom::getrandom(&mut b).map_err(|e| anyhow::anyhow!("random token unavailable: {e}"))?;
    Ok(b.iter().map(|n| format!("{n:02x}")).collect())
}
fn equal(a: &str, b: &str) -> bool {
    a.len() == b.len()
        && a.bytes()
            .zip(b.bytes())
            .fold(0u8, |diff, (a, b)| diff | (a ^ b))
            == 0
}
#[derive(Clone, Debug)]
struct Request {
    method: String,
    path: String,
    params: HashMap<String, String>,
    headers: HashMap<String, String>,
}
fn decode(s: &str) -> Result<String> {
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
fn request(raw: &str) -> Result<Request> {
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
struct Response {
    code: u16,
    body: Vec<u8>,
    headers: Vec<(String, String)>,
}
impl Response {
    fn json(code: u16, body: Value) -> Self {
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
    fn error(code: u16, msg: &str) -> Self {
        Self::json(code, json!({"error":msg}))
    }
    fn from_error(error: &anyhow::Error) -> Self {
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
    fn write(&self, stream: &mut TcpStream) -> std::io::Result<()> {
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
const HEADER_TIMEOUT: Duration = Duration::from_secs(10);
const HEADER_LIMIT: usize = 16_384;
fn read_headers(
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

pub struct Explorer {
    listener: TcpListener,
    state: Arc<State>,
}
struct State {
    root: DataRoot,
    port: u16,
    auth: String,
    bootstrap: Mutex<String>,
    live: Mutex<Option<Service>>,
    freshen: Option<Arc<dyn Fn() -> Result<()> + Send + Sync>>,
}
impl Explorer {
    pub fn bind(root: DataRoot, port: u16) -> Result<Self> {
        let listener = TcpListener::bind(SocketAddrV4::new(Ipv4Addr::LOCALHOST, port))
            .context("explorer could not bind to loopback")?;
        let port = listener.local_addr()?.port();
        listener.set_nonblocking(true)?;
        Ok(Self {
            listener,
            state: Arc::new(State {
                root,
                port,
                auth: token()?,
                bootstrap: Mutex::new(token()?),
                live: Mutex::new(None),
                freshen: None,
            }),
        })
    }
    /// The CLI supplies its normal freshness entry point; HTTP reads use the
    /// same census and query APIs as command-line history.
    pub fn with_refresh(
        mut self,
        freshen: impl Fn() -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        Arc::get_mut(&mut self.state)
            .expect("server not yet shared")
            .freshen = Some(Arc::new(freshen));
        self
    }
    pub fn port(&self) -> u16 {
        self.state.port
    }
    pub fn url(&self) -> String {
        format!(
            "http://127.0.0.1:{}/#bootstrap={}",
            self.state.port,
            self.state.bootstrap.lock().unwrap()
        )
    }
    pub fn serve(&self, signals: &crate::terminal::Signals) -> Result<()> {
        let active = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        while signals.pending() == 0 {
            match self.listener.accept() {
                Ok((stream, peer)) => {
                    if !peer.ip().is_loopback() {
                        continue;
                    }
                    if active.load(std::sync::atomic::Ordering::Relaxed) >= 16 {
                        continue;
                    }
                    active.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    let count = active.clone();
                    let state = self.state.clone();
                    std::thread::spawn(move || {
                        let _ = state.connection(stream);
                        count.fetch_sub(1, std::sync::atomic::Ordering::Relaxed);
                    });
                }
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(20))
                }
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    }
}
impl State {
    fn connection(&self, stream: TcpStream) -> Result<()> {
        self.connection_with_progress(stream, || {})
    }
    fn connection_with_progress(
        &self,
        mut stream: TcpStream,
        mut progress: impl FnMut(),
    ) -> Result<()> {
        // macOS accepted sockets inherit O_NONBLOCK from the listener.
        stream.set_nonblocking(false)?;
        stream.set_write_timeout(Some(Duration::from_secs(10)))?;
        let deadline = std::time::Instant::now() + HEADER_TIMEOUT;
        let raw = match read_headers(
            |byte, budget| {
                stream.set_read_timeout(Some(budget))?;
                let n = stream.read(byte)?;
                if n > 0 {
                    progress();
                }
                Ok(n)
            },
            || deadline.saturating_duration_since(std::time::Instant::now()),
        ) {
            Ok(raw) => raw,
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                ) =>
            {
                Response::error(408, "request header deadline exceeded").write(&mut stream)?;
                return Ok(());
            }
            Err(e) => return Err(e.into()),
        };
        if raw.is_empty() {
            return Ok(());
        }
        let response = if raw.len() >= HEADER_LIMIT {
            Response::error(413, "request headers are too large")
        } else {
            match std::str::from_utf8(&raw).ok().and_then(|s| request(s).ok()) {
                Some(r) => self.route(&r),
                None => Response::error(400, "invalid request"),
            }
        };
        response.write(&mut stream)?;
        Ok(())
    }
    fn route(&self, r: &Request) -> Response {
        let host = r.headers.get("host").map(String::as_str).unwrap_or("");
        let hosts = [
            format!("127.0.0.1:{}", self.port),
            format!("localhost:{}", self.port),
        ];
        if !hosts.iter().any(|h| h == host) {
            return Response::error(403, "invalid host");
        }
        if r.method == "POST" {
            if !r
                .headers
                .get("origin")
                .is_some_and(|o| hosts.iter().any(|h| o == &format!("http://{h}")))
            {
                return Response::error(403, "invalid browser origin");
            }
            if r.path != "/auth" {
                return Response::error(405, "the explorer is read-only");
            }
            let length = match r
                .headers
                .get("content-length")
                .map(|s| s.parse::<i64>())
                .transpose()
            {
                Ok(n) => n.unwrap_or(0),
                Err(_) => return Response::error(400, "invalid request length"),
            };
            if length != 0 || r.headers.contains_key("transfer-encoding") {
                return Response::error(413, "authentication requests have no body");
            }
            let mut expected = self.bootstrap.lock().unwrap();
            let supplied = r
                .headers
                .get("x-agrep-bootstrap")
                .map(String::as_str)
                .unwrap_or("");
            if expected.is_empty() || !equal(supplied, &expected) {
                return Response::error(403, "explorer link is invalid or already used");
            }
            expected.clear();
            return Response::json(200, json!({"token":self.auth}));
        }
        if r.method != "GET" {
            return Response::error(405, "the explorer is read-only");
        }
        if r.path == "/" {
            let nonce = match token() {
                Ok(t) => t,
                Err(_) => return Response::error(503, "random token unavailable"),
            };
            let html = APP
                .replacen("<style>", &format!("<style nonce=\"{nonce}\">"), 1)
                .replacen("<script>", &format!("<script nonce=\"{nonce}\">"), 1);
            return Response{code:200,body:html.into_bytes(),headers:vec![("Content-Type".into(),"text/html; charset=utf-8".into()),("Content-Security-Policy".into(),format!("default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"))]};
        }
        let authorized = r
            .headers
            .get("authorization")
            .and_then(|value| value.strip_prefix("Bearer "))
            .is_some_and(|value| equal(value, &self.auth));
        if !authorized {
            return Response::error(401, "open a fresh explorer link with `agrep ui`");
        }
        match self.api(r) {
            Ok(response) => response,
            Err(error) => Response::from_error(&error),
        }
    }
    fn api(&self, r: &Request) -> Result<Response> {
        let param =
            |key: &str, default: &str| r.params.get(key).cloned().unwrap_or_else(|| default.into());
        if r.path == "/api/board" {
            let mut live = self.live.lock().unwrap();
            let watcher = live.get_or_insert_with(Service::new);
            let snapshot = watcher.snapshot(Duration::from_millis(180));
            return Ok(Response::json(200, live::board::public(&snapshot)));
        }
        if !["/api/status", "/api/chats", "/api/around", "/api/search"].contains(&r.path.as_str()) {
            return Ok(Response::error(404, "not found"));
        }
        if let Some(freshen) = &self.freshen {
            freshen()?;
        }
        let c = store::reader(&self.root)?;
        match r.path.as_str() {
            "/api/status" => {
                let sessions: i64 =
                    c.query_row("SELECT COUNT(*) FROM sessions", [], |r| r.get(0))?;
                let messages: i64 = c.query_row(
                    "SELECT COUNT(*) FROM prose WHERE visible=1 AND who!='agent'",
                    [],
                    |r| r.get(0),
                )?;
                Ok(Response::json(
                    200,
                    json!({"messages":messages,"sessions":sessions,"indexed":true}),
                ))
            }
            "/api/chats" => {
                let n = match param("n", "80").parse::<i64>() {
                    Ok(n) => n.clamp(1, 200) as usize,
                    Err(_) => return Ok(Response::error(400, "n must be an integer")),
                };
                let side = param("side", "0");
                if !["0", "1"].contains(&side.as_str()) {
                    return Ok(Response::error(400, "side must be 0 or 1"));
                }
                Ok(Response::json(200, api::chats(&c, n, side == "1")?))
            }
            "/api/around" => {
                let id = param("session", "");
                if id.trim().is_empty() || id.len() > 4096 {
                    return Ok(Response::error(400, "session is required"));
                }
                let (turn, radius) = match (
                    param("turn", "0").parse::<i64>(),
                    param("radius", "4").parse::<i64>(),
                ) {
                    (Ok(t), Ok(r)) => (t, r.clamp(0, 12)),
                    _ => return Ok(Response::error(400, "turn and radius must be integers")),
                };
                let window = match api::window(&c, &id, turn, radius) {
                    Ok(w) => w,
                    Err(e) if e.downcast_ref::<rusqlite::Error>().is_none() => {
                        return Ok(Response::error(400, &e.to_string()))
                    }
                    Err(e) => return Err(e),
                };
                Ok(match window {
                    Some(v) => Response::json(200, v),
                    None => Response::error(404, "chat is no longer indexed"),
                })
            }
            "/api/search" => {
                let q = param("q", "").trim().to_owned();
                if q.is_empty() {
                    return Ok(Response::error(400, "enter a search query"));
                }
                if q.len() > 4096 {
                    return Ok(Response::error(413, "search query is too large"));
                }
                let mode = param("mode", "keyword");
                if !["keyword", "semantic"].contains(&mode.as_str()) {
                    return Ok(Response::error(400, "mode must be keyword or semantic"));
                }
                let sort = param("sort", "score");
                if !["score", "time"].contains(&sort.as_str()) {
                    return Ok(Response::error(400, "sort must be score or time"));
                }
                if mode == "semantic" {
                    return Ok(Response::error(400, "this explorer accepts keyword search"));
                }
                let n = match param("n", "40").parse::<i64>() {
                    Ok(n) => n.clamp(1, 100) as usize,
                    Err(_) => return Ok(Response::error(400, "n must be an integer")),
                };
                let result = match query::search(
                    &c,
                    &Options {
                        pattern: q.clone(),
                        regex: false,
                        word: false,
                        sort: sort.clone(),
                        limit: n,
                        exact: false,
                        count_only: false,
                        count_by_tier: false,
                        content: false,
                        chats: false,
                        tools: self.root.config()?.tools,
                        filters: Filters::default(),
                    },
                ) {
                    Ok(result) => result,
                    Err(e) if e.downcast_ref::<rusqlite::Error>().is_none() => {
                        return Ok(Response::error(400, &e.to_string()))
                    }
                    Err(e) => return Err(e),
                };
                Ok(Response::json(
                    200,
                    json!({"query":q,"mode":mode,"sort":sort,"engine":"keyword","total":result.total,"totals_exact":result.total_basis=="exact","hits":result.hits}),
                ))
            }
            _ => Ok(Response::error(404, "not found")),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn req(port: u16, method: &str, path: &str, headers: &str) -> Request {
        request(&format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{headers}\r\n"
        ))
        .unwrap()
    }
    #[test]
    fn one_use_auth_origin_host_and_asset_policy() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path().join("data"), true).unwrap();
        let server = Explorer::bind(root, 0).unwrap();
        assert!(server.listener.local_addr().unwrap().ip().is_loopback());
        let s = &server.state;
        let port = server.port();
        let r = s.route(&req(port, "GET", "/", ""));
        assert_eq!(r.code, 200);
        let html = String::from_utf8(r.body).unwrap();
        assert!(html.contains("nonce=\""));
        assert!(html.contains("/api/board"));
        assert!(r
            .headers
            .iter()
            .any(|(k, v)| k == "Content-Security-Policy" && v.contains("default-src 'none'")));
        assert_eq!(s.route(&req(port, "GET", "/api/status", "")).code, 401);
        let mut evil = req(port, "GET", "/", "");
        evil.headers
            .insert("host".into(), "attacker.example".into());
        assert_eq!(s.route(&evil).code, 403);
        let bootstrap = s.bootstrap.lock().unwrap().clone();
        let headers=format!("Origin: http://127.0.0.1:{port}\r\nX-Agrep-Bootstrap: {bootstrap}\r\nContent-Length: 0\r\n");
        assert_eq!(s.route(&req(port, "POST", "/auth", "")).code, 403);
        let r = s.route(&req(port, "POST", "/auth", &headers));
        assert_eq!(r.code, 200);
        let body: Value = serde_json::from_slice(&r.body).unwrap();
        assert_eq!(body["token"], s.auth);
        assert!(!r
            .headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case("set-cookie")));
        assert_eq!(s.route(&req(port, "POST", "/auth", &headers)).code, 403);
        assert_eq!(s.route(&req(port, "POST", "/delete", &headers)).code, 405);
        let headers = format!("Authorization: Bearer {}\r\n", s.auth);
        assert_eq!(s.route(&req(port, "GET", "/unknown", &headers)).code, 404);
        assert!(!server.url().contains(&bootstrap));
    }
    #[test]
    fn api_shapes_bounds_and_real_socket() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path().join("data"), true).unwrap();
        let c = store::writer(&root).unwrap();
        c.execute_batch("INSERT INTO sessions VALUES('pi','session-one','demo','/work/demo','','session-one',0,'Title',1,100);INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position,visible) VALUES(1,'pi','session-one','demo',0,'user','model','',1,X'68656c6c6f','x','',0,1);").unwrap();
        let server = Explorer::bind(root, 0).unwrap();
        let s = &server.state;
        let headers = format!("Authorization: Bearer {}\r\n", s.auth);
        let p = server.port();
        let status = s.route(&req(p, "GET", "/api/status", &headers));
        assert_eq!(status.code, 200);
        let status: Value = serde_json::from_slice(&status.body).unwrap();
        assert_eq!(status["messages"], 1);
        let chats = s.route(&req(p, "GET", "/api/chats", &headers));
        let chats: Value = serde_json::from_slice(&chats.body).unwrap();
        assert_eq!(chats["chats"][0]["first_text"], "Title");
        let around = s.route(&req(
            p,
            "GET",
            "/api/around?session=session-&turn=999",
            &headers,
        ));
        let around: Value = serde_json::from_slice(&around.body).unwrap();
        assert_eq!(around["center"], 0);
        assert_eq!(around["turns"][0]["text"], "hello");
        assert!(around["events"].is_array());
        for (path, code) in [
            ("/api/search?q=", 400),
            ("/api/search?q=!!", 200),
            ("/api/search?q=x&sort=bad", 400),
            ("/api/search?q=x&n=no", 400),
            ("/api/search?q=x&mode=semantic&sort=time", 400),
            ("/api/chats?side=yes", 400),
            ("/api/around?session=nope", 404),
            ("/unknown", 404),
        ] {
            assert_eq!(s.route(&req(p, "GET", path, &headers)).code, code, "{path}");
        }
        server.listener.set_nonblocking(false).unwrap();
        let mut client = TcpStream::connect((Ipv4Addr::LOCALHOST, p)).unwrap();
        let state = server.state.clone();
        let worker = std::thread::spawn(move || {
            let (stream, _) = server.listener.accept().unwrap();
            state.connection(stream).unwrap();
        });
        client
            .write_all(
                format!("GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n{headers}\r\n")
                    .as_bytes(),
            )
            .unwrap();
        let mut body = String::new();
        client.read_to_string(&mut body).unwrap();
        worker.join().unwrap();
        assert!(body.starts_with("HTTP/1.1 200"));
        assert!(body.contains("\"messages\":1"));
    }
    #[test]
    fn query_decoder_and_duplicate_headers() {
        let r = request(
            "GET /api/search?q=hello+%E4%B8%96%E7%95%8C HTTP/1.1\r\nHost: localhost:1\r\n\r\n",
        )
        .unwrap();
        assert_eq!(r.params["q"], "hello 世界");
        assert!(request("GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n").is_err());
        assert!(decode("%GG").is_err());
        assert!(decode("%世").is_err());
    }
}

#[cfg(test)]
mod freshness_tests {
    use super::*;
    #[test]
    fn normal_freshness_callback_runs_before_history_read() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path(), true).unwrap();
        store::writer(&root).unwrap();
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let count = calls.clone();
        let server = Explorer::bind(root, 0).unwrap().with_refresh(move || {
            count.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            Ok(())
        });
        let s = &server.state;
        let r = request(&format!(
            "GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\r\n",
            s.port, s.auth
        ))
        .unwrap();
        assert_eq!(s.route(&r).code, 200);
        assert_eq!(calls.load(std::sync::atomic::Ordering::Relaxed), 1);
    }
}

#[cfg(test)]
mod socket_tests {
    use super::*;
    #[test]
    fn accepted_nonblocking_socket_waits_for_request() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path(), true).unwrap();
        let server = Explorer::bind(root, 0).unwrap();
        server.listener.set_nonblocking(false).unwrap();
        let mut client = TcpStream::connect((Ipv4Addr::LOCALHOST, server.port())).unwrap();
        let (stream, _) = server.listener.accept().unwrap();
        stream.set_nonblocking(true).unwrap();
        let state = server.state.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        client.write_all(b"G").unwrap();
        let worker = std::thread::spawn(move || {
            let mut announced = false;
            tx.send(state.connection_with_progress(stream, || {
                if !announced {
                    ready_tx.send(()).unwrap();
                    announced = true;
                }
            }))
            .unwrap();
        });
        ready_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        write!(
            client,
            "ET / HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n\r\n",
            server.port()
        )
        .unwrap();
        let mut response = String::new();
        client.read_to_string(&mut response).unwrap();
        assert!(response.starts_with("HTTP/1.1 200"));
        rx.recv_timeout(Duration::from_secs(2)).unwrap().unwrap();
        worker.join().unwrap();
    }
}

#[cfg(test)]
mod boundary_tests {
    use super::*;
    #[test]
    fn cookie_alone_never_authorizes_or_refreshes() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path(), true).unwrap();
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let seen = calls.clone();
        let server = Explorer::bind(root, 0).unwrap().with_refresh(move || {
            seen.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            Ok(())
        });
        let s = &server.state;
        use sha2::{Digest, Sha256};
        let legacy = format!(
            "agrep_ui_{:x}",
            Sha256::digest(format!("{}\0{}", s.root.path().display(), s.port).as_bytes())
        )[..21]
            .to_owned();
        let request = request(&format!(
            "GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nCookie: {}={}\r\n\r\n",
            s.port, legacy, s.auth
        ))
        .unwrap();
        assert_eq!(s.route(&request).code, 401);
        assert_eq!(calls.load(std::sync::atomic::Ordering::Relaxed), 0);
    }
    #[test]
    fn refreshing_preserves_error_class_and_cause() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path(), true).unwrap();
        let server = Explorer::bind(root, 0).unwrap().with_refresh(|| {
            Err(std::io::Error::new(
                std::io::ErrorKind::PermissionDenied,
                "cannot read fixture transcript",
            )
            .into())
        });
        let s = &server.state;
        let r = request(&format!(
            "GET /api/status HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n\r\n",
            s.port, s.auth
        ))
        .unwrap();
        let response = s.route(&r);
        assert_eq!(response.code, 403);
        let body: Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(body["error_kind"], "permission-denied");
        assert!(body["error"]
            .as_str()
            .unwrap()
            .contains("cannot read fixture transcript"));
        let error = anyhow::Error::from(rusqlite::Error::SqliteFailure(
            rusqlite::ffi::Error::new(rusqlite::ffi::SQLITE_CORRUPT),
            Some("malformed database".into()),
        ));
        assert_eq!(Response::from_error(&error).code, 500);
    }
    #[test]
    fn progress_does_not_reset_the_header_deadline() {
        let mut budgets = vec![];
        let mut remaining = [
            Duration::from_secs(10),
            Duration::from_secs(3),
            Duration::ZERO,
        ]
        .into_iter();
        let error = read_headers(
            |byte, budget| {
                budgets.push(budget);
                byte[0] = b'G';
                Ok(1)
            },
            || remaining.next().unwrap(),
        )
        .unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::TimedOut);
        assert_eq!(budgets, [Duration::from_secs(10), Duration::from_secs(3)]);
    }
}
