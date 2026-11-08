use super::*;
impl PassContext<'_> {
    pub(super) fn quick_count(&self, result: &mut Results, plan_started: Instant) -> Result<bool> {
        let c = self.c;
        let o = self.o;
        let m = self.m;
        let table = self.table;
        let scope = self.scope;
        let values = self.values;
        let partial = self.m.partial;
        if m.quick_count
            && !o.chats
            && m.explicit.is_none()
            && !m.lowercase_terms.iter().any(|t| t.special)
            && !partial
            && m.terms.iter().flatten().all(|t| t.chars().count() >= 3)
            && (table != "tools"
                || m.terms
                    .iter()
                    .flatten()
                    .all(|t| m.tool_supported.contains(t)))
        {
            let expression = if table == "prose" {
                m.prose_fts(&o.pattern)
            } else {
                m.fts(None)
            };
            if let Some(expression) = expression {
                let count_scope = self.count_scope()?;
                result.timings.plan_ms += plan_started.elapsed().as_secs_f64() * 1000.;
                let started = Instant::now();
                let count = self.count_postings(&expression, count_scope, result)?;
                let mut count = count;
                if m.count_needs_verification() {
                    // A lowercase plural variant may match while the original token's
                    // re.I fallback does not. Only case-alias rows need correction.
                    let sql = format!("SELECT p.text,p.session FROM {table}_case_fts f CROSS JOIN {table} p ON p.id=f.rowid WHERE {table}_case_fts MATCH ?{scope}");
                    let mut bind = vec![Value::Text(m.case_expression())];
                    bind.extend(values.clone());
                    let mut statement = c.prepare(&sql)?;
                    let mut rows = statement.query(params_from_iter(bind))?;
                    while let Some(row) = rows.next()? {
                        let text = store::decode(table, row.get(0)?)?;
                        if m.evaluate(&text, "tool", 0, 0, 0, None)?.is_none() {
                            count -= 1;
                            if o.filters.sessions.is_some() {
                                let session: String = row.get(1)?;
                                if let Some(n) = result.session_counts.get_mut(&session) {
                                    *n -= 1;
                                }
                            }
                        }
                    }
                }
                result.timings.sqlite_ms += started.elapsed().as_secs_f64() * 1000.;
                result.total += count;
                if table == "tools" {
                    result.tool_rows += count;
                }
                return Ok(true);
            }
        }

        Ok(false)
    }
    fn count_postings(
        &self,
        expression: &String,
        planned: CountScope,
        result: &mut Results,
    ) -> Result<usize> {
        let c = self.c;
        let o = self.o;
        let m = self.m;
        let table = self.table;
        let scope = self.scope;
        let values = self.values;
        let f = &o.filters;
        let CountScope {
            narrowed,
            sessions,
            small_scope,
        } = planned;
        let prose = (table == "prose").then_some(expression.as_str());
        let count = if sessions || small_scope {
            let rows = format!("SELECT p.id,p.session FROM {table} p WHERE 1{scope}");
            let tally = f.sessions.is_some().then_some(&mut result.session_counts);
            count_scope(c, m, table, prose, &rows, values, tally)?
        } else if narrowed {
            if table == "tools" {
                count_tool_terms(c, m, scope, values, false)?
            } else {
                let mut bind = vec![Value::Text(expression.clone())];
                bind.extend(values.clone());
                c.query_row(&format!("SELECT count(*) FROM {table}_fts f CROSS JOIN {table} p ON p.id=f.rowid WHERE {table}_fts MATCH ?{scope}"), params_from_iter(bind), |r| r.get(0))?
            }
        } else {
            // Every match minus those inside the rows the caller withholds.
            let all = if table == "tools" {
                count_tool_terms(c, m, "", &[], true)?
            } else {
                c.query_row(
                    &format!("SELECT count(*) FROM {table}_fts WHERE {table}_fts MATCH ?"),
                    [&expression],
                    |r| r.get::<_, usize>(0),
                )?
            };
            match &f.caller {
                Some(caller) => all - caller_withheld(c, m, table, prose, caller)?,
                None => all,
            }
        };
        Ok(count)
    }
}

struct CountScope {
    narrowed: bool,
    sessions: bool,
    small_scope: bool,
}
impl PassContext<'_> {
    fn count_scope(&self) -> Result<CountScope> {
        let c = self.c;
        let f = &self.o.filters;
        let table = self.table;
        let filter_scope = self.filter_scope;
        let values = self.values;
        let narrowed = f.agent.is_some()
            || f.project.is_some()
            || f.exclude_project.is_some()
            || f.model.is_some()
            || f.who.is_some()
            || f.no_who.is_some()
            || f.since.is_some()
            || f.until.is_some()
            || f.no_side;
        let sessions = f.chat.is_some() || f.session_rows.is_some() || f.sessions.is_some();
        let metadata_scope = f.agent.is_some()
            || f.project.is_some()
            || f.exclude_project.is_some()
            || f.model.is_some()
            || f.since.is_some()
            || f.until.is_some();
        let small_scope = if metadata_scope && !sessions {
            c.query_row(
                        &format!("SELECT count(*) FROM (SELECT p.id FROM {table} p WHERE 1{filter_scope} LIMIT 4097)"),
                        params_from_iter(values.clone()),
                        |r| r.get::<_, usize>(0),
                    )? <= 4096
        } else {
            false
        };
        Ok(CountScope {
            narrowed,
            sessions,
            small_scope,
        })
    }
}
