use agrep_core::{data::DataRoot, semantic::post_index};
use anyhow::Result;
/// 0.4.9's refusals (cli.py:1661-1672), before any data is touched: an unknown key names
/// the valid ones, and a missing or unsupported value names the key's choices.
fn checked(values: &[String]) -> Result<Option<(&str, String)>> {
    let Some((key, rest)) = values.split_first() else {
        return Ok(None);
    };
    let refuse = |message: String| super::usage::refusal("agrep set", message);
    let val = rest.join(" ");
    let (choices, help): (&[&str], _) = match key.as_str() {
        "tools" => (&["on", "off"], "on/off"),
        "embeddings" => (&["auto", "off"], "auto/off"),
        "post_index" => (&[], "any command, or off to disable it"),
        _ => {
            return Err(refuse(
                "key must be one of: tools, embeddings, post_index".into(),
            ))
        }
    };
    if val.trim().is_empty() {
        return Err(refuse(format!("{key} requires a value ({help})")));
    }
    if !choices.is_empty() && !choices.contains(&val.as_str()) {
        return Err(refuse(format!("{key} must be {help}")));
    }
    Ok(Some((key, val)))
}
/// What the new value changes, in the user's terms.
fn note(root: &DataRoot, key: &str, enabled: bool) -> String {
    match (key, enabled) {
        ("tools", true) => "searches include tool calls and their output".into(),
        ("tools", false) => {
            "searches leave out tool calls and their output unless you pass --who tool".into()
        }
        ("embeddings", true) => "agent searches add meaning results once the model is installed; new rows are embedded in the background".into(),
        ("embeddings", false) => "agent searches stay keyword-only and new rows are not embedded; -s and index --semantic still work".into(),
        (_, true) => format!(
            "runs after each index that adds or changes rows, for at most {} minutes; its output goes to {}",
            post_index::TIME_LIMIT.as_secs() / 60,
            post_index::output_path(root).display()
        ),
        (_, false) => "nothing runs after indexing".into(),
    }
}
pub fn run(values: Vec<String>) -> Result<i32> {
    let setting = checked(&values)?;
    let root = DataRoot::discover()?;
    root.validate(true)?;
    let _lock = root.lock(true)?;
    let mut c = root.config()?;
    let Some((key, mut val)) = setting else {
        agrep_core::outln!(
            "  tools = {}   (on/off)",
            if c.tools { "on" } else { "off" }
        )?;
        agrep_core::outln!("  embeddings = {}   (auto/off)", c.embeddings)?;
        agrep_core::outln!(
            "  post_index = {}   (any command, or off)",
            post_index::command(&c).unwrap_or("off")
        )?;
        return Ok(0);
    };
    let enabled = match key {
        "tools" => {
            c.tools = val == "on";
            c.tools
        }
        "embeddings" => {
            c.embeddings = val.clone();
            val != "off"
        }
        _ => {
            c.post_index = Some(val.clone());
            if post_index::command(&c).is_none() {
                c.post_index = None;
                val = "off".into();
            }
            c.post_index.is_some()
        }
    };
    let result = root.save_config(&c);
    if let Err(e) = result {
        agrep_core::errln!("{e:#}");
        return Ok(1);
    }
    drop(_lock);
    if root.db().exists() {
        agrep_core::semantic::schedule(&root)?;
    }
    agrep_core::outln!("{key} = {val}  ({})", note(&root, key, enabled))?;
    Ok(0)
}
