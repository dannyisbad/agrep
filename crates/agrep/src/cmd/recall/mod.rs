mod page;
use crate::{ready, Recall};
use agrep_core::{
    caller::{self, Caller, Identity},
    evidence::{self, Request, Window},
    handles, hybrid,
    query::{Hit, Options, Results},
    render, semantic, store, surface,
};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::{
    collections::{BTreeSet, HashMap, HashSet},
    io::IsTerminal,
};
/// One chat's evidence: the first hit's head over every window of the chat.
struct Block {
    hit: Hit,
    window: Window,
    /// The turns the chat's windows centered on.
    hit_turns: BTreeSet<i64>,
    /// The query each merged window came from, in merge order.
    queries: Vec<usize>,
}
/// Bag-of-words scatter and sub-strong meaning matches: evidence that earns one line
/// rather than a transcript.
fn weak(h: &Hit) -> bool {
    scatter(h) || (h.matched == "semantic" && h.sem_weak)
}

fn family_key(h: &Hit, per_session: bool) -> &str {
    if per_session {
        &h.session
    } else {
        &h.family
    }
}
/// 0.4.9's `_weak_scatter`: a bag-of-words row, never strong evidence.
fn scatter(h: &Hit) -> bool {
    h.matched == "all-terms" || h.matched == "content-terms"
}
/// 0.4.9's `_meta_row`: a proven ~meta row, or stored provenance alone.
fn meta_row(h: &Hit) -> bool {
    h.meta_marker || agrep_core::query::meta_role(&h.who, h.turn)
}

mod command;
mod output;
mod selection;
mod windows;
pub(crate) use command::run;
use output::*;
use selection::*;
use windows::*;
#[cfg(test)]
mod tests {
    use super::*;
    use agrep_core::evidence::Row;

