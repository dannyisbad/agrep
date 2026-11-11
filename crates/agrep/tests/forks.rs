//! Forked chats: pi fork copies fold into one meaning row, and `--chat` on a Codex fork
//! child covers the history it inherited from its parent.
mod support;
use agrep_core::{
    data::DataRoot,
    query::{Hit, Options},
    render::{self, Marks, Style},
    semantic, semantic_model, semantic_store, store,
};
use serde_json::{json, Value};
use std::{fs, path::PathBuf, process::Output};
use tempfile::TempDir;

struct Home {
    _t: TempDir,
    h: PathBuf,
    d: PathBuf,
}
impl Home {
    fn new() -> Self {
        let t = TempDir::new().unwrap();
        let (h, d) = (t.path().join("h"), t.path().join("d"));
        Self { _t: t, h, d }
    }
    fn write(&self, relative: &str, records: &[Value]) {
        let path = self.h.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        let body: String = records.iter().map(|r| format!("{r}\n")).collect();
        fs::write(path, body).unwrap();
    }
    fn run(&self, args: &[&str]) -> Output {
        self.run_in("compact", args)
    }
    /// A run under `AGREP_PROFILE=profile`, its stdout a pipe.
    fn run_in(&self, profile: &str, args: &[&str]) -> Output {
        std::process::Command::new(support::binary())
            .env_clear()
            .env("PATH", std::env::var_os("PATH").unwrap_or_default())
            .env("HOME", &self.h)
            .env("AGREP_HOME", &self.h)
            .env("AGREP_DATA_DIR", &self.d)
            .env("AGREP_MODEL_DIR", self.h.join("no-model"))
            .env("AGREP_CALLER_PUBLICATION_DIR", self.h.join("empty-callers"))
            .env("AGREP_PROFILE", profile)
            .args(args)
            .output()
            .unwrap()
    }
    fn index(&self) {
        let out = self.run(&["index"]);
        assert!(out.status.success(), "{}", text(&out.stderr));
    }
}
fn text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

const PI_ROOT: &str = "0dd00001-0000-7000-8000-000000000001";
const PI_FORKS: [&str; 2] = [
    "0dd00002-0000-7000-8000-000000000002",
    "0dd00003-0000-7000-8000-000000000003",
];
const PI_OTHER: &str = "0dd00004-0000-7000-8000-000000000004";
const SHARED: &str = "keep the prompt general so it does not overfit";

/// A pi chat and two forks of it: each fork file copies the chat's history (ids, times and
/// all) under its own session header, then goes on with its own turns.
fn pi_forks(home: &Home) {
    let message = |id: &str, parent: Option<&str>, at: &str, role: &str, body: &str| json!({"type":"message","id":id,"parentId":parent,"timestamp":at,"message":{"role":role,"model":"m","provider":"p","content":[{"type":"text","text":body}]}});
    let history = vec![
        message("u1", None, "2026-09-22T02:44:40.000Z", "user", SHARED),
        message(
            "a1",
            Some("u1"),
            "2026-09-22T02:44:41.000Z",
            "assistant",
            "noted",
        ),
    ];
    let mut root = vec![
        json!({"type":"session","version":3,"id":PI_ROOT,"timestamp":"2026-09-22T02:44:39.000Z","cwd":"/work/merger"}),
    ];
    root.extend(history.clone());
    home.write(
        &format!(".omp/agent/sessions/-work-merger/2026-09-22T02-44-39-000Z_{PI_ROOT}.jsonl"),
        &root,
    );
    for (n, fork) in PI_FORKS.iter().enumerate() {
        let day = 4 + n;
        let mut records = vec![
            json!({"type":"session","version":3,"id":fork,"timestamp":format!("2026-10-0{day}T20:42:57.000Z"),"cwd":"/work/merger","parentSession":PI_ROOT}),
        ];
        records.extend(history.clone());
        records.push(message(
            "u2",
            Some("a1"),
            &format!("2026-10-0{day}T20:43:00.000Z"),
            "user",
            &format!("fork {n} handles a closed stdout pipe"),
        ));
        records.push(message(
            "a2",
            Some("u2"),
            &format!("2026-10-0{day}T20:43:01.000Z"),
            "assistant",
            "done",
        ));
        home.write(
            &format!(".omp/agent/sessions/-work-merger/2026-10-0{day}T20-42-57-000Z_{fork}.jsonl"),
            &records,
        );
    }
}

