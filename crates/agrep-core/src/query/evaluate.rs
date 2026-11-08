use super::*;

impl Matcher {
    fn evaluate_explicit(
        &self,
        pattern: &Pattern,
        text: &str,
        who: &str,
        rec: f64,
        meta: bool,
    ) -> Result<Option<Scored>> {
        let Some(span) = pattern.find(text)? else {
            return Ok(None);
        };
        if self.quick_count {
            return Ok(Some(Scored::bare(0, "counted")));
        }
        let snip = Snip::new(text, std::slice::from_ref(&span));
        let (mut n, mut best) = (0, 0);
        if snip.chars >= self.explicit_chars {
            pattern.each(&snip.text, |m| {
                n += 1;
                let length = snip.text[m].chars().count();
                if best == 0 || length < best {
                    best = length;
                }
            })?;
        }
        let strength = window::strength(n, best, self.explicit_chars);
        Ok(Some(Scored {
            tier: 0,
            score: score(strength, rec.max(0.5), who, 1., meta),
            class: "explicit",
            snippet: snip.text,
            span: Some(span),
        }))
    }
    fn evaluate_all_terms(
        &self,
        row: &Folded,
        who: &str,
        rec: f64,
        meta: bool,
    ) -> Result<Option<Scored>> {
        let text = row.text;
        let Some(spans) = self
            .terms
            .iter()
            .enumerate()
            .map(|(i, _)| self.term_span(row, i))
            .collect::<Result<Option<Vec<_>>>>()?
        else {
            return Ok(None);
        };
        let snip = Snip::new(text, &spans);
        // A contiguous window cannot introduce a phrase absent from the row.
        // Disjoint windows can join terms across their cut markers.
        let phrase = if snip.contiguous()
            && self
                .terms
                .iter()
                .flatten()
                .all(|t| t.bytes().all(|b| b.is_ascii_alphanumeric()))
        {
            0.
        } else {
            self.phrase_strength(row, &snip, &View::new(row, &snip))?
        };
        // First aligned spans already maximize every boundary factor. A cut
        // duplicates its neighbour, so doubled spelling edges need regrading.
        let reusable = text.is_ascii()
            && self.lowercase_terms.iter().all(|t| !t.special)
            && self.terms.iter().flatten().all(|t| {
                let b = t.as_bytes();
                b.len() >= 2
                    && b.iter().all(u8::is_ascii_alphanumeric)
                    && b[0] != b[1]
                    && b[b.len() - 1] != b[b.len() - 2]
            })
            && spans.iter().all(|s| window::aligned(text, s));
        let (factor, class, selected, qualities) =
            if let Some(selected) = reusable.then(|| snip.selected(&spans)).flatten() {
                (1., "aligned", selected, vec![1.; spans.len()])
            } else {
                let graded = boundary_rank::decut_text(&snip.text, true);
                let b = self.boundary.evaluate(&graded, None, true)?;
                let shift = i64::from(snip.text.starts_with(window::CUT));
                let selected = b
                    .spans
                    .iter()
                    .map(|span| span.map(|[start, end]| [start - shift, end - shift]))
                    .collect();
                (b.factor, b.match_class, selected, b.qualities)
            };
        let near = window::proximity(&snip.text, &selected, &qualities, self.query_chars);
        Ok(Some(Scored {
            tier: 1,
            score: score(phrase.max(near), rec, who, factor, meta),
            class,
            span: spans
                .iter()
                .min_by_key(|span| (span.start, span.end))
                .cloned(),
            snippet: snip.text,
        }))
    }
    fn evaluate_content(
        &self,
        row: &Folded,
        who: &str,
        rec: f64,
        meta: bool,
        found: (&[usize], f64),
        cutoff: Option<&Cutoff>,
    ) -> Result<Option<Scored>> {
        let text = row.text;
        let low = &row.low;
        let (found, found_weight) = found;
        // The earliest occurrence of a found term anchors the snippet, as 0.4.9's content
        // lanes place theirs; they find the literal term, so a spelling variant counts
        // only for a term found through it alone.
        let anchor = || {
            found
                .iter()
                .filter_map(|&i| {
                    let literal = &self.content_literals[i];
                    low.find(literal.as_str())
                        .map(|at| (at, at + literal.len()))
                })
                .min_by_key(|&(at, _)| at)
                .map(|(at, end)| row.start(at)..row.end(end))
        };
        // 0.4.9's content lane (py/corpusdb.py:4897-4908, py/search.py:1852-1876): the
        // row's share of the query's weight, to four places, is its whole match strength,
        // without a boundary factor.
        let total = self.idf.iter().sum::<f64>();
        let coverage = window::round4(found_weight / if total == 0. { 1. } else { total });
        if cutoff.is_some_and(|c| c.excludes(2, coverage * rec * weight(who) * meta_scale(meta))) {
            return Ok(Some(Scored::bare(2, "unscored")));
        }
        let anchor = anchor();
        Ok(Some(Scored {
            tier: 2,
            score: score(coverage, rec, who, 1., meta),
            class: "",
            snippet: Snip::new(text, anchor.as_slice()).text,
            span: anchor,
        }))
    }

