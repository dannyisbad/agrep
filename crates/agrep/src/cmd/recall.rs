use crate::{ready, Recall};
use agrep_core::{
    caller::{self, Caller, Identity},
    evidence::{self, Request, Row, Window},
    handles, hybrid,
    query::{Hit, Options},
    render, semantic, store, surface,
};
use anyhow::{bail, Result};
use serde_json::{json, Value};
use std::{
    collections::{BTreeSet, HashSet},
    io::IsTerminal,
};
fn drop_context(blocks: &mut [Block]) -> bool {
    for block in blocks.iter_mut().rev() {
        if let Some((index, _)) = block
            .window
            .rows
            .iter()
            .enumerate()
            .filter(|(_, row)| !block.hit_turns.contains(&row.turn))
            .max_by_key(|(_, row)| (row.turn - block.hit.turn).abs())
        {
            block.window.rows.remove(index);
            return true;
        }
        if let Some(index) = block
            .window
            .events
            .iter()
            .position(|event| !block.hit_turns.contains(&event.turn))
        {
            block.window.events.remove(index);
            return true;
        }
    }
    false
}
struct Block {
    hit_turns: BTreeSet<i64>,
    hit: Hit,
    window: Window,
}
fn head(block: &Block) -> String {
    let h = &block.hit;
    format!(
        "{} · {} · {} · {}{}{}",
        h.handle,
        surface::safe(&h.agent, false),
        surface::safe(&render::project(&h.project), false),
        surface::timestamp(h.ts),
        if h.self_marker { " · ~self" } else { "" },
        if h.matched == "semantic" {
            " · meaning match"
        } else if h.matched != "phrase" {
            " · all terms"
        } else {
            ""
        }
    )
}
fn row_value(r: &Row, w: &Window, text: &str, omitted: usize) -> Value {
    let mut value = json!({"kind":"msg","session":w.session,"project":w.project,"turn":r.turn,"who":r.who,"ts":r.ts,"text":text,"omitted_chars":omitted});
    if r.who == "agent" {
        value["reply_chars"] = json!(r.source_chars);
        value["reply_truncated"] = json!(r.source_chars > r.text.chars().count());
    }
    value
}
fn block_text(b: &Block, cap: usize, color: bool) -> String {
    let mut text = if color {
        format!("\x1b[1m── {}\x1b[0m\n", head(b))
    } else {
        format!("── {}\n", head(b))
    };
    let target = b.window.target.clone();
    for r in &b.window.rows {
        let source = r.text.split_whitespace().collect::<Vec<_>>().join(" ");
        let cmd = format!("agrep around {target} {} -C 0 --max-chars 0", r.turn);
        let (body, _) = evidence::cap(&source, cap, &cmd);
        text += &format!(
            "  {:>4} {}: {}\n",
            r.turn,
            r.who,
            surface::safe(&body, false)
        );
    }
    if b.hit.who == "tool" {
        text += &format!("       {}\n", surface::safe(&b.hit.snippet, false));
    }
    if !b.window.events.is_empty() {
        text += &format!(
            "       [+{} tool calls - agrep around {target} {} -C {} --tool-output 200]\n",
            b.window.events.len(),
            b.window.center,
            b.window
                .rows
                .iter()
                .map(|r| (r.turn - b.window.center).abs())
                .max()
                .unwrap_or(0)
        );
    }
    text
}
fn block_value(b: &Block, cap: usize) -> Value {
    let mut rows = Vec::new();
    let turns: BTreeSet<_> = b.window.rows.iter().map(|r| r.turn).collect();
    for turn in turns {
        for r in b.window.rows.iter().filter(|r| r.turn == turn) {
            let (text, omitted) = evidence::cap(
                &r.text,
                cap,
                &format!(
                    "agrep around {} {} -C 0 --max-chars 0",
                    b.window.target.clone(),
                    r.turn
                ),
            );
            rows.push(row_value(r, &b.window, &text, omitted));
        }
        for e in b.window.events.iter().filter(|e| e.turn == turn) {
            rows.push(json!({"kind":e.kind,"session":b.window.session,"project":b.window.project,"turn":e.turn,"ts":e.ts,"name":e.name,"input":surface::clip(&e.input,if cap==0{4000}else{cap}),"ok":e.ok,"input_chars":e.input_chars,"output_chars":e.output_chars,"output_bytes":e.output_bytes,"input_truncated":e.input_chars>e.input.chars().count(),"output_truncated":e.output_chars>e.output.chars().count()}));
        }
    }
    json!({"session":b.window.session,"turn":b.window.center,"ts":b.hit.ts,"agent":b.window.agent,"project":b.window.project,"score":b.hit.score,"sem_score":if b.hit.matched=="semantic"{Some(b.hit.score)}else{None},"score_kind":null,"matched":b.hit.matched,"handle":b.hit.handle,"self":b.hit.self_marker,"window":rows})
}
pub(crate) fn run(a: Recall, pack: bool, no_auto: bool, color: &str) -> Result<i32> {
    if a.semantic && !semantic::installed(&agrep_core::data::DataRoot::discover()?) {
        eprintln!("semantic model not installed: run agrep setup");
        return Ok(2);
    }
    if a.hits == Some(0) {
        bail!("--hits must be at least 1")
    }
    if a.budget > 0 && a.budget < 64 {
        bail!("a finite --budget must be at least 64 bytes")
    }
    if a.json && a.budget > 0 && a.budget < 2048 {
        bail!("a finite JSON --budget must be at least 2048 bytes")
    }
    if a.json && a.probe {
        bail!("--probe emits a pointer line and cannot be combined with --json")
    }
    let queries = if pack {
        a.query.clone()
    } else {
        vec![a.query.join(" ")]
    };
    if queries.iter().any(|q| q.trim().is_empty()) {
        bail!("a nonempty search pattern is required")
    }
    let target = if a.probe {
        1
    } else {
        a.hits
            .unwrap_or(if a.budget == 0 {
                3
            } else {
                (a.budget / 5000).clamp(3, 8)
            })
            .max(queries.len())
    };
    let root = ready(no_auto)?;
    semantic::schedule(&root)?;
    let c = store::reader(&root)?;
    let mut semantic_coverage = None;
    let mut semantic_ran = false;
    let include_self = a.scope.include_self || (a.scope.chat.is_some() && !a.scope.no_self);
    let identity = Identity::detect();
    if !a.json && !a.scope.include_self && (a.scope.no_self || identity.agent_context) {
        identity.disclose(a.scope.no_self);
    }
    let caller = Caller::resolve(&c, identity)?;
    let mut filters = a.scope.filters()?;
    agrep_core::coverage::validate(&mut filters)?;
    let coverage = agrep_core::coverage::dimensions(&c, &filters)?;
    if let Some(chat) = filters
        .chat
        .as_ref()
        .filter(|_| !queries.iter().any(|q| handles::is_handle(q)))
    {
        let ids = handles::candidates(&c, chat)?;
        if ids.len() != 1 {
            bail!("chat is missing or ambiguous")
        }
        filters.chat = Some(ids[0].clone())
    }
    let direct_filters: Vec<_> = [
        ("--agent", filters.agent.is_some()),
        ("--project", filters.project.is_some()),
        ("--exclude-project", filters.exclude_project.is_some()),
        ("--model", filters.model.is_some()),
        ("--who", filters.who.is_some()),
        ("--no-who", filters.no_who.is_some()),
        ("--chat", filters.chat.is_some()),
        ("--since", filters.since.is_some()),
        ("--until", filters.until.is_some()),
    ]
    .into_iter()
    .filter(|(_, yes)| *yes)
    .map(|(name, _)| name)
    .collect();
    let override_filters =
        queries.iter().any(|q| handles::is_handle(q)) && !direct_filters.is_empty();
    if override_filters && !a.json {
        eprintln!(
            "direct handle overrides filters: {}",
            direct_filters.join(", ")
        );
    }
    let mut buckets = Vec::new();
    let mut known_sessions = HashSet::new();
    let mut totals_exact = true;
    for query in &queries {
        if handles::is_handle(query) {
            let w = evidence::load(&c, &Request::new(query, None, Some(a.context as i64)))?;
            let claim = handles::parse(query)?;
            let event = w.events.iter().find(|e| {
                e.turn == w.center
                    && claim
                        .digest
                        .as_deref()
                        .is_some_and(|d| d == agrep_core::query::content_digest(&e.text))
                    && claim.event.as_ref().is_none_or(|id| id == &e.identity)
            });
            let row = w.rows.iter().find(|r| {
                r.turn == w.center
                    && claim
                        .digest
                        .as_deref()
                        .is_none_or(|d| d == agrep_core::query::content_digest(&r.text))
            });
            let (text, who, ts) = if let Some(e) = event {
                (e.text.clone(), "tool".to_owned(), e.ts)
            } else if let Some(r) = row {
                (r.text.clone(), r.who.clone(), r.ts)
            } else {
                bail!("handle has no indexed row")
            };
            let mut handle = handles::mint(&w.session, w.center, &text, &handles::sessions(&c)?);
            if let Some(e) = event {
                let (a, b) = claim.span.unwrap_or((0, e.text.chars().count()));
                handle += &format!("~{}:{a}-{b}", e.identity);
            }
            let h = Hit {
                session: w.session.clone(),
                agent: w.agent.clone(),
                project: w.project.clone(),
                family: w.family.clone(),
                side: w.side,
                turn: w.center,
                ts,
                text: text.clone(),
                snippet: text.split_whitespace().collect::<Vec<_>>().join(" "),
                who,
                matched: "phrase".into(),
                handle,
                self_marker: caller.resolved && caller.session == w.session,
                ..Hit::default()
            };
            known_sessions.insert(w.session.clone());
            buckets.push(vec![h]);
            continue;
        }
        let options = Options {
            pattern: query.clone(),
            regex: false,
            word: false,
            sort: "score".into(),
            limit: target.saturating_mul(12).max(40),
            exact: false,
            count_only: false,
            count_by_tier: false,
            content: !a.lexical,
            chats: false,
            tools: root.config()?.tools
                || filters
                    .who
                    .as_deref()
                    .is_some_and(|who| who.split(',').any(|w| w == "tool")),
            filters: filters.clone(),
        };
        let lane = if a.semantic
            || (!a.lexical
                && semantic::eligible(query)
                && semantic::installed(&root)
                && root.config()?.embeddings != "off")
        {
            Some(semantic::begin(
                &root,
                &c,
                &options,
                !a.scope.all_side_chats,
            ))
        } else {
            None
        };
        let keyword = if a.semantic {
            Default::default()
        } else {
            caller::search(&c, &options, &caller, include_self, a.scope.no_self)?
        };
        let r = if let Some(lane) = lane {
            match lane.and_then(|work| {
                caller::scoped(&c, &caller, include_self, a.scope.no_self, || {
                    semantic::finish(&c, work)
                })
            }) {
                Ok(mut found) => {
                    semantic_ran = true;
                    semantic_coverage = Some(found.coverage);
                    caller::filter_results(
                        &c,
                        &mut found.result,
                        &caller,
                        include_self,
                        a.scope.no_self,
                    )?;
                    if a.semantic {
                        found.result
                    } else {
                        hybrid::merge(
                            keyword,
                            Some(found.result),
                            options.limit,
                            !a.scope.all_side_chats,
                            if pack {
                                hybrid::Lane::Pack
                            } else {
                                hybrid::Lane::Recall
                            },
                        )
                    }
                }
                Err(e) if a.semantic => return Err(e),
                Err(_) => keyword,
            }
        } else {
            keyword
        };
        totals_exact &= r.total_basis == "exact";
        known_sessions.extend(r.hits.iter().map(|h| h.session.clone()));
        buckets.push(r.hits);
    }
    let mut selected = Vec::new();
    let mut seen = HashSet::new();
    let mut families = HashSet::new();
    let mut offsets = vec![0; buckets.len()];
    while selected.len() < target {
        let mut added = false;
        for (i, bucket) in buckets.iter().enumerate() {
            while let Some(h) = bucket.get(offsets[i]) {
                offsets[i] += 1;
                let family = if a.scope.all_side_chats || filters.chat.is_some() {
                    h.session.clone()
                } else {
                    h.family.clone()
                };
                if seen.contains(&(h.session.clone(), h.turn))
                    || families.contains(&(i, family.clone()))
                {
                    continue;
                }
                seen.insert((h.session.clone(), h.turn));
                families.insert((i, family));
                selected.push((i, h.clone()));
                added = true;
                break;
            }
            if selected.len() == target {
                break;
            }
        }
        if !added {
            break;
        }
    }
    let requests: Vec<_> = selected
        .iter()
        .map(|(_, h)| Request::new(&h.handle, None, Some(a.context as i64)))
        .collect();
    let windows = evidence::windows(&c, &requests)?;
    let blocks: Vec<_> = selected
        .into_iter()
        .zip(windows)
        .map(|((_query, hit), window)| Block {
            hit_turns: BTreeSet::from([hit.turn]),
            hit,
            window,
        })
        .collect();
    let mut merged: Vec<Block> = Vec::new();
    for b in blocks {
        let lo = b
            .window
            .rows
            .first()
            .map(|r| r.turn)
            .unwrap_or(b.window.center);
        let hi = b
            .window
            .rows
            .last()
            .map(|r| r.turn)
            .unwrap_or(b.window.center);
        if let Some(existing) = merged.iter_mut().find(|m| {
            m.window.session == b.window.session
                && m.window.rows.first().is_some_and(|r| r.turn <= hi + 1)
                && m.window.rows.last().is_some_and(|r| r.turn + 1 >= lo)
        }) {
            existing.hit_turns.extend(&b.hit_turns);
            for row in b.window.rows {
                if !existing
                    .window
                    .rows
                    .iter()
                    .any(|r| r.turn == row.turn && r.who == row.who && r.text == row.text)
                {
                    existing.window.rows.push(row);
                }
            }
            existing
                .window
                .rows
                .sort_by_key(|r| (r.turn, r.who == "agent", r.ts));
            for event in b.window.events {
                if !existing
                    .window
                    .events
                    .iter()
                    .any(|e| e.identity == event.identity)
                {
                    existing.window.events.push(event);
                }
            }
        } else {
            merged.push(b)
        }
    }
    let mut blocks = merged;
    // Keep current-window exclusions true after expanding neighboring context.
    if caller.resolved && !include_self && !handles::is_handle(&queries[0]) {
        for b in &mut blocks {
            if b.window.session == caller.session {
                b.window
                    .rows
                    .retain(|r| r.turn < caller.boundary.unwrap_or(0));
            }
        }
    }
    if !a.json {
        agrep_core::coverage::disclose(&coverage);
    }
    let checked = agrep_core::render::checked(no_auto);
    let exit = if blocks.is_empty() {
        if checked {
            1
        } else {
            2
        }
    } else {
        0
    };
    if a.probe {
        let confident = blocks
            .iter()
            .find(|b| b.hit.matched == "phrase" || b.hit.matched == "semantic");
        let line = if let Some(b) = confident {
            let n = known_sessions.len().max(1);
            let lead = if totals_exact {
                format!(
                    "{n} past session{} match{}",
                    if n == 1 { "" } else { "s" },
                    if n == 1 { "es" } else { "" }
                )
            } else {
                format!(
                    "top results span {n} past session{}",
                    if n == 1 { "" } else { "s" }
                )
            };
            format!(
                "recall: {lead} · {} · pull: {}",
                head(b),
                surface::shell(
                    &std::iter::once("agrep".to_owned())
                        .chain(std::iter::once(if pack { "pack" } else { "recall" }.into()))
                        .chain(queries.clone())
                        .collect::<Vec<_>>()
                )
            )
        } else {
            let count: i64 = c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
            format!(
                "recall: no confident match in {count} past conversations{}",
                if checked {
                    ""
                } else {
                    "; freshness unchecked (--no-auto)"
                }
            )
        };
        if a.budget > 0 && line.len() + 1 > a.budget {
            if let Some(b) = confident {
                let pointer = format!("recall: {}\n", b.hit.handle);
                if pointer.len() > a.budget {
                    bail!(
                        "--budget needs at least {} bytes for the first reference",
                        pointer.len()
                    )
                }
                surface::write(&pointer)?
            } else {
                surface::line(surface::prefix(&line, a.budget - 1))?
            }
        } else {
            surface::line(&line)?
        }
        return Ok(if confident.is_some() {
            0
        } else if checked {
            1
        } else {
            2
        });
    }
    let color = color == "always" || (color == "auto" && std::io::stdout().is_terminal());
    if a.json {
        let mut value = json!({"query":if pack{json!(queries)}else{json!(queries[0])},"engine":"sqlite","hits":[],"semantic_status":{"state":if semantic_ran{"complete"}else{"not-run"},"reason":if semantic_ran{None}else{Some(if a.lexical{"--lexical"}else if !semantic::installed(&root){"model-not-installed"}else{"query-ineligible"})}},"self_exclusion":caller.metadata(include_self,a.scope.no_self),"filter_coverage":coverage,"freshness":render::freshness(checked,0),"semantic_coverage":semantic_coverage});
        if override_filters {
            value["filter_override"] = json!({"supplied":direct_filters,"reason":"direct-handle"});
        }
        let mut cap = 0;
        let mut omitted = Vec::new();
        loop {
            value["hits"] = json!(blocks
                .iter()
                .map(|b| block_value(b, cap))
                .collect::<Vec<_>>());
            if !omitted.is_empty() {
                value["omitted_hits"] = json!(omitted)
            }
            let text = value.to_string() + "\n";
            if a.budget == 0 || text.len() <= a.budget {
                surface::write(&text)?;
                break;
            }
            if drop_context(&mut blocks) {
                continue;
            }
            if cap == 0 {
                cap = 1000;
                continue;
            }
            if cap > 64 {
                cap = (cap / 2).max(64);
                continue;
            }
            if blocks.len() > 1 {
                omitted.push(blocks.pop().unwrap().hit.handle);
                continue;
            }
            if cap == 64 {
                bail!(
                    "--budget needs at least {} bytes for the first reference and JSON envelope",
                    text.len()
                )
            }
        }
    } else if blocks.is_empty() {
        let n: i64 = c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
        eprintln!("no hits across {n} indexed sessions.");
    } else {
        let mut cap = 0;
        let mut omitted = Vec::new();
        loop {
            let mut text = blocks
                .iter()
                .map(|b| block_text(b, cap, color))
                .collect::<Vec<_>>()
                .join("\n");
            if !omitted.is_empty() {
                text += &format!(
                    "[+{} hit(s) over budget - {}]\n",
                    omitted.len(),
                    omitted.join(" ")
                );
            }
            if a.budget == 0 || text.len() <= a.budget {
                surface::write(&text)?;
                break;
            }
            if drop_context(&mut blocks) {
                continue;
            }
            if cap == 0 {
                cap = 1000;
                continue;
            }
            if cap > 64 {
                cap = (cap / 2).max(64);
                continue;
            }
            if blocks.len() > 1 {
                omitted.push(blocks.pop().unwrap().hit.handle);
                continue;
            }
            if cap == 64 {
                let b = &blocks[0];
                let minimum = format!(
                    "── {}\n  {}\n",
                    b.hit.handle,
                    surface::clip(
                        &surface::safe(&b.hit.snippet, false),
                        a.budget.saturating_sub(b.hit.handle.len() + 12)
                    )
                );
                if minimum.len() > a.budget || minimum.trim_end().ends_with(&b.hit.handle) {
                    bail!(
                        "--budget needs at least {} bytes for the first reference",
                        b.hit.handle.len() + 16
                    )
                }
                surface::write(&minimum)?;
                break;
            }
        }
    }
    Ok(exit)
}
