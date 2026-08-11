//! Deploy-day takeover adopts the dead owner's verified parse cache.
//!
//! The successor takeover used to discard the foreign cache wholesale, so
//! every deploy paid a full reparse of every transcript. The cache format
//! proves its own integrity (digested base, commit-framed journal), so the
//! takeover now re-owns it: same entries, new identity, nothing reparsed.
//! This pins the integration seam the ingest_cache unit tests cannot see -
//! the disclosure line and the zero-reparse rebuild through the real binary.

mod common;

use std::fs;
use std::path::Path;
use std::process::Command;

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, PermissionsExt};

use common::{fixtures_dir, temp_dir, BIN};

fn run(home: &Path, data: &Path, build_id: &str, full: bool) -> std::process::Output {
    let mut command = Command::new(BIN);
    command.args(["index", "--agent", "claude"]);
    if full {
        command.arg("--full");
    }
    command.env("AGREP_HOME", home);
    command.env("AGREP_DATA_DIR", data);
    command.env("AGREP_RUNTIME_BUILD_ID", build_id);
    for key in [
        "USERPROFILE",
        "HOME",
        "APPDATA",
        "CLINE_DIR",
        "CRUSH_GLOBAL_DATA",
        "LOCALAPPDATA",
        "OPENCODE_DB",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "AGREP_RS_BIN",
    ] {
        command.env_remove(key);
    }
    command.output().expect("spawn owned agrep-rs")
}

const CORPUS_SCHEMA: &str = "
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE msgs(
        id INTEGER PRIMARY KEY,
        session TEXT NOT NULL, turn INTEGER, ts INTEGER,
        agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
        who TEXT, text TEXT,
        fts_text TEXT CHECK(
            fts_text IS NULL OR instr(fts_text, char(0)) = 0),
        content_digest TEXT CHECK(
            content_digest IS NULL OR
            (length(content_digest) = 4 AND
             content_digest NOT GLOB '*[^0-9a-f]*')));
    CREATE INDEX msgs_session ON msgs(session, turn);
    CREATE INDEX msgs_transcript_session_turn ON msgs(session, turn)
        WHERE who <> 'tool';
    CREATE INDEX msgs_who_ts ON msgs(who, coalesce(ts, 0) DESC);
    CREATE INDEX msgs_re_i_exceptions ON msgs(id) WHERE
        instr(text, 'İ') > 0 OR instr(text, 'ı') > 0
        OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0;
    CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT);
    CREATE TABLE session_family(
        session TEXT PRIMARY KEY, root TEXT NOT NULL,
        side INTEGER NOT NULL CHECK(side IN (0, 1))) WITHOUT ROWID;
    CREATE INDEX session_family_root ON session_family(root);
    CREATE TABLE boundary_stats(
        token TEXT PRIMARY KEY, n INTEGER NOT NULL, s INTEGER NOT NULL,
        q INTEGER NOT NULL) WITHOUT ROWID;
    CREATE VIEW msgs_fts_content AS
        SELECT id, coalesce(fts_text, text) AS text FROM msgs;
    CREATE VIEW msgs_prose_fts_content AS
        SELECT id, coalesce(fts_text, text) AS text FROM msgs WHERE who <> 'tool';
    CREATE VIRTUAL TABLE msgs_fts USING fts5(
        text, content='msgs_fts_content', content_rowid='id', tokenize='trigram');
    CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
        text, content='msgs_prose_fts_content', content_rowid='id', tokenize='trigram');
";