    pub(super) fn content(&mut self, pattern: &str) -> Result<()> {
        self.content_literals = content_terms(pattern)
            .into_iter()
            .map(|v| v[0].clone())
            .collect();
        self.terms = self
            .content_literals
            .iter()
            .map(|v| vec![store::search_fold(v)])
            .collect();
        self.content_patterns = self
            .content_literals
            .iter()
            .map(|v| {
                if v.chars().count() >= 3 && v.contains("i\u{307}") {
                    regex::Regex::new(&crate::pattern::literal(v)).map(Some)
                } else {
                    Ok(None)
                }
            })
            .collect::<std::result::Result<_, _>>()?;
        self.lowercase_terms.clear();
        self.dotted_candidates.clear();
        Ok(())
    }
    pub(super) fn matching_expression(&self, row: &Folded) -> Result<&regex::Regex> {
        if !row.raw {
            return Ok(self.phrase.as_ref().unwrap());
        }
        // Ι, ι and ι share a case key with combining U+0345; only that
        // class changes Python's word membership when folded.
        self.raw_capture
            .get_or_init(|| {
                let groups: Vec<_> = boundary_rank::query_tokens(&self.raw_query)
                    .iter()
                    .map(|t| format!("({})", crate::pattern::literal(t)))
                    .collect();
                regex::Regex::new(&groups.join(crate::unicode_v16::keyword_gap()))
            })
            .as_ref()
            .map_err(|e| anyhow::anyhow!(e.to_string()))
    }
    pub(super) fn term_span(
        &self,
        row: &Folded,
        index: usize,
    ) -> Result<Option<std::ops::Range<usize>>> {
        if let Some(term) = self.lowercase_terms.get(index) {
            if term.special || row.aliases & term.mask != 0 {
                if let Some(span) = row.lower_span(&term.lowercase) {
                    return Ok(Some(span));
                }
                let raw = term
                    .raw
                    .get_or_init(|| regex::Regex::new(&crate::pattern::literal(&term.original)))
                    .as_ref()
                    .map_err(|e| anyhow::anyhow!(e.to_string()))?;
                return Ok(raw.find(row.text).map(|m| m.range()));
            }
        }
        Ok(insensitive_span(row, &self.terms[index]))
    }
    pub(super) fn term_present(&self, row: &Folded, index: usize) -> bool {
        if self.partial {
            return row.low.contains(&self.content_literals[index]);
        }
        let terms = &self.terms[index];
        // Each key character is one re.I case class, so this proves the raw fallback.
        if row.low.contains(&terms[0]) {
            return true;
        }
        if let Some(term) = self.lowercase_terms.get(index) {
            if term.special || row.aliases & term.mask != 0 {
                return row.lower_span(&term.lowercase).is_some();
            }
        }
        terms.iter().skip(1).any(|v| row.low.contains(v))
    }
    pub(super) fn count_needs_verification(&self) -> bool {
        self.terms
            .iter()
            .any(|group| group.iter().any(|v| !v.contains(&group[0])))
    }
    pub(super) fn case_expression(&self) -> String {
        let quote = |v: &str| format!("\"{}\"", v.replace('"', "\"\""));
        let expression = self
            .terms
            .iter()
            .map(|group| {
                format!(
                    "({})",
                    group
                        .iter()
                        .map(|v| quote(v))
                        .collect::<Vec<_>>()
                        .join(" OR ")
                )
            })
            .collect::<Vec<_>>()
            .join(" AND ");
        let originals = self
            .terms
            .iter()
            .filter(|group| group.iter().any(|v| !v.contains(&group[0])))
            .map(|group| quote(&group[0]))
            .collect::<Vec<_>>()
            .join(" AND ");
        // Every other candidate already proves the original token's raw fallback.
        format!("({expression}) NOT ({originals})")
    }

