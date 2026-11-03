//! Proven command lineage for the explicit --no-meta filter.
use crate::{query::Hit, store, unicode_v16};
use anyhow::Result;
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;
#[derive(Default)]
struct Invocation {
    read: bool,
    queries: Vec<String>,
}
fn norm(s: &str) -> String {
    unicode_v16::case_fold(&s.split_whitespace().collect::<Vec<_>>().join(" "))
}
fn basename(s: &str) -> String {
    s.rsplit(['/', '\\'])
        .next()
        .unwrap_or(s)
        .to_ascii_lowercase()
}
fn args_only(args: &[String]) -> Option<Vec<String>> {
    const VALUES: &[&str] = &[
        "-C",
        "-n",
        "--agent",
        "--before",
        "--budget",
        "--chat",
        "--color",
        "--context",
        "--exclude-project",
        "--hits",
        "--max",
        "--model",
        "--no-who",
        "--project",
        "--session",
        "--since",
        "--sort",
        "--until",
        "--who",
        "--tool-output",
    ];
    const FLAGS: &[&str] = &[
        "-E",
        "-c",
        "-i",
        "-l",
        "-s",
        "-w",
        "--all-side-chats",
        "--classic",
        "--count",
        "--count-by-tier",
        "--coverage",
        "--flat",
        "--here",
        "--ignore-case",
        "--json",
        "--lexical",
        "--model-soft",
        "--no-auto",
        "--no-meta",
        "--no-self",
        "--no-side",
        "--no-tools",
        "--probe",
        "--self",
        "--semantic",
        "--soft",
    ];
    let (mut out, mut i) = (vec![], 0);
    while i < args.len() {
        let t = &args[i];
        if t == "--" {
            out.extend_from_slice(&args[i + 1..]);
            break;
        }
        if t.starts_with('-') {
            let key = t.split('=').next().unwrap_or(t);
            if VALUES.contains(&key) {
                if !t.contains('=') {
                    i += 1;
                    if i == args.len() {
                        return None;
                    }
                }
            } else if !FLAGS.contains(&key) {
                return None;
            }
        } else {
            out.push(t.clone());
        }
        i += 1;
    }
    Some(out)
}
fn argv(a: &[String]) -> Invocation {
    let mut i = 0;
    while i < a.len() {
        let assignment = a[i].split_once('=').is_some_and(|(k, _)| {
            !k.is_empty()
                && k.chars().enumerate().all(|(n, c)| {
                    c == '_' || c.is_ascii_alphabetic() || (n > 0 && c.is_ascii_digit())
                })
        });
        if assignment || ["env", "time", "command"].contains(&basename(&a[i]).as_str()) {
            i += 1;
        } else {
            break;
        }
    }
    if i == a.len() {
        return Invocation::default();
    }
    let exe = basename(&a[i]);
    let python = exe.strip_prefix("python").is_some_and(|suffix| {
        suffix
            .strip_suffix(".exe")
            .unwrap_or(suffix)
            .chars()
            .all(|c| c.is_ascii_digit() || c == '.')
    });
    if python || ["py", "py.exe"].contains(&exe.as_str()) {
        i += 1;
        while i < a.len() && a[i].starts_with('-') {
            if ["-c", "-m"].contains(&a[i].as_str()) {
                return Invocation::default();
            }
            i += 1;
        }
        if i == a.len() || basename(&a[i]) != "cli.py" {
            return Invocation::default();
        }
    } else if ![
        "agrep",
        "agrep.exe",
        "agrep.cmd",
        ".agrep.exe",
        ".agrep.cmd",
    ]
    .contains(&exe.as_str())
    {
        return Invocation::default();
    }
    let a = &a[i + 1..];
    if a.is_empty() {
        return Invocation {
            read: true,
            queries: vec![],
        };
    }
    let verb = a[0].to_ascii_lowercase();
    if [
        "around",
        "audit",
        "board",
        "chats",
        "doctor",
        "postcompact",
        "status",
        "tail",
        "--help",
        "-h",
        "--version",
    ]
    .contains(&verb.as_str())
    {
        return Invocation {
            read: true,
            queries: vec![],
        };
    }
    let explicit = ["search", "recall", "pack"].contains(&verb.as_str());
    if !explicit
        && [
            "index", "reindex", "setup", "remove", "set", "archive", "restore", "run", "resume",
            "ui", "serve", "summary", "__work",
        ]
        .contains(&verb.as_str())
    {
        return Invocation::default();
    }
    let Some(parts) = args_only(if explicit { &a[1..] } else { a }) else {
        return Invocation::default();
    };
    let queries = if verb == "pack" {
        parts
            .iter()
            .map(|s| norm(s))
            .filter(|s| !s.is_empty())
            .collect()
    } else {
        let q = norm(&parts.join(" "));
        if q.is_empty() {
            vec![]
        } else {
            vec![q]
        }
    };
    Invocation {
        read: !queries.is_empty(),
        queries,
    }
}
fn shell(s: &str) -> Invocation {
    if s.lines().count() != 1 {
        return Invocation::default();
    }
    let (mut quote, mut escape) = (None, false);
    for c in s.chars() {
        if escape {
            escape = false;
            continue;
        }
        if c == '\\' && quote != Some('\'') {
            escape = true;
            continue;
        }
        if let Some(q) = quote {
            if c == q {
                quote = None;
            } else if q == '"' && matches!(c, '$' | '\u{0060}') {
                return Invocation::default();
            }
        } else if matches!(c, '\'' | '"') {
            quote = Some(c);
        } else if matches!(
            c,
            ';' | '&' | '|' | '<' | '>' | '$' | '\u{0060}' | '\r' | '\n'
        ) {
            return Invocation::default();
        }
    }
    if quote.is_some() || escape {
        return Invocation::default();
    }
    shlex::split(s).map_or_else(Invocation::default, |a| argv(&a))
}
fn input(s: &str) -> Invocation {
    let mut commands = vec![s.to_string()];
    let mut vectors = vec![];
    let strings = |v: &Value| {
        v.as_array().and_then(|a| {
            a.iter()
                .map(|v| v.as_str().map(str::to_string))
                .collect::<Option<Vec<_>>>()
        })
    };
    if let Ok(v) = serde_json::from_str::<Value>(s) {
        if let Some(a) = strings(&v) {
            vectors.push(a);
        }
        if let Some(o) = v.as_object() {
            let keys: Vec<_> = ["cmd", "command", "argv"]
                .into_iter()
                .filter(|k| o.contains_key(*k))
                .collect();
            if keys.len() == 1 {
                let v = &o[keys[0]];
                if keys[0] == "argv" {
                    if let Some(a) = strings(v) {
                        vectors.push(a);
                    } else if let Some(a) = v
                        .as_str()
                        .and_then(|s| serde_json::from_str::<Value>(s).ok())
                        .and_then(|v| strings(&v))
                    {
                        vectors.push(a);
                    }
                } else if let Some(s) = v.as_str() {
                    commands.push(s.to_string());
                }
            }
        }
    }
    if let Some((head, tail)) = s.split_once("tools.exec_command(") {
        static WRAPPER: std::sync::OnceLock<regex::Regex> = std::sync::OnceLock::new();
        if WRAPPER
            .get_or_init(|| {
                regex::Regex::new(
                    r"^\s*(?:(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*)?(?:await\s*)?$",
                )
                .unwrap()
            })
            .is_match(head)
        {
            let tail = tail
                .trim_start()
                .strip_prefix('{')
                .unwrap_or("")
                .trim_start();
            for key in ["cmd:", "\"cmd\":", "'cmd':"] {
                if let Some(rest) = tail.strip_prefix(key) {
                    if let Some(Ok(s)) = serde_json::Deserializer::from_str(rest.trim_start())
                        .into_iter::<String>()
                        .next()
                    {
                        commands.push(s);
                    }
                }
            }
        }
    }
    let mut out = Invocation::default();
    for i in commands.iter().take(16).map(|s| shell(s)).chain(
        vectors
            .iter()
            .take(16)
            .filter(|v| v.len() <= 128)
            .map(|v| argv(v)),
    ) {
        out.read |= i.read;
        out.queries.extend(i.queries);
    }
    out
}
fn event(text: Vec<u8>, body: String) -> Result<Invocation> {
    let v: Value = serde_json::from_str(&body)?;
    if v["kind"] != "tool" {
        return Ok(Invocation::default());
    }
    let name = v["name"].as_str().unwrap_or("");
    if ![
        "bash",
        "cmd",
        "exec",
        "exec_command",
        "powershell",
        "pwsh",
        "shell",
        "terminal",
        "run_terminal_cmd",
    ]
    .contains(
        &name
            .rsplit('.')
            .next()
            .unwrap_or("")
            .to_ascii_lowercase()
            .as_str(),
    ) {
        return Ok(Invocation::default());
    }
    let text = store::decode("tools", text)?;
    let (command, _) = crate::tool_text::split(
        &text,
        "tool",
        name,
        v["ok"].as_bool(),
        v["input_end"].as_u64().map(|n| n as usize),
        v["output_start"].as_u64().map(|n| n as usize),
    );
    if command.is_empty()
        || command.len() > 64 * 1024
        || v["input_chars"].as_u64() != Some(command.chars().count() as u64)
    {
        return Ok(Invocation::default());
    }
    Ok(input(&command))
}
pub(crate) fn is_meta(c: &Connection, h: &Hit, query: &str) -> Result<bool> {
    if matches!(h.who.as_str(), "harness" | "synthetic") || (h.who == "subagent" && h.turn == 0) {
        return Ok(true);
    }
    if h.who == "tool" {
        let row:Option<(Vec<u8>,String)>=c.query_row("SELECT t.text,e.body FROM tools t JOIN events e ON e.source=t.source AND e.session=t.session AND e.call_id=t.extra WHERE t.id=?",[h.rowid],|r|Ok((r.get(0)?,r.get(1)?))).optional()?;
        return match row {
            Some((t, b)) => Ok(event(t, b)?.read),
            None => Ok(false),
        };
    }
    if !matches!(h.who.as_str(), "agent" | "subagent") {
        return Ok(false);
    }
    let mut q=c.prepare("SELECT t.text,e.body FROM tools t JOIN events e ON e.source=t.source AND e.session=t.session AND e.call_id=t.extra WHERE t.agent=?1 AND t.session=?2 AND t.position>=?3 AND t.position<=?4 AND t.visible=1")?;
    let query = norm(query);
    for row in q.query_map(
        params![h.agent, h.session, h.turn.saturating_sub(4), h.turn],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )? {
        let (t, b) = row?;
        if event(t, b)?.queries.contains(&query) {
            return Ok(true);
        }
    }
    Ok(false)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_proven_commands_are_meta() {
        for s in [
            "agrep search 'needle text' --json",
            "env NAME=value agrep recall 'needle text'",
            "{\"cmd\":\"agrep search 'needle text'\"}",
            "await tools.exec_command({cmd: \"agrep search 'needle text'\"})",
        ] {
            let r = input(s);
            assert!(r.read, "{s}");
            assert!(r.queries.contains(&"needle text".into()), "{s}");
        }
        for s in [
            "echo 'agrep search needle'",
            "agrep search needle; echo nope",
            "unknown --cmd 'agrep search needle'",
            "agrep search $QUERY",
            "agrep setup",
        ] {
            assert!(!input(s).read, "{s}");
        }
        assert!(input("agrep around @example:1.abcd").read);
    }
}
