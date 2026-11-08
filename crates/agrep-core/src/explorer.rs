//! Small authenticated loopback HTTP server. All history comes from shared core APIs.
mod api;
mod endpoints;
mod http;
use crate::{
    data::DataRoot,
    live::{self, Service},
    store,
};
use anyhow::{Context, Result};
use http::{read_headers, request, Request, Response, HEADER_LIMIT, HEADER_TIMEOUT};
use serde_json::json;
use std::{
    io::Read,
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
    freshen: Option<Arc<dyn Fn() -> Result<bool> + Send + Sync>>,
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
    pub fn with_freshness(
        mut self,
        freshen: impl Fn() -> Result<bool> + Send + Sync + 'static,
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
                    // Signal handlers only set a flag, so the wait wakes every 20 ms to check it.
                    wait_for_connection(&self.listener, Duration::from_millis(20))?
                }
                Err(e) => return Err(e.into()),
            }
        }
        Ok(())
    }
}
/// Waits up to `limit` for a connection to arrive at `listener`.
#[cfg(unix)]
fn wait_for_connection(listener: &TcpListener, limit: Duration) -> std::io::Result<()> {
    use std::os::fd::AsRawFd;
    let mut poll = libc::pollfd {
        fd: listener.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    if unsafe { libc::poll(&mut poll, 1, limit.as_millis() as libc::c_int) } < 0 {
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
    Ok(())
}
/// Waits up to `limit` for a connection to arrive at `listener`.
#[cfg(windows)]
fn wait_for_connection(listener: &TcpListener, limit: Duration) -> std::io::Result<()> {
    use std::os::windows::io::AsRawSocket;
    use windows_sys::Win32::Networking::WinSock::{WSAPoll, POLLRDNORM, WSAPOLLFD};
    let mut poll = WSAPOLLFD {
        fd: listener.as_raw_socket() as usize,
        events: POLLRDNORM,
        revents: 0,
    };
    if unsafe { WSAPoll(&mut poll, 1, limit.as_millis() as i32) } < 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
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
            return self.authenticate(r, &hosts);
        }
        if r.method != "GET" {
            return Response::error(405, "the explorer is read-only");
        }
        if r.path == "/" {
            return app();
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
    /// Exchanges the one-use bootstrap secret from the explorer link for the API token.
    fn authenticate(&self, r: &Request, hosts: &[String]) -> Response {
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
        Response::json(200, json!({"token":self.auth}))
    }
    fn api(&self, r: &Request) -> Result<Response> {
        if r.path == "/api/board" {
            let mut live = self.live.lock().unwrap();
            let watcher = live.get_or_insert_with(Service::new);
            let snapshot = watcher.snapshot(Duration::from_millis(180));
            return Ok(Response::json(200, live::board::public(&snapshot)));
        }
        if !["/api/status", "/api/chats", "/api/around", "/api/search"].contains(&r.path.as_str()) {
            return Ok(Response::error(404, "not found"));
        }
        let checked = self.freshen.as_ref().map(|f| f()).transpose()?;
        let checked = checked.unwrap_or(false);
        let c = store::reader(&self.root)?;
        match r.path.as_str() {
            "/api/status" => endpoints::status(&c, checked),
            "/api/chats" => endpoints::chats(&c, r),
            "/api/around" => endpoints::around(&c, r),
            "/api/search" => endpoints::search(&self.root, &c, checked, r),
            _ => Ok(Response::error(404, "not found")),
        }
    }
}
/// The app page, its inline style and script allowed by a fresh nonce.
fn app() -> Response {
    let nonce = match token() {
        Ok(t) => t,
        Err(_) => return Response::error(503, "random token unavailable"),
    };
    let html = APP
        .replacen("<style>", &format!("<style nonce=\"{nonce}\">"), 1)
        .replacen("<script>", &format!("<script nonce=\"{nonce}\">"), 1);
    Response{code:200,body:html.into_bytes(),headers:vec![("Content-Type".into(),"text/html; charset=utf-8".into()),("Content-Security-Policy".into(),format!("default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'"))]}
}

#[cfg(test)]
mod tests {
    use super::*;
    use http::decode;
    use serde_json::Value;
    use std::io::Write;
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
    fn a_building_meaning_lane_never_answers_from_published_vectors() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path().join("data"), true).unwrap();
        let c = store::writer(&root).unwrap();
        c.execute_batch("INSERT INTO sessions VALUES('pi','session-one','demo','/work/demo','','session-one',0,'Title',1,100);INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position,visible) VALUES(1,'pi','session-one','demo',0,'user','model','',1,'database migration failure','x','',0,1);").unwrap();
        c.execute_batch("WITH RECURSIVE rows(n) AS (VALUES(2) UNION ALL SELECT n+1 FROM rows WHERE n<1024) INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra) SELECT 1,'pi','session-one','demo',n,'user','',0,0,'database migration failure','x','' FROM rows;").unwrap();
        // A build is refused before model loading, even when no vectors are published.
        let model = crate::semantic_model::directory(&root);
        std::fs::create_dir_all(&model).unwrap();
        for (name, size, _) in crate::semantic_model::FILES {
            std::fs::File::create(model.join(name))
                .unwrap()
                .set_len(size)
                .unwrap();
        }
        let server = Explorer::bind(root, 0).unwrap();
        let s = &server.state;
        let headers = format!("Authorization: Bearer {}\r\n", s.auth);
        for mode in ["semantic", "keyword"] {
            let path = format!("/api/search?q=database+migration+failure&mode={mode}");
            let r = s.route(&req(server.port(), "GET", &path, &headers));
            assert_eq!(r.code, 200, "{mode}");
            let body: Value = serde_json::from_slice(&r.body).unwrap();
            let semantic = &body["semantic"];
            if mode == "semantic" {
                assert_eq!(semantic["complete"], false);
                assert_eq!(semantic["progress"]["total_rows"], 1024);
                assert_eq!(body["hits"], json!([]));
            } else {
                assert!(semantic.is_null(), "{body}");
                assert!(body["notice"].is_null(), "{body}");
            }
        }
    }
    #[test]
    fn a_failed_meaning_lane_never_answers_semantic_mode_with_keyword_rows() {
        let t = tempfile::tempdir().unwrap();
        let root = DataRoot::open(t.path().join("data"), true).unwrap();
        let c = store::writer(&root).unwrap();
        c.execute_batch("INSERT INTO sessions VALUES('pi','session-one','demo','/work/demo','','session-one',0,'Title',1,100);INSERT INTO prose(source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position,visible) VALUES(1,'pi','session-one','demo',0,'user','model','',1,'database migration failure','x','',0,1);INSERT INTO semantic_refs SELECT id,digest,digest FROM prose;").unwrap();
        // An embedded row makes the lane load the weights, which zero-filled files of the
        // right sizes fail.
        let model = crate::semantic_model::directory(&root);
        std::fs::create_dir_all(&model).unwrap();
        for (name, size, _) in crate::semantic_model::FILES {
            std::fs::File::create(model.join(name))
                .unwrap()
                .set_len(size)
                .unwrap();
        }
        let server = Explorer::bind(root, 0).unwrap();
        let s = &server.state;
        let headers = format!("Authorization: Bearer {}\r\n", s.auth);
        let search = |query: &str| {
            let r = s.route(&req(
                server.port(),
                "GET",
                &format!("/api/search?{query}"),
                &headers,
            ));
            (r.code, serde_json::from_slice::<Value>(&r.body).unwrap())
        };
        for (query, state) in [
            ("q=database+migration+failure&mode=semantic", "unavailable"),
            ("q=!!&mode=semantic", "query-rejected"),
        ] {
            let (code, body) = search(query);
            assert_eq!(code, 200, "{query}");
            assert_eq!(body["hits"], json!([]), "{query}: {body}");
            assert_eq!(body["total"], 0, "{query}");
            assert_eq!(body["totals_exact"], false, "{query}");
            assert_eq!(body["notice"], Value::Null, "{query}");
            assert_eq!(
                body["semantic"],
                json!({"state":state,"complete":false,"fallback_recommended":true}),
                "{query}"
            );
        }
        // Automatic hybrid answers from the keyword lane and says why no meaning row joined.
        let (code, body) = search("q=database+migration+failure");
        assert_eq!(code, 200);
        assert_eq!(body["semantic"], Value::Null, "{body}");
        assert_eq!(
            body["notice"],
            "meaning search unavailable; showing keyword matches"
        );
        let (code, body) = search("q=database&mode=semantic&sort=time");
        assert_eq!(code, 400);
        assert_eq!(
            body["error"],
            "meaning search is relevance-ordered; use sort=score"
        );
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
        let server = Explorer::bind(root, 0).unwrap().with_freshness(move || {
            count.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            Ok(true)
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
    use std::io::Write;
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
    use serde_json::Value;
    #[test]
    fn cookie_alone_never_authorizes_or_refreshes() {
        let temp = tempfile::tempdir().unwrap();
        let root = DataRoot::open(temp.path(), true).unwrap();
        let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let seen = calls.clone();
        let server = Explorer::bind(root, 0).unwrap().with_freshness(move || {
            seen.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            Ok(true)
        });
        let s = &server.state;
        let legacy = format!(
            "agrep_ui_{}",
            crate::store::digest(&format!("{}\0{}", s.root.path().display(), s.port))
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
        let server = Explorer::bind(root, 0).unwrap().with_freshness(|| {
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