    pub(super) fn evaluate(
        &self,
        text: &str,
        who: &str,
        turn: i64,
        ts: i64,
        now: i64,
        cutoff: Option<&Cutoff>,
    ) -> Result<Option<Scored>> {
        let rec = recency(ts, now).max(if who == "user" { 0.35 } else { 0. });
        let meta = meta_role(who, turn);
        if let Some(pattern) = &self.explicit {
            return self.evaluate_explicit(pattern, text, who, rec, meta);
        }

        if !self.dotted_candidates.is_empty() && !text.contains(['İ', 'ı', 'ſ', 'K']) {
            // 0.4.9 lowercased keyword tokens before the FTS/LIKE candidate
            // filter, widening only to its case-exception rows.
            let lowercase = crate::unicode_v16::lowercase(text);
            if self.dotted_candidates.iter().any(|term| {
                !lowercase.contains(term) && (term.chars().count() >= 3 || text.is_ascii())
            }) {
                return Ok(None);
            }
        }
        let row = if self.partial {
            Folded::lowercase(text)
        } else {
            Folded::new(text)
        };
        let low = &row.low;
        if self.partial {
            // The legacy content FTS lane had no rare-case widening. A
            // lowercase expansion alone cannot supply an indexed trigram.
            let candidate = |i: usize| {
                low.contains(&self.content_literals[i])
                    && self.content_patterns[i]
                        .as_ref()
                        .is_none_or(|p| p.is_match(text))
            };
            if self.coverage_term.is_some_and(|i| !candidate(i))
                || self.coverage_excluded.iter().any(|&i| candidate(i))
            {
                return Ok(None);
            }
        }
        if self.quick_count {
            let mut matches = (0..self.terms.len()).map(|i| self.term_present(&row, i));
            let found = if self.partial {
                matches.any(|v| v)
            } else {
                matches.all(|v| v)
            };
            return Ok(found.then(|| Scored::bare(0, "counted")));
        }
        let mut found = Vec::new();
        let mut found_weight = 0.;
        // A literal phrase or the selected term spans already prove membership on
        // ASCII rows. Case-alias and content lanes keep their separate eligibility.
        let verify_terms =
            self.partial || !text.is_ascii() || self.lowercase_terms.iter().any(|t| t.special);
        if verify_terms {
            for i in 0..self.terms.len() {
                let present = self.term_present(&row, i);
                if !present {
                    if self.partial {
                        continue;
                    } else {
                        return Ok(None);
                    }
                }
                found_weight += self.idf.get(i).copied().unwrap_or(1.);
                found.push(i);
            }
        }
        if verify_terms && found.is_empty() {
            return Ok(None);
        }
        let first = if self.partial {
            None
        } else if self.terms.len() == 1 {
            self.term_span(&row, 0)?
        } else {
            self.matching_expression(&row)?
                .find(row.matching_text())
                .map(|m| row.matching_span(m.range()))
        };
        if (self.phase == 1 && first.is_none()) || (self.phase == 2 && first.is_some()) {
            return Ok(None);
        }
        if let Some(first) = first {
            // 0.4.9 anchors a lone word at its first aligned occurrence and a phrase at
            // its first match.
            let anchor = first;
            let snip = Snip::new(text, std::slice::from_ref(&anchor));
            let view = View::new(&row, &snip);
            let strength = self.phrase_strength(&row, &snip, &view)?;
            let (w, s) = speaker(who);
            // Boundary factors never exceed one, so this is the row's best score.
            if cutoff.is_some_and(|c| c.excludes(0, strength * rec * w * s * meta_scale(meta))) {
                return Ok(Some(Scored::bare(0, "unscored")));
            }
            let (factor, class) = self.phrase_boundary(&row, &snip, &view)?;
            return Ok(Some(Scored {
                tier: 0,
                score: score(strength, rec, who, factor, meta),
                class,
                snippet: snip.text,
                span: Some(anchor),
            }));
        }
        if !self.partial {
            return self.evaluate_all_terms(&row, who, rec, meta);
        }
        self.evaluate_content(&row, who, rec, meta, (&found, found_weight), cutoff)
    }
    /// The largest coverage factor a partial row reaches when the terms it holds weigh at
    /// most `weight`: rounding is monotonic, so the rounded share bounds every rounded
    /// coverage.
    pub(super) fn partial_share(&self, weight: f64) -> f64 {
        let total = self.idf.iter().sum::<f64>();
        window::round4(weight / if total == 0. { 1. } else { total })
    }
    /// 0.4.9's phrase strength in a snippet: its matches counted, the tightest measured in
    /// snippet characters.
    pub(super) fn phrase_strength(&self, row: &Folded, snip: &Snip, view: &View) -> Result<f64> {
        let (mut n, mut best) = (0, 0);
        if snip.chars >= self.query_chars {
            for m in self.matching_expression(row)?.find_iter(&view.text) {
                n += 1;
                let length = view.span(row, snip, m.range()).len();
                if best == 0 || length < best {
                    best = length;
                }
            }
        }
        Ok(window::strength(n, best, self.query_chars))
    }
    /// 0.4.9's phrase boundary factor: graded on the de-cut snippet at each match's token
    /// spans, the best factor, then the shorter match.
    pub(super) fn phrase_boundary(
        &self,
        row: &Folded,
        snip: &Snip,
        view: &View,
    ) -> Result<(f64, &'static str)> {
        let capture = if row.raw {
            self.matching_expression(row)?
        } else {
            self.capture.as_ref().unwrap()
        };
        if capture.captures_len() - 1 != self.boundary.len() {
            return Ok((1., "aligned"));
        };
        let graded = boundary_rank::decut_text(&snip.text, false);
        let mut best: Option<(f64, usize, &'static str)> = None;
        for groups in capture.captures_iter(&view.text) {
            let spans: Vec<_> = (1..groups.len())
                .map(|i| {
                    groups.get(i).map(|g| {
                        let span = view.span(row, snip, g.range());
                        [span.start as i64, span.end as i64]
                    })
                })
                .collect();
            let length = groups
                .get(0)
                .map_or(0, |m| view.span(row, snip, m.range()).len());
            let value = self
                .boundary
                .evaluate_compact(&graded, Some(&spans), false)?;
            if best.is_none_or(|(factor, shortest, _)| {
                value.factor > factor || (value.factor == factor && length < shortest)
            }) {
                best = Some((value.factor, length, value.match_class));
            }
        }
        Ok(best.map_or((1., "aligned"), |(factor, _, class)| (factor, class)))
    }
}
