use crate::Postcompact;
use agrep_core::{
    caller::{Caller, Identity},
    data::DataRoot,
    handles, recovery, store, surface,
};
use anyhow::{bail, Result};
pub(crate) fn run(a: Postcompact, no_auto: bool) -> Result<i32> {
    let result = (|| {
        if a.boundary_ms.is_some_and(|v| v < 0) {
            bail!("--boundary-ms must be a non-negative 64-bit timestamp")
        }
        let root = DataRoot::discover()?;
        root.validate(!no_auto)?;
        if !root.db().exists() && !no_auto {
            store::refresh(&root, true, false)?;
        }
        let c = store::reader(&root)?;
        let mut identity = Identity::detect();
        if let Some(session) = &a.session {
            let candidates = handles::candidates(&c, session)?;
            if candidates.len() != 1 {
                bail!("requested session is missing or ambiguous")
            }
            identity.session = Some(candidates[0].clone());
            identity.reason = "explicit-session".into();
        }
        if identity.session.is_none() {
            bail!("could not identify this session; use --session ID")
        }
        let mut caller = Caller::resolve(&c, identity.clone())?;
        drop(c);
        let refresh_error = if !no_auto {
            store::refresh_session(&root, &caller.agent, &caller.session).err()
        } else {
            None
        };
        let c = store::reader(&root)?;
        caller = Caller::resolve(&c, identity)?;
        let mut packet = recovery::packet(&c, &caller, a.boundary_ms)?;
        let partial = no_auto || refresh_error.is_some();
        if partial {
            packet["status"] = serde_json::json!("partial");
            packet["freshness"] = serde_json::json!({"checked":false,"reason":refresh_error.map(|e|e.to_string()).unwrap_or_else(||"--no-auto".into())});
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
                eprintln!("postcompact unavailable: {e:#}")
            }
            Ok(2)
        }
    }
}
