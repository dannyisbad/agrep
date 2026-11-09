use agrep_core::{data::DataRoot, integrations, store};
use anyhow::Result;
use std::io::{self, BufRead, IsTerminal, Write};
#[derive(Default)]
pub struct Options {
    pub yes: bool,
    pub no_teach: bool,
    pub no_hook: bool,
    pub no_semantic: bool,
    pub archive: bool,
    pub no_archive: bool,
}
/// Installs the semantic model, showing the download's bytes and percent on a terminal's
/// stderr; elsewhere it stays quiet.
pub(crate) fn download_model(root: &DataRoot) -> Result<()> {
    let terminal = io::stderr().is_terminal();
    let mut shown = None;
    let result = agrep_core::semantic::provision(root, &mut |done, total| {
        let percent = (done * 100).checked_div(total).unwrap_or(100);
        if terminal && shown != Some(percent) {
            shown = Some(percent);
            agrep_core::surface::note(format_args!("\r{}", progress_line(done, total)));
        }
    });
    if shown.is_some() {
        agrep_core::surface::note(format_args!("\n"));
    }
    result
}
fn progress_line(done: u64, total: u64) -> String {
    let mib = |bytes: u64| bytes as f64 / (1024. * 1024.);
    format!(
        "downloading the semantic model: {:.1} of {:.1} MiB ({}%)",
        mib(done),
        mib(total),
        (done * 100).checked_div(total).unwrap_or(100)
    )
}
fn consent(prompt: &str) -> Result<bool> {
    read_consent(
        io::stdin().is_terminal(),
        prompt,
        &mut io::stdin().lock(),
        &mut io::stdout().lock(),
    )
}
fn read_consent(
    terminal: bool,
    prompt: &str,
    input: &mut impl BufRead,
    output: &mut impl Write,
) -> Result<bool> {
    if !terminal {
        return Ok(false);
    }
    let default_yes = !prompt.contains("archive retention");
    let shown = agrep_core::surface::ask(
        output,
        format_args!("{prompt} {} ", if default_yes { "[Y/n]" } else { "[y/N]" }),
    )?;
    let mut answer = String::new();
    if !shown || input.read_line(&mut answer)? == 0 {
        return Ok(false);
    }
    let answer = answer.trim().to_lowercase();
    Ok(if answer.is_empty() {
        default_yes
    } else {
        matches!(answer.as_str(), "y" | "yes")
    })
}
/// Semantic integration calls this same narrow provisioning function signature.
pub fn run(o: Options) -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    agrep_core::outln!("agrep setup — local conversation search")?;
    let targets = integrations::targets(&agrep_core::ingest::home());
    if !o.no_teach {
        agrep_core::outln!("Agent instructions: agrep writes one marked block; text outside the markers stays untouched. `agrep remove` removes it.")?;
        for t in targets.iter().filter(|t| t.detected) {
            agrep_core::outln!("  {}", t.path.display())?;
        }
    }
    let teach = !o.no_teach && (o.yes || consent("write these instructions?")?);
    if !o.no_teach && !teach {
        agrep_core::outln!("agent instructions skipped; `agrep setup --yes` opts in")?;
    }
    if !o.no_hook {
        agrep_core::outln!("Recovery integrations: Claude PreCompact, Codex SessionStart/compact, and pi/omp extensions in detected client directories. They teach `agrep postcompact` after compaction.")?;
        for path in integrations::hook_paths(&agrep_core::ingest::home()) {
            agrep_core::outln!("  {}", path.display())?;
        }
    }
    let hooks = !o.no_hook && (o.yes || consent("install these recovery integrations?")?);
    if !o.no_hook && !hooks {
        agrep_core::outln!("post-compact integrations skipped; `agrep setup --yes` opts in")?;
    }
    let mut errors = integrations::install(&root, teach, hooks)?;
    let archive = if o.archive {
        Some(true)
    } else if o.no_archive {
        Some(false)
    } else if o.yes {
        None
    } else if io::stdin().is_terminal() {
        agrep_core::outln!("Optional archive retention keeps compressed copies of the agents' original stores under {}.",root.path().join("archive").display())?;
        Some(consent("enable source-store archive retention?")?)
    } else {
        None
    };
    if let Some(enabled) = archive {
        let _lock = root.lock(true)?;
        let mut c = root.config()?;
        c.archive.enabled = enabled;
        root.save_config(&c)?;
        agrep_core::outln!(
            "archive auto-capture {}",
            if enabled { "enabled" } else { "disabled" }
        )?;
    }
    match store::refresh(&root, true, false) {
        Ok(refresh) => {
            if refresh.changed_rows > 0 {
                agrep_core::semantic::mark_index_changed(&root)?;
            }
            agrep_core::outln!("search index ready")?;
        }
        Err(e) => errors.push(format!("search index: {e:#}")),
    }
    if !o.no_semantic && root.config()?.embeddings != "off" {
        agrep_core::outln!(
            "Semantic model: downloads about 95 MiB once from Hugging Face into {}.",
            agrep_core::semantic_model::directory(&root).display()
        )?;
        if o.yes || consent("download the semantic model?")? {
            if let Err(e) = download_model(&root) {
                errors.push(format!("semantic model: {e:#}"));
            } else {
                agrep_core::outln!("The meaning index builds in the background. Meaning results join searches once the queued rows fit within about one second of CPU embedding; keywords are ready now.")?;
            }
        } else {
            agrep_core::outln!("semantic download skipped; `agrep setup --yes` opts in")?;
        }
    }
    agrep_core::semantic::schedule(&root)?;
    for e in &errors {
        agrep_core::errln!("  ! {}", agrep_core::render::safe(e));
    }
    agrep_core::outln!(
        "{}",
        if errors.is_empty() {
            "setup complete"
        } else {
            "setup incomplete; fix the files above and run `agrep setup` again"
        }
    )?;
    Ok(if errors.is_empty() { 0 } else { 1 })
}

#[cfg(test)]
mod tests {
    use super::{progress_line, read_consent};
    #[test]
    fn download_progress_names_bytes_and_percent() {
        assert_eq!(
            progress_line(12_897_484, 98_916_591),
            "downloading the semantic model: 12.3 of 94.3 MiB (13%)"
        );
        assert_eq!(
            progress_line(0, 0),
            "downloading the semantic model: 0.0 of 0.0 MiB (100%)"
        );
    }
    #[test]
    fn consent_requires_terminal_and_declining_never_writes() {
        for (terminal, input, accepted) in [
            (false, "yes\n", false),
            (true, "", false),
            (true, "n\n", false),
            (true, "NO\n", false),
            (true, "\n", true),
            (true, "yes\n", true),
        ] {
            let mut output = Vec::new();
            assert_eq!(
                read_consent(terminal, "install?", &mut input.as_bytes(), &mut output).unwrap(),
                accepted
            );
            assert_eq!(output.is_empty(), !terminal);
        }
    }
    #[test]
    fn a_prompt_no_one_can_see_is_declined_without_reading_an_answer() {
        struct Closed;
        impl std::io::Write for Closed {
            fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
                Err(std::io::ErrorKind::BrokenPipe.into())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Err(std::io::ErrorKind::BrokenPipe.into())
            }
        }
        let mut input = "yes\n".as_bytes();
        assert!(!read_consent(true, "install?", &mut input, &mut Closed).unwrap());
        assert_eq!(input, b"yes\n");
    }
}
