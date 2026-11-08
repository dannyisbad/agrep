use super::*;
use crate::store;
use rusqlite::params;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::data::DataRoot;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    };

    #[test]
    fn a_window_reads_controls_without_materializing_unrelated_tool_rows() {
        let mut work = Vec::new();
        for count in [2048, 8192] {
            let temp = tempfile::tempdir().unwrap();
            let root = DataRoot::open(temp.path(), true).unwrap();
            let mut c = store::writer(&root).unwrap();
            let tx = c.transaction().unwrap();
            tx.execute_batch("INSERT INTO sources VALUES('fixture','pi',0,0,x'02',NULL); INSERT INTO event_sessions VALUES(1,'pi','selected'),(2,'pi','unrelated'); INSERT INTO symbols VALUES(1,'tool'),(2,'shell'),(3,'control'),(4,'compaction');").unwrap();
            tx.execute(
                "INSERT INTO tool_blobs(id,digest,body) VALUES(1,X'01',?)",
                [store::encode_tool("shell: needle").unwrap()],
            )
            .unwrap();
            for id in 1..=count {
                tx.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,input_end,project_key,blob) VALUES(?1,?1,1,?2,?3,1,2,?1,?4,?4,6,0,0,'','','',13,0,1)", params![id, if id <= 2 {1} else {2}, id.to_string(), id-1]).unwrap();
            }
            tx.execute_batch("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key) VALUES(90000,90000,1,1,'control',3,4,7,1,1,0,0,0,'','','marker',0)").unwrap();
            tx.commit().unwrap();
            let operations = Arc::new(AtomicUsize::new(0));
            let recorded = operations.clone();
            c.progress_handler(
                100,
                Some(move || {
                    recorded.fetch_add(100, Ordering::Relaxed);
                    false
                }),
            );
            let events = load_events(&c, "selected", 0, 1).unwrap();
            assert_eq!(events.len(), 3);
            let control = events.iter().find(|e| e.kind == "control").unwrap();
            assert_eq!((control.ts, control.turn), (7, 1));
            assert_eq!(events.iter().filter(|e| e.input == "needle").count(), 2);
            work.push(operations.load(Ordering::Relaxed));
        }
        assert!(work[1] <= work[0] * 2 + 100, "{work:?}");
    }
}
#[cfg(test)]
mod weak_snippet_tests {
    fn weak(text: &str, fallback: &str, query: &str) -> String {
        let anchors = super::anchor_patterns(query);
        super::weak_snippet(text, fallback, &anchors.iter().collect::<Vec<_>>())
    }
    // Expected lines were rendered by agrep 0.4.9's `_weak_line` (py/recall.py).
    #[test]
    fn weak_snippets_anchor_like_0_4_9() {
        let text = format!(
            "intro {}the Flaky   Windows cache broke again {}",
            "filler word ".repeat(20),
            "tail word ".repeat(20)
        );
        assert_eq!(
            weak(&text, "", "flaky windows cache"),
            "…er word filler word filler word filler word filler word the Flaky Windows cache broke again tail word tail word tail word tail word tail wo…"
        );
        assert_eq!(
            weak(
                &"no match here ".repeat(20),
                "snippet mentions windows twice windows",
                "windows"
            ),
            "snippet mentions windows twice windows"
        );
        assert_eq!(weak("short text", "", "zzz"), "short text");
    }
}
#[cfg(test)]
mod parity_tests {
    use super::*;
    // Expected values were produced by agrep 0.4.9's own `_cap` and `_tool_line` with
    // `_selected_tool_match` (py/around.py) and `_anchored_cap` with `_anchor_patterns`
    // (py/recall.py).
    #[test]
    fn caps_and_anchored_caps_match_0_4_9() {
        let words: Vec<String> = (0..80).map(|i| format!("w{i:03}")).collect();
        let words = words.join(" ");
        // A space exactly 200 characters before the limit still ends the kept text.
        let edge = format!("{} {}", "a".repeat(50), "b".repeat(250));
        assert_eq!(
            cap(&edge, 250, "CMD"),
            (format!("{} [+251 chars - CMD]", "a".repeat(50)), 251)
        );
        assert_eq!(
            cap(&words, 120, "CMD"),
            ("w000 w001 w002 w003 w004 w005 w006 w007 w008 w009 w010 w011 w012 w013 w014 w015 w016 w017 w018 w019 w020 w021 w022 w023 [+280 chars - CMD]".into(), 280)
        );
        assert_eq!(
            cap(&words, 60, "CMD"),
            (
                "w000 w001 w002 w003 w004 w005 w006 w007 w008 w009 w010 w011 [+340 chars - CMD]"
                    .into(),
                340
            )
        );
        let anchors = anchor_patterns("copper falcon migration");
        let anchors: Vec<&Anchor> = anchors.iter().collect();
        let long = format!(
            "{}the copper falcon migration stalled {} copper falcon migration again {}",
            "lead ".repeat(60),
            "tail ".repeat(200),
            "end ".repeat(100)
        );
        let lead = |n: usize| "lead ".repeat(n).trim_end().to_owned();
        let tail = |n: usize| "tail ".repeat(n).trim_end().to_owned();
        assert_eq!(
            anchored_cap(&long, 500, "CMD", &anchors),
            (format!("[210 chars] {} the copper falcon migration stalled {} [826 chars] {}  copper falcon migration again {} [313 chars] [snippet - open: CMD]", lead(18), tail(17), tail(18), "end ".repeat(22).trim_end()), 1349)
        );
        assert_eq!(
            anchored_cap(&long, 260, "CMD", &anchors),
            (format!("[+260 chars - CMD] {} the copper falcon migration stalled {} [+1,252 chars - CMD]", lead(8), tail(36)), 1512)
        );
        let late = format!("{}falcon here {}", "x ".repeat(300), "y ".repeat(50));
        assert_eq!(
            anchored_cap(&late, 200, "CMD", &anchors),
            (
                format!(
                    "[+554 chars - CMD] {}falcon here {}",
                    "x ".repeat(23),
                    "y ".repeat(50)
                ),
                554
            )
        );
    }
    #[test]
    fn selected_tool_events_render_0_4_9_lines() {
        let output = format!(
            "line one\n{}ERROR copper falcon migration timeout\n{}",
            "pad ".repeat(40),
            "tail ".repeat(30)
        );
        let text = format!("bash [failed]: ./run --step 3\n{}", output.trim_end());
        let event = Event {
            turn: 3,
            ts: 1_700_000_000_000,
            name: "bash".into(),
            kind: "tool".into(),
            input: "./run --step 3".into(),
            output: output.trim_end().into(),
            input_chars: 14,
            output_chars: output.chars().count() + 1,
            output_bytes: Some(output.len() + 1),
            ok: Some(false),
            identity: crate::handles::event_id("sess-1", 1_700_000_000_000, &text),
            raw: serde_json::json!({"output_start": 30}),
            text,
        };
        let id = "71ab9121ac178bea371c414e";
        assert_eq!(event.identity, id);
        let found = selected_tool_match(&event, Some(id), Some((205, 228)));
        let pads = |n: usize| "pad ".repeat(n);
        assert_eq!(
            found.preview.as_deref(),
            Some(
                format!(
                    "…d {}ERROR copper falcon migration timeout {}t…",
                    pads(13),
                    "tail ".repeat(10)
                )
                .as_str()
            )
        );
        assert_eq!(
            tool_line(&event, found.output_span, found.preview.as_deref()),
            format!("  ✗ FAILED bash ./run --step 3 … {}ERROR copper falcon migration timeout {}… (358B)", pads(15), "tail ".repeat(12).trim_end())
        );
        let input = selected_tool_match(&event, Some(id), Some((17, 20)));
        assert_eq!(
            tool_line(&event, input.output_span, input.preview.as_deref()),
            format!("  ✗ FAILED bash …run · line one {}… (358B)", pads(27))
        );
        assert!(!selected_tool_match(&event, Some("other"), Some((17, 20))).selected);
    }
}
