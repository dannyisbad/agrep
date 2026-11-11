//! Caller identity is explicit environment/process evidence, never cwd guessing.
use crate::{
    handles,
    query::{self, Options, Results},
};
use anyhow::Result;
use rusqlite::{Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
mod publication;
use publication::published;

use std::{
    collections::{BTreeMap, BTreeSet},
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
    /// The stderr line for a caller that self-exclusion cannot name. A forced `--no-self`
    /// says it was not applied in 0.4.9's words: recall and pack (`recall`) spoke for the
    /// caller, search, chats and summary for this session (py/recall.py:2018,
    /// py/session_context.py:1754).
    pub fn disclose(&self, forced: bool, recall: bool) {
        static SAID: AtomicBool = AtomicBool::new(false);
        if (!self.agent_context && !forced)
            || self.session.is_some()
            || SAID.swap(true, Ordering::Relaxed)
        {
            return;
        }
        let conflict = self.reason == "identity-conflict";
        crate::errln!(
            "{}",
            match (forced, recall, conflict) {
                (true, true, _) => {
                    "--no-self was not applied: caller identity is unknown; no session was excluded"
                }
                (true, false, true) => {
                    "--no-self was not applied: caller session identities conflict; self-exclusion is disabled and current conversations may appear"
                }
                (true, false, false) => {
                    "--no-self was not applied: could not identify this session - results may include the current conversation"
                }
                (false, _, true) => {
                    "caller session identities conflict: self-exclusion is off and this chat's own rows may appear"
                }
                (false, _, false) => {
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
    /// The read snapshot's session ids for hit handles, loaded by the first search.
    #[serde(skip)]
    pub(crate) sessions: std::sync::OnceLock<handles::SessionIndex>,
}
impl Caller {
    /// The session index of `c`, the snapshot this command reads, loaded once.
    pub fn sessions(&self, c: &Connection) -> Result<&handles::SessionIndex> {
        if let Some(index) = self.sessions.get() {
            return Ok(index);
        }
        let index = handles::SessionIndex::load(c)?;
        Ok(self.sessions.get_or_init(|| index))
    }
    pub fn resolve(c: &Connection, identity: Identity) -> Result<Self> {
        let _span = crate::semantic_profile::span("caller_resolve");
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
        // Each step seeks the children of one session, rather than walking all of the
        // agent's sessions for every descendant.
        out.live_members = c
            .prepare(
                "WITH RECURSIVE descendants(id) AS (SELECT ?1 UNION SELECT s.id FROM descendants d CROSS JOIN sessions s ON s.agent=?2 AND s.parent=d.id) SELECT id FROM sessions WHERE agent=?2 AND family=?3 AND side=1 AND first_ts>=?4 AND id IN descendants",
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
    /// 0.4.9's `calling_self_exclusion` for one surface: the caller rows it withholds, or
    /// `None` when it withholds nothing.
    pub fn policy(&self, request: Request<'_>) -> Option<Policy<'_>> {
        if !request.requested(self) {
            return None;
        }
        self.identity.session.as_ref()?;
        if request.no_self {
            return Some(Policy {
                caller: self,
                window: None,
            });
        }
        self.resolved.then(|| Policy {
            caller: self,
            window: Some(self.boundary.unwrap_or(0)),
        })
    }
    /// 0.4.9's `self_exclusion_disclosure`: the applied policy, or why there is none.
    pub fn metadata(
        &self,
        request: Request<'_>,
        excluded_hits: Option<usize>,
    ) -> serde_json::Value {
        if let Some(policy) = self.policy(request) {
            return policy.disclosure(excluded_hits);
        }
        let reason = if request.include_self {
            "explicit-include"
        } else if request.requested(self) {
            if self.identity.session.is_some() {
                "window-unresolved"
            } else {
                &self.identity.reason
            }
        } else {
            request.waived.unwrap_or("machine-surface")
        };
        serde_json::json!({"active": false, "reason": reason})
    }
}
/// What one surface asks of caller exclusion, as each 0.4.9 command decided it.
#[derive(Clone, Copy, Debug, Default)]
pub struct Request<'a> {
    pub include_self: bool,
    pub no_self: bool,
    /// Why the surface skips automatic exclusion, such as `chat-filter` for one named chat.
    pub waived: Option<&'a str>,
}
impl Request<'_> {
    fn requested(&self, caller: &Caller) -> bool {
        !self.include_self
            && (self.no_self || (caller.identity.agent_context && self.waived.is_none()))
    }
    /// The disclosure of a command that stopped before it resolved its caller, as
    /// 0.4.9's `_machine_fields` gave it on an early error (py/search.py:7441-7450).
    pub fn unresolved(&self, agent_context: bool) -> serde_json::Value {
        let reason = if self.include_self {
            "explicit-include"
        } else if self.no_self || (agent_context && self.waived.is_none()) {
            "caller-unresolved"
        } else {
            self.waived.unwrap_or("machine-surface")
        };
        serde_json::json!({"active": false, "reason": reason})
    }
}
/// The caller rows one surface withholds (0.4.9's `SelfExclusion`).
pub struct Policy<'a> {
    caller: &'a Caller,
    /// The caller transcript's first withheld turn; `None` withholds whole sessions.
    window: Option<i64>,
}
impl Policy<'_> {
    fn session(&self) -> &str {
        if self.caller.resolved {
            &self.caller.session
        } else {
            self.caller.identity.session.as_deref().unwrap_or_default()
        }
    }
    pub fn disclosure(&self, excluded_hits: Option<usize>) -> serde_json::Value {
        let mut out = serde_json::json!({
            "active": true,
            "reason": if self.window.is_some() { "window" } else { "forced" },
            "scope": if self.window.is_some() {
                "current-window"
            } else if self.caller.resolved {
                "session-family"
            } else {
                "session"
            },
            "session": self.session(),
            "excluded_hits": excluded_hits,
            "excluded_hits_known": excluded_hits.is_some(),
            "identity": self.caller.identity.reason,
        });
        if let Some(turn) = self.window {
            out["from_turn"] = turn.into();
        }
        out
    }
    /// 0.4.9's `self_exclusion_notice` for withheld rows or chats; silent at zero.
    pub fn notice(&self, dropped: usize, noun: &str) -> Option<String> {
        (dropped > 0).then(|| {
            format!(
                "excluded {} {noun}{} from {}",
                crate::surface::number(dropped),
                if dropped == 1 { "" } else { "s" },
                if self.window.is_some() {
                    "the current window"
                } else if self.caller.resolved {
                    "this session family"
                } else {
                    "this session"
                }
            )
        })
    }
    /// 0.4.9's `_self_exclusion_match_keys` count: the rows of `o` this policy withheld,
    /// read only inside the withheld sessions, with the caller's own transcript from its
    /// window onward. `chat` narrows the sessions as a `--chat` prefix does. Unknown past
    /// eight sessions, where 0.4.9 stopped probing.
    pub fn withheld(
        &self,
        c: &Connection,
        o: &Options,
        chat: Option<&str>,
    ) -> Result<Option<Withheld>> {
        let _span = crate::semantic_profile::span("self_exclusion_count");
        let caller = self.caller;
        let mut sessions = vec![self.session().to_owned()];
        let members = if self.window.is_some() {
            &caller.live_members
        } else {
            &caller.members
        };
        sessions.extend(
            members
                .iter()
                .filter(|s| s.as_str() != self.session())
                .collect::<BTreeSet<_>>()
                .into_iter()
                .cloned(),
        );
        if let Some(prefix) = chat.map(str::to_lowercase) {
            sessions.retain(|s| s.to_lowercase().starts_with(&prefix));
        }
        if sessions.len() > 8 {
            return Ok(None);
        }
        // `chat` holds an exact session id here, whose prefix these probes match
        // case-sensitively; a soft model filter keeps LIKE's default instead.
        let sensitive = !o.filters.model_soft;
        if sensitive {
            c.execute_batch("PRAGMA case_sensitive_like=ON")?;
        }
        let counted = self.count(c, o, &sessions);
        if sensitive {
            c.execute_batch("PRAGMA case_sensitive_like=OFF")?;
        }
        counted.map(Some)
    }
    fn count(&self, c: &Connection, o: &Options, sessions: &[String]) -> Result<Withheld> {
        let caller = self.caller;
        let mut withheld = Withheld::default();
        // Every probe searches the same pattern over another scope.
        let mut plans = query::Plans::default();
        for session in sessions {
            let from = self.window.filter(|_| session == &caller.session);
            let mut scoped = o.clone();
            scoped.limit = 0;
            scoped.exact = true;
            scoped.chats = false;
            scoped.sort = "time".into();
            scoped.filters.caller = None;
            scoped.filters.no_side = false;
            scoped.filters.no_meta = false;
            if caller.resolved
                && o.filters.agent.as_deref().is_none_or(|agents| {
                    agents
                        .split(',')
                        .any(|a| a.trim().eq_ignore_ascii_case(&caller.agent))
                })
            {
                scoped.filters.agent = Some(caller.agent.clone());
            }
            // The scope 0.4.9 probed, `--chat` with this session's id: the session and
            // any session whose id extends it.
            let mut prefix = scoped.clone();
            prefix.filters.chat = Some(session.clone());
            prefix.content = false;
            prefix.count_only = true;
            prefix.count_by_tier = true;
            scoped.filters.chat = None;
            scoped.filters.session_rows = Some(query::SessionRows {
                session: session.clone(),
                from,
            });
            let tally = |content: bool, plans: &mut query::Plans| -> Result<Withheld> {
                let mut probe = scoped.clone();
                probe.content = content;
                // Counts skip hit hydration. Rows are read only under --no-meta, whose
                // tooling roles 0.4.9's probes dropped. A related-term fallback counts each
                // stage by its coverage rules, which only a tiered count applies.
                probe.count_only = !o.filters.no_meta;
                probe.count_by_tier = !o.filters.no_meta && content;
                let found = query::search_with(c, &probe, plans)?;
                if !o.filters.no_meta {
                    return Ok(Withheld {
                        prose: found.total - found.tool_rows,
                        tools: found.tool_rows,
                    });
                }
                let mut tally = Withheld::default();
                for h in found
                    .hits
                    .iter()
                    .filter(|h| !query::meta_role(&h.who, h.turn))
                {
                    if h.who == "tool" {
                        tally.tools += 1;
                    } else {
                        tally.prose += 1;
                    }
                }
                Ok(tally)
            };
            let mut found = tally(false, &mut plans)?;
            if found.total() == 0 && o.content {
                // 0.4.9 stood in related-term rows only when nothing in its probe scope
                // matched exactly: no turn before the window, and no session whose id
                // extends this one.
                let matched = match from.filter(|&from| from > 0) {
                    Some(from) => {
                        prefix.filters.caller = Some(query::CallerFilter {
                            agent: caller.agent.clone(),
                            session: session.clone(),
                            family: caller.root.clone(),
                            exclude_from_turn: Some(from),
                            exclude_family: false,
                            excluded_sessions: vec![],
                        });
                        query::search_with(c, &prefix, &mut plans)?.total > 0
                    }
                    None => {
                        extended(c, session)?
                            && query::search_with(c, &prefix, &mut plans)?.total > 0
                    }
                };
                if !matched {
                    found = tally(true, &mut plans)?;
                }
            }
            withheld.prose += found.prose;
            withheld.tools += found.tools;
        }
        Ok(withheld)
    }
}
/// Whether another indexed session id extends `session`, so a `--chat` prefix of it
/// also matches that session.
fn extended(c: &Connection, session: &str) -> Result<bool> {
    Ok(c.query_row(
        "SELECT EXISTS(SELECT 1 FROM sessions WHERE id LIKE ?1 ESCAPE '\\' AND id<>?2)",
        rusqlite::params![
            format!(
                "{}%",
                session
                    .replace('\\', "\\\\")
                    .replace('%', "\\%")
                    .replace('_', "\\_")
            ),
            session
        ],
        |r| r.get(0),
    )?)
}
/// Matching rows a policy withheld, by 0.4.9's two keyword lanes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Withheld {
    pub prose: usize,
    pub tools: usize,
}
impl Withheld {
    pub fn total(self) -> usize {
        self.prose + self.tools
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
    search_with(
        c,
        o,
        caller,
        include_self,
        no_self,
        &mut query::Plans::default(),
    )
}
/// `search` keeping its plans for later searches of the same pattern.
pub fn search_with(
    c: &Connection,
    o: &Options,
    caller: &Caller,
    include_self: bool,
    no_self: bool,
    plans: &mut query::Plans,
) -> Result<Results> {
    let options = options(o, caller, include_self, no_self);
    let mut result = query::search_with(c, &options, plans)?;
    decorate(c, &mut result.hits, caller)?;
    Ok(result)
}
/// The forced coverage lane under options `caller::options` already scoped, its rows
/// marked and given handles as search's are.
pub fn coverage(c: &Connection, o: &Options, caller: &Caller) -> Result<Results> {
    let mut result = query::coverage(c, o)?;
    decorate(c, &mut result.hits, caller)?;
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
#[cfg(test)]
mod disclosure {
    use super::*;
    const S1: &str = "11111111-1111-4111-8111-111111111111";
    const U: &str = "99999999-9999-4999-8999-999999999999";
    fn caller(
        session: Option<&str>,
        reason: &str,
        resolved: bool,
        boundary: Option<i64>,
    ) -> Caller {
        Caller {
            identity: Identity {
                session: session.map(str::to_owned),
                reason: reason.into(),
                agent_context: true,
                published_sessions: vec![],
            },
            session: if resolved {
                session.unwrap_or_default().into()
            } else {
                String::new()
            },
            agent: "pi".into(),
            root: session.unwrap_or_default().into(),
            resolved,
            boundary,
            ..Caller::default()
        }
    }
    fn shown(value: serde_json::Value) -> String {
        crate::json_output::to_string(&value).unwrap()
    }
    // Expected strings are agrep 0.4.9's `surface_policy.self_exclusion_disclosure` for the
    // matching `SelfExclusion`/`CallingFamily` or inactive reason, serialized compactly.
    #[test]
    fn disclosures_match_0_4_9() {
        let automatic = Request::default();
        let forced = Request {
            no_self: true,
            ..Request::default()
        };
        for (caller, request, excluded, expected) in [
            (
                caller(Some(S1), "pi", true, Some(3)),
                automatic,
                None,
                r#"{"active":true,"reason":"window","scope":"current-window","session":"11111111-1111-4111-8111-111111111111","excluded_hits":null,"excluded_hits_known":false,"identity":"pi","from_turn":3}"#,
            ),
            (
                caller(Some(S1), "codex", true, None),
                automatic,
                None,
                r#"{"active":true,"reason":"window","scope":"current-window","session":"11111111-1111-4111-8111-111111111111","excluded_hits":null,"excluded_hits_known":false,"identity":"codex","from_turn":0}"#,
            ),
            (
                caller(Some(S1), "claude", true, Some(3)),
                forced,
                None,
                r#"{"active":true,"reason":"forced","scope":"session-family","session":"11111111-1111-4111-8111-111111111111","excluded_hits":null,"excluded_hits_known":false,"identity":"claude"}"#,
            ),
            (
                caller(Some(U), "pi", false, None),
                forced,
                None,
                r#"{"active":true,"reason":"forced","scope":"session","session":"99999999-9999-4999-8999-999999999999","excluded_hits":null,"excluded_hits_known":false,"identity":"pi"}"#,
            ),
            (
                caller(Some(S1), "corroborated", true, Some(5)),
                automatic,
                Some(4),
                r#"{"active":true,"reason":"window","scope":"current-window","session":"11111111-1111-4111-8111-111111111111","excluded_hits":4,"excluded_hits_known":true,"identity":"corroborated","from_turn":5}"#,
            ),
            (
                caller(Some(S1), "pi-process", true, None),
                automatic,
                Some(0),
                r#"{"active":true,"reason":"window","scope":"current-window","session":"11111111-1111-4111-8111-111111111111","excluded_hits":0,"excluded_hits_known":true,"identity":"pi-process","from_turn":0}"#,
            ),
            (
                caller(Some(S1), "pi", true, Some(3)),
                Request {
                    include_self: true,
                    ..automatic
                },
                None,
                r#"{"active":false,"reason":"explicit-include"}"#,
            ),
            (
                caller(Some(S1), "pi", true, Some(3)),
                Request {
                    waived: Some("chat-filter"),
                    ..automatic
                },
                None,
                r#"{"active":false,"reason":"chat-filter"}"#,
            ),
            (
                caller(Some(U), "pi", false, None),
                automatic,
                None,
                r#"{"active":false,"reason":"window-unresolved"}"#,
            ),
            (
                caller(None, "identity-conflict", false, None),
                automatic,
                None,
                r#"{"active":false,"reason":"identity-conflict"}"#,
            ),
            (
                caller(None, "caller-unresolved", false, None),
                forced,
                None,
                r#"{"active":false,"reason":"caller-unresolved"}"#,
            ),
        ] {
            assert_eq!(shown(caller.metadata(request, excluded)), expected);
        }
        let mut outside = caller(None, "caller-unresolved", false, None);
        outside.identity.agent_context = false;
        assert_eq!(
            shown(outside.metadata(Request::default(), None)),
            r#"{"active":false,"reason":"machine-surface"}"#
        );
    }
    // 0.4.9's `self_exclusion_notice` wording for hits and chats in each scope.
    #[test]
    fn notices_match_0_4_9() {
        let window = caller(Some(S1), "pi", true, Some(3));
        let policy = window.policy(Request::default()).unwrap();
        assert_eq!(policy.notice(0, "hit"), None);
        assert_eq!(
            policy.notice(1, "hit").unwrap(),
            "excluded 1 hit from the current window"
        );
        assert_eq!(
            policy.notice(1234, "hit").unwrap(),
            "excluded 1,234 hits from the current window"
        );
        let family = window
            .policy(Request {
                no_self: true,
                ..Request::default()
            })
            .unwrap();
        assert_eq!(
            family.notice(2, "chat").unwrap(),
            "excluded 2 chats from this session family"
        );
        let unindexed = caller(Some(U), "pi", false, None);
        let session = unindexed
            .policy(Request {
                no_self: true,
                ..Request::default()
            })
            .unwrap();
        assert_eq!(
            session.notice(3, "hit").unwrap(),
            "excluded 3 hits from this session"
        );
    }
}
/// Presentation-only decoration: caller exclusions already ran before ranking.
pub fn decorate_semantic(c: &Connection, result: &mut Results, caller: &Caller) -> Result<()> {
    let index = caller.sessions(c)?;
    for h in &mut result.hits {
        h.self_marker = caller.resolved && h.agent == caller.agent && h.session == caller.session;
        h.handle = index.mint(&h.session, h.turn, &h.text);
    }
    Ok(())
}
/// Marks the caller's own keyword rows and gives each its handle.
pub fn decorate(c: &Connection, hits: &mut [query::Hit], caller: &Caller) -> Result<()> {
    if hits.is_empty() {
        return Ok(());
    }
    let index = caller.sessions(c)?;
    // Search names its tool rows' events; the coverage lane reads its rows without them.
    let unnamed: Vec<i64> = hits
        .iter()
        .filter(|h| h.who == "tool" && h.event.is_none() && h.span.is_some_and(|(a, b)| b > a))
        .map(|h| h.rowid)
        .collect();
    let repeated = handles::repeated_calls(c, &unnamed)?;
    for h in hits {
        h.self_marker = caller.resolved && h.agent == caller.agent && h.session == caller.session;
        h.handle = index.mint(&h.session, h.turn, &h.text);
        if h.who == "tool" {
            if let Some((start, end)) = h.span.filter(|(a, b)| b > a) {
                let event = match (&h.event, repeated.get(&h.rowid)) {
                    (Some(event), _) => event.clone(),
                    (None, Some(call)) => handles::repeat_event_id(&h.session, h.ts, &h.text, call),
                    (None, None) => handles::event_id(&h.session, h.ts, &h.text),
                };
                h.handle += &format!("~{event}:{start}-{end}");
            }
        }
    }
    Ok(())
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
            publication::read_publication(&root, pid).unwrap(),
            vec!["first", "second"]
        );
        value["start"] = serde_json::json!("wrong-birth");
        std::fs::write(&path, value.to_string()).unwrap();
        assert!(publication::read_publication(&root, pid).is_none());
        value.as_object_mut().unwrap().remove("start");
        value["updated"] = serde_json::json!(process.born_ms - 3000);
        std::fs::write(&path, value.to_string()).unwrap();
        assert!(publication::read_publication(&root, pid).is_none());
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
}
