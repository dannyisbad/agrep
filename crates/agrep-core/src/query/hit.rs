use super::*;
use crate::handles;

/// The reference codec keeps arbitrary upstream IDs out of shell and handle delimiters.
pub(super) fn session_target(session: &str) -> Result<String> {
    let bytes = session.as_bytes();
    if (3..=128).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(b))
    {
        return Ok(format!("@{session}"));
    }
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut token = String::new();
    let mut bits = 0u32;
    let mut available = 0;
    for byte in bytes {
        bits = (bits << 8) | u32::from(*byte);
        available += 8;
        while available >= 6 {
            available -= 6;
            token.push(ALPHABET[((bits >> available) & 63) as usize] as char);
        }
    }
    if available != 0 {
        token.push(ALPHABET[((bits << (6 - available)) & 63) as usize] as char);
    }
    if token.is_empty() || token.len() > 1024 {
        bail!("session id cannot be encoded safely")
    }
    Ok(format!("@~{token}"))
}

pub fn content_digest(s: &str) -> String {
    let mut h = 0x2325u16;
    for b in s.bytes() {
        h = (h ^ u16::from(b)).wrapping_mul(0x01b3);
    }
    format!("{h:04x}")
}
/// A search row's handle. A tool row's also names its event, and the characters of its
/// match or else of its whole text.
pub(super) fn row_handle(
    session: &str,
    turn: i64,
    text: &str,
    span: Option<(usize, usize)>,
    event: Option<&str>,
) -> Result<String> {
    let target = session_target(session)?;
    let digest = content_digest(text);
    Ok(match event {
        None => format!("{target}:{turn}.{digest}"),
        Some(event) => {
            let (start, end) = span
                .filter(|(start, end)| end > start)
                .unwrap_or((0, text.chars().count()));
            format!("{target}:{turn}.{digest}~{event}:{start}-{end}")
        }
    })
}
/// Gives each tool row of a final page that repeats the time and text of a smaller call
/// id's row in its turn the selector naming it alone ([`handles::repeated_calls`]).
/// `checked` keeps what earlier pages found, so a page ranked again reads only its new
/// rows.
pub(super) fn name_repeated_calls(
    c: &Connection,
    hits: &mut [Hit],
    checked: &mut HashMap<i64, Option<String>>,
) -> Result<()> {
    let unchecked: Vec<i64> = hits
        .iter()
        .filter(|h| h.event.is_some() && !checked.contains_key(&h.rowid))
        .map(|h| h.rowid)
        .collect();
    let mut repeated = handles::repeated_calls(c, &unchecked)?;
    for id in unchecked {
        checked.entry(id).or_insert_with(|| repeated.remove(&id));
    }
    for h in hits.iter_mut().filter(|h| h.event.is_some()) {
        if let Some(Some(call)) = checked.get(&h.rowid) {
            let event = handles::repeat_event_id(&h.session, h.ts, &h.text, call);
            h.handle = row_handle(&h.session, h.turn, &h.text, h.span, Some(&event))?;
            h.event = Some(event);
        }
    }
    Ok(())
}
/// A meaning match's snippet. 0.4.9 served the matched row's text as it is stored
/// (py/segment_query.py:650, py/search.py:1012), leaving control characters for the
/// sanitizer to quote.
pub fn snippet(text: &str, query: &str, max: usize) -> String {
    let chars: Vec<_> = text.chars().collect();
    let needle = boundary_rank::query_tokens(query)
        .first()
        .copied()
        .unwrap_or(query)
        .to_lowercase();
    let low = crate::unicode_v16::lowercase(text);
    let at = low
        .find(&needle)
        .map(|p| low[..p].chars().count())
        .unwrap_or(0);
    let start = at.saturating_sub(60).min(chars.len());
    let end = (start + max).min(chars.len());
    format!(
        "{}{}{}",
        if start > 0 { "…" } else { "" },
        chars[start..end].iter().collect::<String>(),
        if end < chars.len() { "…" } else { "" }
    )
}