    fn coverage() -> semantic::Coverage {
        semantic::Coverage {
            embedded: 24,
            total: 24,
        }
    }
    fn pick(session: &str, turn: i64, matched: &str) -> Hit {
        Hit {
            session: session.into(),
            family: session.into(),
            turn,
            matched: matched.into(),
            who: "user".into(),
            snippet: format!("{session} {turn}"),
            ..Hit::default()
        }
    }
    // py/recall.py:891-932: a query without a meaning row takes its best one over its own
    // family's row, else over its weakest row once it holds two.
    #[test]
    fn packs_reserve_a_meaning_slot_per_query_as_0_4_9() {
        let picked = vec![
            (0, pick("s1", 0, "phrase")),
            (1, pick("s2", 0, "phrase")),
            (0, pick("s3", 0, "all-terms")),
            (1, pick("s4", 0, "phrase")),
        ];
        let meanings = vec![
            vec![pick("s2", 7, "semantic"), pick("s9", 1, "semantic")],
            vec![pick("s2", 5, "semantic")],
        ];
        let out: Vec<(usize, String, i64)> = reserve(picked.clone(), &meanings, 4, false)
            .into_iter()
            .map(|(q, h)| (q, h.session, h.turn))
            .collect();
        assert_eq!(
            out,
            [
                (0, "s1".to_owned(), 0),
                (1, "s2".to_owned(), 5),
                (0, "s9".to_owned(), 1),
                (1, "s4".to_owned(), 0),
            ]
        );
        let unchanged: Vec<_> = reserve(picked.clone(), &meanings, 2, false)
            .into_iter()
            .map(|(q, h)| (q, h.session))
            .collect();
        assert_eq!(
            unchanged,
            picked
                .into_iter()
                .map(|(q, h)| (q, h.session))
                .collect::<Vec<_>>()
        );
    }
    // Expected values are agrep 0.4.9's `recall --json` and `pack --json` output on a fixture
    // with 24 embedded rows, without the keys PORTING.md lists as not carried.
    #[test]
    fn semantic_envelope_matches_0_4_9() {
        let full = json!({"indexed":24,"total":24,"pending":0,"fraction":1.0,"complete":true,"order":"newest-first"});
        let ready = json!({"state":"ready","eligible":true,"reason":null,"min_score":0.82,"score_kind":"cosine","coverage":full,"complete":true,"fallback_recommended":false});
        assert_eq!(semantic::status(&coverage(), true), ready);
        let none = json!({"state":"no-confident-match","eligible":true,"reason":null,"min_score":0.82,"score_kind":"cosine","coverage":full,"complete":true,"fallback_recommended":false});
        let runs = vec![(
            "zebra quantum lighthouse marmalade".to_owned(),
            semantic::status(&coverage(), false),
            Some(coverage()),
        )];
        assert_eq!(
            Value::Object(semantic_meta(&runs, false, false, false)),
            json!({"score_kind":"cosine","semantic_coverage":full,"semantic_status":none})
        );
        let runs = vec![
            (
                "how do we rotate secret credentials".to_owned(),
                ready.clone(),
                Some(coverage()),
            ),
            ("retry backoff".to_owned(), ready.clone(), Some(coverage())),
        ];
        assert_eq!(
            Value::Object(semantic_meta(&runs, true, true, false)),
            json!({"score_kind":"cosine","semantic_coverage":full,
                "semantic_status":{"state":"ready","complete":true,"fallback_recommended":false},
                "semantic_queries":[
                    {"query":"how do we rotate secret credentials","status":ready,"coverage":full},
                    {"query":"retry backoff","status":ready,"coverage":full}],
                "hybrid":true})
        );
        // py/recall.py:2575-2580: meaning rows that lost the merge leave only the status.
        assert_eq!(
            Value::Object(semantic_meta(&runs[..1], false, false, true)),
            json!({"semantic_status":ready})
        );
        // A failed lane reports unavailable and its cause; PORTING.md lists removed fields.
        let error = anyhow::anyhow!("model SHA-256 mismatch: model.safetensors");
        let failed = vec![("q".to_owned(), semantic::failed(&error), None)];
        assert_eq!(
            Value::Object(semantic_meta(&failed, false, false, false)),
            json!({"score_kind":"unavailable","semantic_status":{
                "state":"unavailable","eligible":true,
                "reason":"model SHA-256 mismatch: model.safetensors","min_score":0.82,
                "score_kind":"unavailable","coverage":null,"complete":false,
                "fallback_recommended":true}})
        );
        // agrep 0.4.9's `recall -s --json foo_bar`: a refused query keeps the cosine kind
        // without a result set.
        let error = anyhow::Error::new(semantic::Rejected("identifier-query"));
        let refused = vec![("foo_bar".to_owned(), semantic::failed(&error), None)];
        assert_eq!(
            Value::Object(semantic_meta(&refused, false, false, false)),
            json!({"score_kind":"cosine","semantic_status":{
                "state":"query-rejected","complete":false,"fallback_recommended":true,
                "eligible":false,"reason":"identifier-query","min_score":0.82,"coverage":null}})
        );
    }
    fn row(session: &str, turn: i64, who: &str, text: &str) -> Row {
        Row {
            session: session.into(),
            turn,
            who: who.into(),
            ts: 1_700_000_000_000 + turn,
            text: text.into(),
            model: String::new(),
            model_source: String::new(),
            source: 1,
            source_chars: text.chars().count(),
        }
    }
    fn window(session: &str, center: i64, turns: std::ops::RangeInclusive<i64>) -> Window {
        Window {
            target: session.into(),
            session: session.into(),
            agent: "pi".into(),
            project: "/work/agrep".into(),
            title: String::new(),
            family: session.into(),
            side: false,
            center,
            radius: Some(1),
            latest: false,
            first: 0,
            last: 20,
            rows: turns
                .map(|turn| row(session, turn, "user", &format!("turn {turn}")))
                .collect(),
            events: vec![],
            notes: vec![],
            claim: None,
        }
    }

