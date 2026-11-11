use agrep_core::{data::DataRoot, diagnostics, history as h, render, store};
use anyhow::Result;
use serde_json::{json, Value};
/// 0.4.9 listed at most this many candidates (py/why.py:42).
const CANDIDATE_LINES: usize = 20;
/// 0.4.9's candidate line (py/why.py:403-462): `agent · project  session  «first line»`,
/// the first line cut to 60 characters.
fn candidate_label(candidate: &Value) -> String {
    let text = |key: &str| candidate[key].as_str().unwrap_or("");
    let or = |fallback, key| {
        Some(text(key))
            .filter(|s| !s.is_empty())
            .unwrap_or(fallback)
    };
    let mut label = format!(
        "{} · {}  {}",
        or("?", "agent"),
        or("-", "project"),
        text("session")
    );
    let first = h::one_line(text("first_text"), usize::MAX);
    if !first.is_empty() {
        let quoted: String = if first.chars().count() > 60 {
            first.chars().take(59).chain(['…']).collect()
        } else {
            first
        };
        label += &format!("  «{quoted}»");
    }
    label
}
struct Output<'a> {
    reference: &'a str,
    json: bool,
}
impl Output<'_> {
    fn emit(
        &self,
        verdict: &str,
        exit: i32,
        summary: &str,
        candidates: Value,
        evidence: Value,
        next_action: Option<&str>,
    ) -> Result<i32> {
        let reference = self.reference;
        let json_output = self.json;
        let v = json!({"version":1,"reference":reference,"verdict":verdict,"exit":exit,"summary":summary,"candidates":&candidates,"evidence":&evidence,"next_action":next_action});
        if json_output {
            agrep_core::outln!(
                "{}",
                agrep_core::json_output::to_string(&v).expect("serializing JSON value")
            )?
        } else {
            agrep_core::outln!("{}: {}", verdict, render::safe(summary))?;
            if let Some(action) = next_action {
                agrep_core::outln!("next: {action}")?
            }
            if let Some(path) = evidence["index"]["path"]
                .as_str()
                .or(evidence["index"].as_str())
            {
                agrep_core::outln!("index: {}", render::safe(path))?;
            }
            for source in evidence["sources"].as_array().into_iter().flatten() {
                agrep_core::outln!(
                    "source: {}{}",
                    render::safe(source["path"].as_str().unwrap_or("")),
                    source["last_error"]
                        .as_str()
                        .map(|s| format!(" · {}", render::safe(s)))
                        .unwrap_or_default()
                )?;
            }
            let candidates = candidates.as_array().map_or(&[][..], Vec::as_slice);
            for candidate in candidates.iter().take(CANDIDATE_LINES) {
                agrep_core::outln!("  {}", render::safe(&candidate_label(candidate)))?;
            }
            if candidates.len() > CANDIDATE_LINES {
                agrep_core::outln!("  … {} more", candidates.len() - CANDIDATE_LINES)?;
            }
        }
        Ok(exit)
    }
}
pub fn run(reference: String, json_output: bool) -> Result<i32> {
    match explain(reference.clone(), json_output) {
        Ok(code) => Ok(code),
        Err(error) => Output {
            reference: &reference,
            json: json_output,
        }
        .emit(
            "unprovable",
            2,
            &error.to_string(),
            json!([]),
            json!({"index":DataRoot::discover().ok().map(|root|root.db())}),
            Some("agrep doctor"),
        ),
    }
}
fn explain(reference: String, json_output: bool) -> Result<i32> {
    let out = Output {
        reference: &reference,
        json: json_output,
    };
    let root = DataRoot::discover()?;
    if let Err(e) = root.validate(false) {
        return out.emit(
            "unprovable",
            2,
            &e.to_string(),
            json!([]),
            json!({"data_root":root.0}),
            Some("agrep doctor"),
        );
    }
    if root.db().exists() {
        let (_, census) = crate::ready(false)?;
        crate::finish_census(&root, census)?;
    }
    let c = match store::reader(&root) {
        Ok(c) => c,
        Err(e) => {
            return out.emit(
                "unprovable",
                2,
                &e.to_string(),
                json!([]),
                json!({"index":root.db()}),
                Some("agrep doctor"),
            )
        }
    };
    let all = h::sessions(&c)?;
    let named = agrep_core::rollouts::named(&c, &reference)?;
    if !named.is_empty() {
        return rollout(&out, &c, &all, &named, root.db());
    }
    let (mut matches, _) = match h::resolve_indexed(&c, &all, &reference) {
        Ok(found) => found,
        Err(e) => {
            return out.emit(
                "unprovable",
                2,
                &e.to_string(),
                json!([]),
                json!({"index":root.db()}),
                Some("use a session id or a complete handle"),
            )
        }
    };
    let sources = diagnostics::source_rows(&c)?;
    let matching_sources: Vec<_> = sources
        .iter()
        .filter(|s| s["path"].as_str().is_some_and(|p| p == reference))
        .collect();
    if matches.is_empty() && !matching_sources.is_empty() {
        let mut q=c.prepare("SELECT DISTINCT agent,session FROM prose WHERE source IN (SELECT rowid FROM sources WHERE path=?1) AND visible=1 UNION SELECT DISTINCT agent,session FROM tools WHERE source IN (SELECT rowid FROM sources WHERE path=?1) AND visible=1")?;
        let ids = q
            .query_map([&reference], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        matches = all
            .iter()
            .filter(|s| ids.contains(&(s.agent.clone(), s.id.clone())))
            .collect();
    }
    let candidates = json!(matches);
    let mut evidence =
        json!({"index":{"path":root.db(),"served":"index.db"},"sources":matching_sources});
    if matches.len() > 1 {
        return out.emit(
            "ambiguous",
            2,
            "multiple indexed sessions match",
            candidates,
            evidence,
            Some("use a full session id"),
        );
    }
    if let Some(s) = matches.first() {
        let mut q=c.prepare("SELECT DISTINCT src.path,src.last_error FROM sources src JOIN (SELECT source FROM prose WHERE session=?1 AND agent=?2 AND visible=1 UNION SELECT source FROM tools WHERE session=?1 AND agent=?2 AND visible=1) p ON p.source=src.rowid")?;
        evidence["sources"] = json!(q
            .query_map([&s.id, &s.agent], |r| Ok(
                json!({"path":diagnostics::source_path(&r.get::<_,String>(0)?,&s.agent),"last_error":r.get::<_,Option<String>>(1)?})
            ))?
            .collect::<rusqlite::Result<Vec<_>>>()?);
        let n = h::turn_count(&c, s)?;
        evidence["index"]["turns"] = json!(n);
        return out.emit(
            "indexed",
            0,
            "session is indexed and searchable",
            candidates,
            evidence,
            None,
        );
    }
    if matching_sources.iter().any(|s| !s["last_error"].is_null())
        || sources.iter().any(|s| !s["last_error"].is_null())
    {
        return out.emit(
            "unprovable",
            2,
            "source read failures prevent proving absence",
            candidates,
            json!({"index":root.db(),"sources":sources}),
            Some("agrep doctor"),
        );
    }
    out.emit(
        "not-indexed",
        1,
        if !matching_sources.is_empty() {
            "source is recorded but contains no searchable rows"
        } else {
            "no indexed session or discovered source matches"
        },
        candidates,
        evidence,
        Some("agrep chats"),
    )
}
/// A reference naming Codex rollouts by their file id: where their rows live, or why the
/// file holds none.
fn rollout(
    out: &Output<'_>,
    c: &rusqlite::Connection,
    all: &[h::Session],
    named: &[agrep_core::rollouts::Rollout],
    index: std::path::PathBuf,
) -> Result<i32> {
    use agrep_core::rollouts::Held;
    let [rollout] = named else {
        let ids: Vec<_> = named.iter().map(|rollout| rollout.id.as_str()).collect();
        return out.emit(
            "ambiguous",
            2,
            "multiple Codex rollout files match",
            json!([]),
            json!({"index":{"path":index,"served":"index.db"},"rollouts":ids}),
            Some("use a full rollout id"),
        );
    };
    let sources: Vec<_> = diagnostics::source_rows(c)?
        .into_iter()
        .filter(|s| {
            s["path"]
                .as_str()
                .is_some_and(|path| rollout.paths.iter().any(|p| p == path))
        })
        .collect();
    let evidence = json!({"index":{"path":index,"served":"index.db"},"sources":&sources});
    if sources.iter().any(|s| !s["last_error"].is_null()) {
        return out.emit(
            "unprovable",
            2,
            "the rollout's file failed to read",
            json!([]),
            evidence,
            Some("agrep doctor"),
        );
    }
    let chats = |ids: &[String]| {
        json!(all
            .iter()
            .filter(|s| s.agent == "codex" && ids.contains(&s.id))
            .collect::<Vec<_>>())
    };
    let id = &rollout.id;
    match &rollout.held {
        Held::Chats(ids) => out.emit(
            "indexed",
            0,
            &format!(
                "rollout {id} is a file of chat {}, indexed and searchable",
                ids.join(", ")
            ),
            chats(ids),
            evidence,
            None,
        ),
        Held::Copied(inherited) => {
            let parents: Vec<_> = inherited.iter().map(|link| link.session.clone()).collect();
            out.emit(
                "indexed",
                0,
                &format!(
                    "rollout {id} is a fork holding only history it copied from {}, indexed there up to the fork",
                    parents.join(", ")
                ),
                chats(&parents),
                evidence,
                None,
            )
        }
        Held::Interrupted(threads) => {
            let mut summary = format!(
                "rollout {id} holds no searchable rows: a review interrupted before its result"
            );
            if !threads.is_empty() {
                summary += &format!("; its review thread {} is indexed", threads.join(", "));
            }
            let next = match threads.as_slice() {
                [thread] => format!("agrep <pattern> --chat {thread}"),
                _ => "agrep chats".into(),
            };
            out.emit(
                "not-indexed",
                1,
                &summary,
                chats(threads),
                evidence,
                Some(&next),
            )
        }
        Held::Empty(reason) => out.emit(
            "not-indexed",
            1,
            &format!("rollout {id} holds no searchable rows: {reason}"),
            json!([]),
            evidence,
            Some("agrep chats"),
        ),
    }
}
