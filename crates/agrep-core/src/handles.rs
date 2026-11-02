//! Content-bound, shell-safe citations shared by every evidence surface.
use anyhow::{bail, Context, Result};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Claim {
    pub session: String,
    pub turn: i64,
    pub digest: Option<String>,
    pub event: Option<String>,
    pub span: Option<(usize, usize)>,
}
const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
fn encode(bytes: &[u8]) -> String {
    let (mut bits, mut n) = (0u32, 0);
    let mut out = String::new();
    for b in bytes {
        bits = (bits << 8) | u32::from(*b);
        n += 8;
        while n >= 6 {
            n -= 6;
            out.push(ALPHABET[((bits >> n) & 63) as usize] as char);
        }
    }
    if n > 0 {
        out.push(ALPHABET[((bits << (6 - n)) & 63) as usize] as char);
    }
    out
}
fn decode(s: &str) -> Result<String> {
    let (mut bits, mut n) = (0u32, 0);
    let mut out = Vec::new();
    for b in s.bytes() {
        let v = ALPHABET
            .iter()
            .position(|x| *x == b)
            .context("invalid result handle")?;
        bits = (bits << 6) | v as u32;
        n += 6;
        if n >= 8 {
            n -= 8;
            out.push((bits >> n) as u8);
        }
    }
    let value = String::from_utf8(out).context("invalid result handle")?;
    if value.is_empty() || encode(value.as_bytes()) != s {
        bail!("invalid result handle")
    }
    Ok(value)
}
pub fn plain(s: &str) -> bool {
    (3..=128).contains(&s.len())
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}
pub fn target(session: &str, index: &[String]) -> String {
    if !plain(session) {
        return format!("~{}", encode(session.as_bytes()));
    }
    let needed = index
        .iter()
        .filter(|s| s.as_str() != session)
        .map(|s| {
            s.bytes()
                .zip(session.bytes())
                .take_while(|(a, b)| a == b)
                .count()
                + 1
        })
        .max()
        .unwrap_or(0)
        .max(8)
        .min(session.len());
    session[..needed].to_owned()
}
pub fn mint(session: &str, turn: i64, text: &str, index: &[String]) -> String {
    format!(
        "@{}:{turn}.{}",
        target(session, index),
        crate::query::content_digest(text)
    )
}
pub fn full_target(session: &str) -> String {
    if plain(session) {
        session.to_owned()
    } else {
        format!("~{}", encode(session.as_bytes()))
    }
}
pub fn mint_full(session: &str, turn: i64, text: &str) -> String {
    format!(
        "@{}:{turn}.{}",
        full_target(session),
        crate::query::content_digest(text)
    )
}
pub fn parse(value: &str) -> Result<Claim> {
    let re = regex::Regex::new(
        r"^@?(~[A-Za-z0-9_-]{2,1024}|[A-Za-z0-9][A-Za-z0-9._-]{2,127}):([0-9]{1,19})(?:\.([0-9a-f]{4})(?:~([0-9a-f]{24}):([0-9]{1,7})-([0-9]{1,7}))?)?$",
    )?;
    let c = re.captures(value.trim()).context("invalid result handle")?;
    let session = if let Some(token) = c[1].strip_prefix('~') {
        decode(token)?
    } else {
        c[1].into()
    };
    let span = if let (Some(a), Some(b)) = (c.get(5), c.get(6)) {
        let a: usize = a.as_str().parse()?;
        let b: usize = b.as_str().parse()?;
        if a >= b || b > 4 * 1024 * 1024 {
            bail!("invalid result handle")
        }
        Some((a, b))
    } else {
        None
    };
    Ok(Claim {
        session,
        turn: c[2].parse().context("invalid result handle")?,
        digest: c.get(3).map(|v| v.as_str().into()),
        event: c.get(4).map(|v| v.as_str().into()),
        span,
    })
}
pub fn is_handle(value: &str) -> bool {
    parse(value).is_ok_and(|c| value.starts_with('@') || c.digest.is_some())
}
pub fn normalize(value: &str) -> Result<String> {
    let value = value.trim().trim_start_matches('@');
    if value.len() > 4096 || value.is_empty() {
        bail!("invalid session id")
    }
    if let Some(s) = value.strip_prefix('~') {
        decode(s)
    } else {
        Ok(value.into())
    }
}
pub fn sessions(c: &Connection) -> Result<Vec<String>> {
    Ok(c.prepare("SELECT DISTINCT id FROM sessions ORDER BY id")?
        .query_map([], |r| r.get(0))?
        .collect::<rusqlite::Result<_>>()?)
}
pub fn candidates(c: &Connection, value: &str) -> Result<Vec<String>> {
    let all = crate::history::sessions(c)?;
    let (found, _) = crate::history::resolve_indexed(c, &all, value)?;
    let mut ids: Vec<_> = found.iter().map(|s| s.id.clone()).collect();
    ids.sort();
    ids.dedup();
    Ok(ids)
}
pub fn event_id(session: &str, ts: i64, text: &str) -> String {
    let mut state = blake2b_simd::Params::new()
        .hash_length(12)
        .personal(b"agrep-event-v1")
        .to_state();
    for v in [
        session.as_bytes(),
        ts.to_string().as_bytes(),
        text.as_bytes(),
    ] {
        state.update(&(v.len() as u64).to_le_bytes());
        state.update(v);
    }
    state.finalize().to_hex().to_string()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip_and_widening() {
        let index = vec!["11111111-a".into(), "11111111-b".into()];
        assert_eq!(target("11111111-a", &index), "11111111-a");
        let value = mint("odd:🎯 id", 7, "café", &[]);
        let claim = parse(&value).unwrap();
        assert_eq!(claim.session, "odd:🎯 id");
        assert_eq!(claim.turn, 7);
        assert_eq!(claim.digest, Some(crate::query::content_digest("café")));
        assert!(parse("@abc:2.1234~0123456789abcdef01234567:4-3").is_err());
        assert!(!is_handle("abc:2"));
        assert!(is_handle("@abc:2"));
    }
}
