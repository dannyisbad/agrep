use super::*;

impl Matcher {
    pub(super) fn order_for(
        &mut self,
        c: &Connection,
        table: &str,
        cache: &mut PlanCache,
    ) -> Result<()> {
        let groups = if let Some(i) = self.coverage_term {
            vec![self.terms[i].clone()]
        } else if self.explicit.is_some() {
            self.required.iter().map(|v| vec![v.clone()]).collect()
        } else {
            self.terms.clone()
        };
        self.tool_terms.clear();
        self.tool_supported.clear();
        self.tool_presence.clear();
        self.tool_anchors.clear();
        if table == "tools" {
            if self.explicit.is_none() {
                self.tool_presence = vec![None; self.terms.len()];
            }
            let mut ordered = vec![];
            let mut estimate = if self.partial { 0 } else { usize::MAX };
            let mut indexed = false;
            for (i, variants) in groups.into_iter().enumerate() {
                let mut df = 0usize;
                let mut expanded = vec![];
                let mut supported = true;
                for v in &variants {
                    // A trigram's block postings already bound where it occurs;
                    // its words are read only if a plan uses them.
                    if let Some(doc) = cache.trigram_doc(c, v)? {
                        df = df.saturating_add(doc);
                    } else if let Some(words) = cache.words(c, v)? {
                        df = df.saturating_add(words.doc);
                        expanded.push((v.clone(), words));
                    } else {
                        supported = false;
                        break;
                    }
                }
                if supported {
                    if self.explicit.is_none() && expanded.len() == variants.len() {
                        let mut blocks: Vec<u64> = Vec::new();
                        for (_, words) in &expanded {
                            if words.blocks.len() > blocks.len() {
                                blocks.resize(words.blocks.len(), 0);
                            }
                            for (slot, bits) in blocks.iter_mut().zip(&words.blocks) {
                                *slot |= bits;
                            }
                        }
                        self.tool_presence[self.coverage_term.unwrap_or(i)] = Some(blocks);
                    }
                    self.tool_supported.extend(variants.iter().cloned());
                    self.tool_terms.extend(expanded);
                    indexed = true;
                    if self.partial {
                        estimate = estimate.saturating_add(df);
                    } else {
                        estimate = estimate.min(df);
                    }
                } else {
                    df = usize::MAX;
                    self.anchor_group(c, cache, &variants)?;
                }
                ordered.push((df, i, variants));
            }
            ordered.sort_by_key(|v| (v.0, v.1));
            self.ordered_terms = ordered.into_iter().map(|v| v.2).collect();
            // Phrase positions need three anchors; all-terms cursors use two.
            let read = if self.partial
                || self.quick_count
                || (self.phase == 1 && estimate > 32_768 && self.terms.len() <= 16)
            {
                usize::MAX
            } else if self.phase == 1 {
                3
            } else {
                2
            };
            let needed: Vec<String> = self
                .ordered_terms
                .iter()
                .filter(|group| group.iter().all(|v| self.tool_supported.contains(v)))
                .take(read)
                .flatten()
                .filter(|v| !self.tool_terms.contains_key(*v))
                .cloned()
                .collect();
            for v in needed {
                if let Some(words) = cache.words(c, &v)? {
                    self.tool_terms.insert(v, words);
                }
            }
            self.estimated = if indexed { estimate } else { usize::MAX };
            return Ok(());
        }
        let mut cache: HashMap<String, usize> = HashMap::new();
        let mut ordered = vec![];
        let mut estimate = if self.partial { 0 } else { usize::MAX };
        let mut indexed = false;
        for (i, variants) in groups.into_iter().enumerate() {
            let mut df = 0usize;
            if variants.iter().any(|v| v.chars().count() < 3) {
                df = usize::MAX;
            } else {
                for v in &variants {
                    let chars: Vec<_> = v.chars().collect();
                    let mut least = usize::MAX;
                    for gram in chars.windows(3) {
                        let gram: String = gram.iter().collect();
                        let n = if let Some(n) = cache.get(&gram) {
                            *n
                        } else {
                            let n = c
                                .query_row(
                                    "SELECT doc FROM prose_terms WHERE term=?",
                                    [&gram],
                                    |r| r.get::<_, usize>(0),
                                )
                                .optional()?
                                .unwrap_or(0);
                            cache.insert(gram, n);
                            n
                        };
                        least = least.min(n);
                    }
                    df = df.saturating_add(least);
                }
                indexed = true;
                if self.partial {
                    estimate = estimate.saturating_add(df);
                } else {
                    estimate = estimate.min(df);
                }
            }
            ordered.push((df, i, variants));
        }
        ordered.sort_by_key(|v| (v.0, v.1));
        self.ordered_terms = ordered.into_iter().map(|v| v.2).collect();
        self.estimated = if indexed { estimate } else { usize::MAX };
        Ok(())
    }
    /// Gives each spelling of a group the dictionary cannot expand an expandable anchor;
    /// the words containing it admit every row the spelling matches. No spelling gets one
    /// unless all do.
    fn anchor_group(
        &mut self,
        c: &Connection,
        cache: &mut PlanCache,
        variants: &[String],
    ) -> Result<()> {
        let mut anchors = Vec::with_capacity(variants.len());
        for variant in variants {
            let Some((anchor, words)) = anchor(c, cache, variant)? else {
                return Ok(());
            };
            anchors.push((variant.clone(), anchor, words));
        }
        for (variant, anchor, words) in anchors {
            self.tool_terms.insert(anchor.clone(), words);
            self.tool_anchors.insert(variant, anchor);
        }
        Ok(())
    }
    /// The anchors of a group's spellings, when every spelling has one.
    pub(super) fn anchors(&self, group: &[String]) -> Option<Vec<String>> {
        group
            .iter()
            .map(|variant| self.tool_anchors.get(variant).cloned())
            .collect()
    }
    /// On ASCII rows, the all-terms strength ceiling, with the bridges whose adjacency
    /// would break it: each pair of spellings of two terms, one after the other.
    ///
    /// A phrase match in an all-terms snippet crosses a window joint (three separator
    /// characters). The proximity spread covers one selected occurrence of each term;
    /// below one quality, its adjacency bonus is zero. Aligned occurrences of distinct
    /// terms that neither contain nor overlap each other and are not written adjacently
    /// sit at least one character apart. Only a cut marker in the spread, which adds 80,
    /// can bring a de-cut copy of a neighbour into an occurrence; the start pad, which
    /// Python's negative slice does not count, would put the same character twice at a
    /// spelling's start.
    pub(super) fn all_terms_ceiling(&self) -> Option<(f64, Vec<Vec<String>>)> {
        let count = self.terms.len();
        let spellings = || self.terms.iter().flatten();
        if count < 2
            || spellings().any(|s| {
                s.len() < 3
                    || !s.bytes().all(|b| b.is_ascii_alphanumeric())
                    || s.as_bytes()[0] == s.as_bytes()[1]
            })
        {
            return None;
        }
        let width: usize = self
            .terms
            .iter()
            .map(|v| v.iter().map(String::len).min().unwrap_or(0))
            .sum();
        if width + count - 1 > window::PAD {
            return None;
        }
        let mut bridges = Vec::new();
        for (i, left) in self.terms.iter().enumerate() {
            for (j, right) in self.terms.iter().enumerate() {
                if i == j {
                    continue;
                }
                for (a, b) in left.iter().flat_map(|a| right.iter().map(move |b| (a, b))) {
                    let overlaps = (1..a.len().min(b.len()))
                        .any(|k| a.as_bytes()[a.len() - k..] == b.as_bytes()[..k]);
                    if a.contains(b.as_str()) || overlaps {
                        return None;
                    }
                    let joined = format!("{}{}", &a[a.len() - 2..], &b[..2]);
                    bridges.push(vec![joined[..3].to_owned(), joined[1..].to_owned()]);
                }
            }
        }
        let qlen = self.query_chars as f64;
        let near = 0.5 + 0.5 * (qlen / (width + count - 1) as f64).min(1.);
        Some((near.max(qlen / (qlen + 3.)), bridges))
    }
    // Each alternative keeps its own upper count. Min across terms is a sound occurrence bound.
    pub(super) fn phrase_bridges(&self) -> Vec<Vec<String>> {
        if self.phase != 1 || self.partial || self.explicit.is_some() {
            return vec![];
        }
        self.terms
            .windows(2)
            .map(|pair| {
                let (left, right) = (&pair[0][0], &pair[1][0]);
                if !left
                    .bytes()
                    .chain(right.bytes())
                    .all(|b| b.is_ascii_alphanumeric())
                {
                    return vec![];
                }
                let a = &left[left.len().saturating_sub(2)..];
                let b = &right[..right.len().min(2)];
                let bridge = format!("{a}{b}");
                let width = bridge.len().min(3);
                bridge
                    .as_bytes()
                    .windows(width)
                    .enumerate()
                    .filter(|(at, _)| *at < a.len() && *at + width > a.len())
                    .map(|(_, bytes)| String::from_utf8(bytes.to_vec()).expect("ASCII bridge"))
                    .collect()
            })
            .collect()
    }
    pub(super) fn bound_grams(&self) -> Vec<Vec<Vec<String>>> {
        let ts = if self.explicit.is_some() {
            self.required.iter().map(|s| vec![s.clone()]).collect()
        } else if self.phase == 1 && !self.partial {
            // Phrase matching uses the literal normalized tokens. Summing a
            // plural/stem alternative can double-count the same occurrence
            // and recreate an unattainable repetition ceiling.
            self.terms.iter().map(|v| vec![v[0].clone()]).collect()
        } else {
            self.terms.clone()
        };
        ts.into_iter()
            .enumerate()
            .map(|(i, vs)| {
                if self.coverage_excluded.contains(&i) {
                    return vec![];
                }
                vs.into_iter()
                    .map(|s| {
                        let n = s.chars().count().min(3);
                        let cs: Vec<_> = s.chars().collect();
                        cs.windows(n.max(1)).map(|c| c.iter().collect()).collect()
                    })
                    .collect()
            })
            .collect()
    }
    pub(super) fn prose_fts(&self, _pattern: &str) -> Option<String> {
        let groups = self.coverage_term.map_or_else(
            || self.ordered_terms.clone(),
            |i| vec![self.terms[i].clone()],
        );
        if groups.is_empty() {
            return None;
        }
        let mut out = vec![];
        for group in groups {
            // A literal phrase cannot use a plural-only variant. In the other
            // tiers a shorter substring already admits every containing variant.
            let terms: Vec<_> = if self.phase == 1
                && !self.partial
                && self.explicit.is_none()
                && !self.lowercase_terms.iter().any(|t| t.special)
            {
                group.iter().take(1).collect()
            } else {
                group
                    .iter()
                    .filter(|term| {
                        !group
                            .iter()
                            .any(|other| other.len() < term.len() && term.contains(other.as_str()))
                    })
                    .collect()
            };
            if terms.iter().any(|v| v.chars().count() < 3) {
                // AND may omit a short clause; OR must retain its possible hits.
                if self.partial {
                    return None;
                }
                continue;
            }
            out.push(format!(
                "({})",
                terms
                    .iter()
                    .map(|v| format!("\"{}\"", v.replace('"', "\"\"")))
                    .collect::<Vec<_>>()
                    .join(" OR ")
            ));
        }
        (!out.is_empty()).then(|| {
            format!(
                "text : ({})",
                out.join(if self.partial { " OR " } else { " AND " })
            )
        })
    }
    /// Indexed words for one query group. A block keeps only the words its rows contain.
    pub(super) fn group_words(&self, group: &[String], blocks: Option<&[i64]>) -> Vec<&str> {
        let mut words: Vec<&str> = Vec::new();
        for v in group {
            let expansion = &self.tool_terms[v];
            match blocks {
                None => words.extend(expansion.words.iter().map(|w| w.term.as_str())),
                Some(blocks) => {
                    for &block in blocks {
                        if let Ok(block) = u32::try_from(block) {
                            words.extend(
                                expansion
                                    .in_block(block)
                                    .iter()
                                    .map(|&i| expansion.words[i as usize].term.as_str()),
                            );
                        }
                    }
                }
            }
        }
        words.sort_unstable();
        words.dedup();
        words
    }
    /// The tool rows' necessary text condition, which takes anchors when plans may read
    /// only the rows they admit.
    pub(super) fn fts(&self, blocks: Option<&[i64]>) -> Option<String> {
        self.tool_fts(blocks, self.plan_anchors)
    }
    /// Each group the dictionary cannot expand enters through its spellings' anchors
    /// when `anchored`; without them it is left out.
    pub(super) fn tool_fts(&self, blocks: Option<&[i64]>, anchored: bool) -> Option<String> {
        let mut clauses = vec![];
        for group in &self.ordered_terms {
            let anchors = if group.iter().all(|v| self.tool_supported.contains(v)) {
                None
            } else if let Some(anchors) = self.anchors(group).filter(|_| anchored) {
                Some(anchors)
            } else {
                if self.partial {
                    return None;
                }
                continue;
            };
            let mut words = self.group_words(anchors.as_deref().unwrap_or(group), blocks);
            if let Some(anchors) = anchors
                .as_ref()
                .filter(|_| words.is_empty() && blocks.is_none())
            {
                // An anchor no dictionary word contains is no token either, so its
                // clause admits no row.
                words = anchors.iter().map(String::as_str).collect();
            }
            if words.is_empty() {
                if self.partial {
                    continue;
                }
                return None;
            }
            clauses.push(format!(
                "({})",
                words
                    .into_iter()
                    .map(|s| format!("\"{}\"", s.replace('"', "\"\"")))
                    .collect::<Vec<_>>()
                    .join(" OR ")
            ));
            if !self.partial && !self.quick_count && clauses.len() == 2 {
                break;
            }
        }
        // Intersect the two rarest required groups once; verify remaining terms
        // on their candidates. This avoids reopening a broad single-term stream
        // for each block in a long all-terms query.
        (!clauses.is_empty()).then(|| clauses.join(if self.partial { " OR " } else { " AND " }))
    }
    pub(super) fn word_positions(&self, blocks: Option<&[i64]>) -> Option<(String, Vec<u8>)> {
        if self.phase != 1
            || self.partial
            || self.terms.iter().map(|v| v[0].len()).sum::<usize>() > 4096
            || self.terms.iter().flatten().any(|v| !v.is_ascii())
        {
            return None;
        }
        let mut clauses = vec![];
        let mut ordinals = vec![];
        let literal = !self.lowercase_terms.iter().any(|t| t.special)
            && self
                .terms
                .iter()
                .all(|group| group[0].bytes().all(|b| b.is_ascii_alphanumeric()));
        let mut groups: Vec<_> = self
            .terms
            .iter()
            .map(|group| {
                if literal {
                    &group[..1]
                } else {
                    group.as_slice()
                }
            })
            .enumerate()
            .filter(|(_, group)| group.iter().all(|v| self.tool_supported.contains(v)))
            .collect();
        // A phrase prefilter needs only a necessary positional condition.
        // Intersecting every common word stream makes long questions expensive
        // even when three selective terms already rule out almost all rows.
        // Keep original ordinals so skipped words still contribute distance;
        // canonical text verifies the complete phrase afterwards.
        if groups.len() > 3 {
            // A literal token phrase lets FTS reject scattered common words
            // without enumerating their positions for the span verifier.
            let consecutive = (self.estimated > 32_768 && self.terms.len() <= 16)
                .then(|| {
                    groups.windows(3).find(|run| {
                        run.windows(2).all(|pair| pair[1].0 == pair[0].0 + 1)
                            && run.iter().all(|(_, group)| {
                                self.group_words(group, None) == [group[0].as_str()]
                            })
                    })
                })
                .flatten();
            if let Some(run) = consecutive {
                groups = run.to_vec();
            } else {
                groups.sort_by_key(|(i, group)| {
                    (
                        self.ordered_terms
                            .iter()
                            .position(|ordered| ordered.first() == group.first())
                            .unwrap_or(usize::MAX),
                        *i,
                    )
                });
                groups.truncate(3);
                groups.sort_by_key(|(i, _)| *i);
            }
        }
        for (i, group) in groups {
            let words = self.group_words(group, blocks);
            if words.is_empty() {
                return None;
            }
            clauses.push(format!(
                "({})",
                words
                    .iter()
                    .map(|w| format!("\"{}\"", w.replace('"', "\"\"")))
                    .collect::<Vec<_>>()
                    .join(" OR ")
            ));
            for _ in words {
                ordinals.extend((i as u32).to_le_bytes());
            }
        }
        (clauses.len() >= 2).then(|| (clauses.join(" AND "), ordinals))
    }
    pub(super) fn prose_positions(&self) -> Option<(String, Vec<u8>)> {
        if self.phase != 1 || self.partial || self.explicit.is_some() {
            return None;
        }
        let mut clauses = Vec::new();
        let mut ordinals = Vec::new();
        for (i, terms) in self.terms.iter().enumerate() {
            let term = &terms[0];
            if term.chars().count() >= 3 {
                clauses.push(format!("\"{}\"", term.replace('"', "\"\"")));
                ordinals.extend((i as u32).to_le_bytes());
            }
        }
        (clauses.len() >= 2).then(|| (clauses.join(" AND "), ordinals))
    }
}
/// One spelling's anchor: the spelling itself when the dictionary expands it, or else its
/// rarest run that a unicode61 token always holds whole. That is an ASCII alphanumeric
/// run of 3 to 4096 bytes, or a run of two or more characters unicode61 keeps as itself.
fn anchor(
    c: &Connection,
    cache: &mut PlanCache,
    variant: &str,
) -> Result<Option<(String, std::sync::Arc<Expansion>)>> {
    if let Some(words) = cache.words(c, variant)? {
        return Ok(Some((variant.to_owned(), words)));
    }
    let mut rarest: Option<(usize, &str)> = None;
    for run in variant
        .split(|ch: char| !ch.is_ascii_alphanumeric())
        .filter(|run| (3..=4096).contains(&run.len()))
    {
        // A run occurs in no more rows than its rarest trigram's words.
        let mut doc = usize::MAX;
        for gram in run.as_bytes().windows(3) {
            let gram = std::str::from_utf8(gram).expect("ASCII run");
            doc = doc.min(cache.trigram_doc(c, gram)?.unwrap_or(0));
        }
        if rarest.is_none_or(|(least, _)| doc < least) {
            rarest = Some((doc, run));
        }
    }
    let mut chosen = match rarest {
        Some((_, run)) => cache.words(c, run)?.map(|words| (run.to_owned(), words)),
        None => None,
    };
    for run in token_runs(c, variant)? {
        let words = cache.run_words(c, &run)?;
        if chosen
            .as_ref()
            .is_none_or(|(_, least)| words.doc < least.doc)
        {
            chosen = Some((run, words));
        }
    }
    Ok(chosen)
}
/// The non-ASCII tokens unicode61 finds in `variant` that it keeps whole as themselves:
/// wherever the folded text holds `variant`, one indexed token holds each of them.
fn token_runs(c: &Connection, variant: &str) -> Result<Vec<String>> {
    if variant.is_ascii() {
        return Ok(Vec::new());
    }
    let tokenizers = crate::tool_index::Tokenizers::new(c)?;
    let mut tokenizer = tokenizers.tokenizer()?;
    let mut tokens = Vec::new();
    tokenizer.tokens(variant, |token| tokens.push(token.to_vec()))?;
    let mut runs = Vec::new();
    for token in tokens {
        let Ok(run) = String::from_utf8(token) else {
            continue;
        };
        // Overlapping windows keep every literal of up to 4 KiB inside one indexed token.
        if run.is_ascii()
            || run.len() > 4096
            || run.chars().nth(1).is_none()
            || !variant.contains(run.as_str())
            || runs.contains(&run)
        {
            continue;
        }
        let mut whole = Vec::new();
        tokenizer.tokens(&run, |token| whole.push(token.to_vec()))?;
        if whole == [run.as_bytes()] {
            runs.push(run);
        }
    }
    Ok(runs)
}
