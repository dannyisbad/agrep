//! Usage refusals worded, ordered and corrected as 0.4.9's parsers raised them (exit 2).
use crate::{Around, Cli, Recall, Search};
use anyhow::Result;
use chrono::TimeZone;
use clap::{parser::ValueSource, ArgMatches, CommandFactory};
use std::collections::HashMap;
use std::ffi::OsString;

/// A usage error printed verbatim: `prog: message`, or the bare line 0.4.9 logged. A
/// refusal with a code is one 0.4.9 also reported in a `--json` record.
#[derive(Debug)]
pub(crate) struct Refusal(String, Option<&'static str>);
impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Refusal {}
/// 0.4.9's `KNOWN_AGENTS`, the names `--agent` accepts (py/session_context.py:1484).
const KNOWN_AGENTS: [&str; 12] = [
    "claude",
    "codex",
    "opencode",
    "antigravity",
    "agy",
    "kimi",
    "cline",
    "gemini",
    "crush",
    "cursor",
    "pi",
    "omp",
];
/// 0.4.9's `agent_filter_error` (py/session_context.py:1500-1522): the refusal of the first
/// `--agent` value before `--` that is blank or names no agent.
pub(crate) fn unknown_agent(args: &[OsString]) -> Option<String> {
    let words: Vec<_> = args.iter().map(|a| a.to_string_lossy()).collect();
    for (i, word) in words.iter().enumerate() {
        if word == "--" {
            break;
        }
        let value = match word.strip_prefix("--agent=") {
            Some(value) => value,
            None if word == "--agent" => match words.get(i + 1) {
                Some(value) => value,
                None => continue,
            },
            None => continue,
        };
        if value.trim().is_empty() {
            return Some(agrep_core::coverage::blank_notice("--agent"));
        }
        if !KNOWN_AGENTS.contains(&value) {
            return Some(format!(
                "unknown agent '{value}' - valid: {}",
                KNOWN_AGENTS.join(", ")
            ));
        }
    }
    None
}
pub(crate) fn refusal(prog: &str, message: impl std::fmt::Display) -> anyhow::Error {
    anyhow::Error::new(Refusal(
        if prog.is_empty() {
            message.to_string()
        } else {
            format!("{prog}: {message}")
        },
        None,
    ))
}
fn coded(code: &'static str, message: String) -> anyhow::Error {
    anyhow::Error::new(Refusal(message, Some(code)))
}
/// The code and reason of a refusal 0.4.9 reported in a `--json` record: an unreadable
/// time names no reason, an empty window names itself.
pub(crate) fn recorded(e: &anyhow::Error) -> Option<(&'static str, Option<&str>)> {
    let refusal = e.downcast_ref::<Refusal>()?;
    let code = refusal.1?;
    Some((
        code,
        (code == "empty-time-window").then_some(refusal.0.as_str()),
    ))
}

/// 0.4.9's `_json_error` record of a command that stopped before it searched
/// (py/search.py:7502-7520, py/recall.py:1785-1807). Search's leads with `kind` and
/// carries an empty completeness in `unit`s; recall's and pack's have neither.
pub(crate) fn stopped(
    unit: Option<&str>,
    query: serde_json::Value,
    engine: &str,
    (code, reason): (&str, Option<&str>),
    request: agrep_core::caller::Request<'_>,
    no_auto: bool,
) -> Result<()> {
    use serde_json::{json, Value};
    let mut error = json!({ "code": code });
    if let Some(reason) = reason {
        error["reason"] = json!(agrep_core::render::safe(reason));
    }
    let mut out = serde_json::Map::new();
    if unit.is_some() {
        out.insert("kind".into(), json!("agrep-meta"));
    }
    out.insert("query".into(), query);
    out.insert("engine".into(), json!(engine));
    out.insert("hits".into(), json!([]));
    out.insert("error".into(), error);
    let agent_context = agrep_core::caller::Identity::detect().agent_context;
    out.insert("self_exclusion".into(), request.unresolved(agent_context));
    if let Some(unit) = unit {
        out.insert("completeness".into(), json!({"shown":0,"total":0,"total_basis":"floor","unit":unit,"truncated":true,"action_unavailable_reason":"the query did not produce a result set"}));
    }
    out.insert("filter_coverage".into(), json!({"empty_dimensions":[],"checked":false,"reason":"the query returned before the index was filtered"}));
    let checked = agrep_core::render::checked(no_auto);
    out.insert(
        "freshness".into(),
        if checked {
            agrep_core::render::freshness(true, 0)
        } else {
            json!({"state":"unchecked","failing":false,"checked":false,"may_be_stale":true})
        },
    );
    out.insert("semantic_coverage".into(), Value::Null);
    agrep_core::surface::line(&agrep_core::json_output::to_string(&Value::Object(out))?)?;
    Ok(())
}

/// 0.4.9's `_resolve_chat` (py/search.py:1130-1151): the one session a `--chat` value
/// names, or `None` once its miss or ambiguity is logged.
pub(crate) fn chat(c: &rusqlite::Connection, value: &str) -> Result<Option<String>> {
    use agrep_core::{briefing::repr, handles::SessionIndex, render::safe};
    let mut found = SessionIndex::load(c)?.candidates(c, value)?;
    let value = value.trim();
    let shown = repr(&safe(
        value
            .strip_prefix('@')
            .filter(|_| !agrep_core::handles::is_handle(value))
            .unwrap_or(value),
    ));
    match found.len() {
        1 => return Ok(found.pop()),
        0 => eprintln!("no session matches {shown} - ids come from `agrep <pattern> --json`."),
        n => {
            eprintln!("{shown} is ambiguous ({n} sessions):");
            let listed = &found[..n.min(10)];
            for session in listed {
                eprintln!("  {}", safe(session));
            }
            let among = SessionIndex::from(found.clone());
            let hint: Vec<_> = listed.iter().map(|s| safe(&among.target(s))).collect();
            eprintln!("add a char: {}", hint.join(" / "));
        }
    }
    Ok(None)
}

/// One option as a gate addresses it: its spelling, its clap id, and what it prints.
struct Opt {
    flag: &'static str,
    id: &'static str,
    renders: &'static str,
}
/// 0.4.9's `OptionGate` (py/surface_policy.py:1042-1111): `option` changes nothing under
/// `blocked_by`, or without all of `needs`, so supplying it is refused, never dropped.
struct Gate {
    option: Opt,
    blocked_by: &'static [Opt],
    needs: &'static [Opt],
}
const fn opt(flag: &'static str, id: &'static str, renders: &'static str) -> Opt {
    Opt { flag, id, renders }
}
const COUNT: Opt = opt("-c", "count", "prints one number");
const COUNT_BY_TIER: Opt = opt(
    "--count-by-tier",
    "count_by_tier",
    "prints one number per tier",
);
const JSON: Opt = opt("--json", "json", "already writes one machine row per hit");
const CHATS: Opt = opt("-l", "chats", "lists matching chats, one line each");
const FLAT: Opt = opt("--flat", "flat", "writes one tab-separated row per hit");
const SOFT: Gate = Gate {
    option: opt("--soft", "soft", ""),
    blocked_by: &[],
    needs: &[opt("--model", "model", "")],
};
const SEARCH_GATES: &[Gate] = &[
    Gate {
        option: CHATS,
        blocked_by: &[COUNT, COUNT_BY_TIER],
        needs: &[],
    },
    Gate {
        option: JSON,
        blocked_by: &[COUNT, COUNT_BY_TIER],
        needs: &[],
    },
    Gate {
        option: FLAT,
        blocked_by: &[COUNT, COUNT_BY_TIER, JSON, CHATS],
        needs: &[],
    },
    Gate {
        option: opt("--classic", "classic", ""),
        blocked_by: &[COUNT, COUNT_BY_TIER, JSON, FLAT, CHATS],
        needs: &[],
    },
    Gate {
        option: opt("-n", "max", ""),
        blocked_by: &[COUNT, COUNT_BY_TIER],
        needs: &[],
    },
    Gate {
        option: opt("--sort", "sort", ""),
        blocked_by: &[COUNT, COUNT_BY_TIER],
        needs: &[],
    },
    SOFT,
];
const RECALL_GATES: &[Gate] = &[
    Gate {
        option: opt("--hits", "hits", ""),
        blocked_by: &[opt("--probe", "probe", "prints one pointer line")],
        needs: &[],
    },
    SOFT,
];
const DOCTOR_GATES: &[Gate] = &[Gate {
    option: opt("--fix", "fix", "repairs what the report flags"),
    blocked_by: &[opt("--json", "json", "prints one machine report")],
    needs: &[],
}];
const TAIL_GATES: &[Gate] = &[Gate {
    option: opt("--events", "events", ""),
    blocked_by: &[opt(
        "--snapshot",
        "snapshot",
        "prints current session state once, not an event stream",
    )],
    needs: &[],
}];
const AROUND_GATES: &[Gate] = &[
    Gate {
        option: opt("--max-chars", "max_chars", ""),
        blocked_by: &[opt("--full", "full", "uncaps indexed message text")],
        needs: &[],
    },
    Gate {
        option: opt("--tool-output", "tool_output", ""),
        blocked_by: &[opt("--no-tools", "no_tools", "hides tool-call lines")],
        needs: &[],
    },
];
/// The first refusal a set of supplied options earns, with the option it refuses.
fn refused(gates: &[Gate], supplied: impl Fn(&str) -> bool) -> Option<(String, &'static str)> {
    for gate in gates.iter().filter(|g| supplied(g.option.id)) {
        if let Some(blocker) = gate.blocked_by.iter().find(|b| supplied(b.id)) {
            return Some((
                format!(
                    "{} cannot be combined with {}, which {}",
                    gate.option.flag, blocker.flag, blocker.renders
                ),
                gate.option.id,
            ));
        }
        if let Some(needed) = gate.needs.iter().find(|n| !supplied(n.id)) {
            return Some((
                format!("{} has no effect without {}", gate.option.flag, needed.flag),
                gate.option.id,
            ));
        }
    }
    None
}
/// 0.4.9's `_without_gate_option` (py/surface_policy.py:1114-1167): `argv` without one
/// option, its aliases, its values and its letters in short clusters.
fn without(argv: &[String], id: &str, specs: &HashMap<String, (String, bool)>) -> Vec<String> {
    let mut kept = Vec::new();
    let mut index = 0;
    while index < argv.len() {
        let token = &argv[index];
        index += 1;
        if token == "--" {
            kept.extend_from_slice(&argv[index - 1..]);
            break;
        }
        let (flag, attached) = match token.split_once('=') {
            Some((flag, _)) => (flag, true),
            None => (token.as_str(), false),
        };
        if let Some((dest, takes_value)) = specs.get(flag) {
            let end = (index + usize::from(*takes_value && !attached)).min(argv.len());
            if dest != id {
                kept.push(token.clone());
                kept.extend_from_slice(&argv[index..end]);
            }
            index = end;
            continue;
        }
        if !token.starts_with('-') || token.starts_with("--") || token == "-" {
            kept.push(token.clone());
            continue;
        }
        let mut cluster = String::from("-");
        let mut values: &[String] = &[];
        for (offset, letter) in token.char_indices().skip(1) {
            let Some((dest, takes_value)) = specs.get(&format!("-{letter}")) else {
                cluster.push_str(&token[offset..]);
                break;
            };
            if dest != id {
                cluster.push(letter);
            }
            if *takes_value {
                let rest = &token[offset + letter.len_utf8()..];
                if !rest.is_empty() {
                    if dest != id {
                        cluster.push_str(rest);
                    }
                } else {
                    if dest != id {
                        values = &argv[index..(index + 1).min(argv.len())];
                    }
                    index += 1;
                }
                break;
            }
        }
        if cluster != "-" {
            kept.push(cluster);
            kept.extend_from_slice(values);
        }
    }
    kept
}
/// The words argparse's `nargs="*"` positional left over in 0.4.9: it took the first run
/// of words, so a word typed after an option that follows that run was unrecognized.
fn stray_words(argv: &[String], specs: &HashMap<String, (String, bool)>) -> Vec<String> {
    // None before the first word, Some(false) inside the run, Some(true) after it.
    let mut run = None;
    let mut stray = Vec::new();
    let mut index = 0;
    while index < argv.len() {
        let token = &argv[index];
        index += 1;
        if token == "--" {
            if run == Some(true) {
                stray.extend_from_slice(&argv[index..]);
            }
            break;
        }
        if token == "-" || !token.starts_with('-') {
            if run == Some(true) {
                stray.push(token.clone());
            } else {
                run = Some(false);
            }
            continue;
        }
        if run == Some(false) {
            run = Some(true);
        }
        let (flag, attached) = match token.split_once('=') {
            Some((flag, _)) => (flag, true),
            None => (token.as_str(), false),
        };
        if let Some((_, takes_value)) = specs.get(flag) {
            index += usize::from(*takes_value && !attached);
            continue;
        }
        if token.starts_with("--") {
            continue;
        }
        for (offset, letter) in token.char_indices().skip(1) {
            let Some((_, takes_value)) = specs.get(&format!("-{letter}")) else {
                break;
            };
            if *takes_value {
                index += usize::from(token[offset + letter.len_utf8()..].is_empty());
                break;
            }
        }
    }
    stray
}
/// The command line as parsed: which options were typed and the command's own words.
pub(crate) struct Invocation {
    command: Option<(String, ArgMatches)>,
    argv: Vec<OsString>,
}
impl Invocation {
    /// `args` holds the program, the command word and the command's arguments.
    pub(crate) fn new(args: &[OsString], matches: &ArgMatches) -> Self {
        Self {
            command: matches
                .subcommand()
                .map(|(name, sub)| (name.to_owned(), sub.clone())),
            argv: args.iter().skip(2).cloned().collect(),
        }
    }
    /// Whether the command's last `n` words followed a `--`, so none of them is an option.
    pub(crate) fn escaped(&self, n: usize) -> bool {
        n > 0 && self.argv.len() > n && self.argv[self.argv.len() - n - 1] == "--"
    }
    /// 0.4.9's argparse refusal of the words left over after the command's run of words.
    fn one_run(&self, prog: &str) -> Result<()> {
        let Some((command, _)) = &self.command else {
            return Ok(());
        };
        let argv: Vec<String> = self
            .argv
            .iter()
            .map(|a| a.to_string_lossy().into_owned())
            .collect();
        let option = |t: &&String| t.starts_with('-') && *t != "-";
        // Only an option after the first word can end the run.
        if !argv.iter().skip_while(option).any(|t| option(&t)) {
            return Ok(());
        }
        let stray = stray_words(&argv, &specs(command));
        if stray.is_empty() {
            return Ok(());
        }
        Err(refusal(
            prog,
            format!("unrecognized arguments: {}", stray.join(" ")),
        ))
    }
    /// 0.4.9's `option_gate_error` (py/surface_policy.py:1198-1219): the first refused
    /// option, and a copyable command that clears every gate.
    fn gates(&self, gates: &[Gate]) -> Result<()> {
        let Some((command, matches)) = &self.command else {
            return Ok(());
        };
        let typed = |id: &str| matches.value_source(id) == Some(ValueSource::CommandLine);
        let Some((message, mut option)) = refused(gates, typed) else {
            return Ok(());
        };
        let prog = if command == "search" {
            "agrep".to_owned()
        } else {
            format!("agrep {command}")
        };
        let argv: Option<Vec<String>> = self
            .argv
            .iter()
            .map(|a| a.to_str().map(str::to_owned))
            .collect();
        let Some(mut corrected) = argv else {
            return Err(refusal(&prog, message));
        };
        let specs = specs(command);
        let mut dropped = vec![];
        loop {
            corrected = without(&corrected, option, &specs);
            dropped.push(option);
            match refused(gates, |id| typed(id) && !dropped.contains(&id)) {
                Some((_, next)) => option = next,
                None => break,
            }
        }
        let words: Vec<String> = ["agrep".to_owned(), command.clone()]
            .into_iter()
            .chain(corrected)
            .collect();
        // 0.4.9 withheld the command when a word would not print as typed.
        if words
            .iter()
            .any(|w| w.contains('\0') || agrep_core::render::safe(w) != *w)
        {
            return Err(refusal(&prog, message));
        }
        Err(refusal(
            &prog,
            format!("{message}; run: {}", agrep_core::surface::shell(&words)),
        ))
    }
}
/// Every option spelling of `command`, with its clap id and whether it takes a value.
fn specs(command: &str) -> HashMap<String, (String, bool)> {
    let mut cli = Cli::command();
    cli.build();
    let mut specs = HashMap::new();
    for arg in cli
        .find_subcommand(command)
        .into_iter()
        .flat_map(|s| s.get_arguments())
        .filter(|a| !a.is_positional())
    {
        let spec = (
            arg.get_id().as_str().to_owned(),
            arg.get_action().takes_values(),
        );
        for long in arg
            .get_long()
            .into_iter()
            .chain(arg.get_all_aliases().unwrap_or_default())
        {
            specs.insert(format!("--{long}"), spec.clone());
        }
        for short in arg
            .get_short()
            .into_iter()
            .chain(arg.get_all_short_aliases().unwrap_or_default())
        {
            specs.insert(format!("-{short}"), spec.clone());
        }
    }
    specs
}
/// py/search.py:7253-7373, in 0.4.9's order.
pub(crate) fn search(s: &Search, invocation: &Invocation) -> Result<()> {
    invocation.one_run("agrep")?;
    let fail = |message: &str| Err(refusal("agrep", message));
    if s.more.is_some() && (s.count || s.count_by_tier) {
        return fail("-c/--count-by-tier cannot be combined with --more");
    }
    if s.count && s.count_by_tier {
        return fail("-c and --count-by-tier are mutually exclusive");
    }
    invocation.gates(SEARCH_GATES)?;
    if s.more.is_some() {
        let scope = &s.scope;
        let narrowed = !s.pattern.is_empty()
            || s.max.is_some()
            || s.regex
            || s.word
            || s.ignore_case
            || s.chats
            || s.flat
            || s.classic
            || scope.agent.is_some()
            || scope.project.is_some()
            || scope.here
            || scope.exclude_project.is_some()
            || scope.model.is_some()
            || scope.soft
            || scope.no_side
            || scope.who.is_some()
            || scope.no_who.is_some()
            || scope.no_meta
            || scope.chat.is_some()
            || scope.since.is_some()
            || scope.until.is_some()
            || s.sort != "score"
            || s.semantic
            || scope.all_side_chats
            || s.lexical
            || s.json
            || scope.include_self
            || scope.no_self
            || s.coverage
            || s.no_auto
            || s.color != "auto";
        if !narrowed {
            return Ok(());
        }
        if s.json || s.flat {
            let flag = if s.json { "--json" } else { "--flat" };
            return fail(&format!(
                "--more continues a frozen compact page; {flag} freezes none - rerun the query with {flag} -n 80 for a bounded larger page"
            ));
        }
        return fail("--more cannot be combined with search, filter, or output options");
    }
    if [s.semantic, s.regex, s.word].iter().filter(|&&m| m).count() > 1 {
        return fail("search matcher modes are mutually exclusive");
    }
    if s.lexical && s.semantic {
        return fail("-s and --lexical are contradictory; pick one");
    }
    if s.coverage && (s.json || s.flat || s.count || s.count_by_tier || s.chats) {
        return fail("--coverage cannot be combined with -c/--count-by-tier/-l/--json/--flat");
    }
    if s.coverage && (s.semantic || s.lexical || s.regex || s.word) {
        return fail("--coverage pins the coverage lane; drop the other matcher mode");
    }
    if s.count_by_tier && (s.semantic || s.regex || s.word) {
        return fail("--count-by-tier requires the default keyword matcher; drop -s/-E/-w");
    }
    if s.count && s.semantic {
        return fail("-c cannot be combined with --semantic; semantic totals are bounded");
    }
    if s.scope.no_meta && (s.count || s.count_by_tier) {
        return fail("--no-meta cannot narrow -c/--count-by-tier; machine counts keep grep parity");
    }
    let who_tool = s.scope.no_who.is_none()
        && s.scope
            .who
            .as_deref()
            .map(agrep_core::coverage::speakers)
            .and_then(Result::ok)
            .as_deref()
            == Some("tool");
    if s.semantic && who_tool {
        return fail(
            "--semantic cannot search --who tool because tool rows are not embedded; use --lexical",
        );
    }
    if s.semantic && s.sort == "position" {
        return fail("--sort position is undefined for semantic candidates; use score or time");
    }
    if s.scope.all_side_chats && !s.semantic {
        return fail("--all-side-chats requires --semantic");
    }
    Ok(())
}
/// py/surface_policy.py:1222-1229.
pub(crate) fn doctor(invocation: &Invocation) -> Result<()> {
    invocation.gates(DOCTOR_GATES)
}
/// py/tail.py:83-88.
pub(crate) fn tail(invocation: &Invocation) -> Result<()> {
    invocation.gates(TAIL_GATES)
}
/// py/recall.py:1682-1703, in 0.4.9's order.
pub(crate) fn recall(a: &Recall, invocation: &Invocation, prog: &str) -> Result<()> {
    let prog = format!("agrep {prog}");
    let fail = |message: &str| Err(refusal(&prog, message));
    if a.hits == Some(0) {
        return fail("--hits must be at least 1");
    }
    if a.budget > 0 && a.budget < 64 {
        return fail("a finite --budget must be at least 64 bytes");
    }
    if a.json && a.budget > 0 && a.budget < 2048 {
        return fail("a finite JSON --budget must be at least 2048 bytes");
    }
    if a.probe && a.json {
        return fail("--probe emits a pointer line and cannot be combined with --json");
    }
    invocation.gates(RECALL_GATES)
}
/// py/around.py:808-853, in 0.4.9's order.
pub(crate) fn around(a: &Around, invocation: &Invocation) -> Result<()> {
    let radius = a.context.as_deref().map(radius).transpose()?;
    invocation.gates(AROUND_GATES)?;
    if a.whole && radius.is_some_and(|r| r.is_some()) {
        return Err(refusal(
            "agrep around",
            "--whole reads the whole chat; drop -C N (or write -C all)",
        ));
    }
    if radius.flatten().is_some_and(|r| r < 0) {
        return Err(refusal("agrep around", "--context must be 0 or greater"));
    }
    Ok(())
}
/// A `-C` value as 0.4.9's `_context_arg` read it (py/around.py:281-288): `None` for
/// `all`, whatever its case or surrounding space.
pub(crate) fn radius(context: &str) -> Result<Option<i64>> {
    if context.trim().eq_ignore_ascii_case("all") {
        return Ok(None);
    }
    match context.trim().parse() {
        Ok(n) => Ok(Some(n)),
        Err(_) => Err(refusal(
            "agrep around",
            format!(
                "argument -C/--context/--radius: expected an integer or 'all', got '{context}'"
            ),
        )),
    }
}
/// A --since/--until value as epoch ms, as 0.4.9's `_parse_when` read it
/// (py/search.py:1102-1120): an age like `90s` `30m` `24h` `7d` `2w`, or a local
/// `YYYY-MM-DD` with an optional ` HH:MM[:SS]` or `THH:MM[:SS]`.
pub(crate) fn date(value: &str, now: i64) -> Result<i64> {
    let raw = value.trim();
    let lower = raw.to_lowercase();
    let digits = lower.len() - lower.trim_start_matches(|c: char| c.is_ascii_digit()).len();
    let per: i64 = match lower[digits..].trim_start() {
        "s" => 1,
        "m" => 60,
        "h" => 3600,
        "d" => 86400,
        "w" => 604800,
        _ => 0,
    };
    if digits > 0 && per > 0 {
        let n = lower[..digits].parse::<i64>().unwrap_or(i64::MAX);
        return Ok(now.saturating_sub(n.saturating_mul(per * 1000)));
    }
    let b = raw.as_bytes();
    let iso =
        b.len() > 11 && ymd(&b[..10]) && matches!(b[10], b'T' | b't') && b[11].is_ascii_digit();
    let absolute = if iso {
        format!("{} {}", &raw[..10], &raw[11..])
    } else {
        raw.to_owned()
    };
    let naive = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
        .iter()
        .find_map(|f| chrono::NaiveDateTime::parse_from_str(&absolute, f).ok())
        .or_else(|| {
            chrono::NaiveDate::parse_from_str(&absolute, "%Y-%m-%d")
                .ok()
                .and_then(|d| d.and_hms_opt(0, 0, 0))
        });
    let local = naive.and_then(|n| {
        chrono::Local
            .from_local_datetime(&n)
            .earliest()
            .or_else(|| {
                // Python read a skipped wall time with the offset in force before the gap.
                chrono::Local
                    .from_local_datetime(&(n + chrono::Duration::hours(1)))
                    .earliest()
            })
    });
    match local {
        Some(t) => Ok(t.timestamp_millis()),
        None => Err(coded(
            "invalid-time-filter",
            format!("bad time '{raw}' - use 7d / 24h / 2w / 30m, YYYY-MM-DD or YYYY-MM-DDTHH:MM"),
        )),
    }
}
/// `\d{4}-\d{2}-\d{2}` exactly.
fn ymd(b: &[u8]) -> bool {
    b.len() == 10
        && b.iter().enumerate().all(|(i, c)| {
            if i == 4 || i == 7 {
                *c == b'-'
            } else {
                c.is_ascii_digit()
            }
        })
}
/// 0.4.9's window check (py/surface_policy.py:943-980): `[since, until)` with until at
/// or before since holds no instant, a usage error the bounds alone prove.
pub(crate) fn window(since: &str, since_ms: i64, until: &str, until_ms: i64) -> Result<()> {
    if until_ms > since_ms {
        return Ok(());
    }
    let message = if until_ms == since_ms {
        let day = until.trim();
        let next = chrono::NaiveDate::parse_from_str(day, "%Y-%m-%d")
            .ok()
            .filter(|_| ymd(day.as_bytes()))
            .and_then(|d| d.succ_opt());
        let fix = match next {
            Some(day) => format!("use --until {day} for the whole of {since}"),
            None => "give --until a later moment".to_owned(),
        };
        format!(
            "empty time window: --since {since} and --until {until} name the same instant, \
             and [since, until) excludes its end, so it selects no time at all - {fix}"
        )
    } else {
        format!(
            "empty time window: --since {since} is at or after --until {until}, so \
             [since, until) selects no time at all - swap them for the range between them"
        )
    };
    Err(coded("empty-time-window", message))
}
/// Both bounds of a scope, read against one clock and checked as one window.
pub(crate) fn bounds(
    since: Option<&str>,
    until: Option<&str>,
) -> Result<(Option<i64>, Option<i64>)> {
    let now = chrono::Utc::now().timestamp_millis();
    let since_ms = since.map(|s| date(s, now)).transpose()?;
    let until_ms = until.map(|u| date(u, now)).transpose()?;
    if let (Some(s), Some(s_ms), Some(u), Some(u_ms)) = (since, since_ms, until, until_ms) {
        window(s, s_ms, u, u_ms)?;
    }
    Ok((since_ms, until_ms))
}
