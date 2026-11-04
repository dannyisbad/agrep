//! Caller identity is explicit environment/process evidence, never cwd guessing.
use crate::{
    handles,
    query::{self, Options, Results},
};
use anyhow::Result;
use rusqlite::{Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
};
pub const ENV_KEYS: &[&str] = &[
    "CLAUDECODE",
    "CLAUDE_CODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_SANDBOX",
    "CODEX_THREAD_ID",
    "OPENCODE",
    "CLINE_ACTIVE",
    "GEMINI_CLI",
    "CURSOR_AGENT",
    "AGREP_PI_SESSION_ID",
];
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Identity {
    pub session: Option<String>,
    pub reason: String,
    pub agent_context: bool,
    #[serde(default)]
    pub published_sessions: Vec<String>,
}
impl Identity {
    pub fn from_env(env: &BTreeMap<String, String>) -> Self {
        let values: Vec<_> = [
            ("claude", "CLAUDE_CODE_SESSION_ID"),
            ("codex", "CODEX_THREAD_ID"),
            ("pi", "AGREP_PI_SESSION_ID"),
        ]
        .into_iter()
        .filter_map(|(a, k)| {
            env.get(k)
                .filter(|s| !s.trim().is_empty())
                .map(|s| (a, s.trim().to_string()))
        })
        .collect();
        let agent_context = ENV_KEYS
            .iter()
            .any(|k| env.get(*k).is_some_and(|v| !v.is_empty()));
        if values.iter().map(|(_, v)| v).collect::<BTreeSet<_>>().len() > 1 {
            return Self {
                reason: "identity-conflict".into(),
                agent_context,
                ..Self::default()
            };
        }
        Self {
            session: values.first().map(|(_, v)| v.clone()),
            reason: if values.len() > 1 {
                "corroborated"
            } else {
                values
                    .first()
                    .map(|(a, _)| *a)
                    .unwrap_or("caller-unresolved")
            }
            .into(),
            agent_context,
            published_sessions: Vec::new(),
        }
    }
    pub fn detect() -> Self {
        let mut value = Self::from_env(&std::env::vars().collect());
        if value.session.is_none() && value.reason != "identity-conflict" {
            if let Some(s) = published() {
                value.session = s.first().cloned();
                value.published_sessions = s;
                value.reason = "pi-process".into();
                value.agent_context = true;
            }
        }
        value
    }
    pub fn disclose(&self, forced: bool) {
        static SAID: AtomicBool = AtomicBool::new(false);
        if (!self.agent_context && !forced)
            || self.session.is_some()
            || SAID.swap(true, Ordering::Relaxed)
        {
            return;
        }
        eprintln!(
            "{}",
            if self.reason == "identity-conflict" {
                "caller session identities conflict: self-exclusion is off and this chat's own rows may appear"
            } else {
                if forced {
                    "caller unknown: --no-self was not applied"
                } else {
                    "agent shell, caller unknown: this chat's own rows may appear (pi/omp: `agrep setup` installs the extension that names it)"
                }
            }
        );
    }
}
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Caller {
    pub identity: Identity,
    pub session: String,
    pub agent: String,
    pub root: String,
    pub members: Vec<String>,
    pub live_members: Vec<String>,
    pub boundary: Option<i64>,
    pub resolved: bool,
}
impl Caller {
    pub fn resolve(c: &Connection, identity: Identity) -> Result<Self> {
        let mut out = Self {
            identity,
            ..Self::default()
        };
        if out.identity.reason == "pi-process" {
            for s in &out.identity.published_sessions {
                let root: bool = c.query_row(
                    "SELECT EXISTS(SELECT 1 FROM sessions WHERE id=? AND (family=id OR side=0))",
                    [s],
                    |r| r.get(0),
                )?;
                if root {
                    out.identity.session = Some(s.clone());
                    break;
                }
            }
        }
        let Some(id) = out.identity.session.as_deref() else {
            return Ok(out);
        };
        let row = c
            .query_row(
                "SELECT agent,id,family FROM sessions WHERE id=?",
                [id],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()?;
        let Some((agent, session, root)) = row else {
            return Ok(out);
        };
        out.agent = agent;
        out.session = session;
        out.root = root;
        out.members = c
            .prepare("SELECT id FROM sessions WHERE agent=? AND family=?")?
            .query_map([&out.agent, &out.root], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        out.boundary = c.query_row(
            "SELECT max(position) FROM prose WHERE visible=1 AND session=? AND who='recap'",
            [&out.session],
            |r| r.get(0),
        )?;
        let ts: Option<i64> = c.query_row(
            "SELECT min(ts) FROM prose WHERE visible=1 AND session=? AND position=?",
            rusqlite::params![out.session, out.boundary.unwrap_or(0)],
            |r| r.get(0),
        )?;
        out.live_members = c
            .prepare(
                "WITH RECURSIVE descendants(id) AS (SELECT ?1 UNION SELECT s.id FROM sessions s JOIN descendants d ON s.parent=d.id WHERE s.agent=?2) SELECT id FROM sessions WHERE agent=?2 AND family=?3 AND side=1 AND first_ts>=?4 AND id IN descendants",
            )?
            .query_map(
                rusqlite::params![out.session, out.agent, out.root, ts.unwrap_or(0)],
                |r| r.get(0),
            )?
            .collect::<rusqlite::Result<_>>()?;
        for session in &out.identity.published_sessions {
            if !out.members.contains(session) {
                out.members.push(session.clone());
            }
            if session != &out.session && !out.live_members.contains(session) {
                out.live_members.push(session.clone());
            }
        }
        out.resolved = true;
        Ok(out)
    }
    pub fn metadata(&self, include_self: bool, no_self: bool) -> serde_json::Value {
        let requested = !include_self && (no_self || self.identity.agent_context);
        let active = requested && self.resolved;
        serde_json::json!({"active":active,"reason":if include_self{"explicit-include"}else if !requested{"machine-surface"}else if !self.resolved{"caller-unresolved"}else if no_self{"forced"}else{"window"},"identity":self.identity.reason,"scope":if no_self{"family"}else{"window"},"session":self.identity.session,"from_turn":self.boundary.unwrap_or(0),"excluded_hits":null,"excluded_hits_known":false})
    }
}
/// Attach caller identity before either retrieval lane ranks or counts rows.
pub fn options(o: &Options, caller: &Caller, include_self: bool, no_self: bool) -> Options {
    let mut o = o.clone();
    if caller.resolved {
        o.filters.caller = Some(query::CallerFilter {
            agent: caller.agent.clone(),
            session: caller.session.clone(),
            family: caller.root.clone(),
            exclude_from_turn: (!include_self && !no_self).then_some(caller.boundary.unwrap_or(0)),
            exclude_family: false,
            excluded_sessions: if include_self {
                vec![]
            } else if no_self {
                let mut sessions = caller.members.clone();
                sessions.push(caller.session.clone());
                sessions
            } else {
                caller.live_members.clone()
            },
        });
    }
    o
}
pub fn search(
    c: &Connection,
    o: &Options,
    caller: &Caller,
    include_self: bool,
    no_self: bool,
) -> Result<Results> {
    let options = options(o, caller, include_self, no_self);
    let mut result = query::search(c, &options)?;
    decorate(c, &mut result, caller, &options)?;
    Ok(result)
}
/// Session-list surfaces use the same native row predicate as retrieval.
pub fn excludes(
    caller: &Caller,
    agent: &str,
    session: &str,
    turn: i64,
    include_self: bool,
    no_self: bool,
) -> bool {
    let filters = query::Filters {
        caller: caller.resolved.then(|| query::CallerFilter {
            agent: caller.agent.clone(),
            session: caller.session.clone(),
            family: caller.root.clone(),
            exclude_from_turn: (!include_self && !no_self).then_some(caller.boundary.unwrap_or(0)),
            exclude_family: false,
            excluded_sessions: if include_self {
                vec![]
            } else if no_self {
                let mut s = caller.members.clone();
                s.push(caller.session.clone());
                s
            } else {
                caller.live_members.clone()
            },
        }),
        ..Default::default()
    };
    !query::caller_allows(
        &filters,
        &query::Hit {
            agent: agent.into(),
            session: session.into(),
            turn,
            ..Default::default()
        },
    )
}
/// Presentation-only decoration: caller exclusions already ran before ranking.
pub fn decorate_semantic(c: &Connection, result: &mut Results, caller: &Caller) -> Result<()> {
    let index = handles::SessionIndex::load(c)?;
    for h in &mut result.hits {
        h.self_marker = caller.resolved && h.agent == caller.agent && h.session == caller.session;
        h.handle = index.mint(&h.session, h.turn, &h.text);
    }
    Ok(())
}
fn decorate(c: &Connection, result: &mut Results, caller: &Caller, o: &Options) -> Result<()> {
    if result.hits.is_empty() {
        return Ok(());
    }
    let explicit = if o.regex || o.word {
        let pattern = if o.word {
            format!(r"\b(?:{})\b", regex::escape(&o.pattern))
        } else {
            o.pattern.clone()
        };
        Some(
            fancy_regex::RegexBuilder::new(&format!("(?i:{pattern})"))
                .backtrack_limit(100_000)
                .build()?,
        )
    } else {
        None
    };
    let spans = MatchSpans::new(&o.pattern);
    let index = handles::SessionIndex::load(c)?;
    for h in &mut result.hits {
        h.self_marker = caller.resolved && h.agent == caller.agent && h.session == caller.session;
        h.handle = index.mint(&h.session, h.turn, &h.text);
        if h.who == "tool" {
            let matched = if let Some(matcher) = &explicit {
                matcher.find(&h.text)?.map(|m| {
                    (
                        h.text[..m.start()].chars().count(),
                        h.text[..m.end()].chars().count(),
                    )
                })
            } else {
                spans.find(&h.text, &h.matched)
            };
            if let Some((start, end)) = matched.filter(|(a, b)| b > a) {
                h.handle += &format!(
                    "~{}:{start}-{end}",
                    handles::event_id(&h.session, h.ts, &h.text)
                );
            }
        }
    }
    Ok(())
}
/// Where 0.4.9 placed a keyword hit's match (`_match_span`), in characters: the first
/// separator-flexible phrase match; a lone word's best-aligned occurrence; for all
/// terms, the earliest of each term's best-aligned occurrence; for content terms, the
/// earliest occurrence of any.
struct MatchSpans {
    words: Vec<String>,
    phrase: Option<regex::Regex>,
    terms: Vec<(String, Vec<String>)>,
    content: Vec<String>,
}
impl MatchSpans {
    fn new(pattern: &str) -> Self {
        let words: Vec<String> = pattern
            .split(|c: char| crate::evidence::py_space(c) || c == '-' || c == '_')
            .filter(|word| !word.is_empty())
            .map(str::to_owned)
            .collect();
        let phrase = (words.len() > 1)
            .then(|| {
                let joined: Vec<String> = words.iter().map(|w| regex::escape(w)).collect();
                regex::Regex::new(&format!("(?i){}", joined.join(r"[\W_]*"))).ok()
            })
            .flatten();
        let mut terms: Vec<(String, Vec<String>)> = Vec::new();
        for word in &words {
            let variants = crate::boundary_rank::term_variants(word);
            if !variants.is_empty() && !terms.iter().any(|(_, seen)| *seen == variants) {
                terms.push((word.clone(), variants));
            }
        }
        let mut content: Vec<String> = Vec::new();
        for term in crate::evidence::content_terms(pattern) {
            let term = term.to_lowercase();
            if !content.contains(&term) {
                content.push(term);
            }
        }
        Self {
            words,
            phrase,
            terms,
            content,
        }
    }
    fn find(&self, text: &str, matched: &str) -> Option<(usize, usize)> {
        let low = text.to_lowercase();
        let terms = || {
            self.terms
                .iter()
                .filter_map(|(word, variants)| insensitive_span(text, &low, word, variants))
                .min()
        };
        match matched {
            "phrase" if self.words.len() == 1 => {
                insensitive_span(text, &low, &self.words[0], &self.words)
            }
            // The engine's phrase tier and this pattern agree; terms cover any drift.
            "phrase" => self
                .phrase
                .as_ref()
                .and_then(|phrase| phrase.find(text))
                .map(|m| {
                    (
                        text[..m.start()].chars().count(),
                        text[..m.end()].chars().count(),
                    )
                })
                .or_else(terms),
            "content-terms" => {
                let found = self
                    .content
                    .iter()
                    .filter_map(|term| low.find(term.as_str()).map(|at| (at, term)))
                    .min_by_key(|(at, _)| *at)?;
                let start = low[..found.0].chars().count();
                Some(original_span(text, start, start + found.1.chars().count()))
            }
            _ => terms(),
        }
    }
}
/// 0.4.9's `insensitive_span`: of the occurrences of any spelling in the lowercased text,
/// the first with the best boundary quality, as original characters.
fn insensitive_span(
    text: &str,
    low: &str,
    word: &str,
    spellings: &[String],
) -> Option<(usize, usize)> {
    let chars: Vec<char> = text.chars().collect();
    let mut first = None;
    let mut best = None;
    let mut best_quality = -1.0;
    for spelling in spellings {
        let needle = spelling.to_lowercase();
        let Some(width) = needle.chars().next().map(char::len_utf8) else {
            continue;
        };
        let mut from = 0;
        while let Some(at) = low[from..].find(&needle).map(|at| from + at) {
            let start = low[..at].chars().count();
            let span = original_span(text, start, start + needle.chars().count());
            first.get_or_insert(span);
            let quality = (f64::from(u8::from(boundary(&chars, span.0)))
                + f64::from(u8::from(boundary(&chars, span.1))))
                / 2.0;
            if quality > best_quality {
                (best, best_quality) = (Some(span), quality);
                if quality == 1.0 {
                    return best;
                }
            }
            from = at + low[at..].chars().next().map_or(width, char::len_utf8);
        }
    }
    if first.is_none() {
        let m = regex::Regex::new(&format!("(?i){}", regex::escape(word)))
            .ok()?
            .find(text)?;
        return Some((
            text[..m.start()].chars().count(),
            text[..m.end()].chars().count(),
        ));
    }
    best.or(first)
}
/// 0.4.9's `_ascii_boundary`: whether a word boundary falls before character `at`.
fn boundary(chars: &[char], at: usize) -> bool {
    if at == 0 || at == chars.len() {
        return true;
    }
    let (left, right) = (chars[at - 1], chars[at]);
    if right == '\''
        && at + 1 < chars.len()
        && left.is_alphabetic()
        && chars[at + 1].is_alphabetic()
    {
        return false;
    }
    if left == '\'' && at >= 2 && chars[at - 2].is_alphabetic() && right.is_alphabetic() {
        return false;
    }
    if !left.is_alphanumeric() || !right.is_alphanumeric() {
        return true;
    }
    if left.is_lowercase() && right.is_uppercase() {
        return true;
    }
    if left.is_uppercase()
        && right.is_uppercase()
        && at + 1 < chars.len()
        && chars[at + 1].is_lowercase()
    {
        return true;
    }
    (left.is_alphabetic() && right.is_numeric()) || (left.is_numeric() && right.is_alphabetic())
}
/// The original characters covering characters `start..end` of `text.to_lowercase()`,
/// which can be longer than `text` (0.4.9's `original_span_for_lowered`).
fn original_span(text: &str, start: usize, end: usize) -> (usize, usize) {
    let mut lowered = 0;
    let mut from = None;
    for (index, c) in text.chars().enumerate() {
        let next = lowered + c.to_lowercase().count();
        if from.is_none() && start < next {
            from = Some(index);
        }
        if let Some(from) = from {
            if end <= next {
                return (from, index + 1);
            }
        }
        lowered = next;
    }
    let n = text.chars().count();
    (from.unwrap_or(n), n)
}
fn publication_root() -> PathBuf {
    if let Some(p) = std::env::var_os("AGREP_CALLER_PUBLICATION_DIR") {
        return p.into();
    }
    #[cfg(unix)]
    {
        PathBuf::from(format!("/tmp/agrep-0.5-caller-v1-{}", unsafe {
            libc::geteuid()
        }))
    }
    #[cfg(not(unix))]
    {
        std::env::temp_dir().join("agrep-0.5-caller-v1-user")
    }
}
fn read_publication(root: &Path, pid: u32) -> Option<Vec<String>> {
    let meta = std::fs::symlink_metadata(root).ok()?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return None;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.mode() & 0o777 != 0o700 || meta.uid() != unsafe { libc::geteuid() } {
            return None;
        }
    }
    let path = root.join(format!("{pid}.json"));
    let meta = std::fs::symlink_metadata(&path).ok()?;
    if !meta.is_file() || meta.file_type().is_symlink() || meta.len() > 65536 {
        return None;
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK);
    }
    let mut file = options.open(path).ok()?;
    let meta = file.metadata().ok()?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.uid() != unsafe { libc::geteuid() } {
            return None;
        }
    }
    if !meta.is_file() || meta.len() > 65536 {
        return None;
    }
    let mut bytes = Vec::new();
    use std::io::Read;
    file.by_ref().take(65537).read_to_end(&mut bytes).ok()?;
    if bytes.len() > 65536 {
        return None;
    }
    let v: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    let process = crate::caller_process::info(pid)?;
    if let Some(start) = v["start"].as_str() {
        if process.identity != start {
            return None;
        }
    } else if v["updated"].as_i64()? + 2000 < process.born_ms {
        return None;
    }
    if v["pid"].as_u64()? != u64::from(pid) {
        return None;
    }
    let values = v["sessions"].as_array()?;
    if values.is_empty() || values.len() > 64 {
        return None;
    }
    let mut sessions = Vec::new();
    for value in values {
        let session = value.as_str()?;
        if session.is_empty()
            || session.len() > 128
            || !session
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
        {
            return None;
        }
        if !sessions.iter().any(|s| s == session) {
            sessions.push(session.to_owned());
        }
    }
    Some(sessions)
}

