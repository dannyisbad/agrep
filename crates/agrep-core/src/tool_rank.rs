//! Occurrence ceilings for ASCII tool terms in folded text.
use anyhow::{bail, Result};
use rusqlite::{ffi, Connection};
use std::sync::{
    atomic::{AtomicI64, AtomicU64, AtomicU8, AtomicUsize, Ordering},
    Arc,
};
use std::{ffi::c_void, ptr};

struct FrontierStats {
    total: AtomicUsize,
    min_unit: AtomicU64,
    max_upper: AtomicU64,
    first: AtomicI64,
    units: [AtomicU64; crate::store::BLOCK as usize],
    counts: [AtomicU8; crate::store::BLOCK as usize],
}
impl Default for FrontierStats {
    fn default() -> Self {
        Self {
            total: AtomicUsize::new(0),
            min_unit: AtomicU64::new(0),
            max_upper: AtomicU64::new(0),
            first: AtomicI64::new(0),
            units: std::array::from_fn(|_| AtomicU64::new(0)),
            counts: std::array::from_fn(|_| AtomicU8::new(0)),
        }
    }
}
pub(super) struct Frontier(Arc<FrontierStats>);
pub(super) struct Prefetched {
    pub ids: Vec<i64>,
    pub counts: Vec<u32>,
    pub total: usize,
    pub min_unit: f64,
    pub max_upper: f64,
}
impl Prefetched {
    pub fn complete(&self, last: f64, bound: f64) -> bool {
        last.max(self.max_upper) < self.min_unit.min(super::ranked(bound))
    }
}
unsafe extern "C" fn drop_frontier(value: *mut c_void) {
    drop(Box::from_raw(value.cast::<Arc<FrontierStats>>()));
}
unsafe extern "C" fn competitive(
    api: *const ffi::Fts5ExtensionApi,
    fts: *mut ffi::Fts5Context,
    context: *mut ffi::sqlite3_context,
    argc: i32,
    argv: *mut *mut ffi::sqlite3_value,
) {
    let result = || -> std::result::Result<i64, i32> {
        if argc != 6 {
            return Err(ffi::SQLITE_MISUSE);
        }
        let Some(user) = (*api).xUserData else {
            return Err(ffi::SQLITE_ERROR);
        };
        let stats = &*user(fts).cast::<Arc<FrontierStats>>();
        let id = ffi::sqlite3_value_int64(*argv.add(1));
        let at = id
            .checked_sub(stats.first.load(Ordering::Relaxed))
            .and_then(|at| usize::try_from(at).ok())
            .ok_or(ffi::SQLITE_CORRUPT)?;
        let encoded = stats
            .units
            .get(at)
            .ok_or(ffi::SQLITE_CORRUPT)?
            .load(Ordering::Relaxed);
        if encoded == 0 {
            return Ok(0);
        }
        let unit = f64::from_bits(encoded - 1);
        let n = window_value(api, fts, argv)?;
        stats.counts[at].store(n as u8, Ordering::Relaxed);
        let upper = super::ranked((1. - 2f64.powi(-(n as i32))) * unit);
        stats.total.fetch_add(1, Ordering::Relaxed);
        stats
            .min_unit
            .fetch_min(super::ranked(unit).to_bits(), Ordering::Relaxed);
        stats
            .max_upper
            .fetch_max(upper.to_bits(), Ordering::Relaxed);
        Ok(i64::from(upper >= ffi::sqlite3_value_double(*argv.add(4))))
    };
    match result() {
        Ok(n) => ffi::sqlite3_result_int64(context, n),
        Err(rc) => ffi::sqlite3_result_error_code(context, rc),
    }
}
pub(super) fn frontier(c: &Connection) -> Result<Frontier> {
    let name = c"agrep.query.tool_frontier";
    unsafe {
        let value =
            ffi::sqlite3_get_clientdata(c.handle(), name.as_ptr()).cast::<Arc<FrontierStats>>();
        if let Some(value) = value.as_ref() {
            return Ok(Frontier(value.clone()));
        }
    }
    let stats = Arc::new(FrontierStats::default());
    let api = crate::fts_span::api(c)?;
    unsafe {
        let Some(create) = (*api).xCreateFunction else {
            bail!("FTS5 function registration is unavailable")
        };
        let value = Box::into_raw(Box::new(stats.clone()));
        let rc = create(
            api,
            c"agrep_tool_competitive".as_ptr(),
            value.cast(),
            Some(competitive),
            Some(drop_frontier),
        );
        if rc != ffi::SQLITE_OK {
            drop_frontier(value.cast());
            bail!("cannot register tool frontier: {rc}");
        }
        // FTS auxiliary registrations live until connection close. Reuse their
        // block buffer across searches on an interactive reader connection.
        let rc = ffi::sqlite3_set_clientdata(
            c.handle(),
            name.as_ptr(),
            Box::into_raw(Box::new(stats.clone())).cast(),
            Some(drop_frontier),
        );
        if rc != ffi::SQLITE_OK {
            bail!("cannot retain tool frontier: {rc}");
        }
    }
    Ok(Frontier(stats))
}
pub(super) fn competitive_rows(
    c: &Connection,
    frontier: &Frontier,
    expression: &str,
    plan: &[u8],
    range: std::ops::Range<i64>,
    page: (i64, f64, bool),
) -> Result<Option<Prefetched>> {
    let (now, cutoff, ascii) = page;
    frontier.0.total.store(0, Ordering::Relaxed);
    frontier
        .0
        .min_unit
        .store(f64::INFINITY.to_bits(), Ordering::Relaxed);
    frontier.0.max_upper.store(0, Ordering::Relaxed);
    frontier.0.first.store(range.start, Ordering::Relaxed);
    for unit in &frontier.0.units {
        unit.store(0, Ordering::Relaxed);
    }
    // One contiguous covering-index read replaces a metadata seek per posting.
    // Zero entries withhold invisible or inactive rows from the FTS callback.
    let mut metadata=c.prepare_cached("SELECT tool_id,ts FROM event_rows INDEXED BY event_tool_id WHERE active=1 AND blob IS NOT NULL AND visible=1 AND tool_id>=?1 AND tool_id<?2")?;
    let mut rows = metadata.query(rusqlite::params![range.start, range.end])?;
    while let Some(row) = rows.next()? {
        let (id, ts) = (row.get::<_, i64>(0)?, row.get::<_, i64>(1)?);
        if now.checked_sub(ts).is_none() {
            bail!("invalid tool timestamp");
        }
        let at = usize::try_from(id - range.start)?;
        let unit = frontier
            .0
            .units
            .get(at)
            .ok_or_else(|| anyhow::anyhow!("tool frontier exceeds one block"))?;
        unit.store(
            (super::recency(ts, now) * super::weight("tool")).to_bits() + 1,
            Ordering::Relaxed,
        );
    }
    let mut query=c.prepare("SELECT rowid FROM tools_fts WHERE tools_fts MATCH ?1 AND rowid>=?2 AND rowid<?3 AND agrep_tool_competitive(tools_fts,?4,rowid,0,0,?5,?6)>0")?;
    let mut rows = query.query(rusqlite::params![
        expression,
        range.start,
        range.end,
        plan,
        cutoff,
        ascii
    ])?;
    let mut ids = Vec::new();
    while let Some(row) = rows.next()? {
        ids.push(row.get(0)?);
        // Abandon an unselective planner buffer; the complete cursor still runs.
        if ids.len() > 128 {
            return Ok(None);
        }
    }
    ids.sort_unstable();
    let counts = ids
        .iter()
        .map(|&id| {
            u32::from(frontier.0.counts[(id - range.start) as usize].load(Ordering::Relaxed))
        })
        .collect();
    Ok(Some(Prefetched {
        ids,
        counts,
        total: frontier.0.total.load(Ordering::Relaxed),
        min_unit: f64::from_bits(frontier.0.min_unit.load(Ordering::Relaxed)),
        max_upper: f64::from_bits(frontier.0.max_upper.load(Ordering::Relaxed)),
    }))
}

