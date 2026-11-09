use super::*;

impl PlanCache {
    pub(super) fn matcher(&mut self, o: &Options, c: &Connection) -> Result<Matcher> {
        let key = (o.pattern.clone(), o.regex, o.word);
        let needs_phrase = !o.regex && !o.word && (!o.count_only || o.count_by_tier);
        if self
            .matcher
            .as_ref()
            .is_none_or(|(cached, m)| *cached != key || (needs_phrase && m.phrase.is_none()))
        {
            self.matcher = Some((key, Matcher::new(o, c)?));
        }
        let mut m = self.matcher.as_ref().expect("cached above").1.clone();
        m.quick_count = o.count_only && !o.count_by_tier;
        Ok(m)
    }
    pub(super) fn coverage_bounds(
        &mut self,
        c: &Connection,
        m: &Matcher,
        lane_total: usize,
    ) -> Result<Option<CoverageBounds>> {
        let mut groups = Vec::new();
        for variants in &m.terms {
            let mut estimate = 0usize;
            let mut terms = Vec::new();
            for term in variants {
                if variants
                    .iter()
                    .any(|other| other.len() < term.len() && term.contains(other.as_str()))
                {
                    continue;
                }
                let chars: Vec<_> = term.chars().collect();
                if chars.len() < 3 {
                    estimate = usize::MAX;
                    break;
                }
                let mut df = usize::MAX;
                for gram in chars.windows(3) {
                    let gram: String = gram.iter().collect();
                    let n = c
                        .query_row("SELECT doc FROM prose_terms WHERE term=?", [&gram], |r| {
                            r.get::<_, usize>(0)
                        })
                        .optional()?
                        .unwrap_or(0);
                    df = df.min(n);
                }
                estimate = estimate.saturating_add(df);
                terms.push(format!("\"{}\"", term.replace('"', "\"\"")));
            }
            // Common and short terms remain unknown (possibly present) in the
            // upper bound; never enumerate their corpus-wide postings.
            groups.push(
                if estimate > 65_536 || estimate.saturating_mul(2) > lane_total {
                    None
                } else {
                    self.candidate_rows(
                        c,
                        "prose",
                        &format!("text : ({})", terms.join(" OR ")),
                        None,
                    )?
                },
            );
        }
        let Some(selected) = m.coverage_term.and_then(|i| groups[i].as_ref()) else {
            return Ok(None);
        };
        let contains = |group: &CandidateGroups, block: i64, id: i64| {
            group
                .get(&block)
                .is_some_and(|ids| ids.binary_search(&id).is_ok())
        };
        let mut candidates = HashMap::new();
        let mut rows = HashMap::new();
        let mut blocks = HashMap::new();
        for (&block, ids) in selected.iter() {
            for &id in ids {
                if m.coverage_excluded
                    .iter()
                    .any(|&i| groups[i].as_ref().is_some_and(|g| contains(g, block, id)))
                {
                    continue;
                }
                let weight = groups
                    .iter()
                    .enumerate()
                    .filter(|(_, g)| g.as_ref().is_none_or(|g| contains(g, block, id)))
                    .map(|(i, _)| m.idf[i])
                    .sum::<f64>();
                candidates.entry(block).or_insert_with(Vec::new).push(id);
                rows.insert(id, weight);
                let upper = blocks.entry(block).or_insert(0.0_f64);
                *upper = upper.max(weight);
            }
        }
        Ok(Some(CoverageBounds {
            candidates: std::sync::Arc::new(candidates),
            rows,
            blocks,
        }))
    }
    pub(super) fn candidate_rows(
        &mut self,
        c: &Connection,
        table: &str,
        expression: &str,
        ordinals: Option<&[u8]>,
    ) -> Result<Option<CandidateGroups>> {
        let key = format!("{table}:{ordinals:?}:{expression}");
        if self.rejected_candidates.contains(&key) {
            return Ok(None);
        }
        if let Some(rows) = self.candidates.get(&key) {
            return Ok(Some(rows.clone()));
        }
        let (sql, bind) = if let Some(ordinals) = ordinals.filter(|_| table == "prose") {
            ("SELECT rowid FROM prose_fts WHERE prose_fts MATCH ? AND agrep_word_span(prose_fts,?,2147483647)".into(),vec![Value::Text(expression.into()),Value::Blob(ordinals.to_vec())])
        } else if let Some(ordinals) = ordinals {
            (format!("SELECT f.rowid FROM tools_fts f LEFT JOIN tool_blocks b ON b.id=f.rowid/{BLOCK} LEFT JOIN event_rows e INDEXED BY event_tool_id ON e.tool_id=f.rowid AND e.active=1 LEFT JOIN tool_blobs t ON t.id=e.blob WHERE tools_fts MATCH ? AND agrep_word_span(tools_fts,?,CASE WHEN t.ascii THEN 0 ELSE coalesce(b.max_gap,2147483647) END)"),vec![Value::Text(expression.into()),Value::Blob(ordinals.to_vec())])
        } else {
            (
                format!("SELECT rowid FROM {table}_fts WHERE {table}_fts MATCH ?"),
                vec![Value::Text(expression.into())],
            )
        };
        let mut by_block: HashMap<i64, Vec<i64>> = HashMap::new();
        let mut query = c.prepare(&sql)?;
        for (retained, row) in query
            .query_map(params_from_iter(bind), |r| r.get::<_, i64>(0))?
            .enumerate()
        {
            let id = row?;
            if retained == 65_536 {
                // This caps a planner allocation, never the matching stream:
                // callers fall back to the complete bounded block cursor.
                self.rejected_candidates.insert(key);
                return Ok(None);
            }
            by_block.entry(id / BLOCK).or_default().push(id);
        }
        for ids in by_block.values_mut() {
            ids.sort_unstable();
        }
        let rows = std::sync::Arc::new(by_block);
        self.candidates.insert(key, rows.clone());
        Ok(Some(rows))
    }
    pub(super) fn words(
        &mut self,
        c: &Connection,
        term: &str,
    ) -> Result<Option<std::sync::Arc<Expansion>>> {
        if !self.words.contains_key(term) {
            let expansion = crate::tool_index::expand(c, term)?
                .map(|words| std::sync::Arc::new(Expansion::new(words)));
            self.words.insert(term.to_owned(), expansion);
        }
        Ok(self.words[term].clone())
    }
    /// Every indexed tool word containing `run`, a non-ASCII run unicode61 keeps whole as
    /// itself, and the run alone as a token when it is too short for the dictionary.
    pub(super) fn run_words(
        &mut self,
        c: &Connection,
        run: &str,
    ) -> Result<std::sync::Arc<Expansion>> {
        if let Some(words) = self.runs.get(run) {
            return Ok(words.clone());
        }
        let chars: Vec<char> = run.chars().collect();
        let mut words = Vec::new();
        let mut push = |r: &rusqlite::Row<'_>| -> Result<()> {
            words.push(crate::tool_index::Word {
                term: r.get(0)?,
                doc: r.get(1)?,
                blocks: crate::tool_index::word_blocks(c, r.get(3)?, r.get_ref(2)?.as_blob()?)?,
            });
            Ok(())
        };
        if chars.len() >= 3 {
            let expression = chars
                .windows(3)
                .map(|gram| format!("\"{}\"", gram.iter().collect::<String>()))
                .collect::<Vec<_>>()
                .join(" AND ");
            let mut q = c.prepare("SELECT w.term,w.doc,w.blocks,w.id FROM tool_lexicon l CROSS JOIN tool_words w ON w.id=l.rowid WHERE tool_lexicon MATCH ?1 AND instr(w.term,?2)>0 AND w.doc>0")?;
            let mut rows = q.query(rusqlite::params![expression, run])?;
            while let Some(r) = rows.next()? {
                push(r)?;
            }
        } else {
            // The trigram lexicon cannot look up two characters: read every word.
            let mut q = c.prepare(
                "SELECT term,doc,blocks,id FROM tool_words WHERE instr(term,?1)>0 AND doc>0",
            )?;
            let mut rows = q.query([run])?;
            while let Some(r) = rows.next()? {
                push(r)?;
            }
            let mut blocks: Vec<u32> = Vec::new();
            let mut doc = 0;
            let mut q = c.prepare("SELECT rowid FROM tools_fts WHERE tools_fts MATCH ?")?;
            for id in q.query_map([format!("\"{run}\"")], |r| r.get::<_, i64>(0))? {
                let block = u32::try_from(id? / BLOCK)?;
                if blocks.last() != Some(&block) {
                    blocks.push(block);
                }
                doc += 1;
            }
            if doc > 0 {
                words.push(crate::tool_index::Word {
                    term: run.to_owned(),
                    doc,
                    blocks,
                });
            }
        }
        let words = std::sync::Arc::new(Expansion::new(words));
        self.runs.insert(run.to_owned(), words.clone());
        Ok(words)
    }
    /// A three-character term's word frequency total without expanding it.
    pub(super) fn trigram_doc(&mut self, c: &Connection, term: &str) -> Result<Option<usize>> {
        if let Some(&doc) = self.trigram_docs.get(term) {
            return Ok(doc);
        }
        let doc = crate::tool_index::trigram_doc(c, term)?;
        self.trigram_docs.insert(term.to_owned(), doc);
        Ok(doc)
    }
    /// The summed tool word frequency of a term; None if it cannot use the dictionary.
    pub(super) fn tool_doc(&mut self, c: &Connection, term: &str) -> Result<Option<usize>> {
        if let Some(doc) = self.trigram_doc(c, term)? {
            return Ok(Some(doc));
        }
        Ok(self.words(c, term)?.map(|words| words.doc))
    }
}
