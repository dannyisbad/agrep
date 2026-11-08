use anyhow::Context;
use anyhow::Result;
use rusqlite::OptionalExtension;
use rusqlite::{params, Connection, Transaction};
use std::collections::HashMap;

fn blob_text<'a>(c: &Connection, cache: &'a mut HashMap<i64, String>, id: i64) -> Result<&'a str> {
    if let std::collections::hash_map::Entry::Vacant(e) = cache.entry(id) {
        let body: Vec<u8> = c
            .prepare_cached("SELECT body FROM tool_blobs WHERE id=?")?
            .query_row([id], |r| r.get(0))?;
        e.insert(crate::tool_store::decode(&body)?);
    }
    Ok(&cache[&id])
}
pub(super) fn reference_text(
    tx: &Transaction<'_>,
    source: i64,
    value: &mut serde_json::Value,
    preferred: Option<i64>,
    writer: &mut crate::tool_store::Writer,
    cache: &mut HashMap<i64, String>,
) -> Result<()> {
    let Some(text) = value.as_str().filter(|s| s.len() >= 96).map(str::to_owned) else {
        return Ok(());
    };
    let trimmed = text.trim();
    let found = if let Some(id) = preferred {
        if trimmed.is_empty() {
            None
        } else {
            blob_text(tx, cache, id)?
                .find(trimmed)
                .map(|start| (id, start, trimmed.len()))
        }
    } else {
        None
    };
    let (id, start, len, left, right) = if let Some((id, start, len)) = found {
        let left = &text[..text.len() - text.trim_start().len()];
        let right = &text[text.trim_end().len()..];
        (id, start, len, left, right)
    } else {
        (
            writer.blob(tx, source, crate::tool_store::Text::Plain(&text))?,
            0,
            text.len(),
            "",
            "",
        )
    };
    writer.own_blob(tx, source, id)?;
    *value = serde_json::json!({"__agrep_text_ref":[id,start,len,left,right]});
    Ok(())
}
#[derive(Clone)]
pub(super) struct EventParts {
    pub(super) call: String,
    pub(super) name: String,
    pub(super) input: String,
    pub(super) output: String,
    pub(super) state: String,
    pub(super) input_chars: i64,
    pub(super) output_chars: i64,
    pub(super) output_bytes: i64,
    pub(super) ok: Option<bool>,
}
pub(super) fn event_parts<'a>(
    c: &Connection,
    id: i64,
    cache: &'a mut HashMap<i64, EventParts>,
) -> Result<&'a EventParts> {
    if cache.contains_key(&id) {
        return Ok(&cache[&id]);
    }
    let mut p = c.prepare_cached("SELECT e.call_id,n.value,b.body,e.input_end,e.output_start,e.input_chars,e.output_chars,e.output_bytes,e.ok,e.state FROM event_rows e JOIN symbols n ON n.id=e.name_key JOIN tool_blobs b ON b.id=e.blob WHERE e.id=?")?.query_row([id],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?,r.get::<_,Vec<u8>>(2)?,r.get::<_,Option<usize>>(3)?,r.get::<_,Option<usize>>(4)?,r.get::<_,i64>(5)?,r.get::<_,i64>(6)?,r.get::<_,i64>(7)?,r.get::<_,Option<bool>>(8)?,r.get::<_,String>(9)?)))?;
    let text = crate::tool_store::decode(&p.2)?;
    let head: String = text.chars().take(p.3.unwrap_or(usize::MAX)).collect();
    let prefix = format!(
        "{}{}",
        p.1,
        if p.8 == Some(false) { " [failed]" } else { "" }
    );
    let input = if head == prefix {
        String::new()
    } else {
        head.strip_prefix(&format!("{prefix}: "))
            .unwrap_or(&head)
            .to_string()
    };
    let output =
        p.4.map_or_else(String::new, |start| text.chars().skip(start).collect());
    let parts = EventParts {
        call: std::mem::take(&mut p.0),
        name: std::mem::take(&mut p.1),
        input,
        output,
        input_chars: p.5,
        output_chars: p.6,
        output_bytes: p.7,
        ok: p.8,
        state: std::mem::take(&mut p.9),
    };
    cache.insert(id, parts);
    Ok(&cache[&id])
}
pub(super) fn padding(original: &str, stored: &str) -> Option<(String, String)> {
    (original.trim() == stored.trim()).then(|| {
        (
            original[..original.len() - original.trim_start().len()].to_string(),
            original[original.trim_end().len()..].to_string(),
        )
    })
}
fn restore_event(
    c: &Connection,
    value: &mut serde_json::Value,
    events: &mut HashMap<i64, EventParts>,
) -> Result<bool> {
    for (key, call) in [
        ("__agrep_event_call", true),
        ("__agrep_event_output", false),
    ] {
        if let Some(a) = value.get(key).and_then(|v| v.as_array()) {
            anyhow::ensure!(a.len() == 3, "invalid native event reference");
            let p = event_parts(c, a[0].as_i64().context("invalid event id")?, events)?;
            let text = format!(
                "{}{}{}",
                a[1].as_str().context("invalid event prefix")?,
                if call {
                    p.input.trim()
                } else {
                    p.output.trim()
                },
                a[2].as_str().context("invalid event suffix")?
            );
            *value = if call {
                serde_json::json!({"type":"toolCall","id":p.call,"name":p.name,"__agrep_input":text,"__agrep_input_chars":p.input_chars,"__agrep_state":p.state})
            } else {
                serde_json::json!({"role":"toolResult","toolCallId":p.call,"content":text,"__agrep_chars":p.output_chars,"__agrep_bytes":p.output_bytes,"isError":p.ok.map(|ok|!ok)})
            };
            return Ok(true);
        }
    }
    Ok(false)
}
pub(super) fn restore_text(
    c: &Connection,
    value: &mut serde_json::Value,
    cache: &mut HashMap<i64, String>,
    events: &mut HashMap<i64, EventParts>,
) -> Result<()> {
    if restore_event(c, value, events)? {
        return Ok(());
    }
    if value
        .as_object()
        .is_some_and(|v| v.len() == 1 && v.contains_key("__agrep_text_ref"))
    {
        let a = value["__agrep_text_ref"]
            .as_array()
            .context("invalid graph text reference")?;
        anyhow::ensure!(a.len() == 5, "invalid graph text reference");
        let id = a[0].as_i64().context("invalid graph blob")?;
        let start = a[1].as_u64().context("invalid graph offset")? as usize;
        let len = a[2].as_u64().context("invalid graph length")? as usize;
        let text = blob_text(c, cache, id)?
            .get(start..start.checked_add(len).context("graph range overflow")?)
            .context("invalid graph text range")?;
        *value = serde_json::Value::String(format!(
            "{}{}{}",
            a[3].as_str().context("invalid graph prefix")?,
            text,
            a[4].as_str().context("invalid graph suffix")?
        ));
    } else {
        match value {
            serde_json::Value::Array(a) => {
                for v in a {
                    restore_text(c, v, cache, events)?;
                }
            }
            serde_json::Value::Object(a) => {
                for v in a.values_mut() {
                    restore_text(c, v, cache, events)?;
                }
            }
            _ => {}
        }
    }
    Ok(())
}
pub(super) fn event_blob(
    tx: &Transaction<'_>,
    source: i64,
    session: Option<i64>,
    call: &str,
) -> Result<Option<(i64, i64)>> {
    if let Some(session) = session {
        Ok(tx.prepare_cached("SELECT id,blob FROM event_rows WHERE source=?1 AND session_key=?2 AND call_id=?3 AND active=1 AND blob IS NOT NULL")?.query_row(params![source,session,call], |r|Ok((r.get(0)?,r.get(1)?))).optional()?)
    } else {
        Ok(None)
    }
}
