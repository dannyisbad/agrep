use super::*;

#[cfg(test)]
mod snippet_tests {
    #[test]
    fn meaning_snippets_keep_control_characters_for_the_sanitizer() {
        let snippet = super::snippet("alpha \x1b[31mred\x07 beta", "red", 280);
        assert_eq!(snippet, "alpha \x1b[31mred\x07 beta");
        assert_eq!(
            crate::surface::safe(&snippet, false),
            r"alpha \u001b[31mred\u0007 beta"
        );
    }
}
#[cfg(test)]
mod meaning_score_tests {
    use super::{meaning_score, Hit};
    // 0.4.9's `_score` with a full match: recency halving every 14 days, a user floor of
    // 0.35, speaker weight times source scale, and 0.45 for stored meta rows
    // (py/search.py:1218-1222, 1397, 1817-1876). 0.4.9's `recall --json` scored an agent
    // meaning row 18.001 days old 0.3281.
    #[test]
    fn meaning_rows_score_by_the_keyword_formula_as_0_4_9() {
        let day = 86_400_000;
        let now = 1_800_000_000_000;
        let row = |who: &str, turn: i64, age: f64| Hit {
            who: who.into(),
            turn,
            ts: now - (age * day as f64) as i64,
            score: 0.9,
            matched: "semantic".into(),
            ..Hit::default()
        };
        assert_eq!(meaning_score(&row("agent", 1, 14.), now), 0.4);
        assert_eq!(meaning_score(&row("agent", 1, 18.001), now), 0.3281);
        assert_eq!(meaning_score(&row("user", 1, 70.), now), 0.35);
        assert_eq!(meaning_score(&row("tool", 1, 0.), now), 0.22);
        assert_eq!(meaning_score(&row("subagent", 0, 0.), now), 0.3251);
        assert_eq!(meaning_score(&row("harness", 3, 0.), now), 0.135);
    }
}
#[cfg(test)]
mod handle_tests {
    #[test]
    fn opaque_session_tokens_are_urlsafe_utf8_without_padding() {
        assert_eq!(
            super::session_target("valid-session").unwrap(),
            "@valid-session"
        );
        assert_eq!(super::session_target("a:b/é").unwrap(), "@~YTpiL8Op");
        assert!(super::session_target("").is_err());
        assert!(super::session_target(&"x".repeat(800)).is_err());
    }
}
#[cfg(test)]
mod repeated_call_tests {
    use super::*;
    /// Parallel calls `b` and `a` share their session, turn, time and text. Each page names
    /// the repeat `b` by a selector of its own; a page ranked again reads only rows the
    /// pages before it lacked.
    #[test]
    fn a_page_ranked_again_reads_only_its_new_rows() {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        c.execute_batch("INSERT INTO event_sessions VALUES(1,'pi','s')")
            .unwrap();
        for (id, call) in [(1, "b"), (2, "a")] {
            c.execute("INSERT INTO event_rows(id,tool_id,source,session_key,call_id,kind_key,name_key,ts,native_turn,position,input_chars,output_chars,output_bytes,child_session,meta,state,project_key,blob) VALUES(?1,?1,1,1,?2,0,0,5,0,0,0,0,0,'','','',0,10)", rusqlite::params![id, call]).unwrap();
        }
        let hit = |id| {
            let event = crate::handles::event_id("s", 5, "same");
            Hit {
                rowid: id,
                session: "s".into(),
                who: "tool".into(),
                ts: 5,
                text: "same".into(),
                handle: row_handle("s", 0, "same", None, Some(&event)).unwrap(),
                event: Some(event),
                ..Hit::default()
            }
        };
        let repeat = crate::handles::repeat_event_id("s", 5, "same", "b");
        let repeat_handle = row_handle("s", 0, "same", None, Some(&repeat)).unwrap();
        let mut checked = HashMap::new();
        let mut page = vec![hit(2)];
        name_repeated_calls(&c, &mut page, &mut checked).unwrap();
        assert_eq!(page[0].handle, hit(2).handle);
        let mut deeper = vec![hit(2), hit(1)];
        name_repeated_calls(&c, &mut deeper, &mut checked).unwrap();
        assert_eq!(deeper[0].handle, hit(2).handle);
        assert_eq!(deeper[1].event.as_deref(), Some(repeat.as_str()));
        assert_eq!(deeper[1].handle, repeat_handle);
        // Without the index every read fails.
        c.execute_batch("DROP INDEX event_position").unwrap();
        let mut again = vec![hit(1), hit(2)];
        name_repeated_calls(&c, &mut again, &mut checked).unwrap();
        assert_eq!(again[0].handle, repeat_handle);
        assert_eq!(again[1].handle, hit(2).handle);
    }
}
#[cfg(test)]
mod window_tests {
    use super::*;
    fn evaluate(pattern: &str, text: &str, phase: u8, partial: bool) -> Scored {
        let options = Options {
            pattern: pattern.into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 10,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: false,
            filters: Filters::default(),
        };
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let mut m = Matcher::new(&options, &c).unwrap();
        (m.phase, m.partial) = (phase, partial);
        if partial {
            m.content(pattern).unwrap();
        }
        m.evaluate(text, "tool", 1, 0, 0, None).unwrap().unwrap()
    }
    // The 0.4.9 CLI returns the combining-mark row as a phrase and the three
    // letter rows as all-terms for `kit cafe` on this four-row pi fixture.
    #[test]
    fn case_keys_do_not_turn_greek_letters_into_phrase_separators() {
        for (text, tier) in [
            ("kitΙcafe", 1),
            ("kitιcafe", 1),
            ("kitιcafe", 1),
            ("kit\u{345}cafe", 0),
        ] {
            assert_eq!(evaluate("kit cafe", text, 0, false).tier, tier, "{text:?}");
        }
    }

