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
    write!(
        output,
        "{prompt} {} ",
        if default_yes { "[Y/n]" } else { "[y/N]" }
    )?;
    output.flush()?;
    let mut answer = String::new();
    if input.read_line(&mut answer)? == 0 {
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
    println!("agrep setup — local conversation search");
    let targets = integrations::targets(&agrep_core::ingest::home());
    if !o.no_teach {
        println!("Agent instructions: agrep writes one marked block; text outside the markers stays untouched. `agrep remove` removes it.");
        for t in targets.iter().filter(|t| t.detected) {
            println!("  {}", t.path.display());
        }
    }
    let teach = !o.no_teach && (o.yes || consent("write these instructions?")?);
    if !o.no_teach && !teach {
        println!("agent instructions skipped; `agrep setup --yes` opts in");
    }
    if !o.no_hook {
        println!("Recovery integrations: Claude PreCompact, Codex SessionStart/compact, and pi/omp extensions in detected client directories. They teach `agrep postcompact` after compaction.");
        for path in integrations::hook_paths(&agrep_core::ingest::home()) {
            println!("  {}", path.display());
        }
    }
    let hooks = !o.no_hook && (o.yes || consent("install these recovery integrations?")?);
    if !o.no_hook && !hooks {
        println!("post-compact integrations skipped; `agrep setup --yes` opts in");
    }
    let mut errors = integrations::install(&root, teach, hooks)?;
    let archive = if o.archive {
        Some(true)
    } else if o.no_archive {
        Some(false)
    } else if o.yes {
        None
    } else if io::stdin().is_terminal() {
        println!("Optional archive retention keeps compressed copies of the agents' original stores under {}.",root.path().join("archive").display());
        Some(consent("enable source-store archive retention?")?)
    } else {
        None
    };
    if let Some(enabled) = archive {
        let _lock = root.lock(true)?;
        let mut c = root.config()?;
        c.archive.enabled = enabled;
        root.save_config(&c)?;
        println!(
            "archive auto-capture {}",
            if enabled { "enabled" } else { "disabled" }
        );
    }
    match store::refresh(&root, true, false) {
        Ok(refresh) => {
            agrep_core::coverage::initialize(&root)?;
            if refresh.changed_rows > 0 {
                agrep_core::semantic::mark_index_changed(&root)?;
            }
            println!("search index ready");
        }
        Err(e) => errors.push(format!("search index: {e:#}")),
    }
    if !o.no_semantic && root.config()?.embeddings != "off" {
        println!(
            "Semantic model: downloads about 95 MiB once from Hugging Face into {}.",
            agrep_core::semantic_model::directory(&root).display()
        );
        if o.yes || consent("download the semantic model?")? {
            if let Err(e) = agrep_core::semantic::provision(&root) {
                errors.push(format!("semantic model: {e:#}"));
            }
        } else {
            println!("semantic download skipped; `agrep setup --yes` opts in");
        }
    }
    agrep_core::semantic::schedule(&root)?;
    for e in &errors {
        eprintln!("  ! {}", agrep_core::render::safe(e));
    }
    println!(
        "{}",
        if errors.is_empty() {
            "setup complete"
        } else {
            "setup incomplete; fix the files above and run `agrep setup` again"
        }
    );
    Ok(if errors.is_empty() { 0 } else { 1 })
}

#[cfg(test)]
mod tests {
    use super::read_consent;
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
}