pub(super) fn word_rows(
    c: &Connection,
    words: &[&str],
    term: &str,
    first: i64,
    last: i64,
) -> Result<std::collections::HashMap<i64, u32>> {
    let mut out = std::collections::HashMap::<i64, u32>::new();
    let mut query=c.prepare_cached("SELECT rowid,agrep_tool_occurrences(tools_fts,?4) FROM tools_fts WHERE tools_fts MATCH ?1 AND rowid>=?2 AND rowid<?3")?;
    for word in words {
        let weight = (word.matches(term).count() as u32).to_le_bytes();
        let expression = format!("\"{}\"", word.replace('"', "\"\""));
        for row in query.query_map(
            rusqlite::params![expression, first, last, weight.as_slice()],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, u32>(1)?)),
        )? {
            let (id, count) = row?;
            let total = out.entry(id).or_default();
            *total = total.saturating_add(count).min(60);
        }
    }
    Ok(out)
}

pub(super) fn window_plan(words: &[&str], term: &str) -> Vec<u8> {
    let mut out = Vec::new();
    crate::store::put_varint(term.len() as u32, &mut out);
    crate::store::put_varint(words.len() as u32, &mut out);
    for word in words {
        crate::store::put_varint(word.len() as u32, &mut out);
        let (mut offsets, mut from) = (Vec::new(), 0);
        while let Some(at) = word[from..].find(term) {
            let at = from + at;
            offsets.push(at as u32);
            from = at + 1;
        }
        crate::store::put_varint(offsets.len() as u32, &mut out);
        for offset in offsets {
            crate::store::put_varint(offset, &mut out);
        }
    }
    out
}
struct WindowPlan {
    offsets: Vec<Vec<u32>>,
    unit_weights: bool,
    lengths: Vec<u32>,
    term_len: usize,
}
unsafe extern "C" fn destroy_window(value: *mut c_void) {
    drop(Box::from_raw(value.cast::<WindowPlan>()));
}
unsafe fn window_value(
    api: *const ffi::Fts5ExtensionApi,
    fts: *mut ffi::Fts5Context,
    argv: *mut *mut ffi::sqlite3_value,
) -> std::result::Result<i64, i32> {
    let api = &*api;
    let (Some(get), Some(set), Some(count), Some(instance)) =
        (api.xGetAuxdata, api.xSetAuxdata, api.xInstCount, api.xInst)
    else {
        return Err(ffi::SQLITE_ERROR);
    };
    let mut plan = get(fts, 0).cast::<WindowPlan>();
    if plan.is_null() {
        let size = ffi::sqlite3_value_bytes(*argv);
        let data = ffi::sqlite3_value_blob(*argv).cast::<u8>();
        if size <= 0 || data.is_null() {
            return Err(ffi::SQLITE_MISMATCH);
        }
        let data = std::slice::from_raw_parts(data, size as usize);
        let mut at = 0;
        let term_len =
            crate::store::take_varint(data, &mut at).map_err(|_| ffi::SQLITE_MISMATCH)? as usize;
        let words = crate::store::take_varint(data, &mut at).map_err(|_| ffi::SQLITE_MISMATCH)?;
        let mut offsets = Vec::new();
        let mut lengths = Vec::new();
        for _ in 0..words {
            lengths
                .push(crate::store::take_varint(data, &mut at).map_err(|_| ffi::SQLITE_MISMATCH)?);
            let n = crate::store::take_varint(data, &mut at).map_err(|_| ffi::SQLITE_MISMATCH)?;
            if n == 0 {
                return Err(ffi::SQLITE_MISMATCH);
            }
            let mut positions = Vec::new();
            for _ in 0..n {
                positions.push(
                    crate::store::take_varint(data, &mut at).map_err(|_| ffi::SQLITE_MISMATCH)?,
                );
            }
            offsets.push(positions);
        }
        if at != data.len() {
            return Err(ffi::SQLITE_MISMATCH);
        }
        let unit_weights = offsets.iter().all(|within| within.len() == 1);
        plan = Box::into_raw(Box::new(WindowPlan {
            offsets,
            unit_weights,
            lengths,
            term_len,
        }));
        let rc = set(fts, plan.cast(), Some(destroy_window));
        if rc != ffi::SQLITE_OK {
            return Err(rc);
        }
    }
    let plan = &*plan;
    let mut n = 0;
    let rc = count(fts, &mut n);
    if rc != ffi::SQLITE_OK {
        return Err(rc);
    }
    if n > 8192 {
        return Ok(60);
    }
    let mut total = if plan.unit_weights { n as usize } else { 0 };
    for i in 0..if plan.unit_weights { 0 } else { n } {
        let (mut phrase, mut column, mut offset) = (0, 0, 0);
        let rc = instance(fts, i, &mut phrase, &mut column, &mut offset);
        if rc != ffi::SQLITE_OK {
            return Err(rc);
        }
        let Some(within) = plan.offsets.get(phrase as usize) else {
            return Err(ffi::SQLITE_MISMATCH);
        };
        total = total.saturating_add(within.len());
    }
    let global = total.min(60) as i64;
    let cutoff = ffi::sqlite3_value_double(*argv.add(4));
    let repetition = 1. - 2f64.powi(-(global as i32));
    if cutoff <= 0. || total > 8192 || super::ranked(repetition * super::weight("tool")) < cutoff {
        return Ok(global);
    }
    if ffi::sqlite3_value_int64(*argv.add(3))
        .checked_sub(ffi::sqlite3_value_int64(*argv.add(2)))
        .is_none()
    {
        return Err(ffi::SQLITE_CORRUPT);
    }
    let recency = super::recency(
        ffi::sqlite3_value_int64(*argv.add(2)),
        ffi::sqlite3_value_int64(*argv.add(3)),
    );
    let ceiling = super::ranked(repetition * recency * super::weight("tool"));
    if ceiling < cutoff {
        return Ok(global);
    }
    if ffi::sqlite3_value_int(*argv.add(5)) == 0 || plan.term_len > 4096 - 2 * crate::window::PAD {
        return Ok(global);
    }
    let mut tokens = Vec::with_capacity(n as usize);
    for i in 0..n {
        let (mut phrase, mut column, mut offset) = (0, 0, 0);
        let rc = instance(fts, i, &mut phrase, &mut column, &mut offset);
        if rc != ffi::SQLITE_OK {
            return Err(rc);
        }
        if offset < 0 || column != 0 || phrase < 0 {
            return Err(ffi::SQLITE_MISMATCH);
        }
        tokens.push((offset as u32, phrase as usize));
    }
    tokens.sort_unstable();
    let (mut prior, mut length, mut coordinate) = (None, 0u32, 0u64);
    let mut positions = Vec::with_capacity(total);
    for (token, phrase) in tokens {
        let Some(&word_len) = plan.lengths.get(phrase) else {
            return Err(ffi::SQLITE_MISMATCH);
        };
        if word_len == 0 {
            return Err(ffi::SQLITE_MISMATCH);
        }
        if let Some(previous) = prior {
            if token != previous {
                coordinate += 2 * u64::from(token - previous) + u64::from(length - 1);
                length = word_len;
            }
        } else {
            length = word_len;
        }
        prior = Some(token);
        for &offset in &plan.offsets[phrase] {
            positions.push(coordinate + u64::from(offset));
        }
    }
    // ASCII tokens need at least one character and a separator. Matched words
    // supply their exact widths; all other words retain that minimum. Synthetic
    // long-token chunks overlap by 4096 bytes, so a window this small still has
    // a complete, contiguous copy in the postings. Other text keeps the global bound.
    positions.sort_unstable();
    let (mut first, mut maximum) = (0, 0);
    for last in 0..positions.len() {
        while positions[last] - positions[first] > (2 * crate::window::PAD) as u64 {
            first += 1;
        }
        maximum = maximum.max(last - first + 1);
    }
    Ok(maximum.min(60) as i64)
}
unsafe extern "C" fn window_occurrences(
    api: *const ffi::Fts5ExtensionApi,
    fts: *mut ffi::Fts5Context,
    context: *mut ffi::sqlite3_context,
    argc: i32,
    argv: *mut *mut ffi::sqlite3_value,
) {
    let result = if argc == 6 {
        window_value(api, fts, argv)
    } else {
        Err(ffi::SQLITE_MISUSE)
    };
    match result {
        Ok(n) => ffi::sqlite3_result_int64(context, n),
        Err(rc) => ffi::sqlite3_result_error_code(context, rc),
    }
}

