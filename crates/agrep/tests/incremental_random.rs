use agrep_core::{data::DataRoot, store};
use rusqlite::Connection;
use serde_json::{json, Value};
use std::{
    fs,
    io::{Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    process::{Command, Output},
};
use tempfile::TempDir;
mod support;
fn run(home: &Path, data: &Path, args: &[&str]) -> Output {
    Command::new(support::binary())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap_or_default())
        .env("HOME", home)
        .env("AGREP_HOME", home)
        .env("AGREP_DATA_DIR", data)
        .env("AGREP_PROFILE", "classic")
        .args(args)
        .output()
        .unwrap()
}
fn index(home: &Path, data: &Path) {
    let out = run(home, data, &["index"]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
}
struct Source {
    path: PathBuf,
    agent: usize,
    id: String,
    rows: Vec<String>,
}
fn source(home: &Path, agent: usize, serial: usize) -> Source {
    let id = format!("a0000000-0000-4000-8000-{serial:012}");
    let relative = match agent {
        0 => format!(".pi/agent/sessions/project/{id}.jsonl"),
        1 => format!(".claude/projects/project/{id}.jsonl"),
        _ => format!(".codex/sessions/2026/01/01/rollout-{id}.jsonl"),
    };
    let header = match agent {
        0 => json!({"type":"session","version":3,"id":id,"cwd":"/work/project"}),
        1 => json!({"type":"system","sessionId":id,"cwd":"/work/project"}),
        _ => json!({"type":"session_meta","payload":{"id":id,"cwd":"/work/project"}}),
    };
    Source {
        path: home.join(relative),
        agent,
        id,
        rows: vec![header.to_string()],
    }
}
fn append(s: &mut Source, serial: usize) {
    let text = format!(
        "{} marker{serial} café ＫＩＴ unicode 界",
        ["amber common", "gamma needle", "common needle"][serial % 3]
    );
    let date = format!("2026-01-01T12:{:02}:{:02}Z", serial / 60 % 60, serial % 60);
    let prior = format!("m{}", s.rows.len() - 1);
    let id = format!("m{}", s.rows.len());
    let value = match s.agent {
        0 => {
            json!({"type":"message","id":id,"parentId":if s.rows.len()==1 {Value::Null} else {json!(prior)},"timestamp":date,"message":{"role":"user","content":text}})
        }
        1 => {
            json!({"type":"user","uuid":id,"parentUuid":prior,"sessionId":s.id,"cwd":"/work/project","timestamp":date,"message":{"role":"user","content":text}})
        }
        _ => {
            json!({"type":"response_item","timestamp":date,"payload":{"type":"message","role":"user","content":[{"type":"input_text","text":text}]}})
        }
    };
    s.rows.push(value.to_string());
    if serial.is_multiple_of(3) {
        let call = format!("call{serial}");
        if s.agent == 1 {
            s.rows.push(json!({"type":"assistant","uuid":format!("call{serial}"),"sessionId":s.id,"cwd":"/work/project","timestamp":date,"message":{"role":"assistant","content":[{"type":"tool_use","id":call,"name":"shell","input":{"command":"inspect common"}}]}}).to_string());
        } else if s.agent == 2 {
            s.rows.push(json!({"type":"response_item","timestamp":date,"payload":{"type":"function_call","name":"shell","arguments":"{\"command\":\"inspect common\"}","call_id":call}}).to_string());
        }
        let tool = match s.agent {
            0 => {
                json!({"type":"message","id":format!("m{}",s.rows.len()),"parentId":id,"timestamp":date,"message":{"role":"bashExecution","command":"inspect common","exitCode":0,"output":format!("tool {text}")}})
            }
            1 => {
                json!({"type":"user","uuid":format!("tool{serial}"),"sessionId":s.id,"cwd":"/work/project","timestamp":date,"message":{"role":"user","content":[{"type":"tool_result","tool_use_id":call,"content":format!("tool {text}")}]}})
            }
            _ => {
                json!({"type":"response_item","timestamp":date,"payload":{"type":"function_call_output","call_id":call,"output":format!("tool {text}")}})
            }
        };
        s.rows.push(tool.to_string());
    }
}
fn write(s: &Source) {
    fs::create_dir_all(s.path.parent().unwrap()).unwrap();
    fs::write(&s.path, s.rows.join("\n") + "\n").unwrap();
}
fn logical(data: &Path) -> Vec<Vec<String>> {
    let root = DataRoot::open(data, false).unwrap();
    let c = store::reader(&root).unwrap();
    let mut out = Vec::new();
    for table in ["prose", "tools"] {
        let mut q=c.prepare(&format!("SELECT agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position,visible FROM {table}")).unwrap();
        let mut rows = q.query([]).unwrap();
        let mut values = Vec::new();
        while let Some(r) = rows.next().unwrap() {
            let mut fields: Vec<_> = (0..13)
                .map(|i| r.get::<_, rusqlite::types::Value>(i).unwrap())
                .collect();
            fields[8] =
                rusqlite::types::Value::Text(store::decode(table, r.get(8).unwrap()).unwrap());
            values.push(format!("{fields:?}"));
        }
        values.sort();
        out.push(values);
    }
    for sql in ["SELECT * FROM sessions ORDER BY agent,id","SELECT term,doc FROM prose_terms WHERE doc>0 ORDER BY term","SELECT term,doc FROM tool_words WHERE doc>0 ORDER BY term","SELECT token,n,s FROM term_stats ORDER BY token","SELECT key,CAST(value AS INTEGER) FROM state WHERE key IN ('prose_count','prose_user_count','tools_count') ORDER BY key"] {
        let mut q=c.prepare(sql).unwrap();let n=q.column_count();
        out.push(q.query_map([],|r|Ok(format!("{:?}",(0..n).map(|i|r.get::<_,rusqlite::types::Value>(i)).collect::<rusqlite::Result<Vec<_>>>()?))).unwrap().collect::<rusqlite::Result<_>>().unwrap());
    }
    out
}
fn search(
    home: &Path,
    data: &Path,
    pattern: &str,
    who: &str,
    limit: &str,
) -> (Option<i32>, Vec<String>) {
    let out = run(
        home,
        data,
        &[pattern, "--who", who, "--json", "--no-auto", "-n", limit],
    );
    let rows: Vec<_> = String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(|line| serde_json::from_str::<Value>(line).unwrap())
        .filter(|v| v.get("handle").is_some())
        .map(|v| v.to_string())
        .collect();
    (out.status.code(), rows)
}

#[test]
fn full_refresh_reparses_middle_of_append_only_log() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let mut s = source(&home, 0, 0);
    s.rows.push(json!({"type":"message","id":"m0","parentId":null,"message":{"role":"user","content":"x".repeat(4*64*1024)}}).to_string());
    write(&s);
    index(&home, &data);
    let mut file = fs::OpenOptions::new().write(true).open(&s.path).unwrap();
    file.seek(SeekFrom::Start(2 * 64 * 1024 + 17)).unwrap();
    file.write_all(b"y").unwrap();
    drop(file);
    let out = run(&home, &data, &["index", "--full"]);
    assert!(
        out.status.success(),
        "{}",
        String::from_utf8_lossy(&out.stderr)
    );
    let fresh = temp.path().join("fresh");
    index(&home, &fresh);
    assert_eq!(logical(&data), logical(&fresh));
}

