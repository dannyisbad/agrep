use super::*;

pub(super) fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}
/// Row ids as a JSON array for `json_each`.
pub(super) fn id_list(ids: &[i64]) -> String {
    use std::fmt::Write;
    let mut out = String::with_capacity(ids.len() * 8 + 2);
    out.push('[');
    for (index, id) in ids.iter().enumerate() {
        if index > 0 {
            out.push(',');
        }
        let _ = write!(out, "{id}");
    }
    out.push(']');
    out
}
pub(super) fn sql_filters(
    f: &Filters,
    alias: &str,
    c: &Connection,
) -> Result<(String, Vec<Value>)> {
    let mut clauses = vec![];
    let mut values = vec![];
    // A session scope holds a few sessions' rows. Without table statistics the planner
    // ranks the agent, time and project indexes above the session indexes and reads
    // every row of the agent; a unary plus keeps those columns off the access path.
    let off = if f.chat.is_some() || f.session_rows.is_some() || f.sessions.is_some() {
        "+"
    } else {
        ""
    };
    if let Some(v) = &f.agent {
        let agents: Vec<_> = v.split(',').collect();
        clauses.push(format!(
            "{off}{alias}.agent IN ({})",
            vec!["?"; agents.len()].join(",")
        ));
        values.extend(agents.into_iter().map(|v| Value::Text(v.into())));
    }
    let mut add = |col: &str, op: &str, value: Value| {
        clauses.push(format!("{off}{alias}.{col} {op} ?"));
        values.push(value);
    };
    if let Some(v) = f.since {
        add("ts", ">=", v.into());
    }
    if let Some(v) = f.until {
        add("ts", "<", v.into());
    }
    session_filters(c, f, alias, &mut clauses, &mut values)?;
    if let Some(v) = &f.model {
        clauses.push(if f.model_soft {
            format!("{alias}.model LIKE ? ESCAPE '\\'")
        } else {
            format!("{alias}.model = ? COLLATE NOCASE")
        });
        values.push(Value::Text(if f.model_soft {
            format!("%{}%", escape_like(v))
        } else {
            v.clone()
        }));
    }
    for (pattern, exclude) in [(&f.project, false), (&f.exclude_project, true)] {
        if let Some(pattern) = pattern {
            let projects: Vec<String> = c
                .prepare("SELECT DISTINCT project FROM sessions")?
                .query_map([], |r| r.get(0))?
                .collect::<rusqlite::Result<Vec<String>>>()?
                .into_iter()
                .filter(|p| project_ok(p, pattern))
                .collect();
            if projects.is_empty() {
                if !exclude {
                    clauses.push("0".into())
                }
            } else {
                clauses.push(format!(
                    "{off}{alias}.project {}IN ({})",
                    if exclude { "NOT " } else { "" },
                    vec!["?"; projects.len()].join(",")
                ));
                values.extend(projects.into_iter().map(Value::Text));
            }
        }
    }
    if let Some(v) = &f.who {
        let terms: Vec<_> = v.split(',').collect();
        clauses.push(format!(
            "{alias}.who IN ({})",
            vec!["?"; terms.len()].join(",")
        ));
        values.extend(terms.into_iter().map(|v| Value::Text(v.into())));
    }
    if let Some(v) = &f.no_who {
        let terms: Vec<_> = v.split(',').collect();
        clauses.push(format!(
            "{alias}.who NOT IN ({})",
            vec!["?"; terms.len()].join(",")
        ));
        values.extend(terms.into_iter().map(|v| Value::Text(v.into())));
    }
    if let Some(caller) = &f.caller {
        if !caller.excluded_sessions.is_empty() {
            clauses.push(format!(
                "NOT ({alias}.agent=? AND {alias}.session IN ({}))",
                vec!["?"; caller.excluded_sessions.len()].join(",")
            ));
            values.push(Value::Text(caller.agent.clone()));
            values.extend(caller.excluded_sessions.iter().cloned().map(Value::Text));
        }
        if caller.exclude_family {
            clauses.push(format!("NOT ({alias}.agent=? AND EXISTS(SELECT 1 FROM sessions s WHERE s.agent={alias}.agent AND s.id={alias}.session AND s.family=?))"));
            values.push(Value::Text(caller.agent.clone()));
            values.push(Value::Text(caller.family.clone()));
        } else if let Some(from) = caller.exclude_from_turn {
            clauses.push(format!(
                "NOT ({alias}.agent=? AND {alias}.session=? AND {alias}.position>=?)"
            ));
            values.push(Value::Text(caller.agent.clone()));
            values.push(Value::Text(caller.session.clone()));
            values.push(Value::Integer(from));
        }
    }
    if f.no_side {
        clauses.push(format!("NOT EXISTS(SELECT 1 FROM sessions s WHERE s.agent={alias}.agent AND s.id={alias}.session AND s.side=1)"));
    }
    Ok((
        if clauses.is_empty() {
            String::new()
        } else {
            format!(" AND {}", clauses.join(" AND "))
        },
        values,
    ))
}
/// The same caller predicate used by SQL retrieval, for the semantic lane.
pub fn caller_allows(f: &Filters, h: &Hit) -> bool {
    f.caller.as_ref().is_none_or(|c| {
        h.agent != c.agent
            || !(c.excluded_sessions.contains(&h.session)
                || (c.exclude_family && h.family == c.family)
                || (h.session == c.session && c.exclude_from_turn.is_some_and(|p| h.turn >= p)))
    })
}
pub(crate) fn project_ok(value: &str, pattern: &str) -> bool {
    let p = pattern.to_lowercase();
    let v = value.to_lowercase();
    if p.contains(['*', '?', '[']) {
        globset::GlobBuilder::new(&p)
            .literal_separator(false)
            .build()
            .is_ok_and(|g| g.compile_matcher().is_match(&v))
    } else {
        v == p || v.rsplit(['/', '\\']).next() == Some(p.as_str())
    }
}

fn session_filters(
    c: &Connection,
    f: &Filters,
    alias: &str,
    clauses: &mut Vec<String>,
    values: &mut Vec<Value>,
) -> Result<()> {
    if let Some(v) = &f.chat {
        // The prefix is matched on the small `sessions` table, which names every row's
        // session, so the rows are then reached through the session indexes; a LIKE
        // on the rows themselves cannot use them and scans the whole lane.
        let mut ids: Vec<String> = c
            .prepare_cached("SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\'")?
            .query_map([format!("{}%", escape_like(v))], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        ids.sort_unstable();
        ids.dedup();
        if ids.is_empty() {
            clauses.push("0".into());
        } else {
            clauses.push(format!(
                "{alias}.session IN (SELECT value FROM json_each(?))"
            ));
            values.push(Value::Text(serde_json::to_string(&ids)?));
        }
    }
    if let Some(rows) = &f.session_rows {
        clauses.push(format!("{alias}.session=?"));
        values.push(Value::Text(rows.session.clone()));
        if let Some(from) = rows.from {
            clauses.push(format!("{alias}.position>=?"));
            values.push(Value::Integer(from));
        }
    }
    if let Some(sessions) = &f.sessions {
        clauses.push(format!(
            "{alias}.session IN (SELECT value FROM json_each(?))"
        ));
        values.push(Value::Text(serde_json::to_string(sessions)?));
    }
    Ok(())
}
