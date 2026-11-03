use crate::{ready, Around};
use agrep_core::{
    evidence::{self, Display, Request},
    store, surface,
};
use anyhow::{bail, Result};
use std::io::IsTerminal;
pub(crate) fn run(mut a: Around, no_auto: bool, color: &str) -> Result<i32> {
    let result = (|| {
        if let Some(who) = &a.who {
            a.who = Some(agrep_core::coverage::speakers(who)?);
        }
        let exact = agrep_core::handles::is_handle(&a.session);
        let radius = if a.whole || a.context.as_deref() == Some("all") {
            None
        } else {
            let n = a
                .context
                .as_deref()
                .map(str::parse::<i64>)
                .transpose()?
                .unwrap_or(if exact { 0 } else { 4 });
            if n < 0 {
                bail!("--context must be 0 or greater")
            }
            Some(n)
        };
        if a.no_tools
            && a.who
                .as_deref()
                .is_some_and(|w| w.split(',').any(|s| s == "tool"))
        {
            bail!("--no-tools conflicts with --who tool")
        }
        let root = ready(no_auto)?;
        let c = store::reader(&root)?;
        let w = evidence::load(&c, &Request::new(&a.session, a.turn, radius))?;
        let text = evidence::render(
            &w,
            &Display {
                caller: agrep_core::caller::Caller::resolve(
                    &c,
                    agrep_core::caller::Identity::detect(),
                )?,
                json: a.json,
                checked: agrep_core::render::checked(no_auto),
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
        surface::write(&text)?;
        Ok(0)
    })();
    match result {
        Err(e) if a.json => {
            surface::line(&agrep_core::json_output::to_string(
                &serde_json::json!({"kind":"agrep-meta","error":{"code":"window-unavailable","reason":format!("{e:#}")}}),
            )?)?;
            Ok(2)
        }
        other => other,
    }
}
