use crate::Postcompact;
use agrep_core::{
    caller::{Caller, Identity},
    handles, recovery, store, surface,
};
use anyhow::{bail, Result};
pub(crate) fn run(a: Postcompact, no_auto: bool) -> Result<i32> {
    let result = (|| {
        if a.boundary_ms.is_some_and(|v| v < 0) {
            bail!("--boundary-ms must be a non-negative 64-bit timestamp")
        }
        let (root, census) = crate::ready(no_auto)?;
        let identity = Identity::detect();
        crate::finish_census(&root, census)?;
        let c = store::reader(&root)?;
        let mut identity = identity;
        if let Some(session) = &a.session {
            let candidates = handles::SessionIndex::load(&c)?.candidates(&c, session)?;
            if candidates.len() != 1 {
                bail!("requested session is missing or ambiguous")
            }
            identity.session = Some(candidates[0].clone());
            identity.reason = "explicit-session".into();
        }
        if identity.session.is_none() {
            bail!("could not identify this session; use --session ID")
        }
        let caller = Caller::resolve(&c, identity)?;
        let mut packet = recovery::packet(&c, &caller, a.boundary_ms)?;
        let partial = !agrep_core::render::checked(no_auto);
        if partial {
            packet["status"] = serde_json::json!("partial");
            packet["freshness"] = serde_json::json!({"checked":false,"reason":if no_auto { "--no-auto" } else { "read-only data root" }});
        }
        let code = if partial {
            2
        } else if packet["status"] == "empty" {
            if no_auto {
                2
            } else {
                1
            }
        } else {
            0
        };
        if a.json {
            surface::line(&agrep_core::json_output::to_string(&packet)?)?
        } else {
            surface::write(&recovery::human(&packet))?
        }
        Ok(code)
    })();
    match result {
        Ok(code) => Ok(code),
        Err(e) => {
            if a.json {
                surface::line(&agrep_core::json_output::to_string(&recovery::failure(
                    "unavailable",
                    &format!("{e:#}"),
                ))?)?
            } else {
                agrep_core::errln!("postcompact unavailable: {e:#}")
            }
            Ok(2)
        }
    }
}