const CORPUS_TRIGGERS: &str = "
    CREATE TRIGGER msgs_ai AFTER INSERT ON msgs BEGIN
        INSERT INTO msgs_fts(rowid, text)
            VALUES (new.id, coalesce(new.fts_text, new.text));
    END;
    CREATE TRIGGER msgs_ad AFTER DELETE ON msgs BEGIN
        INSERT INTO msgs_fts(msgs_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
    END;
    CREATE TRIGGER msgs_au AFTER UPDATE OF text, fts_text ON msgs
    WHEN coalesce(old.fts_text, old.text) IS NOT coalesce(new.fts_text, new.text) BEGIN
        INSERT INTO msgs_fts(msgs_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
        INSERT INTO msgs_fts(rowid, text)
            VALUES (new.id, coalesce(new.fts_text, new.text));
    END;
    CREATE TRIGGER msgs_prose_ai AFTER INSERT ON msgs WHEN new.who <> 'tool' BEGIN
        INSERT INTO msgs_prose_fts(rowid, text)
            VALUES (new.id, coalesce(new.fts_text, new.text));
    END;
    CREATE TRIGGER msgs_prose_ad AFTER DELETE ON msgs WHEN old.who <> 'tool' BEGIN
        INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
    END;
    CREATE TRIGGER msgs_prose_au_old AFTER UPDATE OF text, fts_text, who ON msgs
    WHEN old.who <> 'tool' AND
         (coalesce(old.fts_text, old.text) IS NOT coalesce(new.fts_text, new.text)
          OR new.who = 'tool') BEGIN
        INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
            VALUES('delete', old.id, coalesce(old.fts_text, old.text));
        INSERT INTO msgs_prose_fts(rowid, text)
            SELECT new.id, coalesce(new.fts_text, new.text)
            WHERE new.who <> 'tool';
    END;
    CREATE TRIGGER msgs_prose_au_new AFTER UPDATE OF text, fts_text, who ON msgs
    WHEN old.who = 'tool' AND new.who <> 'tool' BEGIN
        INSERT INTO msgs_prose_fts(rowid, text)
            VALUES(new.id, coalesce(new.fts_text, new.text));
    END;
";

const CORPUS_SCHEMA_13: &str = "
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE msgs(
        id INTEGER PRIMARY KEY,
        session TEXT NOT NULL, turn INTEGER, ts INTEGER,
        agent TEXT, project TEXT, concept TEXT, model TEXT, model_source TEXT,
        who TEXT, text TEXT,
        content_digest TEXT CHECK(
            content_digest IS NULL OR
            (length(content_digest) = 4 AND
             content_digest NOT GLOB '*[^0-9a-f]*')));
    CREATE INDEX msgs_session ON msgs(session, turn);
    CREATE INDEX msgs_transcript_session_turn ON msgs(session, turn)
        WHERE who <> 'tool';
    CREATE INDEX msgs_who_ts ON msgs(who, coalesce(ts, 0) DESC);
    CREATE INDEX msgs_re_i_exceptions ON msgs(id) WHERE
        instr(text, 'İ') > 0 OR instr(text, 'ı') > 0
        OR instr(text, 'ſ') > 0 OR instr(text, 'K') > 0;
    CREATE TABLE session_sig(session TEXT PRIMARY KEY, sig TEXT);
    CREATE TABLE session_family(
        session TEXT PRIMARY KEY, root TEXT NOT NULL) WITHOUT ROWID;
    CREATE INDEX session_family_root ON session_family(root);
    CREATE TABLE boundary_stats(
        token TEXT PRIMARY KEY, n INTEGER NOT NULL, s INTEGER NOT NULL,
        q INTEGER NOT NULL) WITHOUT ROWID;
    CREATE VIRTUAL TABLE msgs_fts USING fts5(
        text, content='msgs', content_rowid='id', tokenize='trigram');
    CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
        text, content='msgs', content_rowid='id', tokenize='trigram');
";

/// This compatibility fixture pins the production metadata and both searchable FTS lanes.
fn forge_owned_corpus(data: &Path, owner: &str) {
    let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
    connection.execute_batch(CORPUS_SCHEMA).unwrap();
    connection
        .execute_batch(&format!(
            "INSERT INTO meta(key, value) VALUES('build_id', '{owner}');
             INSERT INTO meta(key, value) VALUES('schema', '15');
             INSERT INTO meta(key, value) VALUES('stamp', 'takeover-fixture-stamp');
             INSERT INTO meta(key, value) VALUES('fts_triggers', '4');"
        ))
        .unwrap();
    for index in 0..256 {
        connection
            .execute(
                "INSERT INTO msgs(session, turn, ts, agent, project, concept, model,
                     model_source, who, text, content_digest)
                 VALUES('session', ?2, ?2, 'claude', 'project', '', '', '', 'user', ?1, 'abcd')",
                rusqlite::params![
                    format!("takeoverneedle row {index} {}", "x".repeat(512)),
                    i64::from(index)
                ],
            )
            .unwrap();
    }
    connection
        .execute(
            "INSERT INTO msgs(session, turn, ts, agent, project, concept, model,
                 model_source, who, text, content_digest)
             VALUES('tools', 1, 1, 'claude', 'project', '', '', '', 'tool',
                 'toolonlyneedle', 'abcd')",
            [],
        )
        .unwrap();
    connection
        .execute(
            "INSERT INTO msgs(session, turn, ts, agent, project, concept, model,
                 model_source, who, text, fts_text, content_digest)
             VALUES('nul', 1, 1, 'claude', 'project', '', '', '', 'user',
                 ?1, ?2, 'abcd')",
            rusqlite::params!["nulbefore\0nulafter", "nulbefore nulafter"],
        )
        .unwrap();
    connection
        .execute_batch(
            "INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild');
             INSERT INTO msgs_prose_fts(rowid, text)
                 SELECT id, coalesce(fts_text, text) FROM msgs WHERE who <> 'tool';",
        )
        .unwrap();
    connection.execute_batch(CORPUS_TRIGGERS).unwrap();
}

