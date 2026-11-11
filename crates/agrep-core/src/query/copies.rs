use super::*;

/// The id, chat and speaker of each row the filters admit at one agent and time.
type Stamped = Vec<(i64, String, String)>;

/// The rows other chats hold as copies of a page's rows, under the filters a search
/// ranked with (see `crate::forks`).
pub struct Copies<'a> {
    c: &'a Connection,
    o: &'a Options,
    /// The search's filters as SQL over a row table aliased `p`, built on first use.
    scope: Option<(String, Vec<Value>)>,
    /// The rows the filters admit by table, agent and time, read once for all the page
    /// rows written at that moment.
    stamped: HashMap<(&'static str, String, i64), Stamped>,
}
impl<'a> Copies<'a> {
    pub fn new(c: &'a Connection, o: &'a Options) -> Self {
        Self {
            c,
            o,
            scope: None,
            stamped: HashMap::new(),
        }
    }
    /// The visible rows the filters admit that hold `row`'s agent, speaker, time and text
    /// in another chat, each as `row` from its own chat, in chat and turn order. Their
    /// handles and self marks are left to the caller.
    pub fn of(&mut self, row: &Hit) -> Result<Vec<Hit>> {
        if row.ts == 0 {
            return Ok(Vec::new());
        }
        let table = if row.who == "tool" { "tools" } else { "prose" };
        let key = (table, row.agent.clone(), row.ts);
        if !self.stamped.contains_key(&key) {
            let rows = self.stamped_rows(table, &row.agent, row.ts)?;
            self.stamped.insert(key.clone(), rows);
        }
        let digest = crate::store::digest(&row.text);
        // A record stores its text before its digest and visibility, so only rows of the
        // same speaker in another chat read them.
        let mut detail = self.c.prepare_cached(&format!(
            "SELECT p.project,p.position,p.model,p.model_source FROM {table} p WHERE p.id=?1 AND p.visible=1 AND p.digest=?2"
        ))?;
        let mut copies = Vec::new();
        for (rowid, session, who) in &self.stamped[&key] {
            if *who != row.who || *session == row.session {
                continue;
            }
            let Some((project, turn, model, model_source)) = detail
                .query_row(rusqlite::params![rowid, digest], |r| {
                    Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?))
                })
                .optional()?
            else {
                continue;
            };
            let (family, side) = self
                .c
                .prepare_cached("SELECT family,side FROM sessions WHERE agent=?1 AND id=?2")?
                .query_row([&row.agent, session], |r| Ok((r.get(0)?, r.get(1)?)))
                .optional()?
                .unwrap_or_default();
            let copy = Hit {
                session: session.clone(),
                project,
                family,
                side,
                turn,
                model,
                model_source,
                rowid: *rowid,
                handle: String::new(),
                self_marker: false,
                ..row.clone()
            };
            // `--no-meta` drops a ranked copy that is tooling about history in its chat.
            if self.o.filters.no_meta
                && crate::history_meta::is_meta(self.c, &copy, &self.o.pattern)?
            {
                continue;
            }
            copies.push(copy);
        }
        copies.sort_by(|a, b| {
            a.session
                .cmp(&b.session)
                .then(a.turn.cmp(&b.turn))
                .then(a.rowid.cmp(&b.rowid))
        });
        Ok(copies)
    }
    /// Every row the filters admit in `table` at `agent` and `ts`.
    fn stamped_rows(&mut self, table: &str, agent: &str, ts: i64) -> Result<Stamped> {
        if self.scope.is_none() {
            let mut filters = self.o.filters.clone();
            normalize(&mut filters);
            self.scope = Some(sql_filters(&filters, "p", self.c)?);
        }
        let (clause, values) = self.scope.as_ref().expect("built above");
        let mut bind: Vec<Value> = vec![agent.to_owned().into(), ts.into()];
        bind.extend(values.iter().cloned());
        let rows = self
            .c
            .prepare_cached(&format!(
                "SELECT p.id,p.session,p.who FROM {table} p WHERE p.agent=? AND p.ts=?{clause}"
            ))?
            .query_map(params_from_iter(bind), |r| {
                Ok((r.get(0)?, r.get(1)?, r.get(2)?))
            })?
            .collect::<rusqlite::Result<_>>()?;
        Ok(rows)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    /// One pi message in four chats at one moment (one hidden, one in another project),
    /// and rows that only look like it: another speaker, another time, other words.
    fn store() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        crate::store::schema(&c).unwrap();
        let rows = [
            (1, "root", "/work/a", "user", 5, "shared", true),
            (2, "fork-b", "/work/b", "user", 5, "shared", true),
            (3, "fork-a", "/work/a", "user", 5, "shared", true),
            (4, "fork-c", "/work/a", "user", 5, "shared", false),
            (5, "other", "/work/a", "agent", 5, "shared", true),
            (6, "later", "/work/a", "user", 6, "shared", true),
            (7, "words", "/work/a", "user", 5, "unshared", true),
        ];
        for (id, session, project, who, ts, text, visible) in rows {
            c.execute(
                "INSERT INTO prose(id,source,agent,session,project,turn,who,model,model_source,ts,text,digest,extra,position,visible) VALUES(?1,1,'pi',?2,?3,?1,?4,'m','explicit',?5,?6,?7,'',?1,?8)",
                rusqlite::params![id, session, project, who, ts, text.as_bytes(), crate::store::digest(text), visible],
            )
            .unwrap();
            c.execute(
                "INSERT OR IGNORE INTO sessions VALUES('pi',?1,?2,'','',?1,0,'',0,0)",
                [session, project],
            )
            .unwrap();
        }
        c
    }
    fn options(filters: Filters) -> Options {
        Options {
            pattern: "shared".into(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: 1,
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: false,
            chats: false,
            tools: false,
            filters,
        }
    }
    /// A copy is the same message in another chat that the search's filters admit, read
    /// as the shown row from its own chat.
    #[test]
    fn copies_are_the_message_in_other_chats_the_filters_admit() {
        let c = store();
        let shown = Hit {
            session: "root".into(),
            agent: "pi".into(),
            who: "user".into(),
            ts: 5,
            text: "shared".into(),
            score: 0.5,
            rowid: 1,
            ..Hit::default()
        };
        let found = |filters: Filters| -> Vec<(String, String, i64, f64)> {
            let o = options(filters);
            Copies::new(&c, &o)
                .of(&shown)
                .unwrap()
                .into_iter()
                .map(|h| (h.session, h.project, h.turn, h.score))
                .collect()
        };
        assert_eq!(
            found(Filters::default()),
            [
                ("fork-a".into(), "/work/a".into(), 3, 0.5),
                ("fork-b".into(), "/work/b".into(), 2, 0.5)
            ]
        );
        let in_a = Filters {
            project: Some("a".into()),
            agent: Some(" PI".into()),
            ..Filters::default()
        };
        assert_eq!(found(in_a).len(), 1);
        let caller = Filters {
            caller: Some(CallerFilter {
                agent: "pi".into(),
                session: "fork-a".into(),
                family: "fork-a".into(),
                exclude_from_turn: None,
                exclude_family: false,
                excluded_sessions: vec!["fork-a".into()],
            }),
            ..Filters::default()
        };
        assert_eq!(found(caller)[0].0, "fork-b");
    }
}
