//! A conservative word-position prefilter; canonical text remains the verifier.
use anyhow::{bail, Result};
use rusqlite::{ffi, Connection};
use std::{ffi::c_void, ptr};

struct Plan {
    phrases: Vec<usize>,
    ordinals: Vec<usize>,
}
unsafe extern "C" fn destroy(value: *mut c_void) {
    drop(Box::from_raw(value.cast::<Plan>()));
}
fn possible(positions: &mut [Vec<i32>], ordinals: &[usize], extra: i32) -> bool {
    for list in positions.iter_mut() {
        list.sort_unstable();
        list.dedup();
        if list.is_empty() {
            return false;
        }
    }
    let mut prior = positions[0].clone();
    for i in 1..positions.len() {
        let distance = (ordinals[i] - ordinals[i - 1]).min(i32::MAX as usize) as i32;
        let width = distance.saturating_mul(extra.saturating_add(1));
        prior = positions[i]
            .iter()
            .copied()
            .filter(|&at| {
                let first = prior.partition_point(|&p| p < at.saturating_sub(width));
                prior.get(first).is_some_and(|&p| p <= at)
            })
            .collect();
        if prior.is_empty() {
            return false;
        }
    }
    true
}
unsafe extern "C" fn span(
    api: *const ffi::Fts5ExtensionApi,
    fts: *mut ffi::Fts5Context,
    context: *mut ffi::sqlite3_context,
    count: i32,
    values: *mut *mut ffi::sqlite3_value,
) {
    let result = || -> std::result::Result<bool, i32> {
        if count != 2 {
            return Err(ffi::SQLITE_MISUSE);
        }
        let api = &*api;
        let Some(get) = api.xGetAuxdata else {
            return Err(ffi::SQLITE_ERROR);
        };
        let mut plan = get(fts, 0).cast::<Plan>();
        if plan.is_null() {
            let value = *values;
            let bytes = ffi::sqlite3_value_bytes(value);
            let data = ffi::sqlite3_value_blob(value).cast::<u8>();
            if bytes <= 0 || bytes % 4 != 0 || data.is_null() {
                return Err(ffi::SQLITE_MISMATCH);
            }
            let data = std::slice::from_raw_parts(data, bytes as usize);
            let phrases: Vec<_> = data
                .chunks_exact(4)
                .map(|b| u32::from_le_bytes(b.try_into().unwrap()) as usize)
                .collect();
            let mut ordinals = phrases.clone();
            ordinals.sort_unstable();
            ordinals.dedup();
            plan = Box::into_raw(Box::new(Plan { phrases, ordinals }));
            let Some(set) = api.xSetAuxdata else {
                destroy(plan.cast());
                return Err(ffi::SQLITE_ERROR);
            };
            let rc = set(fts, plan.cast(), Some(destroy));
            // FTS5 calls the destructor itself when xSetAuxdata fails.
            if rc != ffi::SQLITE_OK {
                return Err(rc);
            }
        }
        let plan = &*plan;
        if plan.ordinals.len() < 2 {
            return Ok(true);
        }
        let Some(inst_count) = api.xInstCount else {
            return Err(ffi::SQLITE_ERROR);
        };
        let Some(inst) = api.xInst else {
            return Err(ffi::SQLITE_ERROR);
        };
        let mut n = 0;
        let rc = inst_count(fts, &mut n);
        if rc != ffi::SQLITE_OK {
            return Err(rc);
        }
        let mut positions = vec![Vec::new(); plan.ordinals.len()];
        for i in 0..n {
            let (mut phrase, mut column, mut offset) = (0, 0, 0);
            let rc = inst(fts, i, &mut phrase, &mut column, &mut offset);
            if rc != ffi::SQLITE_OK {
                return Err(rc);
            }
            if let Some(&ordinal) = plan.phrases.get(phrase as usize) {
                if let Ok(group) = plan.ordinals.binary_search(&ordinal) {
                    positions[group].push(offset);
                }
            }
        }
        Ok(possible(
            &mut positions,
            &plan.ordinals,
            ffi::sqlite3_value_int(*values.add(1)).max(0),
        ))
    };
    match result() {
        Ok(value) => ffi::sqlite3_result_int(context, i32::from(value)),
        Err(rc) => ffi::sqlite3_result_error_code(context, rc),
    }
}
/// The API pointer is borrowed from this connection; only the auxiliary function
/// is registered. Neither the database nor the connection's read transaction changes.
pub(crate) fn install(c: &Connection) -> Result<()> {
    unsafe {
        let mut api: *mut ffi::fts5_api = ptr::null_mut();
        let mut statement = ptr::null_mut();
        let rc = ffi::sqlite3_prepare_v2(
            c.handle(),
            c"SELECT fts5(?1)".as_ptr(),
            -1,
            &mut statement,
            ptr::null_mut(),
        );
        if rc != ffi::SQLITE_OK {
            bail!("cannot obtain FTS5 extension API: {rc}");
        }
        let rc = ffi::sqlite3_bind_pointer(
            statement,
            1,
            (&mut api as *mut *mut ffi::fts5_api).cast(),
            c"fts5_api_ptr".as_ptr(),
            None,
        );
        let step = if rc == ffi::SQLITE_OK {
            ffi::sqlite3_step(statement)
        } else {
            rc
        };
        ffi::sqlite3_finalize(statement);
        if step != ffi::SQLITE_ROW || api.is_null() {
            bail!("FTS5 extension API is unavailable: {step}");
        }
        let Some(create) = (*api).xCreateFunction else {
            bail!("FTS5 function registration is unavailable");
        };
        let rc = create(
            api,
            c"agrep_word_span".as_ptr(),
            ptr::null_mut(),
            Some(span),
            None,
        );
        if rc != ffi::SQLITE_OK {
            bail!("cannot register word-position prefilter: {rc}");
        }
    }
    Ok(())
}
/// ASCII punctuation never creates a unicode61 word. Conservatively count every
/// non-ASCII character that the phrase regex permits in a separator (including
/// private-use and numeric symbols), even when unicode61 would also ignore it.
pub(crate) fn gap_upper(text: &str) -> usize {
    if text.is_ascii() {
        return 0;
    }
    let (mut gap, mut maximum) = (0, 0);
    for c in text.chars() {
        if c != '_' && regex_syntax::is_word_character(c) {
            gap = 0;
        } else if !c.is_ascii() {
            gap += 1;
            maximum = maximum.max(gap);
        }
    }
    maximum
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ordered_positions_allow_combined_words_and_bound_intervening_words() {
        assert!(possible(&mut [vec![4], vec![4], vec![6]], &[0, 1, 3], 0));
        assert!(!possible(&mut [vec![4], vec![8]], &[0, 1], 0));
        assert!(possible(&mut [vec![4], vec![8]], &[0, 1], 3));
        assert!(!possible(&mut [vec![8], vec![4]], &[0, 1], 20));
        assert_eq!(gap_upper("a_\u{e000} \u{e001} b"), 2);
        assert_eq!(gap_upper("a -\n _____ b"), 0);
    }
}