/// An unrelated chat with one turn.
fn pi_other(home: &Home) {
    home.write(
        &format!(".omp/agent/sessions/-work-other/2026-09-01T10-00-00-000Z_{PI_OTHER}.jsonl"),
        &[
            json!({"type":"session","version":3,"id":PI_OTHER,"timestamp":"2026-09-01T10:00:00.000Z","cwd":"/work/other"}),
            json!({"type":"message","id":"u1","parentId":null,"timestamp":"2026-09-01T10:00:01.000Z","message":{"role":"user","model":"m","provider":"p","content":[{"type":"text","text":"another chat closes its stdout pipe"}]}}),
        ],
    );
}

/// Unit vectors at a chosen cosine to the query vector `e0`: the shared turn is the best
/// match, each fork's own turn the next.
fn publish_vectors(root: &DataRoot) {
    let c = store::reader(root).unwrap();
    let pending = semantic_store::batch(&c, 64).unwrap();
    let refs: Vec<_> = pending.iter().map(|p| (p.key.clone(), 0)).collect();
    let vectors: Vec<_> = pending
        .iter()
        .map(|p| {
            let body = p.chunks.join(" ");
            let cosine = if body.contains("overfit") {
                0.95f32
            } else if body.contains("fork 1") {
                0.93
            } else if body.contains("fork 0") {
                0.92
            } else if body.contains("another chat") {
                0.9
            } else {
                0.
            };
            let mut v = vec![0f32; semantic_model::DIM];
            v[0] = cosine;
            v[1] = (1. - cosine * cosine).sqrt();
            v
        })
        .collect();
    semantic_store::publish(root, &pending, &refs, &vectors).unwrap();
}

fn meaning_page(root: &DataRoot, limit: usize, chats: bool) -> Vec<Hit> {
    let c = store::reader(root).unwrap();
    let o = Options {
        pattern: "how do we handle a closed stdout pipe".into(),
        regex: false,
        word: false,
        sort: "score".into(),
        limit,
        exact: false,
        count_only: false,
        count_by_tier: false,
        content: false,
        chats,
        tools: false,
        filters: Default::default(),
    };
    let mut q = vec![0f32; semantic_model::DIM];
    q[0] = 1.;
    semantic::search_vector(root, &c, &o, true, &q)
        .unwrap()
        .hits
}

