use agrep_core::data::DataRoot;
use anyhow::{bail, Result};
pub fn run(values: Vec<String>) -> Result<i32> {
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let _lock = root.lock(true)?;
    let mut c = root.config()?;
    if values.is_empty() {
        println!(
            "  tools = {}   (on/off)",
            if c.tools { "on" } else { "off" }
        );
        println!("  embeddings = {}   (auto/off)", c.embeddings);
        println!(
            "  post_index = {}   (any command, or off)",
            c.post_index.as_deref().unwrap_or("off")
        );
        return Ok(0);
    }
    let key = &values[0];
    let val = values[1..].join(" ");
    if val.trim().is_empty() {
        bail!("{key} requires a value")
    }
    let note = match key.as_str() {
        "tools" => {
            c.tools = match val.as_str() {
                "on" => true,
                "off" => false,
                _ => bail!("tools must be on/off"),
            };
            "search index picks it up on the next query"
        }
        "embeddings" => {
            if !matches!(val.as_str(), "auto" | "off") {
                bail!("embeddings must be auto/off")
            };
            c.embeddings = val.clone();
            "off disables model downloads and meaning search"
        }
        "post_index" => {
            c.post_index = if val == "off" {
                None
            } else {
                Some(val.clone())
            };
            "runs after each successful index"
        }
        _ => bail!("key must be one of: tools, embeddings, post_index"),
    };
    let result = root.save_config(&c);
    if let Err(e) = result {
        eprintln!("{e:#}");
        return Ok(1);
    }
    drop(_lock);
    if root.db().exists() {
        agrep_core::semantic::schedule(&root)?;
    }
    println!("{key} = {val}  ({note})");
    Ok(0)
}