fn forge_schema13_owned_corpus(data: &Path, owner: &str) {
    let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
    connection.execute_batch(CORPUS_SCHEMA_13).unwrap();
    connection
        .execute_batch(&format!(
            "INSERT INTO meta(key, value) VALUES('build_id', '{owner}');
             INSERT INTO meta(key, value) VALUES('schema', '13');
             INSERT INTO meta(key, value) VALUES('stamp', 'takeover-fixture-stamp');
             INSERT INTO meta(key, value) VALUES('fts_triggers', '3');"
        ))
        .unwrap();
    for index in 0..256 {
        connection
            .execute(
                "INSERT INTO msgs(session, turn, ts, agent, project, concept, model,
                     model_source, who, text, content_digest)
                 VALUES('session', ?2, ?2, 'claude', 'project', '', '', '', 'user', ?1, 'abcd')",
                rusqlite::params![
                    format!("takeoverneedle row {index} {}", "x".repeat(512)),
                    i64::from(index)
                ],
            )
            .unwrap();
    }
    connection
        .execute_batch(
            "INSERT INTO msgs_fts(rowid, text) SELECT id, text FROM msgs;
             INSERT INTO msgs_prose_fts(rowid, text)
                 SELECT id, text FROM msgs WHERE who <> 'tool';",
        )
        .unwrap();
}

fn add_schema13_nul_incompatibility(data: &Path) {
    let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
    connection
        .execute(
            "INSERT INTO msgs(session, turn, ts, agent, project, concept, model,
                 model_source, who, text, content_digest)
             VALUES('nul', 1, 1, 'claude', 'project', '', '', '', 'user', ?1, 'abcd')",
            ["x\0nulafter"],
        )
        .unwrap();
    let id = connection.last_insert_rowid();
    for table in ["msgs_fts", "msgs_prose_fts"] {
        connection
            .execute(
                &format!("INSERT INTO {table}(rowid, text) VALUES(?1, 'x nulafter')"),
                [id],
            )
            .unwrap();
    }
}

fn rank1_integrity(data: &Path) -> rusqlite::Result<()> {
    let connection = rusqlite::Connection::open(data.join("corpus.db"))?;
    for table in ["msgs_fts", "msgs_prose_fts"] {
        connection.execute(
            &format!("INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)"),
            [],
        )?;
    }
    Ok(())
}

