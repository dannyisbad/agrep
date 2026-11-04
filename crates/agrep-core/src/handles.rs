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
/// Distinct indexed session ids in byte order. In that order, an id's longest
/// common prefix with any other id is the one it shares with a neighbor, and
/// every id with a given prefix is contiguous.
#[derive(Default, Clone)]
pub struct SessionIndex(Vec<String>);
impl std::fmt::Debug for SessionIndex {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_tuple("SessionIndex").field(&self.0.len()).finish()
    }
}
impl From<Vec<String>> for SessionIndex {
    fn from(mut ids: Vec<String>) -> Self {
        ids.sort_unstable();
        ids.dedup();
        Self(ids)
    }
}
impl SessionIndex {
    pub fn load(c: &Connection) -> Result<Self> {
        // Sorting and deduplicating here spares SQLite a temporary DISTINCT index.
        Ok(c.prepare_cached("SELECT id FROM sessions")?
            .query_map([], |r| r.get(0))?
            .collect::<rusqlite::Result<Vec<String>>>()?
            .into())
    }
    /// The shortest prefix, at least 8 bytes, that no other indexed id shares.
    pub fn target(&self, session: &str) -> String {
        if !plain(session) {
            return format!("~{}", encode(session.as_bytes()));
        }
        let at = self.0.partition_point(|s| s.as_str() < session);
        let next = &self.0[at..];
        let next = next
            .first()
            .filter(|s| s.as_str() != session)
            .or(next.get(1));
        let needed = self.0[..at]
            .last()
            .into_iter()
            .chain(next)
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
    pub fn mint(&self, session: &str, turn: i64, text: &str) -> String {
        format!(
            "@{}:{turn}.{}",
            self.target(session),
            crate::query::content_digest(text)
        )
    }
    /// Session ids a reference names, resolved as history::resolve_indexed
    /// does. Its first steps, an exact id and then every id it prefixes, need
    /// only this index; other references read every session.
    pub fn candidates(&self, c: &Connection, value: &str) -> Result<Vec<String>> {
        let id = crate::history::identity(value)?;
        let at = self.0.partition_point(|s| s.as_str() < id.as_str());
        if self.0.get(at) == Some(&id) {
            return Ok(vec![id]);
        }
        let prefixed: Vec<_> = self.0[at..]
            .iter()
            .take_while(|s| s.starts_with(id.as_str()))
            .cloned()
            .collect();
        if !prefixed.is_empty() {
            return Ok(prefixed);
        }
        let all = crate::history::sessions(c)?;
        let (found, _) = crate::history::resolve_indexed(c, &all, value)?;
        let mut ids: Vec<_> = found.iter().map(|s| s.id.clone()).collect();
        ids.sort();
        ids.dedup();
        Ok(ids)
    }
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
    static HANDLE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(
            r"^@?(~[A-Za-z0-9_-]{2,1024}|[A-Za-z0-9][A-Za-z0-9._-]{2,127}):([0-9]{1,19})\.([0-9a-f]{4})(?:~([0-9a-f]{24}):([0-9]{1,7})-([0-9]{1,7}))?$",
        )
        .expect("handle pattern")
    });
    let c = HANDLE
        .captures(value.trim())
        .context("invalid result handle")?;
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
    if value.trim().starts_with('@') && value.contains(':') {
        return Ok(parse(value)?.session);
    }
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
        let index = SessionIndex::from(vec!["11111111-a".into(), "11111111-b".into()]);
        assert_eq!(index.target("11111111-a"), "11111111-a");
        let value = SessionIndex::default().mint("odd:🎯 id", 7, "café");
        let claim = parse(&value).unwrap();
        assert_eq!(claim.session, "odd:🎯 id");
        assert_eq!(claim.turn, 7);
        assert_eq!(claim.digest, Some(crate::query::content_digest("café")));
        assert!(parse("@abc:2.1234~0123456789abcdef01234567:4-3").is_err());
        assert!(!is_handle("abc:2"));
        assert!(!is_handle("@abc:2"));
        assert!(parse("@abc:2").is_err());
    }

    /// Ids over a small alphabet share long prefixes; agents repeat some ids.
    fn ids(seed: u64, n: usize) -> Vec<String> {
        let mut state = seed;
        let mut next = move |n: u64| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) % n
        };
        let mut ids = Vec::new();
        for _ in 0..n {
            let length = 3 + next(14) as usize;
            let mut id: String = (0..length)
                .map(|_| ['a', 'b', '0', '-', '.', '_'][next(6) as usize])
                .collect();
            id.replace_range(..1, "a");
            if next(9) == 0 {
                id.push('é');
            }
            ids.push(id);
        }
        ids
    }

    /// The shortest unique target as found before the index was sorted: a
    /// common prefix with every other id.
    fn scanned_target(session: &str, index: &[String]) -> String {
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

    #[test]
    fn sorted_neighbors_give_the_shortest_unique_target() {
        for seed in 0..64 {
            let all = ids(seed, 1 + seed as usize * 3);
            let index = SessionIndex::from(all.clone());
            for session in all.iter().chain(&ids(seed + 1000, 40)) {
                assert_eq!(
                    index.target(session),
                    scanned_target(session, &all),
                    "{session}"
                );
            }
        }
    }

    #[test]
    fn indexed_candidates_resolve_like_every_session() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(
            "CREATE TABLE sessions(agent TEXT NOT NULL,id TEXT NOT NULL,project TEXT NOT NULL,cwd TEXT NOT NULL DEFAULT '',parent TEXT NOT NULL,family TEXT NOT NULL,side INTEGER NOT NULL,title TEXT NOT NULL,first_ts INTEGER NOT NULL,last_ts INTEGER NOT NULL,PRIMARY KEY(agent,id)) WITHOUT ROWID;
             CREATE TABLE sources(path TEXT NOT NULL,agent TEXT NOT NULL);
             CREATE TABLE reply_shapes(source INTEGER NOT NULL,session TEXT NOT NULL);
             CREATE TABLE events(source INTEGER NOT NULL,session TEXT NOT NULL);",
        )
        .unwrap();
        let all = ids(7, 60);
        for (i, id) in all.iter().enumerate() {
            for agent in ["pi", "codex"].iter().take(1 + i % 2) {
                c.execute(
                    "INSERT OR IGNORE INTO sessions VALUES(?1,?2,?3,'','','',0,?4,?5,?5)",
                    rusqlite::params![
                        agent,
                        id,
                        format!("/work/p{}", i % 4),
                        format!("title {i}"),
                        i as i64
                    ],
                )
                .unwrap();
            }
        }
        c.execute("INSERT INTO sources VALUES('/logs/a0b1c2.jsonl','pi')", [])
            .unwrap();
        c.execute("INSERT INTO events VALUES(1,?1)", [&all[3]])
            .unwrap();
        let index = SessionIndex::load(&c).unwrap();
        let scanned = |value: &str| -> Result<Vec<String>, String> {
            let every = crate::history::sessions(&c).map_err(|e| e.to_string())?;
            let (found, _) =
                crate::history::resolve_indexed(&c, &every, value).map_err(|e| e.to_string())?;
            let mut ids: Vec<_> = found.iter().map(|s| s.id.clone()).collect();
            ids.sort();
            ids.dedup();
            Ok(ids)
        };
        let mut references = vec![
            "zz-missing".to_owned(),
            "p2".into(),
            "title 9".into(),
            "/logs/a0b1c2.jsonl".into(),
            "@~YWJj:3.0a0b".into(),
            String::new(),
        ];
        for id in &all {
            references.push(id.clone());
            references.push(format!("@{id}"));
            references.push(format!("~{}", encode(id.as_bytes())));
            references.push(index.mint(id, 2, "text"));
            for end in (1..id.len()).filter(|end| id.is_char_boundary(*end)) {
                references.push(id[..end].to_owned());
            }
        }
        for value in references {
            assert_eq!(
                index.candidates(&c, &value).map_err(|e| e.to_string()),
                scanned(&value),
                "{value:?}"
            );
        }
    }
}
