//! Synthetic agent stores shared by the benchmark and the matched comparison.
use anyhow::Result;
use serde_json::json;
use std::{
    fs::{self, File},
    io::{BufWriter, Write},
    path::{Path, PathBuf},
};

/// The planted answer for the semantic workload's natural-language query.
pub const SEMANTIC_FIXTURE: &str = "The cobalt lantern project's migration checklist is stored beside its release plan. We agreed to preserve the deployment instructions there.";
pub const SEMANTIC_QUERY: &str = "where did we save the cobalt lantern deployment checklist";

pub fn utterance(i: usize, tool: bool, dense: bool, reply: bool) -> String {
    if dense {
        return match i % 9 {
            0 => format!("ticket {i:08} deepold ordinary historical explanation with a single aligned occurrence"),
            3 => format!("ticket {i:08} deepold's ordinary historical explanation with a possessive occurrence"),
            6 => format!("ticket {i:08} xdeepoldx ordinary historical explanation with an interior occurrence"),
            _ => format!("ticket {i:08} contemporary unrelated status without the target token"),
        };
    }
    const WORDS: &[&str] = &[
        "the",
        "build",
        "history",
        "project",
        "test",
        "code",
        "cache",
        "release",
        "error",
        "workflow",
        "windows",
        "flaky",
        "query",
        "how",
        "did",
        "go",
        "drafting",
        "last",
        "run",
        "design",
        "token",
        "file",
        "index",
        "stream",
        "request",
        "response",
        "result",
        "configuration",
        "permission",
        "path",
        "content",
        "safe",
        "read",
        "write",
        "model",
        "source",
        "revision",
        "worktree",
        "compile",
        "benchmark",
        "counter",
        "boundary",
        "unicode",
        "session",
        "tool",
        "message",
        "trace",
        "agent",
        "commit",
        "dependency",
        "package",
        "database",
        "active",
        "old",
        "new",
        "fast",
        "slow",
        "state",
        "analysis",
        "summary",
        "check",
        "concurrency",
        "hello",
    ];
    let target = if tool {
        match i % 20 {
            0 => 3500,
            1..=5 => 1300,
            _ => 400,
        }
    } else if reply {
        match i % 20 {
            0 => 8000,
            1..=5 => 4000,
            _ => 1500,
        }
    } else {
        match i % 20 {
            0 => 1500,
            1..=4 => 400,
            _ => 150,
        }
    };
    let mut state = (i as u64 + 71) * 0x9e3779b1;
    let mut s = format!("reference {i:08x}: ");
    while s.len() < target {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        s.push_str(WORDS[(state as usize) % WORDS.len()]);
        s.push(' ');
    }
    s
}
static DENSE_ANCHOR: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
fn dense_ts(i: usize, rows: usize) -> i64 {
    let now = *DENSE_ANCHOR.get_or_init(|| chrono::Utc::now().timestamp_millis());
    now - if i.is_multiple_of(3) {
        180 * 86_400_000
    } else {
        86_400_000
    } - ((i * 7919) % rows.max(1)) as i64 * 1000
}
/// A full corpus oracle without retrieval, block bounds, FTS or candidate budgets.
/// The three fixed source forms have precomputed boundary factors; selection
/// compares all eligible rows and only retains the best forty identities.
pub fn dense_expected(rows: usize, args: &[&str]) -> Result<(Vec<String>, usize)> {
    let word = args.contains(&"-w");
    let explicit = word || args.contains(&"-E");
    let phrase = args.first() == Some(&"deepold ordinary");
    let time = args.windows(2).any(|p| p == ["--sort", "time"]);
    let project = args.contains(&"--project");
    let pattern = if phrase {
        "deepold ordinary"
    } else {
        "deepold"
    };
    let boundary = agrep_core::boundary_rank::prepare_query(pattern, &Default::default());
    let mut factors = [0.; 3];
    for (slot, ordinal) in [0, 3, 6].into_iter().enumerate() {
        factors[slot] = if explicit {
            0.5
        } else {
            0.175
                * boundary
                    .evaluate(&utterance(ordinal, false, true, false), None, true)?
                    .factor
        };
    }
    let sessions = (rows / 170).max(1);
    let names: Vec<_> = (0..sessions).map(|s| format!("bench-{s}")).collect();
    let mut best: Vec<(f64, i64, usize, usize, usize)> = Vec::new();
    let compare = |a: &(f64, i64, usize, usize, usize), b: &(f64, i64, usize, usize, usize)| {
        (if time {
            b.1.cmp(&a.1).then(b.0.total_cmp(&a.0))
        } else {
            b.0.total_cmp(&a.0).then(b.1.cmp(&a.1))
        })
        .then(names[a.2].cmp(&names[b.2]))
        .then(a.3.cmp(&b.3))
    };
    let (mut at, mut count) = (0, 0);
    for sid in 0..sessions {
        let n = (rows - at) / (sessions - sid);
        for turn in 0..n {
            let i = at + turn;
            if !i.is_multiple_of(3)
                || (word && i % 9 == 6)
                || (phrase && i % 9 != 0)
                || (project && sid % 50 != 7)
            {
                continue;
            }
            count += 1;
            let candidate = (factors[(i % 9) / 3], dense_ts(i, rows), sid, turn, i);
            if best.len() < 40 || compare(&candidate, best.last().unwrap()).is_lt() {
                best.push(candidate);
                best.sort_by(compare);
                best.truncate(40);
            }
        }
        at += n;
    }
    let index = agrep_core::handles::SessionIndex::from(names.clone());
    let handles = best
        .iter()
        .map(|(_, _, sid, turn, i)| {
            index.mint(
                &names[*sid],
                *turn as i64,
                &utterance(*i, false, true, false),
            )
        })
        .collect();
    Ok((handles, count))
}
pub fn generate(home: &Path, rows: usize, dense: bool) -> Result<(PathBuf, u64)> {
    let base = home.join(".pi/agent/sessions/bench");
    fs::create_dir_all(&base)?;
    let sessions = (rows / 170).max(1);
    let mut last = PathBuf::new();
    let mut at = 0;
    let mut text_bytes = 0u64;
    for sid in 0..sessions {
        // The measured history is overwhelmingly pi/OMP. A 20% prose-only
        // Codex population distorted both the role mix and filter selectivity.
        let codex = !dense && sid % 100 == 1;
        let file_base = if codex {
            home.join(".codex/sessions/bench")
        } else if !dense && sid % 10 != 0 {
            base.join(format!("2024-01-01T00-00-00-000Z_bench-{}", sid / 10 * 10))
        } else {
            base.clone()
        };
        fs::create_dir_all(&file_base)?;
        let path = file_base.join(format!(
            "{}2024-01-01T00-00-00-000Z_bench-{sid}.jsonl",
            if codex { "rollout-" } else { "" }
        ));
        let mut f = BufWriter::new(File::create(&path)?);
        let date = if dense {
            "2023-01-01T12:00:00Z".to_owned()
        } else {
            chrono::Utc::now().to_rfc3339()
        };
        writeln!(
            f,
            "{}",
            if codex {
                json!({"type":"session_meta","timestamp":date,"payload":{"id":format!("bench-{sid}"),"cwd":format!("/work/project-{}",sid%50)}})
            } else {
                json!({"type":"session","version":3,"id":format!("bench-{sid}"),"cwd":format!("/work/project-{}",sid%50),"timestamp":date})
            }
        )?;
        let n = (rows - at) / (sessions - sid);
        let mut parent: Option<String> = None;
        for j in 0..n {
            let date = if dense {
                chrono::DateTime::from_timestamp_millis(dense_ts(at, rows))
                    .unwrap()
                    .to_rfc3339()
            } else {
                date.clone()
            };
            let assistant = !dense && j % 80 == 1;
            let tool = !dense && j % 9 != 0 && !assistant;
            let id = format!("e{j}");
            let text = utterance(at, tool, dense, assistant);
            text_bytes += text.len() as u64;
            if codex {
                if !assistant {
                    writeln!(
                        f,
                        "{}",
                        json!({"type":"event_msg","timestamp":date,"payload":{"type":"user_message","message":text}})
                    )?;
                }
                writeln!(
                    f,
                    "{}",
                    json!({"type":"response_item","timestamp":date,"payload":{"type":"message","role":if assistant{"assistant"}else{"user"},"content":[{"type":if assistant{"output_text"}else{"input_text"},"text":text}]}})
                )?;
            } else if tool {
                let call = format!("c{j}");
                let command = format!("inspect source {at}");
                text_bytes += command.len() as u64 + 1;
                writeln!(
                    f,
                    "{}",
                    json!({"type":"message","id":&call,"parentId":parent,"timestamp":date,"message":{"role":"assistant","content":[{"type":"toolCall","id":&id,"name":"shell","arguments":{"command":command}}]}})
                )?;
                writeln!(
                    f,
                    "{}",
                    json!({"type":"message","id":&id,"parentId":call,"timestamp":date,"message":{"role":"toolResult","toolCallId":&id,"toolName":"shell","content":[{"type":"text","text":text}]}})
                )?;
            } else {
                writeln!(
                    f,
                    "{}",
                    json!({"type":"message","id":&id,"parentId":parent,"timestamp":date,"message":{"role":if assistant{"assistant"}else{"user"},"content":text}})
                )?;
            }
            parent = Some(id);
            at += 1;
        }
        f.flush()?;
        if !codex {
            last = path;
        }
    }
    Ok((last, text_bytes))
}