#[test]
fn appends_after_blank_and_unterminated_lines_equal_fresh_builds() {
    let id = "a0000000-0000-4000-8000-000000000001";
    let pi_header = json!({"type":"session","version":3,"id":id,"cwd":"/work/project"});
    // Id-less tool calls are named after their record's line in the file.
    let pi = |n: usize| {
        let parent = n
            .checked_sub(1)
            .map_or(Value::Null, |p| json!(format!("e{p}")));
        let message = if n.is_multiple_of(2) {
            json!({"role":"user","content":format!("needle prompt {n}")})
        } else {
            json!({"role":"assistant","content":[{"type":"text","text":format!("reply {n}")},{"type":"toolCall","name":"bash","arguments":{"command":format!("echo {n}")}}]})
        };
        json!({"type":"message","id":format!("e{n}"),"parentId":parent,"timestamp":format!("2026-01-01T12:00:{n:02}Z"),"message":message}).to_string()
    };
    let claude = |n: usize| {
        json!({"type":"user","uuid":format!("u{n}"),"sessionId":id,"cwd":"/work/project","timestamp":format!("2026-01-01T12:00:{n:02}Z"),"message":{"role":"user","content":format!("needle prompt {n}")}}).to_string()
    };
    let lines = |record: &dyn Fn(usize) -> String, from: usize, to: usize, end: &str| {
        (from..to).map(|n| record(n) + end).collect::<String>()
    };
    let pi_path = format!(".pi/agent/sessions/project/{id}.jsonl");
    let claude_path = format!(".claude/projects/project/{id}.jsonl");
    let cases = [
        (
            "blank lines between, after and before appended records",
            &pi_path,
            vec![
                format!("{pi_header}\n{}\n\n", lines(&pi, 0, 2, "\n")),
                format!("\n \n{}\t\n", lines(&pi, 2, 4, "\n")),
                format!("\n\n{}", lines(&pi, 4, 6, "\n")),
            ],
        ),
        (
            "CRLF lines and blank CRLF lines",
            &pi_path,
            vec![
                format!("{pi_header}\r\n{}\r\n", lines(&pi, 0, 2, "\r\n")),
                format!("\r\n{}", lines(&pi, 2, 4, "\r\n")),
            ],
        ),
        (
            "a record refreshed before its newline was written",
            &pi_path,
            vec![
                format!("{pi_header}\n{}{}", lines(&pi, 0, 2, "\n"), pi(2)),
                format!("\n{}", lines(&pi, 3, 5, "\n")),
            ],
        ),
        (
            "a CRLF record refreshed before its newline was written",
            &pi_path,
            vec![
                format!("{pi_header}\r\n{}", pi(0)),
                format!("\r\n{}", lines(&pi, 1, 4, "\r\n")),
            ],
        ),
        (
            "a pi record whose line an append continues",
            &pi_path,
            vec![format!("{pi_header}\n{}", pi(0)), lines(&pi, 1, 4, "\n")],
        ),
        (
            "a claude record whose line an append continues",
            &claude_path,
            vec![claude(0), lines(&claude, 1, 4, "\n")],
        ),
    ];
    for (case, relative, steps) in cases {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let data = temp.path().join("data");
        let path = home.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        for step in steps {
            let mut file = fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .unwrap();
            file.write_all(step.as_bytes()).unwrap();
            drop(file);
            index(&home, &data);
        }
        let fresh = temp.path().join("fresh");
        index(&home, &fresh);
        assert_eq!(logical(&data), logical(&fresh), "{case}");
    }
}