#[test]
fn pi_fork_copies_of_a_turn_fold_into_one_meaning_row() {
    let home = Home::new();
    pi_forks(&home);
    pi_other(&home);
    home.index();
    let root = DataRoot::open(home.d.clone(), false).unwrap();
    publish_vectors(&root);

    // Before, the three copies filled the page; now the newest fork shows the turn once
    // and the page goes on to another chat.
    let page = meaning_page(&root, 3, false);
    let rows: Vec<(&str, &str)> = page
        .iter()
        .map(|h| (h.session.as_str(), h.text.as_str()))
        .collect();
    assert_eq!(
        rows,
        [
            (PI_FORKS[1], SHARED),
            (PI_OTHER, "another chat closes its stdout pipe")
        ]
    );
    let shared = &page[0];
    let forks: Vec<&str> = shared.forks.iter().map(|f| f.session.as_str()).collect();
    assert_eq!(forks, [PI_FORKS[0], PI_ROOT]);
    assert!(page[1].forks.is_empty());

    let line = render::compact_line(shared, &Marks::new("closed stdout pipe", &Style::default()));
    assert!(
        line.contains("~semantic (meaning match, also in 2 forks)"),
        "{line}"
    );
    let value = render::hit_value(shared, 0);
    let listed: Vec<&str> = value["forks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["handle"].as_str().unwrap())
        .collect();
    assert_eq!(listed.len(), 2);
    assert!(listed[0].starts_with(&format!("@{}:", PI_FORKS[0])));
    assert!(listed[1].starts_with(&format!("@{PI_ROOT}:")));

    // A chat listing still names every chat holding the turn.
    let chats = meaning_page(&root, 10, true);
    let mut sessions: Vec<&str> = chats
        .iter()
        .filter(|h| h.text == SHARED)
        .map(|h| h.session.as_str())
        .collect();
    sessions.sort_unstable();
    assert_eq!(sessions, [PI_ROOT, PI_FORKS[0], PI_FORKS[1]]);
    assert!(chats.iter().all(|h| h.forks.is_empty()));
}

const PI_OLDER: &str = "0dd00005-0000-7000-8000-000000000005";

/// An older chat whose own turn shares a word with the forks' shared turn.
fn pi_older(home: &Home) {
    home.write(
        &format!(".omp/agent/sessions/-work-older/2026-08-01T10-00-00-000Z_{PI_OLDER}.jsonl"),
        &[
            json!({"type":"session","version":3,"id":PI_OLDER,"timestamp":"2026-08-01T10:00:00.000Z","cwd":"/work/older"}),
            json!({"type":"message","id":"u1","parentId":null,"timestamp":"2026-08-01T10:00:01.000Z","message":{"role":"user","model":"m","provider":"p","content":[{"type":"text","text":"an older chat feared the eval would overfit"}]}}),
        ],
    );
}

#[test]
fn pi_fork_copies_of_a_turn_fold_into_one_keyword_row() {
    let home = Home::new();
    pi_forks(&home);
    pi_older(&home);
    home.index();
    let stdout = |args: &[&str]| {
        let out = home.run(args);
        assert!(out.status.success(), "{}", text(&out.stderr));
        text(&out.stdout)
    };

    // Before, the shared turn's three copies took both rows of `-n 2`; now the newest fork
    // shows it once, lists the other copies, and the page goes on to the older chat.
    let page = |args: &[&str]| -> Vec<Value> {
        stdout(args)
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    };
    let lines = page(&["overfit", "--json", "-n", "2"]);
    let rows: Vec<&str> = lines[1..]
        .iter()
        .map(|r| r["session"].as_str().unwrap())
        .collect();
    assert_eq!(rows, [PI_FORKS[1], PI_OLDER], "{lines:?}");
    let forks: Vec<&str> = lines[1]["forks"]
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["session"].as_str().unwrap())
        .collect();
    assert_eq!(forks, [PI_FORKS[0], PI_ROOT]);
    let handle = lines[1]["forks"][1]["handle"].as_str().unwrap();
    assert!(
        handle.starts_with(&format!("@{}:", &PI_ROOT[..8])),
        "{handle}"
    );
    assert!(lines[2].get("forks").is_none());
    assert_eq!(lines[0]["completeness"]["shown"], 4, "{lines:?}");
    // A page standing for every matching row is not cut.
    let lines = page(&["overfit", "--json"]);
    assert_eq!(lines.len(), 3, "{lines:?}");
    let completeness = &lines[0]["completeness"];
    assert_eq!(
        (
            &completeness["shown"],
            &completeness["total"],
            &completeness["total_basis"],
            &completeness["truncated"]
        ),
        (&json!(4), &json!(4), &json!("exact"), &json!(false)),
        "{lines:?}"
    );

    // The compact and classic pages mark the folded row; the shared turn shows once.
    let compact = stdout(&["overfit"]);
    assert_eq!(
        compact.matches("so it does not overfit").count(),
        1,
        "{compact}"
    );
    assert!(
        compact.contains(&format!("@{}:", &PI_FORKS[1][..8]))
            && compact.contains("(also in 2 forks)"),
        "{compact}"
    );
    assert!(
        compact.contains(&format!("@{}:", &PI_OLDER[..8])),
        "{compact}"
    );
    let classic = stdout(&["overfit", "--classic", "--color=always"]);
    assert_eq!(classic.matches("overfit\x1b").count(), 2, "{classic}");
    assert!(classic.contains(" also in 2 forks"), "{classic}");

    // A flat row counts the copies it stands for; `-l` and `-c` keep every chat.
    let flat = stdout(&["overfit", "--flat"]);
    let rows: Vec<&str> = flat
        .lines()
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(rows, [PI_FORKS[1], PI_OLDER], "{flat}");
    assert!(
        flat.lines().next().unwrap().ends_with(" (also in 2 forks)"),
        "{flat}"
    );
    assert_eq!(stdout(&["overfit", "-c"]), "4\n");
    let chats = stdout(&["overfit", "-l"]);
    for session in [PI_ROOT, PI_FORKS[0], PI_FORKS[1], PI_OLDER] {
        assert!(chats.contains(&session[..8]), "{chats}");
    }
}