fn corrupt_schema13_postings_without_changing_row_coverage(data: &Path) {
    let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
    let (id, text) = connection
        .query_row(
            "SELECT id, text FROM msgs WHERE session='session' ORDER BY id LIMIT 1",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
        )
        .unwrap();
    for table in ["msgs_fts", "msgs_prose_fts"] {
        connection
            .execute(
                &format!("INSERT INTO {table}({table}, rowid, text) VALUES('delete', ?1, ?2)"),
                rusqlite::params![id, text],
            )
            .unwrap();
        connection
            .execute(
                &format!("INSERT INTO {table}(rowid, text) VALUES(?1, 'wrongposting')"),
                [id],
            )
            .unwrap();
    }
}

fn copy_dir(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_dir(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), &target).unwrap();
        }
    }
}

fn owner_at(path: &Path) -> String {
    rusqlite::Connection::open(path)
        .unwrap()
        .query_row("SELECT value FROM meta WHERE key='build_id'", [], |row| {
            row.get(0)
        })
        .unwrap()
}

fn fts_hits(path: &Path) -> i64 {
    rusqlite::Connection::open(path)
        .unwrap()
        .query_row(
            "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH 'takeoverneedle'",
            [],
            |row| row.get(0),
        )
        .unwrap()
}

fn forge_wal_owned_corpus(data: &Path, owner: &str) -> rusqlite::Connection {
    let seed = data.join("wal-seed.db");
    let connection = rusqlite::Connection::open(&seed).unwrap();
    assert_eq!(
        connection
            .query_row("PRAGMA journal_mode=WAL", [], |row| row.get::<_, String>(0))
            .unwrap(),
        "wal"
    );
    connection
        .execute_batch("PRAGMA wal_autocheckpoint=0")
        .unwrap();
    connection
        .execute_batch(&format!(
            "BEGIN;
             CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
             INSERT INTO meta(key, value) VALUES('build_id', '{owner}');
             CREATE TABLE bulk(id INTEGER PRIMARY KEY, body TEXT);
             INSERT INTO bulk(body) VALUES('{}');
             COMMIT;",
            "x".repeat(8192)
        ))
        .unwrap();
    let seed_wal = seed.with_file_name("wal-seed.db-wal");
    assert!(seed_wal.exists());
    fs::copy(&seed, data.join("corpus.db")).unwrap();
    fs::copy(&seed_wal, data.join("corpus.db-wal")).unwrap();
    connection
}