unsafe extern "C" fn occurrences(
    api: *const ffi::Fts5ExtensionApi,
    fts: *mut ffi::Fts5Context,
    context: *mut ffi::sqlite3_context,
    argc: i32,
    argv: *mut *mut ffi::sqlite3_value,
) {
    let result = || -> std::result::Result<i64, i32> {
        if argc != 1 {
            return Err(ffi::SQLITE_MISUSE);
        }
        let value = *argv;
        let size = ffi::sqlite3_value_bytes(value);
        let bytes = ffi::sqlite3_value_blob(value).cast::<u8>();
        if size <= 0 || size % 4 != 0 || bytes.is_null() {
            return Err(ffi::SQLITE_MISMATCH);
        }
        let weights = std::slice::from_raw_parts(bytes, size as usize);
        let api = &*api;
        let (Some(count), Some(instance)) = (api.xInstCount, api.xInst) else {
            return Err(ffi::SQLITE_ERROR);
        };
        let mut n = 0;
        let rc = count(fts, &mut n);
        if rc != ffi::SQLITE_OK {
            return Err(rc);
        }
        let mut total = 0i64;
        for i in 0..n {
            let (mut phrase, mut column, mut offset) = (0, 0, 0);
            let rc = instance(fts, i, &mut phrase, &mut column, &mut offset);
            if rc != ffi::SQLITE_OK {
                return Err(rc);
            }
            let at = phrase as usize * 4;
            let Some(weight) = weights.get(at..at + 4) else {
                return Err(ffi::SQLITE_MISMATCH);
            };
            total = (total + i64::from(u32::from_le_bytes(weight.try_into().unwrap()))).min(60);
        }
        Ok(total)
    };
    match result() {
        Ok(total) => ffi::sqlite3_result_int64(context, total),
        Err(rc) => ffi::sqlite3_result_error_code(context, rc),
    }
}

