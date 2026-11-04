//! Presentation seam for optional semantic retrieval. Scores stay lane-local.
use crate::query::{Hit, Results};
use std::collections::HashSet;
/// Meaning rows one merge considers (py/search.py:2060).
pub const ROWS: usize = 3;
/// 0.4.9's `_weak_lexical_hit`: a bag-of-words row or a match inside a word.
pub fn weak_lexical(h: &Hit) -> bool {
    matches!(h.matched.as_str(), "all-terms" | "content-terms") || h.boundary_class == "interior"
}
pub fn family(h: &Hit, diverse: bool) -> &str {
    if diverse && !h.family.is_empty() {
        &h.family
    } else {
        &h.session
    }
}
/// 0.4.9's `_hybrid_text_key`: the snippet (else the text) lowercased, with Python's
/// whitespace runs folded.
pub fn text_key(h: &Hit) -> String {
    let raw = if h.snippet.is_empty() {
        &h.text
    } else {
        &h.snippet
    };
    raw.to_lowercase()
        .split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
/// 0.4.9's `_merge_auto_semantic_hits` (py/search.py:2122-2207): interleaves labeled lanes
/// from lexical quality, never from hit count. `semantic` comes strong rows first, each
/// band in the lane's order.
pub fn merge(
    mut keyword: Results,
    semantic: Option<Results>,
    limit: usize,
    diverse: bool,
) -> Results {
    let Some(semantic) = semantic else {
        return keyword;
    };
    // 0.4.9's `hybrid_more` (py/search.py:7962-7966): a lane held rows it did not return.
    let lanes_more = [&keyword, &semantic]
        .iter()
        .any(|lane| lane.total_basis != "exact" || lane.total > lane.hits.len());
    let cap = limit;
    let mut lexical = std::mem::take(&mut keyword.hits);
    if cap == 0 {
        keyword.hits = lexical;
        return keyword;
    }
    let fam = |h: &Hit| family(h, diverse).to_owned();
    let strong_lexical = lexical.iter().any(|h| !weak_lexical(h));
    // Scatter cousins corroborate only when the lexical lane is all scatter.
    let corroborating: HashSet<String> = lexical
        .iter()
        .filter(|h| !strong_lexical || !weak_lexical(h))
        .filter(|h| !h.session.is_empty())
        .map(fam)
        .collect();
    let visible: HashSet<String> = lexical
        .iter()
        .take(cap)
        .filter(|h| !h.session.is_empty())
        .map(fam)
        .collect();
    let mut seen_text: HashSet<String> = lexical
        .iter()
        .map(text_key)
        .filter(|k| !k.is_empty())
        .collect();
    let mut raw = semantic.hits;
    raw.sort_by_key(|h| h.sem_weak);
    raw.truncate(ROWS);
    if !lexical.is_empty() && !lexical.iter().take(cap).any(|h| !weak_lexical(h)) {
        // Weak neighbors may supplement an exact anchor but never lead a page whose
        // lexical lane is itself only scatter.
        raw.retain(|h| !h.sem_weak);
        if raw.is_empty() {
            lexical.truncate(cap);
            keyword.hits = lexical;
            return keyword;
        }
    }
    let overlaps: Vec<usize> = raw
        .iter()
        .enumerate()
        .filter(|(_, h)| !h.session.is_empty() && corroborating.contains(family(h, diverse)))
        .map(|(i, _)| i)
        .collect();
    let mut overlap_at = overlaps.last().copied();
    let lead_family = raw.first().map(fam).unwrap_or_default();
    if overlaps.first() == Some(&0)
        && visible.contains(&lead_family)
        && lexical
            .iter()
            .take(cap)
            .any(|h| !weak_lexical(h) && family(h, diverse) == lead_family)
    {
        // Only a strong visible row of the same family may suppress the semantic lead.
        let later_hidden = overlaps[1..]
            .iter()
            .copied()
            .filter(|&i| !visible.contains(family(&raw[i], diverse)))
            .max();
        let Some(at) = later_hidden else {
            lexical.truncate(cap);
            keyword.hits = lexical;
            return keyword;
        };
        overlap_at = Some(at);
    } else if overlap_at == Some(0) && raw.len() > 1 {
        overlap_at = Some(1);
    }
    let mut meaning: Vec<Hit> = Vec::new();
    for (index, mut h) in raw.into_iter().enumerate() {
        let key = text_key(&h);
        if meaning
            .iter()
            .any(|m| m.session == h.session && m.turn == h.turn)
        {
            continue;
        }
        if let Some(at) = lexical
            .iter()
            .position(|k| k.session == h.session && k.turn == h.turn)
        {
            if !weak_lexical(&lexical[at]) {
                continue;
            }
            // A weak lexical copy of the same row must not veto its meaning twin.
            lexical.remove(at);
        } else if !key.is_empty() && seen_text.contains(&key) {
            continue;
        }
        h.matched = "semantic".into();
        meaning.push(h);
        if !key.is_empty() {
            seen_text.insert(key);
        }
        if overlap_at.is_some_and(|at| index >= at) {
            break;
        }
    }
    let mut hits = if lexical.is_empty() {
        meaning.truncate(cap.min(ROWS));
        meaning
    } else if meaning.is_empty() || cap == 1 {
        lexical
    } else if overlap_at.is_some() {
        let used: HashSet<String> = meaning
            .iter()
            .filter(|h| !h.session.is_empty())
            .map(fam)
            .collect();
        meaning.extend(
            lexical
                .into_iter()
                .filter(|h| h.session.is_empty() || !used.contains(family(h, diverse))),
        );
        meaning
    } else if !strong_lexical {
        meaning.truncate(meaning.len().min(ROWS).min(cap));
        meaning.extend(lexical);
        meaning
    } else {
        let lead = lexical
            .iter()
            .position(weak_lexical)
            .unwrap_or(lexical.len())
            .min(3)
            .min(cap - 1)
            .max(1);
        let mut hits = lexical;
        hits.insert(lead.min(hits.len()), meaning.remove(0));
        hits
    };
    hits.truncate(cap);
    if hits.iter().any(|h| h.matched == "semantic") {
        keyword.total = keyword.total.max(hits.len());
        keyword.total_basis = "floor".into();
        keyword.hybrid_more = Some(lanes_more);
    }
    keyword.hits = hits;
    keyword
}
#[cfg(test)]
mod tests {
    use super::*;
    fn h(s: &str, matched: &str, weak: bool) -> Hit {
        Hit {
            session: s.into(),
            family: s.into(),
            turn: 0,
            matched: matched.into(),
            text: s.into(),
            who: "user".into(),
            sem_weak: weak,
            ..Hit::default()
        }
    }
    fn results(hits: Vec<Hit>) -> Results {
        let mut r = Results::default();
        r.hits = hits;
        r
    }
    fn run(keyword: Vec<Hit>, semantic: Vec<Hit>, cap: usize) -> Vec<(String, String)> {
        merge(results(keyword), Some(results(semantic)), cap, true)
            .hits
            .into_iter()
            .map(|h| (h.session, h.matched))
            .collect()
    }
    fn pairs(v: &[(&str, &str)]) -> Vec<(String, String)> {
        v.iter()
            .map(|(a, b)| (a.to_string(), b.to_string()))
            .collect()
    }
    #[test]
    fn scatter_pages_keep_only_strong_meaning_rows_as_0_4_9() {
        // py/search.py:2146-2153, 2191-2199. 0.4.9's `recall "how do we rotate secret
        // credentials" --json` over six chats whose keyword rows are all scatter: of the
        // three meaning rows (cosines .882, .870, .845), the two carrying only "secret" are
        // weak, so the page is the one strong row, then the other chats.
        let scatter: Vec<Hit> = ["f005", "f004", "f003", "f002", "f001", "f000"]
            .iter()
            .map(|s| h(s, "content-terms", false))
            .collect();
        let meaning = vec![
            h("f000", "semantic", true),
            h("f002", "semantic", false),
            h("f005", "semantic", true),
        ];
        assert_eq!(
            run(scatter.clone(), meaning, 8),
            pairs(&[
                ("f002", "semantic"),
                ("f005", "content-terms"),
                ("f004", "content-terms"),
                ("f003", "content-terms"),
                ("f001", "content-terms"),
                ("f000", "content-terms"),
            ])
        );
        assert_eq!(
            run(scatter, vec![h("x", "semantic", true)], 3),
            pairs(&[
                ("f005", "content-terms"),
                ("f004", "content-terms"),
                ("f003", "content-terms")
            ])
        );
    }
    #[test]
    fn strong_lexical_pages_slot_one_meaning_row_as_0_4_9() {
        // py/search.py:2203-2207
        let keyword = vec![
            h("a", "phrase", false),
            h("b", "phrase", false),
            h("c", "all-terms", false),
        ];
        assert_eq!(
            run(
                keyword.clone(),
                vec![h("x", "semantic", true), h("y", "semantic", false)],
                4
            ),
            pairs(&[
                ("a", "phrase"),
                ("b", "phrase"),
                ("y", "semantic"),
                ("c", "all-terms"),
            ])
        );
        // A strong visible row of the meaning lead's family suppresses the lead.
        assert_eq!(
            run(keyword, vec![h("a", "semantic", false)], 4),
            pairs(&[("a", "phrase"), ("b", "phrase"), ("c", "all-terms")])
        );
    }
    #[test]
    fn meaning_replaces_its_weak_twin_and_fills_an_empty_lexical_lane() {
        let keyword = vec![h("a", "phrase", false), h("b", "all-terms", false)];
        let r = merge(
            results(keyword),
            Some(results(vec![h("b", "semantic", false)])),
            3,
            true,
        );
        assert_eq!(r.hits.len(), 2);
        assert_eq!(r.hits[1].matched, "semantic");
        assert_eq!(r.total_basis, "floor");
        let only = (0..5)
            .map(|i| h(&i.to_string(), "semantic", true))
            .collect();
        assert_eq!(run(vec![], only, 8).len(), 3);
    }
}