/// Four more older chats, each with its own turn about overfitting, the youngest first.
fn pi_overfit_chats(home: &Home) -> Vec<String> {
    (0..4)
        .map(|i| {
            let id = format!("0dd0001{i}-0000-7000-8000-00000000001{i}");
            let day = 19 - i;
            home.write(
                &format!(".omp/agent/sessions/-work-chat/2026-08-{day}T10-00-00-000Z_{id}.jsonl"),
                &[
                    json!({"type":"session","version":3,"id":id,"timestamp":format!("2026-08-{day}T10:00:00.000Z"),"cwd":"/work/chat"}),
                    json!({"type":"message","id":"u1","parentId":null,"timestamp":format!("2026-08-{day}T10:00:01.000Z"),"message":{"role":"user","model":"m","provider":"p","content":[{"type":"text","text":format!("chat {i} worried the model would overfit")}]}}),
                ],
            );
            id
        })
        .collect()
}

/// `-n N` prints N rows in every output form, a row's fork copies counted on it rather
/// than printed as rows, and the shared turn folds alike at every `-n`: at `-n 1` its
/// copies rank below the page, which used to show it unfolded.
#[test]
fn every_form_prints_n_rows_and_folds_a_turn_alike_at_every_n() {
    let home = Home::new();
    pi_forks(&home);
    pi_older(&home);
    let chats = pi_overfit_chats(&home);
    home.index();
    let stdout = |profile: &str, args: &[&str]| {
        let out = home.run_in(profile, args);
        assert!(out.status.success(), "{}", text(&out.stderr));
        text(&out.stdout)
    };
    for n in [1usize, 2, 5] {
        let limit = n.to_string();
        let expected: Vec<&str> = [PI_FORKS[1]]
            .into_iter()
            .chain(chats.iter().map(String::as_str))
            .take(n)
            .collect();

        let lines: Vec<Value> = stdout("compact", &["overfit", "--json", "-n", &limit])
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        let rows: Vec<&str> = lines[1..]
            .iter()
            .map(|r| r["session"].as_str().unwrap())
            .collect();
        assert_eq!(rows, expected, "-n {n}: {lines:?}");
        let forks: Vec<&str> = lines[1]["forks"]
            .as_array()
            .unwrap_or_else(|| panic!("-n {n}: {lines:?}"))
            .iter()
            .map(|f| f["session"].as_str().unwrap())
            .collect();
        assert_eq!(forks, [PI_FORKS[0], PI_ROOT], "-n {n}");
        assert!(lines[2..].iter().all(|r| r.get("forks").is_none()));
        let completeness = &lines[0]["completeness"];
        assert_eq!(completeness["shown"], n + 2, "-n {n}: {completeness}");
        assert!(
            completeness["total"].as_u64().unwrap() >= n as u64 + 2,
            "-n {n}: {completeness}"
        );

        // Piped outside an agent profile, and `--flat` inside one, print TSV rows.
        for (profile, args) in [
            ("classic", vec!["overfit", "-n", &limit]),
            ("compact", vec!["overfit", "--flat", "-n", &limit]),
        ] {
            let flat = stdout(profile, &args);
            let rows: Vec<&str> = flat
                .lines()
                .map(|l| l.split('\t').next().unwrap())
                .collect();
            assert_eq!(rows, expected, "-n {n} {args:?}: {flat}");
            assert!(
                flat.lines().next().unwrap().ends_with(" (also in 2 forks)"),
                "-n {n} {args:?}: {flat}"
            );
            assert_eq!(flat.matches("also in").count(), 1, "{flat}");
        }

        // The terminal page: one turn line per row, the folded one marked.
        let classic = stdout("classic", &["overfit", "--color=always", "-n", &limit]);
        let turns = classic
            .lines()
            .filter(|l| l.starts_with("  \x1b[33m"))
            .count();
        assert_eq!(turns, n, "-n {n}: {classic}");
        assert_eq!(classic.matches(" also in 2 forks").count(), 1, "{classic}");

        let compact = stdout("compact", &["overfit", "-n", &limit]);
        assert_eq!(compact.lines().count(), n, "-n {n}: {compact}");
        let first = compact.lines().next().unwrap();
        assert!(
            first.contains(&format!("@{}:", &PI_FORKS[1][..8]))
                && first.contains("(also in 2 forks)"),
            "-n {n}: {compact}"
        );
    }
}

