use super::*;

#[cfg(test)]
mod tests {
    use super::{
        cold_prior, decut_text, normalize_token, prepare_query, query_tokens, term_anchor,
        term_variants, BoundaryScore, Stats,
    };

    #[test]
    fn aligned_ascii_shortcut_preserves_unicode_quality() {
        for token in ["error", "hi", "release workflow", "don't"] {
            let query = prepare_query(token, &Stats::new());
            for left in ["", " ", "a", "界", "e\u{301}", "\u{200d}", "'", "("] {
                for right in ["", " ", "a", "界", "\u{301}", "\u{200d}", "'s", ")"] {
                    let text = format!("界 {left}{token}{right} end");
                    let expected = query.evaluate_unicode(&text, None, true).unwrap();
                    let actual = query.evaluate_compact(&text, None, true).unwrap();
                    assert_eq!(actual.match_class, expected.match_class, "{text:?}");
                    assert!((actual.factor - expected.factor).abs() < 1e-14, "{text:?}");
                }
            }
        }
    }

    #[test]
    fn query_shape_and_normalization_match_python() {
        assert_eq!(
            query_tokens(" cyber-filter_thing "),
            ["cyber", "filter", "thing"]
        );
        assert_eq!(normalize_token("Straße"), "strasse");
        assert_eq!(normalize_token("e\u{301}"), "é");
        assert_eq!(cold_prior("id"), 0.90);
        assert_eq!(cold_prior("東京"), 0.0);
    }

    #[test]
    fn evaluates_code_boundaries() {
        let score = prepare_query("http", &Stats::new())
            .evaluate("myHTTPServer", None, true)
            .unwrap();
        assert_eq!(score.match_class, "aligned");
        assert_eq!(score.spans, [Some([2, 6])]);
    }

    #[test]
    fn folds_only_minimal_s_es_and_ies_variants() {
        // Mirrors py/test_boundary_rank.py
        // test_term_variants_are_minimal_and_keep_short_terms_exact word for word.
        let expected: &[(&str, &[&str])] = &[
            ("calls", &["calls", "call"]),
            ("call", &["call", "calls"]),
            ("policies", &["policies", "policy"]),
            ("policy", &["policy", "policies"]),
            ("status", &["status", "statuses"]),
            ("statuses", &["statuses", "status"]),
            ("boxes", &["boxes", "box"]),
            ("glass", &["glass", "glasses"]),
            ("branches", &["branches", "branch"]),
            ("houses", &["houses", "house"]),
            ("cases", &["cases", "case"]),
            ("tries", &["tries", "try"]),
            ("days", &["days"]),
            ("this", &["this"]),
            ("does", &["does"]),
            ("try", &["try"]),
            ("don", &["don"]),
            ("t", &["t"]),
            ("id", &["id"]),
            ("Straße", &["strasse", "strasses"]),
            ("東京", &["東京"]),
        ];
        for (token, variants) in expected {
            assert_eq!(term_variants(token), *variants, "{token}");
        }
        assert_eq!(term_anchor("policies"), "polic");
        assert_eq!(term_anchor("tries"), "tries");

        let score = prepare_query("don calls", &Stats::new())
            .evaluate("calls dont", None, true)
            .unwrap();
        assert_eq!(score.spans, [Some([6, 9]), Some([0, 5])]);
        assert_eq!(score.qualities, [0.5, 1.0]);
    }