#[test]
fn takeover_adopts_the_cache_and_reparses_nothing() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-adopt-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-adopt-data");

    let first = run(&home, &data, owner_a, true);
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    forge_owned_corpus(&data, owner_a);

    // deploy day: a new identity finds the dead owner's stores
    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    let stdout = String::from_utf8_lossy(&second.stdout);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("foreign parse cache verified and adopted"),
        "{stderr}"
    );
    assert!(
        stdout.contains("(0 changed"),
        "adopted cache still reparsed sources: {stdout}"
    );

    // Atomic replacement preserves the FTS while breaking aliases and reader snapshots.
    assert!(data.join("corpus.db").exists(), "{stderr}");
    assert!(
        stderr.contains("search db verified and adopted"),
        "{stderr}"
    );
    let owner: String = rusqlite::Connection::open(data.join("corpus.db"))
        .unwrap()
        .query_row("SELECT value FROM meta WHERE key='build_id'", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(owner, owner_b);
    assert_eq!(fts_hits(&data.join("corpus.db")), 256);
    let metadata = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
    assert_eq!(
        metadata
            .query_row("SELECT value FROM meta WHERE key='schema'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
        "15"
    );
    assert_eq!(
        metadata
            .query_row("SELECT text FROM msgs WHERE session='nul'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
        "nulbefore\0nulafter"
    );
    for term in ["nulbefore", "nulafter"] {
        assert_eq!(
            metadata
                .query_row(
                    "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH ?1",
                    [term],
                    |row| row.get::<_, i64>(0)
                )
                .unwrap(),
            1,
            "{term}"
        );
    }
    assert_eq!(
        metadata
            .query_row(
                "SELECT count(*) FROM msgs_fts WHERE msgs_fts MATCH 'orenul'",
                [],
                |row| row.get::<_, i64>(0)
            )
            .unwrap(),
        0
    );
    assert_eq!(
        metadata
            .query_row("SELECT value FROM meta WHERE key='stamp'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
        "takeover-fixture-stamp"
    );
    // Windows: an open connection blocks the child's replace-over (no
    // delete sharing); POSIX rename hid this. Close before the next run.
    drop(metadata);

    // a torn cache never adopts: corrupt it, flip identities again, and the
    // takeover must fall back to the discard path and still converge
    let cache = data.join(".ingest_cache.bin");
    let mut bytes = fs::read(&cache).unwrap();
    let last = bytes.len() - 1;
    bytes[last] ^= 0xFF;
    fs::write(&cache, bytes).unwrap();
    let third = run(&home, &data, owner_a, false);
    let stderr = String::from_utf8_lossy(&third.stderr);
    assert!(third.status.success(), "{stderr}");
    assert!(stderr.contains("foreign parse cache discarded"), "{stderr}");
}

#[test]
fn a_torn_search_db_falls_back_to_the_discard() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-torn-db-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-torn-db-data");
    let first = run(&home, &data, owner_a, true);
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    forge_owned_corpus(&data, owner_a);
    // corrupt an interior page: the owner row on page one still reads, but
    // quick_check finds the torn b-tree and adoption must refuse
    let db = data.join("corpus.db");
    let mut bytes = fs::read(&db).unwrap();
    assert!(bytes.len() > 8192, "forged db too small to tear");
    let total = bytes.len();
    for page_start in (4096..total).step_by(4096) {
        let end = (page_start + 512).min(total);
        for byte in &mut bytes[page_start + 32..end] {
            *byte ^= 0xFF;
        }
    }
    fs::write(&db, bytes).unwrap();

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("search db discarded for rebuild"),
        "{stderr}"
    );
    assert!(!db.exists(), "a torn foreign corpus.db must not survive");
}

#[cfg(unix)]
#[test]
fn takeover_publishes_a_distinct_private_inode_without_mutating_hardlinks() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-hardlink-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-hardlink-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    forge_owned_corpus(&data, owner_a);

    let corpus = data.join("corpus.db");
    fs::set_permissions(&corpus, fs::Permissions::from_mode(0o644)).unwrap();
    let outside = temp_dir("takeover-hardlink-outside").join("shared.db");
    fs::hard_link(&corpus, &outside).unwrap();
    let outside_before = fs::read(&outside).unwrap();
    let old_inode = fs::metadata(&outside).unwrap().ino();

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("search db verified and adopted"),
        "{stderr}"
    );
    assert_eq!(owner_at(&outside), owner_a);
    assert_eq!(fs::read(&outside).unwrap(), outside_before);
    assert_eq!(fs::metadata(&outside).unwrap().ino(), old_inode);
    assert_ne!(fs::metadata(&corpus).unwrap().ino(), old_inode);
    assert_eq!(owner_at(&corpus), owner_b);
    assert_eq!(
        fs::metadata(&corpus).unwrap().permissions().mode() & 0o777,
        0o600
    );
}

#[cfg(unix)]
#[test]
fn takeover_keeps_an_existing_reader_on_its_old_snapshot() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-reader-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-reader-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    forge_owned_corpus(&data, owner_a);

    let corpus = data.join("corpus.db");
    let held = rusqlite::Connection::open_with_flags(
        &corpus,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .unwrap();
    held.execute_batch("BEGIN").unwrap();
    let held_owner: String = held
        .query_row("SELECT value FROM meta WHERE key='build_id'", [], |row| {
            row.get(0)
        })
        .unwrap();
    assert_eq!(held_owner, owner_a);

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("search db verified and adopted"),
        "{stderr}"
    );
    assert_eq!(owner_at(&corpus), owner_b);
    assert_eq!(
        held.query_row("SELECT value FROM meta WHERE key='build_id'", [], |row| {
            row.get::<_, String>(0)
        })
        .unwrap(),
        owner_a
    );
}

