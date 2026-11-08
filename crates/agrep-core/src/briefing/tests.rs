use super::pending::question_head;
use super::prose::{blocks, final_sentence, open_section, prose, section};
use super::todos::todos;
use super::*;
use rusqlite::Connection;
use serde_json::{json, Value};
use std::cell::OnceCell;

fn event(name: &'static str, state: Value) -> Event<'static> {
    Event {
        ts: 0,
        kind: "tool",
        name,
        ok: Some(true),
        child_session: String::new(),
        meta: String::new(),
        state: state.to_string(),
        input_chars: 0,
        input_end: None,
        output_start: None,
        blob: None,
        text: OnceCell::new(),
    }
}
fn open(events: &[Event]) -> Vec<String> {
    todos(&Connection::open_in_memory().unwrap(), events)
        .unwrap()
        .0
}
#[test]
fn whole_lists_and_partial_merges_preserve_fields() {
    let initial = event(
        "todo_write",
        json!({"todos":[{"id":"a","content":"keep name","status":"in_progress"},{"id":2,"content":"second","status":"pending"}]}),
    );
    let merge = event(
        "todo_write",
        json!({"merge":true,"todos":[{"id":"a","status":"completed"}]}),
    );
    assert_eq!(open(&[initial, merge]), vec!["second"]);
    assert_eq!(
        open(&[event(
            "write_todos",
            json!({"todos":[{"description":"Gemini task","status":"In Progress"}]})
        )]),
        vec!["Gemini task"]
    );
}
#[test]
fn phase_operations_and_failed_updates_are_evidence() {
    let init = || {
        event(
            "todo",
            json!({"list":[{"phase":"build","items":["compile","test"]}]}),
        )
    };
    let done = event("todo", json!({"op":"done","phase":"build"}));
    assert!(open(&[init(), done]).is_empty());
    let mut failed = event("todo", json!({"op":"done","phase":"build"}));
    failed.ok = Some(false);
    assert_eq!(open(&[init(), failed]), vec!["compile", "test"]);
}
// Expected values are 0.4.9's own helpers in py/summary.py.
#[test]
fn completed_checklists_and_later_work_do_not_reopen() {
    assert_eq!(
        section("Next steps:\n- [x] done\n- None"),
        Some(("next steps".into(), vec![], 1))
    );
    let text = "Next steps:\n- run tests All tests passed.";
    let parts = blocks(text, &[23]);
    assert_eq!(parts, ["Next steps:\n- run tests", "All tests passed."]);
    assert!(open_section(&parts, &[1]).is_none());
    assert_eq!(
        section(
            "## Follow-ups\n- check `x` (done)\n- [ ] wire the CLI. Let me know if you want more."
        ),
        Some(("follow-ups".into(), vec!["wire the CLI.".into()], 1))
    );
    assert_eq!(
        final_sentence(&prose(
            "Done.\n\nSee https://x.y/z?a=1 for **details**. Shall we ship it?"
        )),
        "Shall we ship it?"
    );
    let asked = question_head;
    assert_eq!(
        asked(
            r#"{"questions":[{"question":"Pick one?"}]}"#,
            "AskUserQuestion"
        ),
        "Pick one?"
    );
    assert_eq!(
        asked(r#"{"plan": "step one\nstep tw"#, "ExitPlanMode"),
        "step one step tw"
    );
    assert_eq!(asked(r#"{"x": 1"#, "ExitPlanMode"), "presented a plan");
    assert_eq!(
        repr("I'll check the 'legacy' schema \"before\" that."),
        r#"'I\'ll check the \'legacy\' schema "before" that.'"#
    );
    assert_eq!(repr("it's\u{200d}\u{a0}é"), r#""it's\u200d\xa0é""#);
}
#[test]
fn interval_union_counts_overlap_once() {
    assert_eq!(
        merge(vec![(0, 20), (10, 30), (30, 40), (50, 50)]),
        vec![(0, 40)]
    );
}