const CODEX_PARENT: &str = "01990a01-0001-7000-8000-000000000001";
const CODEX_CHILD: &str = "01990a02-0002-7000-8000-000000000002";
const CODEX_PLAIN_CHILD: &str = "01990a03-0003-7000-8000-000000000003";

fn codex_user(at: &str, body: &str) -> [Value; 2] {
    [
        json!({"type":"response_item","timestamp":at,"payload":{"type":"message","role":"user","content":[{"type":"input_text","text":body}]}}),
        json!({"type":"event_msg","timestamp":at,"payload":{"type":"user_message","message":body}}),
    ]
}
fn codex_reply(at: &str, body: &str) -> Value {
    json!({"type":"response_item","timestamp":at,"payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":body}]}})
}
fn codex_task(at: &str, agent: &str, body: &str) -> Value {
    json!({"type":"response_item","timestamp":at,"payload":{"type":"agent_message","author":"/root","recipient":agent,"content":[{"type":"input_text","text":format!("Message Type: NEW_TASK\nTask name: {agent}\nSender: /root\nPayload:\n{body}")}]}})
}

/// A Codex chat that forks a child partway, then goes on; the child's rollout replays the
/// parent's history up to the fork before its own task. A second child starts fresh.
fn codex_fork(home: &Home) {
    let dir = ".codex/sessions/2026/11/06";
    let mut parent = vec![
        json!({"timestamp":"2026-11-06T23:52:15.000Z","type":"session_meta","payload":{"id":CODEX_PARENT,"timestamp":"2026-11-06T23:52:15.000Z","cwd":"/work/product","source":"cli"}}),
    ];
    parent.extend(codex_user(
        "2026-11-06T23:52:20.000Z",
        "Build the locked product model from the protocol",
    ));
    parent.push(codex_reply(
        "2026-11-06T23:52:21.000Z",
        "Reading the locked model.",
    ));
    parent.extend(codex_user(
        "2026-11-06T23:55:00.000Z",
        "after the fork only the parent saw this",
    ));
    parent.push(codex_reply("2026-11-06T23:55:01.000Z", "ok"));
    home.write(
        &format!("{dir}/rollout-2026-11-06T18-52-15-{CODEX_PARENT}.jsonl"),
        &parent,
    );

    let spawn = |child: &str, forked: bool| {
        let mut meta = json!({"id":child,"timestamp":"2026-11-06T23:52:38.811Z","parent_thread_id":CODEX_PARENT,"cwd":"/work/product","thread_source":"subagent","source":{"subagent":{"thread_spawn":{"parent_thread_id":CODEX_PARENT,"agent_path":"/root/worker"}}}});
        if forked {
            meta["forked_from_id"] = json!(CODEX_PARENT);
        }
        json!({"timestamp":"2026-11-06T23:52:38.849Z","type":"session_meta","payload":meta})
    };
    let mut child = vec![
        spawn(CODEX_CHILD, true),
        json!({"timestamp":"2026-11-06T23:52:38.849Z","type":"session_meta","payload":{"id":CODEX_PARENT,"cwd":"/work/product","source":"cli"}}),
    ];
    child.extend(codex_user(
        "2026-11-06T23:52:38.849Z",
        "Build the locked product model from the protocol",
    ));
    child.push(codex_reply(
        "2026-11-06T23:52:38.849Z",
        "Reading the locked model.",
    ));
    child.push(codex_task(
        "2026-11-06T23:52:40.000Z",
        "/root/worker",
        "check the agent interfaces",
    ));
    child.push(codex_reply(
        "2026-11-06T23:52:41.000Z",
        "Interfaces checked.",
    ));
    home.write(
        &format!("{dir}/rollout-2026-11-06T18-52-38-{CODEX_CHILD}.jsonl"),
        &child,
    );

    let plain = vec![
        spawn(CODEX_PLAIN_CHILD, false),
        codex_task(
            "2026-11-06T23:52:40.000Z",
            "/root/worker",
            "audit the bridges",
        ),
        codex_reply("2026-11-06T23:52:41.000Z", "Bridges audited."),
    ];
    home.write(
        &format!("{dir}/rollout-2026-11-06T18-52-38-{CODEX_PLAIN_CHILD}.jsonl"),
        &plain,
    );
}