#[test]
fn wal_family_rebuilds_without_publishing_an_incompatible_database() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-wal-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-wal-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    let writer = forge_wal_owned_corpus(&data, owner_a);

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        !stderr.contains("search db verified and adopted"),
        "{stderr}"
    );
    assert!(
        stderr.contains("search db discarded for rebuild"),
        "{stderr}"
    );
    assert!(!data.join("corpus.db").exists());
    for suffix in ["-journal", "-wal", "-shm"] {
        assert!(!data.join(format!("corpus.db{suffix}")).exists());
    }
    drop(writer);
}

#[test]
fn invalid_search_schema_is_rebuildable() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-schema-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-schema-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
    connection
        .execute_batch("CREATE TABLE unrelated(value TEXT)")
        .unwrap();
    drop(connection);

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("search db discarded for rebuild"),
        "{stderr}"
    );
    assert!(!data.join("corpus.db").exists());
}

#[test]
fn orphaned_database_sidecars_are_rebuilt_as_one_family() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-orphan-sidecars-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-orphan-sidecars-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    forge_owned_corpus(&data, owner_a);
    fs::remove_file(data.join("corpus.db")).unwrap();
    for suffix in ["-journal", "-wal", "-shm"] {
        fs::write(data.join(format!("corpus.db{suffix}")), suffix.as_bytes()).unwrap();
    }

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("search db discarded for rebuild"),
        "{stderr}"
    );
    for suffix in ["", "-journal", "-wal", "-shm"] {
        assert!(!data.join(format!("corpus.db{suffix}")).exists());
    }
}

#[test]
fn malformed_cache_preflight_preserves_a_valid_foreign_database() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-cache-preflight-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-cache-preflight-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    forge_owned_corpus(&data, owner_a);
    let corpus = data.join("corpus.db");
    let before = fs::read(&corpus).unwrap();
    let mut malformed = vec![0_u8; 12];
    malformed.extend_from_slice(b"AGRPCB01");
    fs::write(data.join(".ingest_cache.bin"), malformed).unwrap();

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("serving the published snapshot read-only"),
        "{stderr}"
    );
    assert_eq!(fs::read(&corpus).unwrap(), before);
    assert_eq!(owner_at(&corpus), owner_a);
    assert!(
        !stderr.contains("search db verified and adopted"),
        "{stderr}"
    );
}

#[test]
fn incomplete_or_corrupt_fts_lanes_are_rebuilt() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    for mode in [
        "trigram-empty",
        "trigram-corrupt",
        "trigram-config",
        "prose-empty",
        "prose-corrupt",
        "prose-wrong-content",
        "nul-sidecar-missing",
        "nul-sidecar-tampered",
    ] {
        let home = temp_dir(&format!("takeover-{mode}-home"));
        copy_dir(&fixtures_dir().join("claude").join("home"), &home);
        let data = temp_dir(&format!("takeover-{mode}-data"));
        assert!(run(&home, &data, owner_a, true).status.success());
        forge_owned_corpus(&data, owner_a);
        let connection = rusqlite::Connection::open(data.join("corpus.db")).unwrap();
        match mode {
            "trigram-empty" => connection
                .execute_batch(
                    "DROP TABLE msgs_fts;
                     CREATE VIRTUAL TABLE msgs_fts USING fts5(
                         text, content='msgs_fts_content', content_rowid='id', tokenize='trigram');",
                )
                .unwrap(),
            "trigram-corrupt" => connection
                .execute_batch(
                    "INSERT INTO msgs_fts(msgs_fts, rowid, text)
                         SELECT 'delete', id, text FROM msgs WHERE id=1;",
                )
                .unwrap(),
            "trigram-config" => connection
                .execute_batch(
                    "DROP TABLE msgs_fts;
                     CREATE VIRTUAL TABLE msgs_fts USING fts5(
                         text, content='msgs_fts_content', content_rowid='id', tokenize='unicode61');
                     INSERT INTO msgs_fts(msgs_fts) VALUES('rebuild');",
                )
                .unwrap(),
            "prose-empty" => connection
                .execute_batch(
                    "DROP TABLE msgs_prose_fts;
                     CREATE VIRTUAL TABLE msgs_prose_fts USING fts5(
                         text, content='msgs_prose_fts_content', content_rowid='id',
                         tokenize='trigram');",
                )
                .unwrap(),
            "prose-corrupt" => connection
                .execute_batch(
                    "INSERT INTO msgs_prose_fts(rowid, text)
                         SELECT id, text FROM msgs WHERE who='tool';",
                )
                .unwrap(),
            "prose-wrong-content" => connection
                .execute_batch(
                    "INSERT INTO msgs_prose_fts(msgs_prose_fts, rowid, text)
                         SELECT 'delete', id, text FROM msgs WHERE id=1;
                     INSERT INTO msgs_prose_fts(rowid, text)
                         VALUES(1, 'different searchable prose with the same document id');",
                )
                .unwrap(),
            "nul-sidecar-missing" => connection
                .execute_batch("UPDATE msgs SET fts_text=NULL WHERE session='nul'")
                .unwrap(),
            "nul-sidecar-tampered" => connection
                .execute_batch("UPDATE msgs SET fts_text='wrong sidecar' WHERE session='nul'")
                .unwrap(),
            _ => unreachable!(),
        }
        drop(connection);

        let second = run(&home, &data, owner_b, false);
        let stderr = String::from_utf8_lossy(&second.stderr);
        assert!(second.status.success(), "{mode}: {stderr}");
        assert!(
            stderr.contains("search db discarded for rebuild"),
            "{mode}: {stderr}"
        );
        assert!(!data.join("corpus.db").exists(), "{mode}: {stderr}");
    }
}

