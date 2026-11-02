use agrep_core::{data::DataRoot, diagnostics, history as h, render, store};
use anyhow::Result;
use serde_json::{json, Value};
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
    ) -> i32 {
        let reference = self.reference;
        let json_output = self.json;
        let v = json!({"version":1,"reference":reference,"verdict":verdict,"exit":exit,"summary":summary,"candidates":&candidates,"evidence":&evidence,"next_action":next_action});
        if json_output {
            println!("{v}")
        } else {
            println!("{}: {}", verdict, render::safe(summary));
            if let Some(action) = next_action {
                println!("next: {action}")
            }
            if let Some(path) = evidence["index"]["path"]
                .as_str()
                .or(evidence["index"].as_str())
            {
                println!("index: {}", render::safe(path));
            }
            for source in evidence["sources"].as_array().into_iter().flatten() {
                println!(
                    "source: {}{}",
                    render::safe(source["path"].as_str().unwrap_or("")),
                    source["last_error"]
                        .as_str()
                        .map(|s| format!(" · {}", render::safe(s)))
                        .unwrap_or_default()
                );
            }
            for candidate in candidates.as_array().into_iter().flatten() {
                if let Some(id) = candidate["session"].as_str() {
                    println!("session: {}", render::safe(id));
                }
            }
        }
        exit
    }
}
pub fn run(reference: String, json_output: bool) -> Result<i32> {
    match explain(reference.clone(), json_output) {
        Ok(code) => Ok(code),
        Err(error) => Ok(Output {
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
        )),
    }
}
fn explain(reference: String, json_output: bool) -> Result<i32> {
    let out = Output {
        reference: &reference,
        json: json_output,
    };
    let root = DataRoot::discover()?;
    if let Err(e) = root.validate(false) {
        return Ok(out.emit(
            "unprovable",
            2,
            &e.to_string(),
            json!([]),
            json!({"data_root":root.0}),
            Some("agrep doctor"),
        ));
    }
    let c = match store::reader(&root) {
        Ok(c) => c,
        Err(e) => {
            return Ok(out.emit(
                "unprovable",
                2,
                &e.to_string(),
                json!([]),
                json!({"index":root.db()}),
                Some("agrep doctor"),
            ))
        }
    };
    let all = h::sessions(&c)?;
    let (mut matches, _) = match h::resolve_indexed(&c, &all, &reference) {
        Ok(found) => found,
        Err(e) => {
            return Ok(out.emit(
                "unprovable",
                2,
                &e.to_string(),
                json!([]),
                json!({"index":root.db()}),
                Some("use a session id or a complete handle"),
            ))
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
        return Ok(out.emit(
            "ambiguous",
            2,
            "multiple indexed sessions match",
            candidates,
            evidence,
            Some("use a full session id"),
        ));
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
        return Ok(out.emit(
            "indexed",
            0,
            "session is indexed and searchable",
            candidates,
            evidence,
            None,
        ));
    }
    if matching_sources.iter().any(|s| !s["last_error"].is_null())
        || sources.iter().any(|s| !s["last_error"].is_null())
    {
        return Ok(out.emit(
            "unprovable",
            2,
            "source read failures prevent proving absence",
            candidates,
            json!({"index":root.db(),"sources":sources}),
            Some("agrep doctor"),
        ));
    }
    let live = store::census()?;
    let stale = live.iter().any(|s| {
        sources
            .iter()
            .find(|v| v["path"] == s.path)
            .is_none_or(|v| {
                v["bytes"].as_u64() != Some(s.size)
                    || v["mtime_ns"].as_i64() != Some(s.mtime_ns)
                    || v["change_token"].as_str() != Some(s.token.as_str())
            })
    });
    if stale {
        return Ok(out.emit(
            "unprovable",
            2,
            "source files changed since the last refresh",
            candidates,
            evidence,
            Some("agrep index"),
        ));
    }
    Ok(out.emit(
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
    ))
}
