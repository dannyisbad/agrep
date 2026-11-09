use super::*;

#[derive(Clone, Copy)]
pub(super) struct RowContext<'a, 'p> {
    pub(super) plan: &'p TablePlan<'a>,
    pub(super) coverage: Option<&'p CoverageBounds>,
    pub(super) admitted: Option<&'p HashMap<i64, Vec<i64>>>,
    pub(super) anchored: Option<&'p HashMap<i64, Vec<i64>>>,
    pub(super) occurrences: Option<&'p HashMap<i64, u32>>,
    pub(super) eager_prose: bool,
    pub(super) lazy_identity: bool,
    pub(super) weighted: bool,
    pub(super) bound: f64,
}
struct RowMetadata {
    who: String,
    ts: i64,
    turn: i64,
}
pub(super) struct Candidate<'a, 'p, 'row, 'stmt> {
    ctx: RowContext<'a, 'p>,
    r: &'row rusqlite::Row<'stmt>,
    identity_cache: std::cell::OnceCell<(String, String)>,
}
impl<'a, 'p, 'row, 'stmt> Candidate<'a, 'p, 'row, 'stmt> {
    pub(super) fn new(ctx: RowContext<'a, 'p>, r: &'row rusqlite::Row<'stmt>) -> Self {
        Self {
            ctx,
            r,
            identity_cache: Default::default(),
        }
    }
    fn identity(&self) -> Result<&(String, String)> {
        if self.identity_cache.get().is_none() {
            let value = if self.ctx.lazy_identity {
                self.ctx.plan.ctx.c.prepare_cached("SELECT s.session,s.agent FROM event_rows e INDEXED BY event_tool_id JOIN event_sessions s ON s.id=e.session_key WHERE e.active=1 AND e.blob IS NOT NULL AND e.tool_id=?")?
                    .query_row([self.r.get::<_,i64>(0)?], |r|Ok((r.get(0)?,r.get(1)?)))?
            } else {
                (self.r.get(1)?, self.r.get(2)?)
            };
            let _ = self.identity_cache.set(value);
        }
        Ok(self.identity_cache.get().expect("loaded above"))
    }
    pub(super) fn process(
        &self,
        result: &mut Results,
        sessions: &mut HashSet<(String, String)>,
    ) -> Result<()> {
        let o = self.ctx.plan.ctx.o;
        let m = self.ctx.plan.ctx.m;
        let now = self.ctx.plan.ctx.now;
        let table = self.ctx.plan.ctx.table;
        let admitted = self.ctx.admitted;
        let r = self.r;
        let identity = || self.identity();
        let metadata = RowMetadata {
            who: r.get(5)?,
            ts: r.get(8)?,
            turn: r.get(4)?,
        };
        let who = &metadata.who;
        let ts = metadata.ts;
        let turn = metadata.turn;
        let cutoff = if o.sort == "score" && o.limit > 0 && result.hits.len() >= o.limit {
            let session = if ts == result.hits[o.limit - 1].ts {
                identity()?.0.as_str()
            } else {
                ""
            };
            Some(Cutoff::of(
                &result.hits[o.limit - 1],
                o.chats,
                ts,
                session,
                turn,
            ))
        } else {
            None
        };
        if self.excluded_by_ceiling(&metadata, cutoff.as_ref(), result)? {
            return Ok(());
        }
        for admitted in [admitted, self.ctx.anchored].into_iter().flatten() {
            let id: i64 = r.get(0)?;
            if admitted
                .get(&(id / BLOCK))
                .is_some_and(|ids| ids.binary_search(&id).is_err())
            {
                return Ok(());
            }
        }
        // The index and verifier read the same folded text. An ASCII
        // alphanumeric occurrence cannot cross a unicode61 token boundary.
        // Overlapping long-token chunks only raise its occurrence ceiling.
        if self.counted_by_occurrences(ts, cutoff.as_ref())? {
            result.total += 1;
            result.rows += 1;
            result.tool_rows += 1;
            if o.filters.sessions.is_some() {
                *result
                    .session_counts
                    .entry(identity()?.0.clone())
                    .or_default() += 1;
            }
            return Ok(());
        }
        let hydrate_started = Instant::now();
        let text = self.hydrate()?;
        result.timings.hydrate_ms += hydrate_started.elapsed().as_secs_f64() * 1000.;
        result.examined += 1;
        let score_started = Instant::now();
        let evaluated = m.evaluate(&text, who, turn, ts, now, cutoff.as_ref())?;
        result.timings.score_ms += score_started.elapsed().as_secs_f64() * 1000.;
        let Some(scored) = evaluated else {
            return Ok(());
        };
        let tier = scored.tier;
        result.total += 1;
        result.rows += 1;
        if o.filters.sessions.is_some() {
            *result
                .session_counts
                .entry(identity()?.0.clone())
                .or_default() += 1;
        }
        if o.chats {
            sessions.insert((identity()?.1.clone(), identity()?.0.clone()));
        }
        if table == "tools" {
            result.tool_rows += 1;
        }
        if scored.class == "unscored" {
            return Ok(());
        }
        let class = if tier == 2 {
            "content_terms"
        } else if tier == 1 {
            "all_terms"
        } else {
            match scored.class {
                "aligned" => "phrase_aligned",
                "partial" => "phrase_partial",
                _ => "phrase_interior",
            }
        };
        if !m.quick_count {
            *result.tiers.entry(class.into()).or_default() += 1;
        }
        if o.count_only {
            return Ok(());
        }
        if let Some(h) = self.display_hit(text, scored, metadata, result)? {
            retain_hit(result, o, h);
        }
        Ok(())
    }

