//! Content-bound, shell-safe citations shared by every evidence surface.
use anyhow::{bail, Context, Result};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

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
/// A tool row's event selector: its session, time and text hashed. Parallel calls can
/// share all three within a turn; a row that does with a visible row of a smaller call
/// id hashes its own call id too ([`repeat_event_id`]), so that each selector names one
/// row and the first of them keeps the selector handles named before.
pub fn event_id(session: &str, ts: i64, text: &str) -> String {
    selector(&[
        session.as_bytes(),
        ts.to_string().as_bytes(),
        text.as_bytes(),
    ])
}
/// The selector of a tool row repeating the time and text of a smaller call id's row.
pub fn repeat_event_id(session: &str, ts: i64, text: &str, call: &str) -> String {
    selector(&[
        session.as_bytes(),
        ts.to_string().as_bytes(),
        text.as_bytes(),
        call.as_bytes(),
    ])
}
fn selector(fields: &[&[u8]]) -> String {
    let mut state = blake2b_simd::Params::new()
        .hash_length(12)
        .personal(b"agrep-event-v1")
        .to_state();
    for v in fields {
        state.update(&(v.len() as u64).to_le_bytes());
        state.update(v);
    }
    state.finalize().to_hex().to_string()
}
/// The call ids of the visible tool rows `ids` that repeat the time and text of a visible
/// row with a smaller call id in their turn, by tool id: the rows whose selector is their
/// [`repeat_event_id`], as [`EventIds`] gives it. One query reads every row.
pub fn repeated_calls(c: &Connection, ids: &[i64]) -> Result<HashMap<i64, String>> {
    if ids.is_empty() {
        return Ok(HashMap::new());
    }
    // Equal blobs are equal texts: tool text is stored once per digest. A common text's
    // blob is shared by thousands of rows, so the rows are found by turn and time.
    let found = c
        .prepare_cached(
            "SELECT e.tool_id,e.call_id FROM json_each(?1) j JOIN event_rows e INDEXED BY event_tool_id ON e.tool_id=j.value AND e.active=1 JOIN event_sessions s ON s.id=e.session_key WHERE EXISTS(SELECT 1 FROM event_sessions k JOIN event_rows o INDEXED BY event_position ON o.session_key=k.id AND o.position=e.position AND o.ts=e.ts WHERE k.session=s.session AND o.id<>e.id AND o.blob=e.blob AND o.call_id<e.call_id AND o.active=1 AND o.visible=1)",
        )?
        .query_map([serde_json::to_string(ids)?], |r| Ok((r.get(0)?, r.get(1)?)))?
        .collect::<rusqlite::Result<_>>()?;
    Ok(found)
}
/// Gives the event selectors of one session's tool rows, read in turn, time and call id
/// order.
pub struct EventIds<'a> {
    session: &'a str,
    /// The turn and time of the rows read last, and their texts and call ids.
    run: Option<(i64, i64)>,
    seen: Vec<(String, String)>,
}
impl<'a> EventIds<'a> {
    pub fn new(session: &'a str) -> Self {
        Self {
            session,
            run: None,
            seen: Vec::new(),
        }
    }
    /// The selector of the next row; `text` is what the selector hashes.
    pub fn next_id(&mut self, turn: i64, ts: i64, call: &str, text: &str) -> String {
        if self.run != Some((turn, ts)) {
            self.run = Some((turn, ts));
            self.seen.clear();
        }
        if self.seen.iter().any(|(t, c)| t == text && c != call) {
            return repeat_event_id(self.session, ts, text, call);
        }
        self.seen.push((text.to_owned(), call.to_owned()));
        event_id(self.session, ts, text)
    }
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

    /// A row's selector is the same whether search reads the row alone or on a page with
    /// other rows, or a window reads its turn: only rows repeating the time and text of a
    /// smaller visible call id in their turn hash their call id, whichever agent read the
    /// session.
    #[test]
    fn a_selector_is_the_same_read_alone_or_with_its_turn() {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        c.execute_batch(
            "INSERT INTO event_sessions VALUES(1,'pi','s'),(2,'codex','s'),(3,'pi','t')",
        )
        .unwrap();
        let texts = [(10, "same"), (11, "other")];
        // (tool id, session key, call id, time, turn, blob, visible)
        let rows = [
            (1, 1, "c", 5, 0, 10, true),
            (2, 1, "a", 5, 0, 10, true),
            (3, 1, "b", 5, 0, 10, false),
            (4, 2, "d", 5, 0, 10, true),
            (5, 1, "e", 6, 0, 10, true),
            (6, 1, "f", 5, 1, 10, true),
            (7, 1, "g", 5, 0, 11, true),
            (8, 3, "h", 5, 0, 10, true),
        ];
        for (id, key, call, ts, turn, blob, visible) in rows {
            c.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key,blob,visible) VALUES(?1,?1,1,?2,?3,0,0,?4,?5,?5,0,0,0,'','','',0,?6,?7)", rusqlite::params![id, key, call, ts, turn, blob, visible]).unwrap();
        }
        let text = |blob| texts.iter().find(|(b, _)| *b == blob).unwrap().1;
        // The selector search gives row `id` when `page` holds the page's repeated calls.
        let searched = |page: &HashMap<i64, String>, id, session, ts, text| match page.get(&id) {
            Some(call) => repeat_event_id(session, ts, text, call),
            None => event_id(session, ts, text),
        };
        let visible: Vec<i64> = rows.iter().filter(|r| r.6).map(|r| r.0).collect();
        let page = repeated_calls(&c, &visible).unwrap();
        let mut window: Vec<_> = rows.iter().filter(|r| r.6 && r.1 != 3).collect();
        window.sort_by_key(|r| (r.4, r.3, r.2));
        let mut ids = EventIds::new("s");
        let mut repeats = Vec::new();
        for &&(id, _, call, ts, turn, blob, _) in &window {
            let in_turn = ids.next_id(turn, ts, call, text(blob));
            let alone = repeated_calls(&c, &[id]).unwrap();
            assert_eq!(
                searched(&alone, id, "s", ts, text(blob)),
                in_turn,
                "row {id}"
            );
            assert_eq!(
                searched(&page, id, "s", ts, text(blob)),
                in_turn,
                "row {id}"
            );
            if in_turn != event_id("s", ts, text(blob)) {
                assert_eq!(in_turn, repeat_event_id("s", ts, text(blob), call));
                repeats.push(id);
            }
        }
        repeats.sort();
        assert_eq!(repeats, [1, 4]);
        let alone = repeated_calls(&c, &[8]).unwrap();
        for page in [&alone, &page] {
            assert_eq!(searched(page, 8, "t", 5, "same"), event_id("t", 5, "same"));
        }
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
        crate::store::schema(&c).unwrap();
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
        c.execute(
            "INSERT INTO sources VALUES('/logs/a0b1c2.jsonl','pi',0,0,x'',NULL)",
            [],
        )
        .unwrap();
        c.execute("INSERT INTO event_sessions VALUES(1,'pi',?1)", [&all[3]])
            .unwrap();
        c.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key) VALUES(1,1,1,1,'call',0,0,0,0,0,0,0,0,'','','',0)", [])
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
