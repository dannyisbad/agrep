//! Presentation seam for optional semantic retrieval. Scores stay lane-local.
use crate::query::{Hit, Results};
use std::collections::HashSet;
#[derive(Clone, Copy, Debug)]
pub enum Lane {
    Compact,
    Recall,
    Pack,
    Classic,
}
fn weak(h: &Hit) -> bool {
    h.who == "tool" || h.matched != "phrase" || h.boundary_class == "interior"
}
fn family(h: &Hit, diverse: bool) -> &str {
    if diverse && !h.family.is_empty() {
        &h.family
    } else {
        &h.session
    }
}
fn key(h: &Hit) -> String {
    h.text
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_lowercase()
}
/// The semantic result uses cosine scores. <0.82 is below retrieval floor;
/// 0.82..0.84 is a weak neighbor, allowed beside strong lexical evidence only.
pub fn merge(
    mut keyword: Results,
    semantic: Option<Results>,
    limit: usize,
    diverse: bool,
    lane: Lane,
) -> Results {
    let Some(semantic) = semantic else {
        return keyword;
    };
    if limit == 0 {
        return keyword;
    }
    let strong = keyword.hits.iter().take(limit).any(|h| !weak(h));
    if matches!(lane, Lane::Classic) && strong {
        return keyword;
    }
    let mut seen_text: HashSet<_> = keyword.hits.iter().filter(|h| !weak(h)).map(key).collect();
    let mut meaning = Vec::new();
    for mut h in semantic.hits {
        if !h.score.is_finite() || h.score < 0.82 || (!strong && h.score < 0.84) {
            continue;
        }
        if keyword
            .hits
            .iter()
            .any(|k| k.session == h.session && k.turn == h.turn && !weak(k))
            || !seen_text.insert(key(&h))
        {
            continue;
        }
        keyword
            .hits
            .retain(|k| !(k.session == h.session && k.turn == h.turn && weak(k)));
        h.matched = "semantic".into();
        meaning.push(h);
    }
    meaning.sort_by(|a, b| b.score.total_cmp(&a.score));
    meaning.truncate(3);
    if meaning.is_empty() {
        keyword.hits.truncate(limit);
        return keyword;
    }
    let corroborating: HashSet<_> = keyword
        .hits
        .iter()
        .filter(|h| !strong || !weak(h))
        .map(|h| family(h, diverse).to_owned())
        .collect();
    let overlaps = meaning
        .iter()
        .rposition(|h| corroborating.contains(family(h, diverse)));
    if meaning.first().is_some_and(|h| {
        keyword
            .hits
            .iter()
            .take(limit)
            .any(|k| !weak(k) && family(k, diverse) == family(h, diverse))
    }) {
        keyword.hits.truncate(limit);
        return keyword;
    }
    let lexical = std::mem::take(&mut keyword.hits);
    let hits = if let Some(last) = overlaps {
        meaning.truncate((last + 1).max(2).min(meaning.len()));
        let used: HashSet<_> = meaning
            .iter()
            .map(|h| family(h, diverse).to_owned())
            .collect();
        meaning.extend(
            lexical
                .into_iter()
                .filter(|h| !used.contains(family(h, diverse))),
        );
        meaning
    } else if !strong {
        meaning.extend(lexical);
        meaning
    } else {
        let lead = lexical
            .iter()
            .position(weak)
            .unwrap_or(lexical.len())
            .clamp(1, 3)
            .min(limit.saturating_sub(1));
        let mut hits = lexical;
        hits.insert(lead, meaning.remove(0));
        hits
    };
    keyword.hits = hits.into_iter().take(limit).collect();
    keyword.total = keyword.total.max(keyword.hits.len());
    keyword.total_basis = "floor".into();
    keyword
}
#[cfg(test)]
mod tests {
    use super::*;
    fn h(s: &str, score: f64, matched: &str) -> Hit {
        Hit {
            session: s.into(),
            family: s.into(),
            score,
            matched: matched.into(),
            text: s.into(),
            who: "user".into(),
            ..Hit::default()
        }
    }
    #[test]
    fn weak_only_needs_strong_semantic() {
        let mut k = Results::default();
        k.hits = vec![h("a", 1., "all-terms")];
        let mut s = Results::default();
        s.hits = vec![h("b", 0.83, "semantic")];
        let r = merge(k, Some(s), 3, true, Lane::Recall);
        assert_eq!(r.hits[0].session, "a");
    }
    #[test]
    fn meaning_replaces_weak_twin_and_diversifies() {
        let mut k = Results::default();
        k.hits = vec![h("a", 1., "phrase"), h("b", 0.5, "all-terms")];
        let mut s = Results::default();
        s.hits = vec![h("b", 0.9, "semantic")];
        let r = merge(k, Some(s), 3, true, Lane::Recall);
        assert_eq!(r.hits.len(), 2);
        assert_eq!(r.hits[1].matched, "semantic");
        assert_eq!(r.total_basis, "floor");
    }
}