    fn excluded_by_ceiling(
        &self,
        metadata: &RowMetadata,
        cutoff: Option<&Cutoff>,
        result: &mut Results,
    ) -> Result<bool> {
        let o = self.ctx.plan.ctx.o;
        let m = self.ctx.plan.ctx.m;
        let now = self.ctx.plan.ctx.now;
        let pass_tier = self.ctx.plan.ctx.pass_tier;
        let partial = self.ctx.plan.ctx.m.partial;
        let bounded = self.ctx.plan.bounded;
        let bound = self.ctx.bound;
        let r = self.r;
        let who = &metadata.who;
        let ts = metadata.ts;
        let turn = metadata.turn;
        let coverage = self.ctx.coverage;
        if !o.exact && o.limit > 0 && result.hits.len() >= o.limit && o.sort == "score" {
            let mut ceiling = weight(who)
                * if m.explicit.is_some() {
                    recency(ts, now).max(0.5)
                } else {
                    recency(ts, now).max(if who == "user" { 0.35 } else { 0. })
                }
                * meta_scale(meta_role(who, turn));
            if partial {
                let id: i64 = r.get(0)?;
                let total = m.idf.iter().sum::<f64>();
                let possible = coverage.as_ref().map_or(total, |coverage| {
                    coverage.rows.get(&id).copied().unwrap_or(total)
                });
                ceiling *= m.partial_share(possible);
            }
            if bounded {
                ceiling = ceiling.min(bound);
            }
            if cutoff.is_some_and(|c| c.excludes(pass_tier, ceiling)) {
                result.total_basis = "floor".into();
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn counted_by_occurrences(&self, ts: i64, cutoff: Option<&Cutoff>) -> Result<bool> {
        let now = self.ctx.plan.ctx.now;
        let occurrences = self.ctx.occurrences;
        let occurrence_weights = self.ctx.weighted;
        let r = self.r;
        let bounded_occurrences = if occurrence_weights {
            r.get::<_, Option<u32>>(6)?
        } else {
            occurrences
                .as_ref()
                .and_then(|counts| counts.get(&r.get::<_, i64>(0).ok()?).copied())
        }
        .filter(|&n| n > 0);
        let counted = bounded_occurrences.is_some_and(|n| {
            let ceiling = (1. - 2f64.powi(-(n.min(60) as i32))) * recency(ts, now) * weight("tool");
            cutoff.is_some_and(|c| c.excludes(0, ceiling))
        });
        Ok(counted)
    }

    fn hydrate(&self) -> Result<String> {
        let c = self.ctx.plan.ctx.c;
        let table = self.ctx.plan.ctx.table;
        let eager_prose = self.ctx.eager_prose;
        let r = self.r;

        let bytes: Vec<u8> = if eager_prose {
            r.get(6)?
        } else {
            match r.get::<_, Option<i64>>(3)? {
                Some(blob) => c
                    .prepare_cached("SELECT body FROM tool_blobs WHERE id=?")?
                    .query_row([blob], |r| r.get(0))?,
                None => c
                    .prepare_cached(&format!("SELECT text FROM {table} WHERE id=?"))?
                    .query_row([r.get::<_, i64>(0)?], |r| r.get(0))?,
            }
        };
        let text = store::decode(table, bytes)?;
        Ok(text)
    }

    fn display_hit(
        &self,
        text: String,
        scored: Scored,
        metadata: RowMetadata,
        result: &Results,
    ) -> Result<Option<Hit>> {
        let RowMetadata { who, ts, turn } = metadata;
        let (tier, score) = (scored.tier, scored.score);
        let c = self.ctx.plan.ctx.c;
        let o = self.ctx.plan.ctx.o;
        let table = self.ctx.plan.ctx.table;
        let r = self.r;
        let identity = || self.identity();
        let session = identity()?.0.clone();
        if o.chats || (o.limit > 0 && result.hits.len() >= o.limit) {
            let ranked = Hit {
                session: session.clone(),
                turn,
                who: who.clone(),
                ts,
                score,
                tier,
                rowid: r.get(0)?,
                ..Default::default()
            };
            let below_page = o.limit > 0
                && result.hits.len() >= o.limit
                && compare(&ranked, &result.hits[o.limit - 1], &o.sort, o.chats).is_ge();
            let below_head = if o.chats && !below_page {
                let agent = &identity()?.1;
                result.hits.iter().any(|old| {
                    &old.agent == agent
                        && old.session == session
                        && compare(&ranked, old, &o.sort, true).is_ge()
                })
            } else {
                false
            };
            if below_page || below_head {
                // Matching/counting is already complete. Display joins,
                // digests and snippets are only needed for page winners.
                return Ok(None);
            }
        }
        let digest = content_digest(&text);
        // Ranking needs only identity, role and time. Fetch display
        // metadata after matching so losing candidates do not join
        // symbol/blob tables or walk past a large prose BLOB.
        let (project, model, model_source, extra): (String, String, String, String) = c
            .prepare_cached(&format!(
                "SELECT project,model,model_source,extra FROM {table} WHERE id=?"
            ))?
            .query_row([r.get::<_, i64>(0)?], |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
            })?;
        let target = session_target(&session)?;
        let span = scored.span.map(|span| {
            (
                text[..span.start].chars().count(),
                text[..span.end].chars().count(),
            )
        });
        let handle = if extra.is_empty() {
            format!("{target}:{turn}.{digest}")
        } else {
            let (start, end) = span
                .filter(|(start, end)| end > start)
                .unwrap_or((0, text.chars().count()));
            format!(
                "{target}:{turn}.{digest}~{}:{start}-{end}",
                crate::handles::event_id(&session, ts, &text),
            )
        };
        let self_marker = o.filters.caller.as_ref().is_some_and(|caller| {
            caller.agent == identity().map(|i| i.1.as_str()).unwrap_or_default()
                && caller.session == session
        });
        let meta_marker = meta_role(&who, turn);
        let h = Hit {
            session,
            agent: identity()?.1.clone(),
            project,
            family: String::new(),
            side: false,
            turn,
            who,
            model,
            model_source,
            ts,
            text,
            snippet: scored.snippet,
            score,
            matched: match tier {
                0 => "phrase",
                1 => "all-terms",
                _ => "content-terms",
            }
            .into(),
            boundary_class: scored.class.into(),
            handle,
            self_marker,
            meta_marker,
            sem_weak: false,
            forks: Vec::new(),
            rowid: r.get(0)?,
            tier,
            span,
        };
        Ok(Some(h))
    }
}

fn retain_hit(result: &mut Results, o: &Options, h: Hit) {
    if o.chats {
        if let Some(i) = result
            .hits
            .iter()
            .position(|old| old.agent == h.agent && old.session == h.session)
        {
            if compare(&h, &result.hits[i], &o.sort, true).is_lt() {
                result.hits[i] = h;
            }
        } else {
            result.hits.push(h)
        }
    } else {
        result.hits.push(h)
    }
    if o.limit > 0 {
        result.hits.sort_by(|a, b| compare(a, b, &o.sort, o.chats));
    }
    if o.limit > 0 && result.hits.len() > o.limit {
        result.hits.truncate(o.limit);
    }
}