    #[test]
    fn python_oracle_matches() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/boundary_conformance.json");
        let fixture: serde_json::Value =
            serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        assert_eq!(fixture["schema"], 2);
        assert_eq!(fixture["python_unicode_version"], "16.0.0");
        for case in fixture["evaluations"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let stats: Stats = case
                .get("stats")
                .cloned()
                .map(serde_json::from_value)
                .transpose()
                .unwrap()
                .unwrap_or_default();
            let spans = case
                .get("spans")
                .cloned()
                .map(serde_json::from_value::<Vec<Option<[i64; 2]>>>)
                .transpose()
                .unwrap();
            let result = prepare_query(case["query"].as_str().unwrap(), &stats).evaluate(
                case["text"].as_str().unwrap(),
                spans.as_deref(),
                case.get("validate_spans")
                    .and_then(|value| value.as_bool())
                    .unwrap_or(true),
            );
            let expected = &case["expected"];
            if let Some(error) = expected.get("error") {
                assert_eq!(
                    result.unwrap_err().to_string(),
                    error.as_str().unwrap(),
                    "{name}"
                );
                continue;
            }
            assert_score(name, &result.unwrap(), expected);
        }
    }

    #[test]
    fn compact_path_matches_full_unicode_scoring() {
        let chunks = [
            "a", "A", "b", "_", "'", "é", "e\u{301}", "😀", "東京", "ß", "\u{200d}", "-",
        ];
        let queries = ["a", "ab", "e", "ss", "id", "東京"];
        let mut state = 0x9e37_79b9_u32;
        for case in 0..500 {
            let mut text = String::new();
            for _ in 0..12 {
                state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                text.push_str(chunks[(state as usize) % chunks.len()]);
            }
            let query = queries[case % queries.len()];
            let prepared = prepare_query(query, &Stats::new());
            let full = prepared.evaluate(&text, None, true).unwrap();
            let compact = prepared.evaluate_compact(&text, None, true).unwrap();
            assert!(
                (full.factor - compact.factor).abs() <= 1e-12,
                "query={query:?} text={text:?}"
            );
            assert_eq!(full.match_class, compact.match_class);
        }

        let prepared = prepare_query("a", &Stats::new());
        let full = prepared.evaluate("a\u{301}", None, true).unwrap();
        let compact = prepared.evaluate_compact("a\u{301}", None, true).unwrap();
        assert!(!full.matched);
        assert_eq!(compact.factor, full.factor);
        assert_eq!(compact.match_class, full.match_class);
    }

    #[test]
    fn separator_fast_path_matches_full_boundary_scores() {
        for query in ["hi", "error", "foo bar", "12"] {
            let prepared = prepare_query(query, &Stats::new());
            for separator in [
                '…', '—', '–', '\u{00a0}', '“', '”', '\u{ff07}', '℃', '™', '\u{200d}', '\u{2019}',
            ] {
                for text in [
                    format!("this{separator}hi errorCode foo{separator}bar 12"),
                    format!("{separator}error{separator}"),
                    format!("shi{separator}pper"),
                ] {
                    let full = prepared.evaluate(&text, None, true).unwrap();
                    let compact = prepared.evaluate_compact(&text, None, true).unwrap();
                    assert_eq!(full.factor, compact.factor, "{query}: {text}");
                    assert_eq!(full.match_class, compact.match_class, "{query}: {text}");
                }
            }
        }
    }
    #[test]
    fn native_decut_matches_snippet_edge_rules() {
        assert_eq!(decut_text("plain", true), "plain");
        assert_eq!(decut_text("a…b", false), "aab");
        assert_eq!(decut_text("……x", false), " xx");
        assert_eq!(decut_text("…abc", true), "aaabc");
        assert_eq!(decut_text("abc…", true), "abccc");
        assert_eq!(decut_text("…a…", true), "aaaaa");
    }

    fn assert_score(name: &str, actual: &BoundaryScore, expected: &serde_json::Value) {
        let expected_factor = expected["factor"].as_f64().unwrap();
        assert!(
            (actual.factor - expected_factor).abs() <= 1e-12,
            "{name}: factor"
        );
        assert_eq!(
            actual.qualities,
            serde_json::from_value::<Vec<f64>>(expected["qualities"].clone()).unwrap(),
            "{name}"
        );
        assert_eq!(
            actual.match_class,
            expected["match_class"].as_str().unwrap(),
            "{name}"
        );
        assert_eq!(
            actual.matched,
            expected["matched"].as_bool().unwrap(),
            "{name}"
        );
        assert_eq!(
            actual.spans,
            serde_json::from_value::<Vec<Option<[i64; 2]>>>(expected["spans"].clone()).unwrap(),
            "{name}"
        );
    }
}

#[cfg(test)]
mod normalization_stream_tests {
    use super::*;
    #[test]
    fn ascii_runs_preserve_every_custom_cluster_boundary() {
        let cases = [
            "",
            "é",
            "e\u{301}",
            "\u{301}",
            "\u{200d}X",
            "X\u{200d}Y\u{301}",
            "\u{1100}\u{1161}\u{11a8}",
            "ß",
            "ﬀ",
            "中",
            "\u{e000}",
            "👩\u{200d}💻",
            "a\u{fe0f}",
        ];
        for left in cases {
            for right in cases {
                let text = format!("ASCII_AbC{left}DEfg{right} HIJK");
                let expected = clusters(&text)
                    .into_iter()
                    .map(|c| {
                        let compatible: String = text[c.start_byte..c.end_byte].nfkc().collect();
                        unicode_v16::case_fold(&compatible)
                    })
                    .collect::<String>();
                assert_eq!(normalize_token(&text), expected, "{text:?}");
            }
        }
    }
}

#[cfg(test)]
mod separator_span_tests {
    use super::*;
    #[test]
    fn separator_fast_path_preserves_character_spans_and_full_scores() {
        for separator in ["—", "…", "\u{a0}", "\u{2028}", "★", "→"] {
            for text in [
                format!("{separator}error{separator}workflow"),
                format!("xerror{separator}HTTPWorkflow{separator}error"),
                format!("ERROR{separator}workflows{separator}"),
            ] {
                let q = prepare_query("error workflow", &Stats::new());
                let fast = q.evaluate(&text, None, true).unwrap();
                let full = q.evaluate_unicode(&text, None, true).unwrap();
                assert_eq!(fast, full, "{text}");
            }
        }
    }
}
