//! The downstream contract between tier-0 ingest and the semantic sidecar + search index.
//!
//! `write_messages` serializes normalized [`Message`]s to JSON Lines (one compact object per
//! line) at `data/messages.jsonl`. The Python sidecar reads this file, computes the authoritative
//! affect read, and writes back; the search index is built from the same rows. Each record carries
//! a stable `id` (`agent:session:turn`) so the sidecar can join its results back onto the source.

use std::fs;
use std::io::{BufWriter, Write};
use std::path::Path;

use serde::Serialize;

use crate::model::Message;

/// One cached row. `id` is the stable join key the sidecar/index reference.
#[derive(Serialize)]
struct Record<'a> {
    id: String,
    agent: &'a str,
    project: &'a str,
    session: &'a str,
    ts: i64,
    turn: u32,
    text: &'a str,
}

impl<'a> Record<'a> {
    fn from_message(m: &'a Message) -> Self {
        Record {
            id: format!("{}:{}:{}", m.agent, m.session, m.turn),
            agent: m.agent,
            project: &m.project,
            session: &m.session,
            ts: m.ts,
            turn: m.turn,
            text: &m.text,
        }
    }
}

/// Write `msgs` as JSON Lines to `path` (creating parent dirs as needed). One compact JSON object
/// per line: `{id, agent, project, session, ts, turn, text}`. Overwrites any existing file.
pub fn write_messages(msgs: &[Message], path: &Path) -> anyhow::Result<()> {
    if let Some(parent) = path.parent() {
        if !parent.as_os_str().is_empty() {
            fs::create_dir_all(parent)?;
        }
    }
    let file = fs::File::create(path)?;
    let mut w = BufWriter::new(file);
    for m in msgs {
        let rec = Record::from_message(m);
        // Compact (no pretty-print) so each record is exactly one line.
        serde_json::to_writer(&mut w, &rec)?;
        w.write_all(b"\n")?;
    }
    w.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_one_compact_line_per_message() {
        let msgs = vec![
            Message {
                agent: "claude",
                project: "tilt".to_string(),
                session: "sess-1".to_string(),
                ts: 1_700_000_000_000,
                turn: 0,
                text: "first".to_string(),
            },
            Message {
                agent: "opencode",
                project: "tilt".to_string(),
                session: "sess-2".to_string(),
                ts: 0,
                turn: 7,
                text: "with \"quotes\" and \n newline".to_string(),
            },
        ];
        let mut path = std::env::temp_dir();
        path.push(format!("tilt-cache-test-{}.jsonl", std::process::id()));
        write_messages(&msgs, &path).unwrap();

        let data = std::fs::read_to_string(&path).unwrap();
        let lines: Vec<&str> = data.lines().collect();
        assert_eq!(lines.len(), 2);

        let v0: serde_json::Value = serde_json::from_str(lines[0]).unwrap();
        assert_eq!(v0["id"], "claude:sess-1:0");
        assert_eq!(v0["agent"], "claude");
        assert_eq!(v0["project"], "tilt");
        assert_eq!(v0["session"], "sess-1");
        assert_eq!(v0["ts"], 1_700_000_000_000i64);
        assert_eq!(v0["turn"], 0);
        assert_eq!(v0["text"], "first");

        let v1: serde_json::Value = serde_json::from_str(lines[1]).unwrap();
        assert_eq!(v1["id"], "opencode:sess-2:7");
        assert_eq!(v1["text"], "with \"quotes\" and \n newline");

        std::fs::remove_file(&path).ok();
    }
}
