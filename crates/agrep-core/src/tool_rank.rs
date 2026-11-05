//! Occurrence ceilings for ASCII tool terms in folded text.
use anyhow::{bail, Result};
use rusqlite::{ffi, Connection};
use std::{ffi::c_void, ptr};

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
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

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
