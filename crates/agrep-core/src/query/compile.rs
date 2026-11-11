use super::*;

/// The folded literals every match of `s` begins with, then those it ends with, as groups
/// of spellings one of which every match holds. Each comes with whether the extractor
/// found a single literal at that end (see [`Matcher::literal_sets`]). A spelling
/// containing another adds nothing to its group and is left out. The extractor cuts a
/// literal past 100 bytes, possibly inside a character, so only its whole characters
/// remain; a group with an empty spelling, or more than 64, constrains nothing.
pub(super) fn literal_terms(s: &str) -> Vec<(Vec<String>, bool)> {
    use regex_syntax::hir::literal::{ExtractKind, Extractor};
    let Ok(hir) = regex_syntax::ParserBuilder::new().build().parse(s) else {
        return vec![];
    };
    let mut out: Vec<(Vec<String>, bool)> = Vec::new();
    for (kind, prefix) in [(ExtractKind::Prefix, true), (ExtractKind::Suffix, false)] {
        let seq = Extractor::new().kind(kind).extract(&hir);
        let Some(literals) = seq.literals().filter(|l| (1..=64).contains(&l.len())) else {
            continue;
        };
        let Some(spellings) = literals
            .iter()
            .map(|literal| whole_characters(literal.as_bytes(), prefix).filter(|s| !s.is_empty()))
            .map(|literal| literal.map(store::search_fold))
            .collect::<Option<Vec<_>>>()
        else {
            continue;
        };
        let mut group: Vec<String> = spellings
            .iter()
            .filter(|s| {
                !spellings
                    .iter()
                    .any(|o| o.len() < s.len() && s.contains(o.as_str()))
            })
            .cloned()
            .collect();
        group.sort_unstable();
        group.dedup();
        let single = literals.len() == 1;
        match out.iter_mut().find(|(existing, _)| *existing == group) {
            Some(found) => found.1 |= single,
            None => out.push((group, single)),
        }
    }
    out
}
/// The longest prefix of `bytes` that is whole characters, or else suffix: a cut leaves
/// at most three continuation bytes of a character in front.
fn whole_characters(bytes: &[u8], prefix: bool) -> Option<&str> {
    if prefix {
        return match std::str::from_utf8(bytes) {
            Ok(s) => Some(s),
            Err(e) => std::str::from_utf8(&bytes[..e.valid_up_to()]).ok(),
        };
    }
    (0..=bytes.len().min(3)).find_map(|skip| std::str::from_utf8(&bytes[skip..]).ok())
}
/// Whether a regular expression matches at most once in a snippet, whose collapsed
/// whitespace leaves no line break: it is `A.*B` with `A` never empty and `.*` greedy and
/// unbounded. The first match's `.*` runs to the last place `B` matches after `A`, and any
/// later match would offer a later one.
pub(super) fn matches_once(pattern: &str) -> bool {
    use regex_syntax::hir::{Class, HirKind};
    let Ok(mut hir) = regex_syntax::ParserBuilder::new().build().parse(pattern) else {
        return false;
    };
    while let HirKind::Capture(capture) = hir.kind() {
        hir = (*capture.sub).clone();
    }
    let HirKind::Concat(parts) = hir.kind() else {
        return false;
    };
    // Any character but a line break, which the snippet's whitespace runs replace.
    let dot = |class: &Class| match class {
        Class::Unicode(class) => {
            let mut missing = class.clone();
            missing.negate();
            missing
                .ranges()
                .iter()
                .all(|range| (range.start()..=range.end()).all(|c| c == '\n' || c == '\r'))
        }
        Class::Bytes(_) => false,
    };
    parts.iter().enumerate().any(|(at, part)| {
        matches!(part.kind(), HirKind::Repetition(r)
            if r.greedy && r.max.is_none() && matches!(r.sub.kind(), HirKind::Class(c) if dot(c)))
            && parts[..at]
                .iter()
                .any(|before| before.properties().minimum_len().is_some_and(|n| n > 0))
    })
}
/// Why `pattern` is no regular expression, read as written, as 0.4.9 compiled the query
/// before searching (py/search.py:7532-7538). The search compiles it inside a group, which
/// would accept a pattern such as `a)(b`.
pub fn regex_error(pattern: &str) -> Option<String> {
    fancy_regex::Regex::new(pattern)
        .err()
        .map(|e| e.to_string())
}
impl Matcher {
    fn token_variants(
        o: &Options,
        token: &str,
        token_count: usize,
        lowercase_terms: &mut Vec<LowercaseTerm>,
    ) -> Vec<String> {
        let mut variants = vec![store::search_fold(token)];
        if token_count > 1 {
            for variant in boundary_rank::term_variants(token) {
                let variant = store::search_fold(&variant);
                if !variants.contains(&variant) {
                    variants.push(variant);
                }
            }
        }
        // Python's lowercase shortcut expands İ, unlike re.I's one-character
        // case classes. Widen candidates but retain the original spellings.
        if !o.word
            && !o.regex
            && (token.contains('İ') || variants.iter().any(|v| v.contains("i\u{307}")))
        {
            let lowercase = if token_count == 1 {
                vec![crate::unicode_v16::lowercase(token)]
            } else {
                boundary_rank::term_variants(token)
                    .iter()
                    .map(|v| crate::unicode_v16::lowercase(v))
                    .collect()
            };
            for variant in &lowercase {
                let variant = store::search_fold(variant);
                if !variants.contains(&variant) {
                    variants.push(variant);
                }
            }
            for variant in variants.clone() {
                let anchor = variant
                    .split('\u{307}')
                    .max_by_key(|part| part.chars().count())
                    .unwrap_or("")
                    .to_owned();
                if !anchor.is_empty() && !variants.contains(&anchor) {
                    variants.push(anchor);
                }
            }
        }
        if !o.word && !o.regex {
            let lowercase = if token_count == 1 {
                vec![crate::unicode_v16::lowercase(token)]
            } else {
                boundary_rank::term_variants(token)
                    .iter()
                    .map(|v| crate::unicode_v16::lowercase(v))
                    .collect()
            };
            let mask = variants.iter().flat_map(|v| v.chars()).fold(0, |mask, c| {
                mask | match c {
                    'i' => 1,
                    's' => 2,
                    c if !c.is_ascii() => 4,
                    _ => 0,
                }
            });
            let special = lowercase != variants;
            lowercase_terms.push(LowercaseTerm {
                lowercase,
                mask,
                special,
                original: token.to_owned(),
                raw: Default::default(),
            });
        }
        variants
    }