#[test]
fn reader_safe_write_shape_drift_is_retained_for_atomic_rebuild() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    for (mode, mutation, retained) in [
        ("column", "ALTER TABLE msgs DROP COLUMN model_source", false),
        ("index", "DROP INDEX msgs_who_ts", true),
        (
            "index-body",
            "DROP INDEX msgs_who_ts; CREATE INDEX msgs_who_ts ON msgs(who)",
            true,
        ),
        ("trigger", "DROP TRIGGER msgs_prose_ai", true),
        (
            "trigger-body",
            "DROP TRIGGER msgs_prose_ai;
             CREATE TRIGGER msgs_prose_ai AFTER INSERT ON msgs BEGIN SELECT 1; END",
            true,
        ),
        (
            "extra-meta-trigger",
            "CREATE TRIGGER meta_sabotage AFTER UPDATE OF value ON meta
             WHEN new.key='build_id' BEGIN DELETE FROM msgs; END",
            true,
        ),
        (
            "aux-table-body",
            "DROP TABLE boundary_stats;
             CREATE TABLE boundary_stats(token TEXT PRIMARY KEY)",
            false,
        ),
    ] {
        let home = temp_dir(&format!("takeover-schema-{mode}-home"));
        copy_dir(&fixtures_dir().join("claude").join("home"), &home);
        let data = temp_dir(&format!("takeover-schema-{mode}-data"));
        assert!(run(&home, &data, owner_a, true).status.success());
        forge_owned_corpus(&data, owner_a);
        rusqlite::Connection::open(data.join("corpus.db"))
            .unwrap()
            .execute_batch(mutation)
            .unwrap();
        if mode == "index" {
            let cache = data.join(".ingest_cache.bin");
            let mut bytes = fs::read(&cache).unwrap();
            let last = bytes.len() - 1;
            bytes[last] ^= 0xff;
            fs::write(cache, bytes).unwrap();
        }

        let second = run(&home, &data, owner_b, false);
        let stderr = String::from_utf8_lossy(&second.stderr);
        assert!(second.status.success(), "{mode}: {stderr}");
        let expected = if retained {
            "search db retained read-only for atomic rebuild"
        } else {
            "search db discarded for rebuild"
        };
        assert!(stderr.contains(expected), "{mode}: {stderr}");
        if mode == "extra-meta-trigger" {
            assert!(stderr.contains("trigger inventory"), "{stderr}");
            assert!(
                !stderr.contains("search db verified and adopted"),
                "{stderr}"
            );
        }
        assert_eq!(
            data.join("corpus.db").exists(),
            retained,
            "{mode}: {stderr}"
        );
        if retained {
            assert_eq!(owner_at(&data.join("corpus.db")), owner_a);
            let owner: serde_json::Value =
                serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap())
                    .unwrap();
            assert_eq!(owner["build_id"], owner_b);
            assert_eq!(owner["retained_corpus_db"]["build_id"], owner_a);
            if mode == "index" {
                assert!(stderr.contains("foreign parse cache discarded"), "{stderr}");
                let third = run(&home, &data, owner_b, false);
                assert!(
                    third.status.success(),
                    "{}",
                    String::from_utf8_lossy(&third.stderr)
                );
                let still_retained: serde_json::Value =
                    serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap())
                        .unwrap();
                assert!(still_retained.get("retained_corpus_db").is_some());

                let published = temp_dir("takeover-schema-current-publish");
                forge_owned_corpus(&published, owner_b);
                let bytes = fs::read(published.join("corpus.db")).unwrap();
                agrep_core::cache::write_bytes_atomic(&data.join("corpus.db"), &bytes).unwrap();
                fs::remove_dir_all(published).unwrap();
                let fourth = run(&home, &data, owner_b, false);
                assert!(
                    fourth.status.success(),
                    "{}",
                    String::from_utf8_lossy(&fourth.stderr)
                );
                let cleared: serde_json::Value =
                    serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap())
                        .unwrap();
                assert!(cleared.get("retained_corpus_db").is_none());
                assert!(cleared.get("legacy_corpus_db").is_none());
            }
        }
    }
}

