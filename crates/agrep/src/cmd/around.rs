use crate::{ready, Around};
use agrep_core::{
    evidence::{self, Display, Rendered, Request},
    store, surface,
};
use anyhow::Result;
use serde_json::{json, Value};
use std::io::IsTerminal;
/// 0.4.9's around freshness record (py/around.py:245-251,762-772): the rows may be stale
/// when nothing checked the sources, and 0.5 also counts changed sources awaiting refresh.
fn stale_record(checked: bool, pending_sources: usize) -> Option<Value> {
    if !checked {
        Some(json!({"state":"unchecked","failing":false,"checked":false,"may_be_stale":true}))
    } else if pending_sources > 0 {
        Some(agrep_core::render::freshness(true, pending_sources))
    } else {
        None
    }
}
pub(crate) fn run(mut a: Around, no_auto: bool, color: &str) -> Result<i32> {
    let result = (|| {
        if a.who.as_deref() == Some("you") {
            a.who = Some("user".into());
        }
        let exact = agrep_core::handles::is_handle(&a.session);
        let radius = match a.context.as_deref() {
            _ if a.whole => None,
            Some(context) => crate::cmd::usage::radius(context)?,
            None => Some(if exact { 0 } else { 4 }),
        };
        let (root, census) = ready(no_auto)?;
        let c = store::reader(&root)?;
        let w = evidence::load(&c, &Request::new(&a.session, a.turn, radius))?;
        let pending_sources = crate::finish_census(&root, census)?;
        let checked = agrep_core::render::checked(no_auto);
        let (who_given, who_tool) = (a.who.is_some(), a.who.as_deref() == Some("tool"));
        let tool_handle = w.claim.as_ref().is_some_and(|c| c.event.is_some());
        let rendered = evidence::render(
            &w,
            &Display {
                caller: agrep_core::caller::Caller::resolve(
                    &c,
                    agrep_core::caller::Identity::detect(),
                )?,
                json: a.json,
                full: a.full,
                max_chars: a.max_chars.unwrap_or(4000),
                explicit_max_chars: a.max_chars.is_some(),
                no_tools: a.no_tools,
                tool_output: a.tool_output.unwrap_or(0),
                who: a.who,
                color: color == "always" || (color == "auto" && std::io::stdout().is_terminal()),
            },
        )?;
        if !a.json {
            for note in &w.notes {
                if let Some(s) = note["note"].as_str() {
                    eprintln!("{s}")
                }
            }
        }
        match rendered {
            Rendered::Page { text, tools } => {
                surface::write(&text)?;
                if let Some(record) = a
                    .json
                    .then(|| stale_record(checked, pending_sources))
                    .flatten()
                {
                    surface::line(&agrep_core::json_output::to_string(
                        &json!({"kind":"agrep-meta","freshness":record}),
                    )?)?;
                }
                // 0.4.9's `_warn_if_oversized`: a large pull names the lever that shrinks it.
                if text.len() >= 30_000 {
                    let cheaper = if radius.is_none() {
                        (!who_given).then(|| "`--who user` keeps the user's turns only".to_owned())
                    } else if tools && !who_tool && !tool_handle {
                        let flag = if a.full || a.tool_output.is_none() {
                            "--full"
                        } else {
                            "--tool-output"
                        };
                        Some(format!("dropping `{flag}` keeps the conversation only"))
                    } else {
                        None
                    };
                    if let Some(cheaper) = cheaper {
                        eprintln!("large pull: ~{}KB rendered - {cheaper}", text.len() / 1024);
                    }
                }
                Ok(0)
            }
            // 0.4.9's `_miss`: an unchecked absence is unverified, not a clean miss.
            Rendered::Miss { reason, scope } => {
                if a.json {
                    let mut row = json!({"kind":"agrep-meta","miss":{"code":"no-speaker-match","reason":reason,"scope":scope}});
                    if let Some(record) = stale_record(checked, pending_sources) {
                        row["freshness"] = record;
                    }
                    surface::line(&agrep_core::json_output::to_string(&row)?)?;
                } else if checked {
                    eprintln!("{reason}");
                } else {
                    eprintln!("{reason}; automatic freshness checks are disabled by --no-auto");
                }
                Ok(if checked { 1 } else { 2 })
            }
        }
    })();
    match result {
        Err(e) if a.json => {
            surface::line(&agrep_core::json_output::to_string(
                &json!({"kind":"agrep-meta","error":{"code":"window-unavailable","reason":format!("{e:#}")}}),
            )?)?;
            Ok(2)
        }
        other => other,
    }
}
