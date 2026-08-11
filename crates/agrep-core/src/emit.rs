//! Streaming row emission for the first-run search: with `index --emit-rows`, every
//! parsed file's messages go to stdout as JSON lines the moment the file lands, so a
//! search can print hits WHILE the cold ingest runs instead of after it. Two line
//! shapes: `{"row":{...}}` (one per message, messages.jsonl field names + `reply`,
//! which has no sidecar yet mid-ingest) and `{"progress":{"agent":...}}` (one per
//! parsed file; the reader knows the file total upfront via `stores`). The human
//! progress lines keep printing - they never start with `{`, so the reader splits
//! on that. Same global-sink shape as `intake`: a process flag beats threading a
//! sink through every adapter signature.

use std::io::Write;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::OnceLock;

use serde::Serialize;

use crate::model::Message;
use crate::row_class;

static MODE_ENABLED: AtomicBool = AtomicBool::new(false);
static SINK_OPEN: AtomicBool = AtomicBool::new(false);
static HARNESS_PREFIXES: OnceLock<Vec<String>> = OnceLock::new();

pub fn enable() {
    SINK_OPEN.store(true, Ordering::Relaxed);
    MODE_ENABLED.store(true, Ordering::Relaxed);
}

/// The local harness-prefix policy the normalize pass will use, so streamed
/// rows classify identically. Set once, before the first adapter emits.
pub fn set_harness_prefixes(prefixes: &[String]) {
    let _ = HARNESS_PREFIXES.set(prefixes.to_vec());
}

pub fn on() -> bool {
    MODE_ENABLED.load(Ordering::Relaxed)
}

fn output_open() -> bool {
    on() && SINK_OPEN.load(Ordering::Relaxed)
}

/// Preserve the emit-mode stdout stream while its reader is present. Once the reader
/// closes, later human progress cannot turn a successful publication into a pipe panic.
pub fn human_line(args: std::fmt::Arguments<'_>) {
    if !on() {
        println!("{args}");
        return;
    }
    if output_open() {
        write_locked(format!("{args}\n").as_bytes());
    } else if matches!(std::env::var("AGREP_PERF_PHASES").as_deref(), Ok("1")) {
        let line = args.to_string();
        if line.trim_start().starts_with("phases: source-check ") {
            eprintln!("* [agrep perf] ingest {}", line.trim());
        }
    }
}

/// Announce a parse-unit count (`{"total":{"files":N}}`) the moment an adapter's
/// work list is known - additive across adapters, so the reader gets its progress
/// denominator from work the ingest does anyway instead of pre-walking the stores.
pub fn total(files: usize) {
    if !output_open() || files == 0 {
        return;
    }
    let mut buf = Vec::with_capacity(32);
    append_json_line(
        &mut buf,
        &TotalEnvelope {
            total: Total { files },
        },
    );
    write_locked(&buf);
}

/// One parsed file: its messages + a progress tick, as a single locked write so
/// parallel parse lanes never interleave partial lines.
pub fn file_done(msgs: &[Message]) {
    if !output_open() {
        return;
    }
    let agent = msgs.first().map(|m| m.agent).unwrap_or("");
    let mut buf = rows_json(msgs);
    append_json_line(
        &mut buf,
        &ProgressEnvelope {
            progress: Progress { agent },
        },
    );
    write_locked(&buf);
}

/// Messages served without a parse (cache hits, guarded last-good snapshots): rows, no tick -
/// the progress denominator is files parsed, and these cost none.
pub fn rows_only(msgs: &[Message]) {
    if !output_open() || msgs.is_empty() {
        return;
    }
    write_locked(&rows_json(msgs));
}

#[derive(Serialize)]
struct TotalEnvelope {
    total: Total,
}

#[derive(Serialize)]
struct Total {
    files: usize,
}

#[derive(Serialize)]
struct ProgressEnvelope<'a> {
    progress: Progress<'a>,
}

#[derive(Serialize)]
struct Progress<'a> {
    agent: &'a str,
}

#[derive(Serialize)]
struct RowEnvelope<'a> {
    row: StreamRow<'a>,
}

#[derive(Serialize)]
struct StreamRow<'a> {
    agent: &'a str,
    id: String,
    model: &'a str,
    project: &'a str,
    reply: &'a str,
    session: &'a str,
    side: bool,
    text: &'a str,
    ts: i64,
    turn: u32,
    who: &'a str,
}

fn rows_json(msgs: &[Message]) -> Vec<u8> {
    static NO_PREFIXES: Vec<String> = Vec::new();
    let prefixes = HARNESS_PREFIXES.get().unwrap_or(&NO_PREFIXES);
    let capacity = msgs
        .iter()
        .map(|m| {
            m.text.len() + m.reply.len() + m.project.len() + m.session.len() + m.model.len() + 192
        })
        .sum();
    let mut buf = Vec::with_capacity(capacity);
    for m in msgs {
        append_json_line(
            &mut buf,
            &RowEnvelope {
                row: StreamRow {
                    agent: m.agent,
                    id: format!("{}:{}:{}", m.agent, m.session, m.turn),
                    model: &m.model,
                    project: &m.project,
                    reply: &m.reply,
                    session: &m.session,
                    side: m.side,
                    text: &m.text,
                    ts: m.ts,
                    turn: m.turn,
                    who: row_class::row_kind(m, prefixes).who(),
                },
            },
        );
    }
    buf
}

fn append_json_line<T: Serialize>(buf: &mut Vec<u8>, value: &T) {
    serde_json::to_writer(&mut *buf, value).expect("serializing JSON into memory cannot fail");
    buf.push(b'\n');
}

fn write_locked(buf: &[u8]) {
    if !SINK_OPEN.load(Ordering::Relaxed) {
        return;
    }
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    if out.write_all(buf).and_then(|()| out.flush()).is_err() {
        SINK_OPEN.store(false, Ordering::Relaxed);
    }
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;

    use super::rows_json;
    use crate::model::Message;

    fn message(text: &str, who: &str, model: &str, side: bool) -> Message {
        Message {
            agent: "claude",
            project: Arc::from("agrep"),
            session: Arc::from("s"),
            ts: 1,
            turn: 0,
            text: Arc::from(text),
            who: Arc::from(who),
            model: Arc::from(model),
            model_source: Arc::from(""),
            reply: Arc::from(""),
            reply_chars: 0,
            side,
            parent: Arc::from(""),
        }
    }

    #[test]
    fn streamed_rows_carry_the_normalize_pass_classification() {
        let rows = [
            (message("real question", "user", "", false), "user"),
            (message("continue", "user", "", false), "control"),
            (message("summary", "recap", "", false), "recap"),
            (message("noise", "user", "<synthetic>", false), "synthetic"),
            (message("[subagent task] go", "user", "", false), "subagent"),
            (message("side turn", "user", "", true), "subagent"),
        ];
        for (m, expected) in rows {
            let line = rows_json(std::slice::from_ref(&m));
            let value: serde_json::Value = serde_json::from_slice(&line).unwrap();
            assert_eq!(value["row"]["who"], *expected, "text={}", m.text);
        }
    }

    #[test]
    fn streamed_rows_keep_the_public_ndjson_shape() {
        let line = rows_json(&[message("real question", "user", "", false)]);
        assert_eq!(
            std::str::from_utf8(&line).unwrap(),
            "{\"row\":{\"agent\":\"claude\",\"id\":\"claude:s:0\",\"model\":\"\",\"project\":\"agrep\",\"reply\":\"\",\"session\":\"s\",\"side\":false,\"text\":\"real question\",\"ts\":1,\"turn\":0,\"who\":\"user\"}}\n"
        );
    }
}