#[cfg(test)]
mod sqlite_tests {
    use super::*;
    #[test]
    fn auxiliary_rejects_impossible_word_positions_without_reading_content() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE VIRTUAL TABLE f USING fts5(text,content='',tokenize='unicode61 remove_diacritics 0');CREATE TABLE gaps(id INTEGER PRIMARY KEY,n INTEGER);").unwrap();
        for (id, text) in [
            (1, "foo bar"),
            (2, "foo x bar"),
            (3, "foobar"),
            (4, "bar foo"),
            (5, "foo \u{e000} bar"),
            (6, "foo-----bar"),
        ] {
            c.execute(
                "INSERT INTO f(rowid,text) VALUES(?1,?2)",
                rusqlite::params![id, text],
            )
            .unwrap();
            c.execute(
                "INSERT INTO gaps VALUES(?1,?2)",
                rusqlite::params![id, gap_upper(text)],
            )
            .unwrap();
        }
        install(&c).unwrap();
        let plan: Vec<u8> = [0u32, 0, 1, 1]
            .into_iter()
            .flat_map(u32::to_le_bytes)
            .collect();
        let got:Vec<i64>=c.prepare("SELECT f.rowid FROM f JOIN gaps g ON g.id=f.rowid WHERE f MATCH ?1 AND agrep_word_span(f,?2,g.n)").unwrap().query_map(rusqlite::params!["(\"foo\" OR \"foobar\") AND (\"bar\" OR \"foobar\")",plan],|r|r.get(0)).unwrap().collect::<rusqlite::Result<_>>().unwrap();
        assert_eq!(got, [1, 3, 5, 6]);
    }
}