#[test]
fn randomized_refreshes_equal_fresh_builds_across_adapters() {
    for seed in [7u64, 0xabc123] {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let data = temp.path().join("data");
        let root = DataRoot::open(&data, true).unwrap();
        // Keep the ceilings loose across the whole mutation sequence.
        let _bounds_worker = root.merge_lock().unwrap().unwrap();
        let mut sources = Vec::new();
        let mut serial = 0;
        for agent in 0..3 {
            for _ in 0..3 {
                let mut s = source(&home, agent, serial);
                serial += 1;
                for j in 0..9 {
                    append(&mut s, serial * 100 + j);
                }
                write(&s);
                sources.push(s);
            }
        }
        index(&home, &data);
        let mut random = seed;
        for step in 0..30 {
            random ^= random << 13;
            random ^= random >> 7;
            random ^= random << 17;
            let at = random as usize % sources.len();
            match step % 5 {
                0 => {
                    let s = &mut sources[at];
                    let old = s.rows.len();
                    append(s, 1000 + step);
                    let mut f = fs::OpenOptions::new().append(true).open(&s.path).unwrap();
                    for row in &s.rows[old..] {
                        writeln!(f, "{row}").unwrap();
                    }
                }
                1 => {
                    let s = &mut sources[at];
                    for row in s.rows.iter_mut().skip(1) {
                        *row = row
                            .replace("common", "rewritten")
                            .replace("needle", "amended");
                    }
                    write(s);
                }
                2 => {
                    let s = &mut sources[at];
                    s.rows.truncate((s.rows.len() / 2).max(1));
                    write(s);
                }
                3 => {
                    let s = sources.remove(at);
                    fs::remove_file(s.path).unwrap();
                }
                _ => {
                    let mut s = source(&home, step % 3, serial);
                    serial += 1;
                    for j in 0..5 {
                        append(&mut s, 2000 + step * 10 + j);
                    }
                    write(&s);
                    sources.push(s);
                }
            }
            index(&home, &data);
            let fresh = tempfile::tempdir_in(temp.path()).unwrap();
            let fresh_data = fresh.path().join("data");
            index(&home, &fresh_data);
            assert_eq!(
                logical(&data),
                logical(&fresh_data),
                "seed {seed} step {step}"
            );
            for (pattern, who) in [
                ("common", "user"),
                ("needle", "tool"),
                ("café", "user"),
                ("kit", "tool"),
                ("rewritten", "user"),
            ] {
                for limit in ["0", "3"] {
                    assert_eq!(
                        search(&home, &data, pattern, who, limit),
                        search(&home, &fresh_data, pattern, who, limit),
                        "seed {seed} step {step} {pattern} {who} limit {limit}"
                    );
                }
            }
            let c = Connection::open(data.join("index.db")).unwrap();
            let mismatches:i64=c.query_row("SELECT count(*) FROM tool_words w WHERE w.doc!=coalesce((SELECT doc FROM tools_vocab v WHERE v.term=w.term),0)",[],|r|r.get(0)).unwrap();
            assert_eq!(mismatches, 0, "seed {seed} step {step}");
        }
    }
}