    // Expected spans were produced by agrep 0.4.9's own matchers: the `[\W_]*` phrase
    // search, `console.insensitive_span` with and without `_keyword_term_specs` variants,
    // and `corpusdb.content`'s earliest lowered occurrence.
    #[test]
    fn hit_spans_follow_0_4_9_match_spans() {
        let text = "bash [failed]: ./run-migration --step 3\nERROR copper falcon migration timeout; copper-falcon_migration";
        let span = |pattern: &str, phase: u8, partial: bool| {
            let span = evaluate(pattern, text, phase, partial).span.unwrap();
            (
                text[..span.start].chars().count(),
                text[..span.end].chars().count(),
            )
        };
        assert_eq!(span("copper falcon migration", 1, false), (46, 69));
        assert_eq!(span("falcon migrations copper", 2, false), (21, 30));
        assert_eq!(span("migration", 1, false), (21, 30));
        assert_eq!(span("timeout copper", 0, true), (46, 52));
    }
}
#[cfg(test)]
mod regex_bound_tests {
    use super::*;
    fn groups(groups: &[&[&str]], single: bool) -> Vec<(Vec<String>, bool)> {
        groups
            .iter()
            .map(|g| (g.iter().map(|s| s.to_string()).collect(), single))
            .collect()
    }
    #[test]
    fn required_literals_come_from_both_ends() {
        assert_eq!(
            literal_terms("Release.*workflow"),
            groups(&[&["release"], &["workflow"]], true)
        );
        assert_eq!(literal_terms("release"), groups(&[&["release"]], true));
        assert!(literal_terms("(?<=a)b").is_empty());
    }
    #[test]
    fn alternations_require_literal_sets() {
        assert_eq!(
            literal_terms("release|workflow"),
            groups(&[&["release", "workflow"]], false)
        );
        // A spelling holding another adds nothing to its set.
        assert_eq!(
            literal_terms("work(tree|flow)s?"),
            groups(&[&["workflow", "worktree"]], false)
        );
        assert_eq!(
            literal_terms("colou?r"),
            groups(&[&["color", "colour"]], false)
        );
        assert_eq!(literal_terms("(foo)?bar"), groups(&[&["bar"]], false));
        // Case variants fold to one spelling, still from several literals.
        assert_eq!(literal_terms("(?i)ab"), groups(&[&["ab"]], false));
        assert!(literal_terms("a?").is_empty());
        let many: Vec<String> = (0..65).map(|i| format!("w{i:02}x")).collect();
        assert!(literal_terms(&many.join("|")).is_empty());
    }
    // The extractor keeps 100 bytes of a literal, cutting this 141-byte sentence inside a
    // character; a literal holding the cut character's remains admitted no row.
    #[test]
    fn literals_cut_by_the_extractor_keep_whole_characters() {
        let sentence = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。何でも薄暗いじめじめした所で";
        for text in [
            sentence.to_owned(),
            format!("a{}b", "É".repeat(60)),
            format!("a{}b", "🚀".repeat(30)),
        ] {
            let folded = store::search_fold(&text);
            let found = literal_terms(&text);
            assert_eq!(found.len(), 2, "{text}");
            let (prefix, suffix) = (&found[0].0[0], &found[1].0[0]);
            assert!(found
                .iter()
                .all(|(group, single)| group.len() == 1 && *single));
            assert!(
                folded.starts_with(prefix.as_str()) && prefix.len() > 96,
                "{prefix}"
            );
            assert!(
                folded.ends_with(suffix.as_str()) && suffix.len() > 96,
                "{suffix}"
            );
        }
    }
    #[test]
    fn only_a_greedy_dot_run_after_a_nonempty_part_matches_once() {
        for (pattern, once) in [
            ("release.*workflow", true),
            ("(release.*workflow)", true),
            ("release.+", true),
            ("release.*?workflow", false),
            (".*workflow", false),
            ("release.{0,9}workflow", false),
            ("release[^x]*workflow", false),
            ("release.*workflow|cache", false),
            ("(?s)release.*workflow", true),
        ] {
            assert_eq!(matches_once(pattern), once, "{pattern}");
        }
        // Any later match would have offered the first match's `.*` a later end.
        let re = fancy_regex::Regex::new("(?i:release.*workflow)").unwrap();
        let snippet = "release a workflow … release b workflow release c";
        assert_eq!(re.find_iter(snippet).count(), 1);
    }
}
#[cfg(test)]
mod chat_scope_tests {
    use super::*;
    fn plan(c: &Connection, table: &str, filters: &Filters) -> Vec<String> {
        let (scope, values) = sql_filters(filters, "p", c).unwrap();
        c.prepare(&format!(
            "EXPLAIN QUERY PLAN SELECT p.id FROM {table} p WHERE 1{scope}"
        ))
        .unwrap()
        .query_map(params_from_iter(values), |r| r.get(3))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
    }
    // The selective-scope probe, which a LIKE on the rows turned into a full scan, and
    // which an agent or time index outranks without statistics.
    #[test]
    fn session_scopes_read_rows_through_the_session_indexes() {
        let temp = tempfile::tempdir().unwrap();
        let root = crate::data::DataRoot::open(temp.path().join("data"), true).unwrap();
        let c = store::writer(&root).unwrap();
        c.execute_batch(
            "INSERT INTO sessions VALUES('pi','chat-one','p','','','chat-one',0,'',0,0);
             INSERT INTO sessions VALUES('codex','chat-two','p','','','chat-two',0,'',0,0);",
        )
        .unwrap();
        let chat = Filters {
            chat: Some("CHAT-O".into()),
            ..Filters::default()
        };
        assert_eq!(
            sql_filters(&chat, "p", &c).unwrap().1,
            [Value::Text(r#"["chat-one"]"#.into())]
        );
        let narrowed = Filters {
            agent: Some("pi".into()),
            project: Some("p".into()),
            since: Some(1),
            until: Some(2),
            ..chat.clone()
        };
        let rows = Filters {
            chat: None,
            session_rows: Some(SessionRows {
                session: "chat-one".into(),
                from: Some(3),
            }),
            ..narrowed.clone()
        };
        for (filters, tool_rows) in [
            (&chat, "(session_key=?)"),
            (&narrowed, "(session_key=?)"),
            (&rows, "(session_key=? AND position>?)"),
        ] {
            let prose = plan(&c, "prose", filters);
            assert!(
                prose
                    .iter()
                    .any(|step| step.contains("INDEX prose_session (session=?)")),
                "{prose:?}"
            );
            assert!(!prose.iter().any(|step| step.starts_with("SCAN p")));
            let tools = plan(&c, "tools", filters);
            for step in [
                "INDEX event_sessions_session (session=?)".to_owned(),
                format!("INDEX event_position {tool_rows}"),
            ] {
                assert!(tools.iter().any(|s| s.contains(&step)), "{tools:?}");
            }
        }
        let none = Filters {
            chat: Some("chat-one%".into()),
            ..Filters::default()
        };
        assert_eq!(sql_filters(&none, "p", &c).unwrap().0, " AND 0");
    }
}
#[cfg(test)]
mod matcher_cache_tests {
    use super::*;
    #[test]
    fn reused_all_terms_spans_match_snippet_regrading() {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        for pattern in [
            "flaky windows cache",
            "how did the drafting go last run",
            "caches window flaky",
            "oo book",
            "cache cache",
        ] {
            let o = Options {
                pattern: pattern.into(),
                regex: false,
                word: false,
                sort: "score".into(),
                limit: 40,
                exact: false,
                count_only: false,
                count_by_tier: false,
                content: false,
                chats: false,
                tools: false,
                filters: Default::default(),
            };
            let mut matcher = Matcher::new(&o, &c).unwrap();
            matcher.phase = 2;
            for gap in [
                " ".into(),
                "\n\t ".into(),
                "x".repeat(79),
                "x".repeat(160),
                "x ".repeat(200),
            ] {
                let tokens = boundary_rank::query_tokens(pattern);
                let text = format!(
                    "prefix {} suffix",
                    tokens.iter().rev().copied().collect::<Vec<_>>().join(&gap)
                );
                let Some(actual) = matcher.evaluate(&text, "user", 1, 0, 0, None).unwrap() else {
                    continue;
                };
                let row = Folded::new(&text);
                let spans = matcher
                    .terms
                    .iter()
                    .enumerate()
                    .map(|(i, _)| matcher.term_span(&row, i).unwrap().unwrap())
                    .collect::<Vec<_>>();
                let snip = Snip::new(&text, &spans);
                let phrase = matcher
                    .phrase_strength(&row, &snip, &View::new(&row, &snip))
                    .unwrap();
                let graded = boundary_rank::decut_text(&snip.text, true);
                let b = matcher.boundary.evaluate(&graded, None, true).unwrap();
                let shift = i64::from(snip.text.starts_with(window::CUT));
                let selected = b
                    .spans
                    .iter()
                    .map(|s| s.map(|[a, b]| [a - shift, b - shift]))
                    .collect::<Vec<_>>();
                let near =
                    window::proximity(&snip.text, &selected, &b.qualities, matcher.query_chars);
                assert_eq!(
                    actual.score,
                    score(phrase.max(near), 1., "user", b.factor, false),
                    "{pattern:?}"
                );
                assert_eq!(actual.class, b.match_class);
                assert_eq!(actual.snippet, snip.text);
            }
        }
    }
    #[test]
    fn native_tool_proximity_keeps_every_ordered_span() {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch("CREATE VIRTUAL TABLE f USING fts5(text,content='',tokenize='unicode61 remove_diacritics 0');
            INSERT INTO f(rowid,text) VALUES(1,'alpha beta gamma'),(2,'alpha x beta gamma'),
            (3,'gamma beta alpha'),(4,'alpha---beta gamma'),(5,'alpha beta gamma alpha x x beta gamma');")
            .unwrap();
        crate::fts_span::install(&c).unwrap();
        let expression = "(\"alpha\") AND (\"beta\") AND (\"gamma\")";
        let ordinals: Vec<_> = [0u32, 1, 2]
            .into_iter()
            .flat_map(u32::to_le_bytes)
            .collect();
        let near = tool_near(expression, &ordinals, 0).unwrap();
        let query = |expression: &str| {
            c.prepare("SELECT rowid FROM f WHERE f MATCH ?1 AND agrep_word_span(f,?2,0)")
                .unwrap()
                .query_map(rusqlite::params![expression, ordinals], |r| {
                    r.get::<_, i64>(0)
                })
                .unwrap()
                .collect::<rusqlite::Result<Vec<_>>>()
                .unwrap()
        };
        assert_eq!(query(expression), [1, 4, 5]);
        let candidates = c
            .prepare("SELECT rowid FROM f WHERE f MATCH ? ORDER BY rowid")
            .unwrap()
            .query_map([near], |r| r.get::<_, i64>(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert_eq!(candidates, [1, 4, 5]);
        assert!(tool_near("(\"foobar\") AND (\"foobar\")", &ordinals[..8], 0).is_none());
    }

    #[test]
    fn tool_phrase_positions_load_all_three_anchor_expansions() {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        let blocks = crate::tool_index::encode_blocks(&[0]);
        for term in ["one", "two", "six"] {
            c.execute(
                "INSERT INTO tool_words(term,doc,blocks) VALUES(?1,200,?2)",
                rusqlite::params![term, blocks],
            )
            .unwrap();
            c.execute(
                "INSERT INTO tool_trigrams(gram,doc) VALUES(?1,200)",
                [i64::from_str_radix(term, 36).unwrap()],
            )
            .unwrap();
        }
        let o = Options {
            pattern: "one two six".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 40,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: true,
            filters: Filters::default(),
        };
        let mut cache = PlanCache::default();
        let mut matcher = cache.matcher(&o, &c).unwrap();
        matcher.phase = 1;
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        let (expression, ordinals) = matcher.word_positions(None).unwrap();
        assert_eq!(expression, "(\"one\") AND (\"two\") AND (\"six\")");
        assert_eq!(
            ordinals,
            [0u32, 1, 2]
                .into_iter()
                .flat_map(u32::to_le_bytes)
                .collect::<Vec<_>>()
        );
        for term in ["windows", "window"] {
            c.execute(
                "INSERT INTO tool_words(term,doc,blocks) VALUES(?1,200,?2)",
                rusqlite::params![term, blocks],
            )
            .unwrap();
        }
        let mut o = o;
        o.pattern = "one windows six".into();
        let mut matcher = cache.matcher(&o, &c).unwrap();
        matcher.phase = 1;
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        assert_eq!(
            matcher.word_positions(None).unwrap().0,
            "(\"one\") AND (\"windows\") AND (\"six\")"
        );
    }
    // A term the dictionary cannot expand enters tool plans through its rarest run, and
    // only where no page ceiling reads rows it rules out.
    #[test]
    fn unexpandable_tool_terms_anchor_on_their_rarest_run() {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        let blocks = crate::tool_index::encode_blocks(&[0]);
        for (term, doc) in [("std", 900), ("sync", 40), ("async", 60)] {
            c.execute(
                "INSERT INTO tool_words(term,doc,blocks) VALUES(?1,?2,?3)",
                rusqlite::params![term, doc, blocks],
            )
            .unwrap();
        }
        for (gram, doc) in [("std", 900), ("syn", 100), ("ync", 100), ("asy", 60)] {
            c.execute(
                "INSERT INTO tool_trigrams(gram,doc) VALUES(?1,?2)",
                rusqlite::params![i64::from_str_radix(gram, 36).unwrap(), doc],
            )
            .unwrap();
        }
        let o = Options {
            pattern: "Std::Sync".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 40,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: true,
            filters: Filters::default(),
        };
        let mut cache = PlanCache::default();
        let mut matcher = cache.matcher(&o, &c).unwrap();
        matcher.phase = 1;
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        assert_eq!(matcher.fts(None), None);
        assert_eq!(
            matcher.tool_fts(None, true).as_deref(),
            Some("(\"async\" OR \"sync\")")
        );
        matcher.plan_anchors = true;
        assert_eq!(matcher.fts(Some(&[1])), None);
        assert_eq!(
            matcher.fts(Some(&[0])).as_deref(),
            Some("(\"async\" OR \"sync\")")
        );
        // A run no word contains is no token either: its clause admits nothing.
        let mut o = o;
        o.pattern = "zzq::sync".into();
        let mut matcher = cache.matcher(&o, &c).unwrap();
        matcher.plan_anchors = true;
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        assert_eq!(matcher.fts(None).as_deref(), Some("(\"zzq\")"));
    }
    fn explicit_matcher(c: &Connection, pattern: &str, word: bool) -> (Matcher, PlanCache) {
        let o = Options {
            pattern: pattern.into(),
            regex: !word,
            word,
            sort: "score".into(),
            limit: 40,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: true,
            filters: Filters::default(),
        };
        let mut cache = PlanCache::default();
        let matcher = cache.matcher(&o, c).unwrap();
        (matcher, cache)
    }
    // A `-w` match has no word character beside it, so plans that may skip rows read only
    // the words where the term can stand alone; a page reads every word holding it.
    #[test]
    fn word_plans_read_only_words_where_the_term_stands_alone() {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        let blocks = crate::tool_index::encode_blocks(&[0]);
        for term in [
            "work",
            "homework",
            "worktree",
            "work2",
            "éwork",
            "work日本",
            "workwork",
        ] {
            c.execute(
                "INSERT INTO tool_words(term,doc,blocks) VALUES(?1,200,?2)",
                rusqlite::params![term, blocks],
            )
            .unwrap();
        }
        let (mut matcher, mut cache) = explicit_matcher(&c, "Work", true);
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        assert_eq!(
            matcher.tool_fts(None, false).as_deref(),
            Some("(\"homework\" OR \"work\" OR \"work2\" OR \"worktree\" OR \"workwork\" OR \"work日本\" OR \"éwork\")")
        );
        assert_eq!(
            matcher.tool_fts(None, true).as_deref(),
            Some("(\"work\" OR \"work日本\" OR \"éwork\")")
        );
        // Where no word lets the term stand alone, such plans read nothing.
        let (mut matcher, mut cache) = explicit_matcher(&c, "ork", true);
        matcher.plan_anchors = true;
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        assert_eq!(matcher.estimated, 0);
    }
    // An alternation's literal set narrows plans that may skip rows; a page's reads, bounds
    // and estimate keep to the single literals.
    #[test]
    fn literal_sets_narrow_only_plans_that_may_skip_rows() {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        let blocks = crate::tool_index::encode_blocks(&[0]);
        for term in ["worktree", "workflows", "release"] {
            c.execute(
                "INSERT INTO tool_words(term,doc,blocks) VALUES(?1,200,?2)",
                rusqlite::params![term, blocks],
            )
            .unwrap();
        }
        let (mut matcher, mut cache) = explicit_matcher(&c, "release.*work(tree|flow)s?", false);
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        assert_eq!(
            matcher.tool_fts(None, false).as_deref(),
            Some("(\"release\")")
        );
        assert_eq!(
            matcher.tool_fts(None, true).as_deref(),
            Some("(\"release\") AND (\"workflows\" OR \"worktree\")")
        );
        assert_eq!(matcher.bound_grams().len(), 1);
        matcher.order_for(&c, "prose", &mut cache).unwrap();
        assert_eq!(
            matcher.prose_fts("").as_deref(),
            Some("text : ((\"release\"))")
        );
        matcher.plan_anchors = true;
        assert_eq!(
            matcher.prose_fts("").as_deref(),
            Some("text : ((\"release\") AND (\"workflow\" OR \"worktree\"))")
        );
    }
    #[test]
    fn common_tool_questions_prefer_consecutive_literal_anchors() {
        let c = Connection::open_in_memory().unwrap();
        store::schema(&c).unwrap();
        let blocks = crate::tool_index::encode_blocks(&[0]);
        for (term, doc) in [
            ("one", 40000),
            ("two", 90000),
            ("six", 80000),
            ("nine", 50000),
            ("ten", 35000),
        ] {
            c.execute(
                "INSERT INTO tool_words(term,doc,blocks) VALUES(?1,?2,?3)",
                rusqlite::params![term, doc, blocks],
            )
            .unwrap();
            if term.len() == 3 {
                c.execute(
                    "INSERT INTO tool_trigrams(gram,doc) VALUES(?1,?2)",
                    rusqlite::params![i64::from_str_radix(term, 36).unwrap(), doc],
                )
                .unwrap();
            }
        }
        let o = Options {
            pattern: "one two six nine ten".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 40,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: true,
            filters: Filters::default(),
        };
        let mut cache = PlanCache::default();
        let mut matcher = cache.matcher(&o, &c).unwrap();
        matcher.phase = 1;
        matcher.order_for(&c, "tools", &mut cache).unwrap();
        let (expression, ordinals) = matcher.word_positions(None).unwrap();
        assert_eq!(expression, "(\"one\") AND (\"two\") AND (\"six\")");
        assert_eq!(
            tool_near(&expression, &ordinals, 0).unwrap(),
            "\"one two six\""
        );
    }
    // Every pass of a search clones one compiled matcher: a pass adjusts only its copy,
    // a count takes its own counting mode, and another pattern compiles anew.
    #[test]
    fn passes_share_one_compiled_matcher() {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let mut o = Options {
            pattern: "cobalt lantern".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 40,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: true,
            chats: false,
            tools: true,
            filters: Filters::default(),
        };
        let mut cache = PlanCache::default();
        let mut first = cache.matcher(&o, &c).unwrap();
        assert!(!first.quick_count);
        first.terms.clear();
        o.count_only = true;
        let second = cache.matcher(&o, &c).unwrap();
        assert!(second.quick_count);
        assert_eq!(second.terms.len(), 2);
        o.pattern = "deployment".into();
        assert_eq!(cache.matcher(&o, &c).unwrap().terms.len(), 1);
        o.count_only = false;
        let scored = cache
            .matcher(&o, &c)
            .unwrap()
            .evaluate("deployment", "user", 1, 0, 0, None)
            .unwrap()
            .unwrap();
        assert_eq!(scored.snippet, "deployment");
    }
}
#[cfg(test)]
mod term_stats_tests {
    use super::*;
    // A term without statistics ranks with the cold prior; a statistics row that cannot
    // be read fails the search instead of silently ranking as if it were absent.
    #[test]
    fn unreadable_term_statistics_fail_the_matcher() {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let options = Options {
            pattern: "cobalt".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 10,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: false,
            filters: Filters::default(),
        };
        Matcher::new(&options, &c).unwrap();
        c.execute("INSERT INTO term_stats VALUES('cobalt','many',1)", [])
            .unwrap();
        let error = Matcher::new(&options, &c).err().unwrap();
        assert!(
            matches!(
                error.downcast_ref::<rusqlite::Error>(),
                Some(rusqlite::Error::InvalidColumnType(0, ..))
            ),
            "{error:#}"
        );
    }
}