#[test]
fn previous_reader_schema_without_content_proof_falls_back_safely() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-schema13-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-schema13-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    forge_schema13_owned_corpus(&data, owner_a);
    add_schema13_nul_incompatibility(&data);
    let messages = fs::read(data.join("messages.jsonl")).unwrap();
    let replies = fs::read(data.join("replies.jsonl")).unwrap();

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("search db discarded for rebuild"),
        "{stderr}"
    );
    assert!(!stderr.contains("search db retained read-only"), "{stderr}");
    assert!(!data.join("corpus.db").exists(), "{stderr}");
    assert_eq!(fs::read(data.join("messages.jsonl")).unwrap(), messages);
    assert_eq!(fs::read(data.join("replies.jsonl")).unwrap(), replies);
    let owner: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap()).unwrap();
    assert_eq!(owner["build_id"], owner_b);
    assert!(owner["retained_corpus_db"].is_null());
}

#[test]
fn previous_reader_schema_with_wrong_postings_is_rebuilt() {
    let owner_a = "aaaaaaaaaaaaaaaaaaaa";
    let owner_b = "bbbbbbbbbbbbbbbbbbbb";
    let home = temp_dir("takeover-schema13-wrong-content-home");
    copy_dir(&fixtures_dir().join("claude").join("home"), &home);
    let data = temp_dir("takeover-schema13-wrong-content-data");
    assert!(run(&home, &data, owner_a, true).status.success());
    forge_schema13_owned_corpus(&data, owner_a);
    rank1_integrity(&data).unwrap();
    corrupt_schema13_postings_without_changing_row_coverage(&data);
    let integrity_error = rank1_integrity(&data).expect_err("wrong postings passed rank=1");
    assert!(
        integrity_error.to_string().contains("malformed"),
        "{integrity_error}"
    );

    let second = run(&home, &data, owner_b, false);
    let stderr = String::from_utf8_lossy(&second.stderr);
    assert!(second.status.success(), "{stderr}");
    assert!(
        stderr.contains("search db discarded for rebuild"),
        "{stderr}"
    );
    assert!(!stderr.contains("search db retained read-only"), "{stderr}");
    assert!(!data.join("corpus.db").exists(), "{stderr}");
    let owner: serde_json::Value =
        serde_json::from_slice(&fs::read(data.join(".derived-owner.json")).unwrap()).unwrap();
    assert_eq!(owner["build_id"], owner_b);
    assert!(owner["retained_corpus_db"].is_null());
}