fn published() -> Option<Vec<String>> {
    let root = publication_root();
    if !root.exists() {
        return None;
    }
    let mut pid = std::process::id();
    let mut seen = BTreeSet::new();
    for _ in 0..24 {
        pid = crate::caller_process::info(pid)?.parent;
        if pid <= 1 || !seen.insert(pid) {
            return None;
        }
        if let Some(s) = read_publication(&root, pid) {
            return Some(s);
        }
    }
    None
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn process_publication_rejects_recycled_identity() {
        let root = std::env::temp_dir().join(format!(
            "agrep-pub-test-{}-{}",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap()
        ));
        std::fs::create_dir(&root).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        }
        let pid = std::process::id();
        let process = crate::caller_process::info(pid).unwrap();
        let path = root.join(format!("{pid}.json"));
        let mut value =
            serde_json::json!({"pid":pid,"sessions":["first","second"],"start":process.identity});
        std::fs::write(&path, value.to_string()).unwrap();
        assert_eq!(
            read_publication(&root, pid).unwrap(),
            vec!["first", "second"]
        );
        value["start"] = serde_json::json!("wrong-birth");
        std::fs::write(&path, value.to_string()).unwrap();
        assert!(read_publication(&root, pid).is_none());
        value.as_object_mut().unwrap().remove("start");
        value["updated"] = serde_json::json!(process.born_ms - 3000);
        std::fs::write(&path, value.to_string()).unwrap();
        assert!(read_publication(&root, pid).is_none());
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn env_conflicts_fail_open() {
        let env = BTreeMap::from([
            ("CODEX_THREAD_ID".into(), "one".into()),
            ("AGREP_PI_SESSION_ID".into(), "two".into()),
        ]);
        let id = Identity::from_env(&env);
        assert!(id.session.is_none());
        assert_eq!(id.reason, "identity-conflict");
    }
    // Expected spans were produced by agrep 0.4.9's own matchers: the `[\W_]*` phrase
    // search, `console.insensitive_span` with and without `_keyword_term_specs` variants,
    // and `corpusdb.content`'s earliest lowered occurrence.
    #[test]
    fn tool_hit_spans_follow_0_4_9_match_spans() {
        let text = "bash [failed]: ./run-migration --step 3\nERROR copper falcon migration timeout; copper-falcon_migration";
        let span = |query: &str, tier: &str| MatchSpans::new(query).find(text, tier);
        assert_eq!(span("copper falcon migration", "phrase"), Some((46, 69)));
        assert_eq!(
            span("falcon migrations copper", "all-terms"),
            Some((21, 30))
        );
        assert_eq!(span("migration", "phrase"), Some((21, 30)));
        assert_eq!(span("timeout copper", "content-terms"), Some((46, 52)));
    }
}