pub(crate) fn install(c: &Connection) -> Result<()> {
    let api = crate::fts_span::api(c)?;
    unsafe {
        let Some(create) = (*api).xCreateFunction else {
            bail!("FTS5 function registration is unavailable");
        };
        let rc = create(
            api,
            c"agrep_tool_occurrences".as_ptr(),
            ptr::null_mut::<c_void>(),
            Some(occurrences),
            None,
        );
        if rc != ffi::SQLITE_OK {
            bail!("cannot register tool occurrence ceiling: {rc}");
        }
        let rc = create(
            api,
            c"agrep_tool_window_occurrences".as_ptr(),
            ptr::null_mut::<c_void>(),
            Some(window_occurrences),
            None,
        );
        if rc != ffi::SQLITE_OK {
            bail!("cannot register tool window ceiling: {rc}");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn competitive_cursor_counts_losers_and_proves_unchanged_row_ceilings() {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        c.execute("INSERT INTO event_sessions VALUES(1,'pi','fixture')", [])
            .unwrap();
        for id in 1..=201i64 {
            let text = if id % 4 == 0 { "error error" } else { "error" };
            c.execute(
                "INSERT INTO tool_blobs(id,digest,ascii,body) VALUES(?1*3,CAST(?1 AS BLOB),1,?2)",
                rusqlite::params![id, crate::tool_store::encode(text).unwrap()],
            )
            .unwrap();
            c.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key,blob,visible) VALUES(?1,?1,1,1,?1,0,0,0,0,0,0,0,0,'','','',0,?1*3,?2)",rusqlite::params![id,id<=200]).unwrap();
            c.execute(
                "INSERT INTO tools_fts(rowid,text) VALUES(?1,?2)",
                rusqlite::params![id, text],
            )
            .unwrap();
        }
        let frontier = frontier(&c).unwrap();
        let page = competitive_rows(
            &c,
            &frontier,
            "\"error\"",
            &window_plan(&["error"], "error"),
            0..202,
            (0, 0.12, true),
        )
        .unwrap()
        .unwrap();
        assert_eq!(page.total, 200);
        assert_eq!(page.ids, (4..=200).step_by(4).collect::<Vec<_>>());
        assert_eq!(page.counts, vec![2; 50]);
        assert_eq!(page.min_unit, 0.22);
        assert_eq!(page.max_upper, 0.165);
        assert!(page.complete(0.12, 0.20));
        assert!(!page.complete(0.12, 0.165));
        let next = super::frontier(&c).unwrap();
        assert!(Arc::ptr_eq(&frontier.0, &next.0));
        let repeated = competitive_rows(
            &c,
            &next,
            "\"error\"",
            &window_plan(&["error"], "error"),
            0..202,
            (0, 0.12, true),
        )
        .unwrap()
        .unwrap();
        assert_eq!(repeated.total, page.total);
        assert_eq!(repeated.ids, page.ids);
    }
    #[test]
    fn separate_word_streams_preserve_or_ids_and_occurrence_counts() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE VIRTUAL TABLE tools_fts USING fts5(text,tokenize='unicode61 remove_diacritics 0'); CREATE VIRTUAL TABLE vocab USING fts5vocab(tools_fts,row);").unwrap();
        for id in 0..200i64 {
            c.execute(
                "INSERT INTO tools_fts(rowid,text) VALUES(?,?)",
                rusqlite::params![
                    id,
                    format!("error{} error{} errorerror", id % 32, (id + 1) % 32)
                ],
            )
            .unwrap();
        }
        install(&c).unwrap();
        let words = c
            .prepare("SELECT term FROM vocab ORDER BY term")
            .unwrap()
            .query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        let expression = words
            .iter()
            .map(|w| format!("\"{w}\""))
            .collect::<Vec<_>>()
            .join(" OR ");
        let weights = words
            .iter()
            .flat_map(|w| (w.matches("error").count() as u32).to_le_bytes())
            .collect::<Vec<_>>();
        let expected=c.prepare("SELECT rowid,agrep_tool_occurrences(tools_fts,?) FROM tools_fts WHERE tools_fts MATCH ? AND rowid>=17 AND rowid<183").unwrap().query_map(rusqlite::params![weights,expression],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,u32>(1)?))).unwrap().collect::<rusqlite::Result<std::collections::HashMap<_,_>>>().unwrap();
        let actual = word_rows(
            &c,
            &words.iter().map(String::as_str).collect::<Vec<_>>(),
            "error",
            17,
            183,
        )
        .unwrap();
        assert_eq!(actual.len(), 166);
        assert!(actual.values().all(|&n| n == 4));
        assert_eq!(actual, expected);
    }
    #[test]
    fn posting_windows_bound_every_canonical_snippet_without_decoding_text() {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        c.execute("INSERT INTO event_sessions VALUES(1,'pi','fixture')", [])
            .unwrap();
        let texts = [
            "error error error".to_owned(),
            format!("error {} error", "x ".repeat(100)),
            "ERROR_error's errorerror".into(),
            "é error error".into(),
            format!("{}error", "a".repeat(9000)),
            format!("{} aaaaaa", "aaa ".repeat(80)),
            format!("error {} error", "…".repeat(60)),
            format!("error {} error", "…".repeat(170)),
            format!("{}error error {}", "x".repeat(8188), "x".repeat(8190)),
            format!("{}errorerror{}", "x".repeat(8187), "x".repeat(40_000)),
            format!(
                "error {} errors {} xerrorx",
                "a ".repeat(25),
                "b ".repeat(25)
            ),
        ];
        for (i, text) in texts.iter().enumerate() {
            let id = i as i64 + 1;
            c.execute(
                "INSERT INTO tool_blobs(id,digest,ascii,body) VALUES(?1,CAST(?1 AS BLOB),?2,?3)",
                rusqlite::params![
                    id,
                    text.is_ascii(),
                    crate::tool_store::encode(text).unwrap()
                ],
            )
            .unwrap();
            c.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key,blob) VALUES(?1,?1,1,1,?1,0,0,0,0,0,0,0,0,'','','',0,?1)",[id]).unwrap();
            c.execute(
                "INSERT INTO tools_fts(rowid,text) VALUES(?1,?2)",
                rusqlite::params![
                    id,
                    crate::tool_index::index_text(&crate::store::search_fold(text)).as_ref()
                ],
            )
            .unwrap();
        }
        install(&c).unwrap();
        for term in ["error", "aaa"] {
            let words = c
                .prepare("SELECT term FROM tools_vocab WHERE instr(term,?)>0 ORDER BY term")
                .unwrap()
                .query_map([term], |r| r.get::<_, String>(0))
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap();
            let expression = words
                .iter()
                .map(|w| format!("\"{w}\""))
                .collect::<Vec<_>>()
                .join(" OR ");
            let plan = window_plan(&words.iter().map(String::as_str).collect::<Vec<_>>(), term);
            let rows=c.prepare_cached("SELECT f.rowid,agrep_tool_window_occurrences(tools_fts,?1,f.rowid,0,0,0.1,(SELECT ascii FROM tool_blobs b WHERE b.id=f.rowid)) FROM tools_fts f WHERE tools_fts MATCH ?2 ORDER BY f.rowid").unwrap().query_map(rusqlite::params![plan,expression],|r|Ok((r.get::<_,usize>(0)?,r.get::<_,usize>(1)?))).unwrap().collect::<rusqlite::Result<Vec<_>>>().unwrap();
            for (id, ceiling) in rows {
                let text = &texts[id - 1];
                let folded = crate::store::search_fold(text);
                for (at, _) in folded.match_indices(term) {
                    let span = at..at + term.len();
                    let snip = crate::window::Snip::new(text, std::slice::from_ref(&span));
                    let actual = crate::store::search_fold(&snip.text).matches(term).count();
                    assert!(
                        ceiling >= actual.min(60),
                        "{term} row {id}: {ceiling} < {actual}"
                    );
                }
                if term == "error" && id == 2 {
                    assert_eq!(ceiling, 1);
                }
            }
        }
    }

    #[test]
    fn counts_each_expanded_words_literal_occurrences() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE VIRTUAL TABLE f USING fts5(text,content='',tokenize='unicode61 remove_diacritics 0');
            INSERT INTO f(rowid,text) VALUES(1,'error ERROR errorerror errors');")
            .unwrap();
        install(&c).unwrap();
        let weights: Vec<u8> = [1u32, 2, 1]
            .into_iter()
            .flat_map(u32::to_le_bytes)
            .collect();
        let count: i64 = c
            .query_row(
                "SELECT agrep_tool_occurrences(f,?1) FROM f WHERE f MATCH '(\"error\" OR \"errorerror\" OR \"errors\")'",
                [weights],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 5);
    }

    #[test]
    fn folded_unicode_and_truncation_markers_keep_literal_occurrence_counts() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE VIRTUAL TABLE f USING fts5(text,content='',tokenize='unicode61 remove_diacritics 0');
            CREATE VIRTUAL TABLE vocabulary USING fts5vocab(f,row);")
            .unwrap();
        let texts = [
            "error… ERROR",
            "ｅｒｒｏｒ error",
            "İerror Kerror Straße error",
            "er\u{200d}ror error\u{0345}error",
        ];
        for (i, text) in texts.iter().enumerate() {
            c.execute(
                "INSERT INTO f(rowid,text) VALUES(?1,?2)",
                rusqlite::params![i as i64 + 1, crate::store::search_fold(text)],
            )
            .unwrap();
        }
        let words: Vec<String> = c
            .prepare("SELECT term FROM vocabulary WHERE instr(term,'error')>0 ORDER BY term")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let expression = words
            .iter()
            .map(|word| format!("\"{word}\""))
            .collect::<Vec<_>>()
            .join(" OR ");
        let weights: Vec<u8> = words
            .iter()
            .flat_map(|word| (word.matches("error").count() as u32).to_le_bytes())
            .collect();
        install(&c).unwrap();
        let counts: Vec<usize> = c
            .prepare("SELECT agrep_tool_occurrences(f,?1) FROM f WHERE f MATCH ?2 ORDER BY rowid")
            .unwrap()
            .query_map(rusqlite::params![weights, expression], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        assert_eq!(counts, [2, 1, 3, 2]);
    }

    #[test]
    fn chunked_long_words_only_raise_the_occurrence_ceiling() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE VIRTUAL TABLE f USING fts5(text,content='',tokenize='unicode61 remove_diacritics 0');
            CREATE VIRTUAL TABLE vocabulary USING fts5vocab(f,row);")
            .unwrap();
        let source = format!(
            "{}Error{}ERROR errors",
            "x".repeat(8190),
            "y".repeat(40_000)
        );
        let folded = source.to_ascii_lowercase();
        c.execute(
            "INSERT INTO f(rowid,text) VALUES(1,?1)",
            [crate::tool_index::index_text(&folded).as_ref()],
        )
        .unwrap();
        let words: Vec<String> = c
            .prepare("SELECT term FROM vocabulary WHERE term LIKE '%error%' ORDER BY term")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        let expression = words
            .iter()
            .map(|word| format!("\"{word}\""))
            .collect::<Vec<_>>()
            .join(" OR ");
        let weights: Vec<u8> = words
            .iter()
            .flat_map(|word| (word.matches("error").count() as u32).to_le_bytes())
            .collect();
        install(&c).unwrap();
        let count: usize = c
            .query_row(
                "SELECT agrep_tool_occurrences(f,?1) FROM f WHERE f MATCH ?2",
                rusqlite::params![weights, expression],
                |r| r.get(0),
            )
            .unwrap();
        assert!(count >= folded.matches("error").count());
    }
}
