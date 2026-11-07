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
fn search(home: &Path, data: &Path, pattern: &str, who: &str) -> (Option<i32>, Vec<String>) {
    let out = run(
        home,
        data,
        &[pattern, "--who", who, "--json", "--no-auto", "-n", "0"],
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
fn randomized_refreshes_equal_fresh_builds_across_adapters() {
    for seed in [7u64, 0xabc123] {
        let temp = TempDir::new().unwrap();
        let home = temp.path().join("home");
        let data = temp.path().join("data");
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
                assert_eq!(
                    search(&home, &data, pattern, who),
                    search(&home, &fresh_data, pattern, who),
                    "seed {seed} step {step} {pattern} {who}"
                );
            }
            let c = Connection::open(data.join("index.db")).unwrap();
            let mismatches:i64=c.query_row("SELECT count(*) FROM tool_words w WHERE w.doc!=coalesce((SELECT doc FROM tools_vocab v WHERE v.term=w.term),0)",[],|r|r.get(0)).unwrap();
            assert_eq!(mismatches, 0, "seed {seed} step {step}");
        }
    }
}
