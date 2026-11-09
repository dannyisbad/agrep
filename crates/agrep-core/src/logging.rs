//! Bounded diagnostic logging shared by optional retrieval and detached work.
use crate::{
    data::{self, DataRoot},
    surface,
};
use anyhow::{bail, Result};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
};

pub fn open(root: &DataRoot) -> Result<File> {
    if data::read_only() {
        bail!("data directory is read-only");
    }
    let directory = root.path().join("log");
    data::private_dir(&directory)?;
    let path = directory.join("agrep.log");
    if path.metadata().is_ok_and(|m| m.len() >= 1024 * 1024) {
        let previous = directory.join("agrep.log.1");
        if previous.exists() {
            fs::remove_file(&previous)?;
        }
        fs::rename(&path, previous)?;
    }
    Ok(OpenOptions::new().create(true).append(true).open(path)?)
}
pub fn semantic_failure(root: &DataRoot, error: &anyhow::Error) {
    if data::read_only() {
        crate::errln!(
            "semantic search failed; returning keyword results: {}",
            surface::safe(&format!("{error:#}"), false)
        );
        return;
    }
    let full = format!("semantic search failed: {error:#}");
    let logged = open(root).and_then(|mut file| {
        writeln!(file, "{full}")?;
        Ok(())
    });
    let mut message =
        "semantic search failed; returning keyword results (details in log/agrep.log)".to_owned();
    if let Err(error) = logged {
        message += &format!(
            "; could not write diagnostic log: {}",
            surface::safe(&error.to_string(), false)
        );
    }
    crate::errln!("{message}");
}

/// Optional query inference reports a panic through its normal error channel.
pub fn spawn_semantic<T: Send + 'static>(
    work: impl FnOnce() -> Result<T> + Send + 'static,
) -> std::io::Result<std::thread::JoinHandle<Result<T>>> {
    static HOOK: std::sync::Once = std::sync::Once::new();
    HOOK.call_once(|| {
        let previous = std::panic::take_hook();
        std::panic::set_hook(Box::new(move |info| {
            if std::thread::current().name() != Some("agrep-semantic-query") {
                previous(info);
            }
        }));
    });
    std::thread::Builder::new()
        .name("agrep-semantic-query".into())
        .spawn(work)
}
pub fn panic_error(payload: Box<dyn std::any::Any + Send>) -> anyhow::Error {
    let message = payload
        .downcast_ref::<String>()
        .map(String::as_str)
        .or_else(|| payload.downcast_ref::<&str>().copied())
        .unwrap_or("non-string panic payload");
    anyhow::anyhow!("semantic query panicked: {message}")
}