#[test]
fn chat_scope_of_a_codex_fork_child_covers_its_inherited_history() {
    let home = Home::new();
    codex_fork(&home);
    home.index();
    let count = |pattern: &str, chat: &str| {
        let out = home.run(&["search", pattern, "-c", "--chat", chat]);
        (
            text(&out.stdout).trim().to_owned(),
            out.status.code(),
            text(&out.stderr),
        )
    };
    let disclosure = format!(
        "--chat {CODEX_CHILD} is a fork: also searching the history it inherited from {CODEX_PARENT} (up to the fork)"
    );

    let (n, code, err) = count("Build the locked product", CODEX_CHILD);
    assert_eq!((n.as_str(), code), ("1", Some(0)), "{err}");
    assert!(err.contains(&disclosure), "{err}");

    // The parent's turns after the fork are not the child's, and a miss stays a miss.
    let (n, code, _) = count("after the fork only the parent", CODEX_CHILD);
    assert_eq!((n.as_str(), code), ("0", Some(1)));
    let (n, code, _) = count("check the agent interfaces", CODEX_CHILD);
    assert_eq!((n.as_str(), code), ("1", Some(0)));

    // A child that did not fork its parent's history inherits nothing.
    let (n, code, err) = count("Build the locked product", CODEX_PLAIN_CHILD);
    assert_eq!((n.as_str(), code), ("0", Some(1)));
    assert!(!err.contains("is a fork"), "{err}");

    // The rows are found under the parent, where they are indexed.
    let out = home.run(&[
        "search",
        "Build the locked product",
        "--json",
        "--chat",
        CODEX_CHILD,
    ]);
    let rows: Vec<Value> = text(&out.stdout)
        .lines()
        .skip(1)
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    assert_eq!(rows.len(), 1, "{}", text(&out.stdout));
    assert_eq!(rows[0]["session"], CODEX_PARENT);
    assert!(
        !text(&out.stderr).contains("is a fork"),
        "{}",
        text(&out.stderr)
    );
}