    pub(super) fn new(o: &Options, c: &Connection) -> Result<Self> {
        // 0.4.9 searched a one-word keyword query for its whole trimmed text, so `--x` keeps
        // its dashes (py/corpusdb.py:4731-4736, 4826-4828).
        let split = boundary_rank::query_tokens(&o.pattern);
        let trimmed = o.pattern.trim();
        let tokens = if !o.word && !o.regex && split.len() == 1 && split[0] != trimmed {
            vec![trimmed]
        } else {
            split
        };
        let mut lowercase_terms = Vec::new();
        let dotted_candidates = if !o.word && !o.regex {
            tokens
                .iter()
                .filter(|t| t.contains('İ'))
                .map(|t| crate::unicode_v16::lowercase(t))
                .collect()
        } else {
            Vec::new()
        };
        let terms = tokens
            .iter()
            .map(|token| Self::token_variants(o, token, tokens.len(), &mut lowercase_terms))
            .collect::<Vec<_>>();
        if o.pattern.is_empty() || (terms.is_empty() && !o.regex && !o.word) {
            bail!("a nonempty search pattern is required")
        }
        let (explicit, literals, phrase, capture) = if o.word {
            (
                Some(Pattern::word(&o.pattern)?),
                vec![(vec![store::search_fold(&o.pattern)], true)],
                None,
                None,
            )
        } else if o.regex {
            (
                Some(Pattern::regex(&o.pattern)?),
                literal_terms(&o.pattern),
                None,
                None,
            )
        } else if o.count_only && !o.count_by_tier {
            // Presence counts return before phrase matching or snippet scoring.
            (None, vec![], None, None)
        } else {
            let tokens: Vec<String> = tokens
                .iter()
                .map(|t| regex::escape(&store::search_fold(t)))
                .collect();
            let groups: Vec<String> = tokens.iter().map(|t| format!("({t})")).collect();
            let gap = if tokens.len() > 1 {
                crate::unicode_v16::keyword_gap()
            } else {
                ""
            };
            let capture = regex::Regex::new(&groups.join(gap))?;
            (None, vec![], Some(capture.clone()), Some(capture))
        };
        let literal_sets = literals
            .iter()
            .filter(|(_, single)| !single)
            .map(|(group, _)| group.clone())
            .collect();
        let required = literals.into_iter().map(|(group, _)| group).collect();
        let query_chars = tokens.iter().map(|t| t.chars().count()).sum();
        let mut stats = Stats::new();
        for token in boundary_rank::query_tokens(&o.pattern) {
            let normalized = store::normalize(token);
            let v = c
                .query_row(
                    "SELECT n,s FROM term_stats WHERE token=?",
                    [&normalized],
                    |r| Ok([r.get(0)?, r.get(1)?]),
                )
                .optional()?;
            if let Some(v) = v {
                stats.insert(normalized, boundary_rank::StatValue::Pair(v));
            }
        }
        Ok(Self {
            quick_count: o.count_only && !o.count_by_tier,
            partial: false,
            phase: 0,
            ordered_terms: terms.clone(),
            lowercase_terms,
            dotted_candidates,
            content_literals: Vec::new(),
            content_patterns: Vec::new(),
            estimated: usize::MAX,
            terms,
            phrase,
            capture,
            raw_query: o.pattern.clone(),
            raw_capture: Default::default(),
            explicit,
            explicit_chars: if o.word { query_chars } else { 0 },
            explicit_once: o.regex && matches_once(&o.pattern),
            boundary: boundary_rank::prepare_query(&o.pattern, &stats),
            query_chars,
            required,
            literal_sets,
            word_terms: HashMap::new(),
            idf: vec![],
            tool_terms: HashMap::new(),
            tool_supported: HashSet::new(),
            tool_presence: Vec::new(),
            tool_anchors: HashMap::new(),
            plan_anchors: false,
            coverage_term: None,
            coverage_excluded: HashSet::new(),
        })
    }
}
