use super::*;

#[derive(Clone, Copy)]
pub(super) struct BlockRead<'a> {
    pub(super) block: i64,
    pub(super) bound: f64,
    pub(super) max_gap: i64,
    pub(super) ascii: bool,
    pub(super) block_ids: Option<&'a Vec<i64>>,
    pub(super) spanned: bool,
    pub(super) prefetched: bool,
    pub(super) buffered: bool,
    pub(super) weights: Option<&'a Vec<u8>>,
}
pub(super) struct RowQuery {
    pub(super) sql: String,
    pub(super) bind: Vec<Value>,
    pub(super) lazy_identity: bool,
    pub(super) eager_prose: bool,
    pub(super) weighted: bool,
}
impl TablePlan<'_> {
    fn uses_fts(&self, read: &BlockRead<'_>) -> bool {
        !self.selective && self.fts.is_some() && !read.buffered && !read.spanned && !read.prefetched
    }
    pub(super) fn candidate_select(
        &self,
        result: &Results,
        read: &BlockRead<'_>,
    ) -> Result<(String, Vec<Value>, bool, bool, bool)> {
        let o = self.ctx.o;
        let m = self.ctx.m;
        let now = self.ctx.now;
        let table = self.ctx.table;
        let scope = self.ctx.scope;
        let fts_table = self.fts_table;
        let fts = &self.fts;
        let selective = self.selective;
        let bounded = self.bounded;
        let partial = self.ctx.m.partial;
        let phase = self.ctx.m.phase;
        let BlockRead { ascii, .. } = *read;
        let mut bind = vec![];
        let occurrence_weights = read.weights;
        if let Some(weights) = occurrence_weights {
            bind.push(Value::Blob(weights.clone()));
            bind.push(Value::Integer(now));
            bind.push(Value::Real(
                if o.limit > 0 && result.hits.len() >= o.limit && result.hits[o.limit - 1].tier == 0
                {
                    result.hits[o.limit - 1].score
                } else {
                    -1.
                },
            ));
            bind.push(Value::Integer(i64::from(ascii)));
        }
        let uses_fts = self.uses_fts(read);
        let candidate_table = if table == "tools" && (uses_fts || read.buffered || bounded) {
            crate::tool_store::CANDIDATES_BY_ID
        } else {
            table
        };
        let from = if uses_fts {
            bind.push(Value::Text(fts.clone().unwrap()));
            format!(
                    "{fts_table} f CROSS JOIN {candidate_table} p ON f.rowid=p.id WHERE {fts_table} MATCH ?"
                )
        } else if table == "prose" && (read.buffered || bounded) {
            // The candidate IDs already establish the selective access path.
            // An agent/time index here can rescan a large scope per block.
            format!("{table} p NOT INDEXED WHERE 1")
        } else {
            format!("{candidate_table} p WHERE 1")
        };
        // A tool candidate row names its stored text, read without the view's joins.
        let blob = if candidate_table == crate::tool_store::CANDIDATES_BY_ID {
            "p.blob"
        } else {
            "NULL"
        };
        let lazy_identity = candidate_table == crate::tool_store::CANDIDATES_BY_ID;
        let identity_columns = if lazy_identity {
            "NULL,NULL"
        } else {
            "p.session,p.agent"
        };
        let eager_prose = table == "prose"
            && !selective
            && !partial
            && m.explicit.is_none()
            && ((phase == 2 && m.terms.len() > 1) || fts.is_none());
        let body = if eager_prose {
            "p.text"
        } else if occurrence_weights.is_some() {
            "agrep_tool_window_occurrences(tools_fts,?,p.id,p.ts,?,?,?)"
        } else {
            "NULL"
        };
        let sql=format!("SELECT p.id,{identity_columns},{blob},p.position,p.who,{body},NULL,p.ts,NULL,NULL,NULL FROM {from}{scope}");
        Ok((
            sql,
            bind,
            lazy_identity,
            eager_prose,
            occurrence_weights.is_some(),
        ))
    }
    pub(super) fn row_query(&self, result: &mut Results, read: &BlockRead<'_>) -> Result<RowQuery> {
        let o = self.ctx.o;
        let table = self.ctx.table;
        let values = self.ctx.values;
        let pass_tier = self.ctx.pass_tier;
        let span_plan = &self.span_plan;
        let bounded = self.bounded;
        let BlockRead {
            block,
            bound,
            max_gap,
            block_ids,
            ..
        } = *read;
        let (mut sql, mut bind, lazy_identity, eager_prose, weighted) =
            self.candidate_select(result, read)?;
        bind.extend(values.clone());
        if self.uses_fts(read) {
            if let Some((_, ordinals)) = &span_plan {
                sql.push_str(&if table == "tools" {
                        " AND agrep_word_span(tools_fts,?,CASE WHEN (SELECT ascii FROM tool_blobs WHERE id=p.blob) THEN 0 ELSE ? END)".into()
                    } else {
                        format!(" AND agrep_word_span({table}_fts,?,?)")
                    });
                bind.push(Value::Blob(ordinals.clone()));
                bind.push(Value::Integer(if table == "prose" {
                    i64::from(i32::MAX)
                } else {
                    max_gap
                }));
            }
        }
        if let Some(ids) = block_ids {
            // One statement text for every list keeps the statement prepared.
            sql.push_str(" AND p.id IN (SELECT value FROM json_each(?))");
            bind.push(Value::Text(id_list(ids)));
        } else if block >= 0 {
            sql.push_str(if self.uses_fts(read) {
                " AND f.rowid>=? AND f.rowid<?"
            } else {
                " AND p.id>=? AND p.id<?"
            });
            bind.extend([
                Value::Integer(block * BLOCK),
                Value::Integer((block + 1) * BLOCK),
            ]);
        }
        if bounded && !o.chats && o.sort == "score" && result.hits.len() >= o.limit {
            let last = &result.hits[o.limit - 1];
            if last.tier == pass_tier && ranked(bound) <= last.score {
                // At the block's best possible score, an older row cannot
                // win the deterministic tie. Keep equal timestamps for
                // the session/position tie breakers below.
                sql.push_str(" AND p.ts>=?");
                bind.push(Value::Integer(last.ts));
                result.total_basis = "floor".into();
            }
        }
        if block_ids.is_some() {
            sql.push_str(" ORDER BY p.id");
        }
        Ok(RowQuery {
            sql,
            bind,
            lazy_identity,
            eager_prose,
            weighted,
        })
    }
}
