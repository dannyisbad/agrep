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
        // Integrator: replace with the engine's one-session inline refresh.
        if !no_auto {
            store::refresh(&root, false, false)?;
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
        let caller = Caller::resolve(&c, identity)?;
        let packet = recovery::packet(&c, &caller, a.boundary_ms)?;
        let code = if packet["status"] == "empty" {
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