/// One turn of a pi chat: its prompt, its calls as (id, command, output), and its reply.
type PiTurn<'a> = (&'a str, &'a [(&'a str, &'a str, &'a str)], &'a str);

fn pi_chat(id: &str, turns: &[PiTurn<'_>]) -> String {
    let mut lines = vec![json!({"type":"session","version":3,"id":id,"cwd":"/work/project"})];
    let mut parent = Value::Null;
    let mut record = |message: Value| {
        let n = lines.len();
        let id = format!("e{n}");
        lines.push(json!({"type":"message","id":id,"parentId":parent,"timestamp":format!("2026-01-01T12:00:{n:02}Z"),"message":message}));
        parent = json!(id);
    };
    for (prompt, calls, reply) in turns {
        if !prompt.is_empty() {
            record(json!({"role":"user","content":prompt}));
        }
        for (call, command, output) in *calls {
            record(
                json!({"role":"assistant","content":[{"type":"toolCall","id":call,"name":"bash","arguments":{"command":command}}]}),
            );
            record(json!({"role":"toolResult","toolCallId":call,"content":output}));
        }
        if !reply.is_empty() {
            record(json!({"role":"assistant","content":[{"type":"text","text":reply}]}));
        }
    }
    lines.iter().map(|l| format!("{l}\n")).collect()
}

fn count(home: &Path, data: &Path, pattern: &str) -> String {
    let out = run(home, data, &[pattern, "-c", "--no-auto"]);
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

// Two files holding one chat are searched as one replay, yet every row any copy holds
// stays searchable exactly once: a fuller result, a call or reply only one copy has, a
// diverged input. Copies the kept row contains, and results a harness elided, stay hidden.
#[test]
fn differing_copies_keep_every_row_searchable_once() {
    let temp = TempDir::new().unwrap();
    let home = temp.path().join("home");
    let data = temp.path().join("data");
    let id = "a0000000-0000-4000-8000-00000000c0de";
    let sessions = home.join(".pi/agent/sessions");
    let alpha = sessions.join(format!("alpha/{id}.jsonl"));
    let beta = sessions.join(format!("beta/{id}.jsonl"));
    let advisor = sessions.join(
        "parent/2026-01-01T00-00-00-000Z_a0000000-0000-4000-8000-0000000000aa/__advisor.jsonl",
    );
    let first: PiTurn = (
        "shared prompt one",
        &[
            ("c1", "grep zebra", "[Stale result elided - 99 tokens]"),
            ("c2", "grep onlyalpha", "found onlyalpha"),
        ],
        "first reply",
    );
    let alpha_turns = [
        first,
        (
            "shared prompt two",
            &[("c4", "cd /work/inputalpha", "ok")],
            "continuation kestrel",
        ),
        ("shared prompt three", &[], "partial"),
    ];
    let beta_turns = [
        (
            "shared prompt one",
            &[
                ("c1", "grep zebra", "zebrafinch result"),
                ("c3", "grep onlybeta", "found onlybeta"),
            ][..],
            "first reply",
        ),
        (
            "shared prompt two",
            &[("c4", "cd /work/inputbeta", "ok")],
            "continuation heron",
        ),
        ("shared prompt three", &[], "partial and complete"),
    ];
    let step = |label: &str, expected: &[(&str, &str)]| {
        index(&home, &data);
        let fresh = tempfile::tempdir_in(temp.path()).unwrap();
        let fresh_data = fresh.path().join("data");
        index(&home, &fresh_data);
        assert_eq!(logical(&data), logical(&fresh_data), "{label}");
        for (pattern, n) in expected {
            assert_eq!(count(&home, &data, pattern), *n, "{label}: {pattern}");
        }
    };
    let write_chat = |path: &Path, turns: &[PiTurn<'_>]| {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, pi_chat(id, turns)).unwrap();
    };
    write_chat(&alpha, &alpha_turns);
    step("one copy", &[("Stale result", "1"), ("heron", "0")]);
    write_chat(&beta, &beta_turns);
    let both = [
        ("zebrafinch", "1"),
        ("Stale result", "0"),
        ("onlyalpha", "1"),
        ("onlybeta", "1"),
        ("inputalpha", "1"),
        ("inputbeta", "1"),
        ("kestrel", "1"),
        ("heron", "1"),
        ("partial", "1"),
        ("first reply", "1"),
        ("shared prompt", "3"),
    ];
    step("a differing copy appended", &both);
    let mut later = alpha_turns.to_vec();
    later.push((
        "late prompt",
        &[("c5", "grep lateralpha", "found lateralpha")],
        "late reply",
    ));
    write_chat(&alpha, &later);
    step(
        "one copy grown",
        &[&both[..], &[("lateralpha", "1")]].concat(),
    );
    fs::remove_file(&beta).unwrap();
    step(
        "a copy deleted",
        &[
            ("zebrafinch", "0"),
            ("Stale result", "1"),
            ("heron", "0"),
            ("onlybeta", "0"),
            ("kestrel", "1"),
            ("lateralpha", "1"),
        ],
    );
    // A chat that yields calls but no turn is searched under its parent's family.
    fs::create_dir_all(advisor.parent().unwrap()).unwrap();
    fs::write(
        &advisor,
        pi_chat(
            "a0000000-0000-4000-8000-0000000000ad",
            &[("", &[("v1", "grep advisorneedle", "advisorneedle hit")], "")],
        ),
    )
    .unwrap();
    step("a turnless side chat", &[("advisorneedle", "1")]);
    let c = Connection::open(data.join("index.db")).unwrap();
    let side: (String, String, bool, i64) = c.query_row("SELECT project,parent,side,last_ts FROM sessions WHERE id='a0000000-0000-4000-8000-0000000000ad'", [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))).unwrap();
    assert_eq!(
        side,
        (
            "/work/project".to_owned(),
            "a0000000-0000-4000-8000-0000000000aa".to_owned(),
            true,
            1767268801000
        )
    );
}