    // Expected values below were computed by agrep 0.4.9's own `_content_terms`,
    // `_water_level` and `_hit_floors` (py/search.py, py/recall.py).
    #[test]
    fn content_terms_water_levels_and_floors_match_0_4_9() {
        for (query, terms) in [
            (
                "how did the drafting go last run?",
                vec!["drafting", "go", "last", "run"],
            ),
            (
                "release-workflow_cache",
                vec!["release", "workflow", "cache"],
            ),
            ("a I it's", vec!["it's"]),
            ("Flaky (windows) cache!", vec!["Flaky", "windows", "cache"]),
            ("x ab", vec!["ab"]),
        ] {
            assert_eq!(evidence::content_terms(query), terms, "{query}");
        }
        for (extras, budget, level) in [
            (vec![5, 1, 9], 12, 6),
            (vec![100, 200, 300], 1000, 1000),
            (vec![50, 50], 30, 15),
            (vec![7], 3, 3),
            (vec![], 5, 5),
        ] {
            assert_eq!(water_level(&extras, budget), level, "{extras:?} {budget}");
        }
        for (texts, budget, floors) in [
            (vec![100, 200], 5000, vec![234, 334]),
            (vec![1500, 1500, 1500], 1000, vec![500, 500, 0]),
            (vec![800, 50, 3000], 900, vec![716, 184, 0]),
            (vec![300, 300, 300, 300], 600, vec![434, 0, 0, 0]),
            (vec![2000], 150, vec![150]),
        ] {
            let needs: Vec<usize> = texts
                .iter()
                .map(|n| (n + 10 + "user".len()).min(JUDGEABLE_BLOCK) + HEAD_COST)
                .collect();
            let heads = vec![HEAD_COST; needs.len()];
            let lows = vec![0; needs.len()];
            assert_eq!(
                hit_floors(&needs, &heads, &lows, budget),
                floors,
                "{texts:?} {budget}"
            );
        }
    }

    #[test]
    fn windows_of_one_chat_merge_into_one_block_with_gap_lines() {
        let hit = |session: &str, turn: i64| Hit {
            session: session.into(),
            turn,
            handle: format!("@{session}:{turn}.abcd"),
            agent: "pi".into(),
            project: "/work/agrep".into(),
            matched: "phrase".into(),
            ..Default::default()
        };
        let query_of = HashMap::from([
            ("@chat-a:10.abcd".to_owned(), 1),
            ("@chat-b:4.abcd".to_owned(), 0),
            ("@chat-a:2.abcd".to_owned(), 0),
        ]);
        let merged = merge_chats(
            vec![
                (hit("chat-a", 10), window("chat-a", 10, 8..=12)),
                (hit("chat-b", 4), window("chat-b", 4, 3..=5)),
                (hit("chat-a", 2), window("chat-a", 2, 1..=3)),
                (hit("chat-a", 12), window("chat-a", 12, 11..=13)),
            ],
            &query_of,
        );
        assert_eq!(merged.len(), 2);
        let b = &merged[0];
        assert_eq!(b.hit_turns, BTreeSet::from([2, 10, 12]));
        assert_eq!(b.queries, [1, 0, 0]);
        assert_eq!(
            b.window.rows.iter().map(|r| r.turn).collect::<Vec<_>>(),
            [1, 2, 3, 8, 9, 10, 11, 12, 13]
        );
        let queries = ["turn".to_owned(), "turn".to_owned()];
        let text = page::text(&page::Page::new(&merged[..1], &queries, 0), false).unwrap();
        assert!(
            text.contains(
                "     3 user: turn 3\n       [turns 4-7 not shown]\n     8 user: turn 8\n"
            ),
            "{text}"
        );
    }

    #[test]
    fn deepening_exhausted_prefixes_selects_like_full_rankings() {
        let mut state = 7u64;
        let mut next = move |n: u64| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) % n
        };
        let key = |selected: &[(usize, Hit)]| {
            selected
                .iter()
                .map(|(i, h)| (*i, h.session.clone(), h.turn))
                .collect::<Vec<_>>()
        };
        for _ in 0..400 {
            let families = 1 + next(6);
            let full: Vec<Vec<Hit>> = (0..1 + next(3))
                .map(|_| {
                    (0..next(40))
                        .map(|_| {
                            let session = next(10);
                            Hit {
                                session: format!("s{session}"),
                                family: format!("f{}", session % families),
                                turn: next(4) as i64,
                                ..Default::default()
                            }
                        })
                        .collect()
                })
                .collect();
            let target = 1 + next(9) as usize;
            for per_session in [false, true] {
                let expected = key(&select(&full, target, per_session).0);
                for depth in 1..=24 {
                    let mut buckets: Vec<Vec<Hit>> = full
                        .iter()
                        .map(|b| b[..depth.min(b.len())].to_vec())
                        .collect();
                    let mut deepen: Vec<Option<usize>> = (0..full.len())
                        .map(|i| (full[i].len() > depth).then_some(i))
                        .collect();
                    let selected =
                        select_deepening(&mut buckets, &mut deepen, target, per_session, |i| {
                            Ok(full[i].clone())
                        })
                        .unwrap();
                    assert_eq!(key(&selected), expected, "depth {depth} target {target}");
                }
            }
        }
    }
}
