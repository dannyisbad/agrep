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
/// Removes the context row farthest from its hit in the last block that has
/// one, returning that block's index and the row's former index.
fn drop_context(blocks: &mut [Block]) -> Option<(usize, usize)> {
    for (at, block) in blocks.iter_mut().enumerate().rev() {
        if let Some((index, _)) = block
            .window
            .rows
            .iter()
            .enumerate()
            .filter(|(_, row)| !block.hit_turns.contains(&row.turn))
            .max_by_key(|(_, row)| (row.turn - block.hit.turn).abs())
        {
            block.window.rows.remove(index);
            return Some((at, index));
        }
        // JSON renders events only at retained prose turns; human output renders
        // their availability count. Deleting the other events one at a time
        // cannot trim that text and used to rebuild large windows thousands of
        // times before the message cap was applied.
    }
    None
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
fn row_line(b: &Block, r: &Row, cap: usize) -> String {
    let source = r.text.split_whitespace().collect::<Vec<_>>().join(" ");
    let cmd = format!(
        "agrep around {} {} -C 0 --max-chars 0",
        b.window.target, r.turn
    );
    let (body, _) = evidence::cap(&source, cap, &cmd);
    format!(
        "  {:>4} {}: {}\n",
        r.turn,
        r.who,
        surface::safe(&body, false)
    )
}
fn row_lines(b: &Block, cap: usize) -> Vec<String> {
    b.window.rows.iter().map(|r| row_line(b, r, cap)).collect()
}
/// A block's text from its rows' lines, which must be `row_lines` at one cap.
fn block_text(b: &Block, lines: &[String], color: bool) -> String {
    let mut text = if color {
        format!("\x1b[1m── {}\x1b[0m\n", head(b))
    } else {
        format!("── {}\n", head(b))
    };
    let target = &b.window.target;
    for line in lines {
        text += line;
    }
    if b.hit.who == "tool" {
        text += &format!("       {}\n", surface::safe(&b.hit.snippet, false));
    }
    let visible_events = b
        .window
        .events
        .iter()
        .filter(|event| b.window.rows.iter().any(|row| row.turn == event.turn))
        .count();
    if visible_events > 0 {
        text += &format!(
            "       [+{} tool calls - agrep around {target} {} -C {} --tool-output 200]\n",
            visible_events,
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
fn json_len(value: &Value) -> Result<usize> {
    struct Count(usize);
    impl std::io::Write for Count {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0 += bytes.len();
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut count = Count(0);
    agrep_core::json_output::to_writer(&mut count, value)?;
    Ok(count.0)
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
/// Takes each query's next hit in turn, skipping turns already taken and
/// families that query already supplied, until `target` hits. Also reports,
/// per query, whether selection asked for a hit past the end of its ranking.
fn select(
    buckets: &[Vec<Hit>],
    target: usize,
    per_session: bool,
) -> (Vec<(usize, Hit)>, Vec<bool>) {
    let mut selected = Vec::new();
    let mut seen = HashSet::new();
    let mut families = HashSet::new();
    let mut offsets = vec![0; buckets.len()];
    let mut exhausted = vec![false; buckets.len()];
    while selected.len() < target {
        let mut added = false;
        for (i, bucket) in buckets.iter().enumerate() {
            loop {
                let Some(h) = bucket.get(offsets[i]) else {
                    exhausted[i] = true;
                    break;
                };
                offsets[i] += 1;
                let family = if per_session {
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
    (selected, exhausted)
}
/// `select` over ranking prefixes. A prefix selects exactly what its full
/// ranking does unless selection ran past it; such a query's prefix, where
/// `deepen` holds its full search, is replaced by `full` until none runs past.
fn select_deepening<T>(
    buckets: &mut [Vec<Hit>],
    deepen: &mut [Option<T>],
    target: usize,
    per_session: bool,
    mut full: impl FnMut(T) -> Result<Vec<Hit>>,
) -> Result<Vec<(usize, Hit)>> {
    loop {
        let (selected, exhausted) = select(buckets, target, per_session);
        let mut deepened = false;
        for (i, ran_out) in exhausted.into_iter().enumerate() {
            if let Some(search) = deepen[i].take_if(|_| ran_out) {
                buckets[i] = full(search)?;
                deepened = true;
            }
        }
        if !deepened {
            return Ok(selected);
        }
    }
}
/// Within `budget` bytes (0 is unbounded), drops the farthest context rows,
/// then caps every message at 1000 and halving characters down to 64, then
/// omits the last hits, until the page fits.
fn text_page(blocks: &mut Vec<Block>, budget: usize, color: bool) -> Result<String> {
    let mut cap = 0;
    let mut omitted = Vec::new();
    // Each row's line depends only on the cap, so a dropped row re-renders
    // nothing and a smaller cap renders each remaining row once.
    let mut lines: Vec<_> = blocks.iter().map(|b| row_lines(b, cap)).collect();
    let mut texts: Vec<_> = blocks
        .iter()
        .zip(&lines)
        .map(|(b, l)| block_text(b, l, color))
        .collect();
    loop {
        let over = if omitted.is_empty() {
            String::new()
        } else {
            format!(
                "[+{} hit(s) over budget - {}]\n",
                omitted.len(),
                omitted.join(" ")
            )
        };
        let len = texts.iter().map(String::len).sum::<usize>() + texts.len() - 1 + over.len();
        if budget == 0 || len <= budget {
            return Ok(texts.join("\n") + &over);
        }
        if let Some((block, row)) = drop_context(blocks) {
            lines[block].remove(row);
            texts[block] = block_text(&blocks[block], &lines[block], color);
            continue;
        }
        if cap == 0 {
            cap = 1000;
        } else if cap > 64 {
            cap = (cap / 2).max(64);
        } else if blocks.len() > 1 {
            omitted.push(blocks.pop().unwrap().hit.handle);
            lines.pop();
            texts.pop();
            continue;
        } else {
            let b = &blocks[0];
            let minimum = format!(
                "── {}\n  {}\n",
                b.hit.handle,
                surface::clip(
                    &surface::safe(&b.hit.snippet, false),
                    budget.saturating_sub(b.hit.handle.len() + 12)
                )
            );
            if minimum.len() > budget || minimum.trim_end().ends_with(&b.hit.handle) {
                bail!(
                    "--budget needs at least {} bytes for the first reference",
                    b.hit.handle.len() + 16
                )
            }
            return Ok(minimum);
        }
        lines = blocks.iter().map(|b| row_lines(b, cap)).collect();
        texts = blocks
            .iter()
            .zip(&lines)
            .map(|(b, l)| block_text(b, l, color))
            .collect();
    }
}
/// `value` with its hits within `budget` bytes, trimmed as `text_page` trims.
fn json_page(value: &mut Value, blocks: &mut Vec<Block>, budget: usize) -> Result<String> {
    let mut cap = 0;
    let mut omitted = Vec::new();
    // Compact JSON is compositional: the page is the envelope with its hits
    // array filled in. Only the block a drop changed is serialized again.
    let mut hits: Vec<_> = blocks.iter().map(|b| block_value(b, cap)).collect();
    let mut sizes = hits.iter().map(json_len).collect::<Result<Vec<_>>>()?;
    loop {
        value["hits"] = json!([]);
        if !omitted.is_empty() {
            value["omitted_hits"] = json!(omitted)
        }
        let len =
            json_len(value)? + sizes.iter().sum::<usize>() + sizes.len().saturating_sub(1) + 1;
        if budget == 0 || len <= budget {
            value["hits"] = Value::Array(hits);
            let text = agrep_core::json_output::to_string(value)? + "\n";
            debug_assert_eq!(text.len(), len);
            return Ok(text);
        }
        if let Some((block, _)) = drop_context(blocks) {
            hits[block] = block_value(&blocks[block], cap);
            sizes[block] = json_len(&hits[block])?;
            continue;
        }
        if cap == 0 {
            cap = 1000;
        } else if cap > 64 {
            cap = (cap / 2).max(64);
        } else if blocks.len() > 1 {
            omitted.push(blocks.pop().unwrap().hit.handle);
            hits.pop();
            sizes.pop();
            continue;
        } else {
            bail!(
                "--budget needs at least {} bytes for the first reference and JSON envelope",
                len
            )
        }
        hits = blocks.iter().map(|b| block_value(b, cap)).collect();
        sizes = hits.iter().map(json_len).collect::<Result<_>>()?;
    }
}
pub(crate) fn run(a: Recall, pack: bool, no_auto: bool, color: &str) -> Result<i32> {
    if a.query
        .iter()
        .any(|s| s.starts_with('@') && s.contains(':') && handles::parse(s).is_err())
    {
        bail!("invalid result handle");
    }
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
    let (root, census) = {
        let _span = agrep_core::semantic_profile::span("freshness_prepare");
        ready(no_auto)?
    };
    let c = store::reader(&root)?;
    let mut semantic_coverage = None;
    let mut semantic_ran = false;
    let mut semantic_failed = false;
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
        let ids = handles::SessionIndex::load(&c)?.candidates(&c, chat)?;
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
    let mut deepen = Vec::new();
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
            let mut handle = handles::SessionIndex::load(&c)?.mint(&w.session, w.center, &text);
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
            deepen.push(None);
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
        let options = caller::options(&options, &caller, include_self, a.scope.no_self);
        let lane = if !semantic_failed
            && (a.semantic
                || (!a.lexical
                    && semantic::eligible(query)
                    && semantic::installed(&root)
                    && root.config()?.embeddings != "off"))
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
        // Selection reads only the head of each query's ranking, and a page as
        // deep as twice the target usually holds it at a fraction of the cost.
        // Merging with meaning results and the probe's session count need the
        // full page.
        let depth = if lane.is_none() && !a.probe {
            options.limit.min(target.saturating_mul(2))
        } else {
            options.limit
        };
        let keyword = if a.semantic && !semantic_failed {
            Default::default()
        } else {
            let _span = agrep_core::semantic_profile::span("keyword");
            let page = Options {
                limit: depth,
                ..options.clone()
            };
            let keyword = caller::search(&c, &page, &caller, include_self, a.scope.no_self)?;
            agrep_core::semantic_profile::event(serde_json::json!({
                "retrieval":keyword.timings,"examined":keyword.examined,"blocks":keyword.blocks
            }));
            keyword
        };
        let r = if let Some(lane) = lane {
            match lane.and_then(|work| semantic::finish(&c, work)) {
                Ok(mut found) => {
                    semantic_ran = true;
                    semantic_coverage = Some(found.coverage);
                    caller::decorate_semantic(&c, &mut found.result, &caller)?;
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
                Err(error) => {
                    agrep_core::logging::semantic_failure(&root, &error);
                    semantic_failed = true;
                    if a.semantic {
                        caller::search(&c, &options, &caller, include_self, a.scope.no_self)?
                    } else {
                        keyword
                    }
                }
            }
        } else {
            keyword
        };
        totals_exact &= r.total_basis == "exact";
        known_sessions.extend(r.hits.iter().map(|h| h.session.clone()));
        deepen.push((depth < options.limit && r.hits.len() == depth).then_some(options));
        buckets.push(r.hits);
    }
    let selected = select_deepening(
        &mut buckets,
        &mut deepen,
        target,
        a.scope.all_side_chats || filters.chat.is_some(),
        |options| {
            let _span = agrep_core::semantic_profile::span("keyword");
            Ok(caller::search(&c, &options, &caller, include_self, a.scope.no_self)?.hits)
        },
    )?;
    let requests: Vec<_> = selected
        .iter()
        .map(|(_, h)| Request::new(&h.handle, None, Some(a.context as i64)))
        .collect();
    let windows_span = agrep_core::semantic_profile::span("windows");
    let windows = evidence::windows(&c, &requests)?;
    drop(windows_span);
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
    let pending_sources = {
        let _span = agrep_core::semantic_profile::span("freshness_join");
        crate::finish_census(&root, census)?
    };
    let _render_span = agrep_core::semantic_profile::span("render");
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
        let mut value = json!({"query":if pack{json!(queries)}else{json!(queries[0])},"engine":"sqlite","hits":[],"semantic_status":{"state":if semantic_failed{"failed"}else if semantic_ran{"complete"}else{"not-run"},"reason":if semantic_failed{Some("retrieval-failed")}else if semantic_ran{None}else{Some(if a.lexical{"--lexical"}else if !semantic::installed(&root){"model-not-installed"}else{"query-ineligible"})}},"self_exclusion":caller.metadata(include_self,a.scope.no_self),"filter_coverage":coverage,"freshness":render::freshness(checked,pending_sources),"semantic_coverage":semantic_coverage});
        if override_filters {
            value["filter_override"] = json!({"supplied":direct_filters,"reason":"direct-handle"});
        }
        surface::write(&json_page(&mut value, &mut blocks, a.budget)?)?;
    } else if blocks.is_empty() {
        let n: i64 = c.query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
        eprintln!("no hits across {n} indexed sessions.");
    } else {
        surface::write(&text_page(&mut blocks, a.budget, color)?)?;
    }
    Ok(exit)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invisible_tool_context_does_not_delay_the_message_cap() {
        let event = evidence::Event {
            turn: 1,
            ts: 1,
            name: "shell".into(),
            kind: "tool".into(),
            input: "unused context".into(),
            output: String::new(),
            input_chars: 14,
            output_chars: 0,
            output_bytes: Some(0),
            ok: Some(true),
            identity: "event".into(),
            text: String::new(),
            raw: json!({}),
        };
        let mut block = Block {
            hit_turns: BTreeSet::from([0]),
            hit: Hit {
                session: "fixture".into(),
                ..Default::default()
            },
            window: Window {
                target: "fixture".into(),
                session: "fixture".into(),
                agent: "pi".into(),
                project: String::new(),
                title: String::new(),
                family: "fixture".into(),
                side: false,
                center: 0,
                radius: Some(2),
                latest: false,
                first: 0,
                last: 2,
                rows: vec![Row {
                    session: "fixture".into(),
                    turn: 0,
                    who: "user".into(),
                    ts: 0,
                    text: "retained evidence".into(),
                    model: String::new(),
                    model_source: String::new(),
                    source: 1,
                    source_chars: 17,
                }],
                events: vec![event; 4096],
                notes: vec![],
                claim: None,
            },
        };
        let before = block_value(&block, 64);
        assert!(drop_context(std::slice::from_mut(&mut block)).is_none());
        assert_eq!(block.window.events.len(), 4096);
        assert_eq!(block_value(&block, 64), before);
        assert!(!block_text(&block, &row_lines(&block, 64), false).contains("4096 tool calls"));
    }

    /// The page builders before incremental rendering: every iteration
    /// rendered and serialized every block again.
    fn rerendered_text(blocks: &mut Vec<Block>, budget: usize, color: bool) -> Result<String> {
        let mut cap = 0;
        let mut omitted: Vec<String> = Vec::new();
        loop {
            let mut text = blocks
                .iter()
                .map(|b| block_text(b, &row_lines(b, cap), color))
                .collect::<Vec<_>>()
                .join("\n");
            if !omitted.is_empty() {
                text += &format!(
                    "[+{} hit(s) over budget - {}]\n",
                    omitted.len(),
                    omitted.join(" ")
                );
            }
            if budget == 0 || text.len() <= budget {
                return Ok(text);
            }
            if drop_context(blocks).is_some() {
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
            let b = &blocks[0];
            let minimum = format!(
                "── {}\n  {}\n",
                b.hit.handle,
                surface::clip(
                    &surface::safe(&b.hit.snippet, false),
                    budget.saturating_sub(b.hit.handle.len() + 12)
                )
            );
            if minimum.len() > budget || minimum.trim_end().ends_with(&b.hit.handle) {
                bail!(
                    "--budget needs at least {} bytes for the first reference",
                    b.hit.handle.len() + 16
                )
            }
            return Ok(minimum);
        }
    }
    fn reserialized_json(
        value: &mut Value,
        blocks: &mut Vec<Block>,
        budget: usize,
    ) -> Result<String> {
        let mut cap = 0;
        let mut omitted: Vec<String> = Vec::new();
        loop {
            value["hits"] = json!(blocks
                .iter()
                .map(|b| block_value(b, cap))
                .collect::<Vec<_>>());
            if !omitted.is_empty() {
                value["omitted_hits"] = json!(omitted)
            }
            let text = agrep_core::json_output::to_string(&value)? + "\n";
            if budget == 0 || text.len() <= budget {
                return Ok(text);
            }
            if drop_context(blocks).is_some() {
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
            bail!(
                "--budget needs at least {} bytes for the first reference and JSON envelope",
                text.len()
            )
        }
    }
    /// Blocks with long, multibyte, control-bearing and whitespace-run rows,
    /// repeated turns, events on and off retained turns, and tool hits.
    fn blocks(seed: u64) -> Vec<Block> {
        let mut state = seed;
        let mut next = move |n: u64| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) % n
        };
        const WORDS: [&str; 8] = [
            "release",
            "café 界",
            "  spaced\t\tout ",
            "\u{1b}[31mred",
            "workflow",
            "🙂",
            "x",
            "\n",
        ];
        let mut blocks = Vec::new();
        for b in 0..1 + next(5) {
            let session = format!("session-{seed}-{b}");
            let center = 3 + next(4) as i64;
            let mut rows = Vec::new();
            for turn in center - 2..=center + 2 {
                for part in 0..1 + next(2) {
                    let mut words = Vec::new();
                    let length = if next(4) == 0 { next(300) } else { next(30) };
                    for _ in 0..length {
                        words.push(WORDS[next(WORDS.len() as u64) as usize]);
                    }
                    let text = words.join(" ");
                    rows.push(Row {
                        session: session.clone(),
                        turn,
                        who: if part == 0 { "user" } else { "agent" }.into(),
                        ts: turn * 10 + part as i64,
                        source_chars: text.chars().count() + next(3) as usize,
                        text,
                        model: String::new(),
                        model_source: String::new(),
                        source: 1,
                    });
                }
            }
            let mut events = Vec::new();
            for i in 0..next(6) {
                let input = "ls -la ".repeat(1 + next(900) as usize);
                events.push(evidence::Event {
                    turn: center - 3 + next(7) as i64,
                    ts: i as i64,
                    name: "shell".into(),
                    kind: "tool".into(),
                    input_chars: input.chars().count() + next(2) as usize,
                    input,
                    output: "ok".into(),
                    output_chars: 2 + next(2) as usize,
                    output_bytes: Some(2),
                    ok: Some(true),
                    identity: format!("event-{i}"),
                    text: String::new(),
                    raw: json!({}),
                });
            }
            let tool = next(3) == 0;
            blocks.push(Block {
                hit_turns: BTreeSet::from([center]),
                hit: Hit {
                    session: session.clone(),
                    agent: "pi".into(),
                    project: "/work/agrep".into(),
                    turn: center,
                    ts: 1_700_000_000_000,
                    who: if tool { "tool" } else { "user" }.into(),
                    snippet: "release workflow café 界 ".repeat(1 + next(20) as usize),
                    matched: if tool { "phrase" } else { "all-terms" }.into(),
                    handle: format!("@{session}:{center}.abcd"),
                    score: 1.5,
                    ..Default::default()
                },
                window: Window {
                    target: session.clone(),
                    session,
                    agent: "pi".into(),
                    project: "/work/agrep".into(),
                    title: String::new(),
                    family: String::new(),
                    side: false,
                    center,
                    radius: Some(2),
                    latest: false,
                    first: 0,
                    last: center + 2,
                    rows,
                    events,
                    notes: vec![],
                    claim: None,
                },
            });
        }
        blocks
    }

    #[test]
    fn deepening_exhausted_prefixes_selects_like_full_rankings() {
        let mut state = 7u64;
        let mut next = move |n: u64| {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) % n
        };
        let key = |selected: &[(usize, Hit)]| {
            selected
                .iter()
                .map(|(i, h)| (*i, h.session.clone(), h.turn))
                .collect::<Vec<_>>()
        };
        for _ in 0..400 {
            let families = 1 + next(6);
            let full: Vec<Vec<Hit>> = (0..1 + next(3))
                .map(|_| {
                    (0..next(40))
                        .map(|_| {
                            let session = next(10);
                            Hit {
                                session: format!("s{session}"),
                                family: format!("f{}", session % families),
                                turn: next(4) as i64,
                                ..Default::default()
                            }
                        })
                        .collect()
                })
                .collect();
            let target = 1 + next(9) as usize;
            for per_session in [false, true] {
                let expected = key(&select(&full, target, per_session).0);
                for depth in 1..=24 {
                    let mut buckets: Vec<Vec<Hit>> = full
                        .iter()
                        .map(|b| b[..depth.min(b.len())].to_vec())
                        .collect();
                    let mut deepen: Vec<Option<usize>> = (0..full.len())
                        .map(|i| (full[i].len() > depth).then_some(i))
                        .collect();
                    let selected =
                        select_deepening(&mut buckets, &mut deepen, target, per_session, |i| {
                            Ok(full[i].clone())
                        })
                        .unwrap();
                    assert_eq!(key(&selected), expected, "depth {depth} target {target}");
                }
            }
        }
    }

    #[test]
    fn incremental_pages_match_full_rerendering_at_every_budget() {
        let outcome = |r: Result<String>| r.map_err(|e| e.to_string());
        for seed in 0..10 {
            let full = text_page(&mut blocks(seed), 0, false).unwrap().len();
            let json_full = json_page(&mut json!({"hits":[]}), &mut blocks(seed), 0)
                .unwrap()
                .len();
            for budget in (0..=full + 64).step_by(full / 32 + 1).chain([64, 65, 100]) {
                for color in [false, true] {
                    assert_eq!(
                        outcome(text_page(&mut blocks(seed), budget, color)),
                        outcome(rerendered_text(&mut blocks(seed), budget, color)),
                        "seed {seed} budget {budget}"
                    );
                }
            }
            for budget in (0..=json_full + 64)
                .step_by(json_full / 32 + 1)
                .chain([2048, 4096])
            {
                let envelope =
                    || json!({"query":"release","hits":[],"freshness":{"checked":false}});
                assert_eq!(
                    outcome(json_page(&mut envelope(), &mut blocks(seed), budget)),
                    outcome(reserialized_json(
                        &mut envelope(),
                        &mut blocks(seed),
                        budget
                    )),
                    "seed {seed} budget {budget}"
                );
            }
        }
    }
}
