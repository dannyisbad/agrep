use super::*;
pub(super) fn emit_probe(
    ctx: &QueryContext<'_>,
    state: &Collected,
    blocks: &[Block],
    notice: Option<&str>,
    checked: bool,
) -> Result<i32> {
    if let Some(notice) = &notice {
        eprintln!("{notice}");
    }
    let confident = blocks
        .iter()
        .find(|b| b.hit.matched == "phrase" || b.hit.matched == "semantic");
    let line = if let Some(b) = confident {
        let n = state.known_sessions.len().max(1);
        let lead = if state.totals_exact {
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
            head(&b.hit),
            surface::shell(
                &std::iter::once("agrep".to_owned())
                    .chain(std::iter::once(
                        if ctx.input.pack { "pack" } else { "recall" }.into()
                    ))
                    .chain(ctx.input.queries.clone())
                    .collect::<Vec<_>>()
            )
        )
    } else {
        let count: i64 = ctx
            .c
            .query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
        format!("recall: no confident match in {count} past conversations")
    };
    if ctx.input.a.budget > 0 && line.len() + 1 > ctx.input.a.budget {
        if let Some(b) = confident {
            let pointer = format!("recall: {}\n", b.hit.handle);
            if pointer.len() > ctx.input.a.budget {
                bail!(
                    "--budget needs at least {} bytes for the first reference",
                    pointer.len()
                )
            }
            surface::write(&pointer)?
        } else {
            surface::line(surface::prefix(&line, ctx.input.a.budget - 1))?
        }
    } else {
        surface::line(&line)?
    }
    render::note_no_auto(ctx.input.no_auto);
    Ok(if confident.is_some() {
        0
    } else if checked && !state.semantic_unavailable {
        1
    } else {
        2
    })
}
pub(super) fn emit_json(
    ctx: &mut QueryContext<'_>,
    state: &Collected,
    blocks: &[Block],
    page: &page::Page<'_>,
    hybrid_visible: bool,
    handle_only: bool,
    fell_back: bool,
) -> Result<()> {
    let mut value = json!({"query":if ctx.input.pack{json!(ctx.input.queries)}else{json!(ctx.input.queries[0])},"engine":if handle_only{"handle"}else{"sqlite"},"hits":[]});
    let unanswered = state.semantic_unavailable && blocks.is_empty();
    if unanswered {
        let mut engines: Vec<&str> = Vec::new();
        for (_, status, _) in &state.lane_runs {
            let engine = match status["state"].as_str() {
                Some("query-rejected") => "semantic:policy",
                Some("unavailable") => "semantic:unavailable",
                _ => continue,
            };
            if !engines.contains(&engine) {
                engines.push(engine);
            }
        }
        value["engine"] = json!(engines.join("+"));
    }
    if !state.handle_notes.is_empty() {
        value["served"] = json!({"served_as_requested":false,"divergences":state.handle_notes});
    }
    if let Some(floored) = ctx.hits_floored.take() {
        value["hits_floored"] = floored;
    }
    let meta = semantic_meta(
        &state.lane_runs,
        ctx.input.queries.len() > 1,
        hybrid_visible,
        state.hybrid_used,
    );
    let lane_coverage = meta.get("semantic_coverage").cloned().unwrap_or_default();
    for (key, field) in meta {
        value[key] = field;
    }
    if state.lane_runs.is_empty() {
        // 0.4.9 separated a lane that never ran from one that searched (py/recall.py:2662-2676).
        let reason = if handle_only {
            "handle-lookup"
        } else if ctx.input.a.lexical {
            "--lexical"
        } else if ctx.tools_only {
            "tool-lane"
        } else if ctx.root.config()?.embeddings == "off" {
            "embeddings-off"
        } else if !semantic::installed(ctx.root) {
            "model-not-installed"
        } else if !ctx.meaning_room {
            "no-meaning-room"
        } else if state.semantic_building {
            "keyword-only"
        } else {
            "query-not-language-shaped"
        };
        value["semantic_status"] = json!({"state":"not-run","reason":reason});
    }
    value["self_exclusion"] = json!(ctx.caller.metadata(ctx.input.request, state.excluded));
    value["filter_coverage"] = match &state.filter_note {
        Some(note) => json!({"empty_dimensions":[],"checked":false,"reason":note}),
        None => json!(ctx.coverage),
    };
    value["freshness"] = render::machine_freshness(ctx.input.no_auto);
    value["semantic_coverage"] = lane_coverage;
    if unanswered {
        value["error"] = json!({"code":"semantic-unavailable"});
    }
    if fell_back {
        value["note"] = json!("no exact phrase match; showing hits containing all terms");
    }
    surface::write(&page::json(page, &mut value)?)?;
    Ok(())
}
pub(super) fn emit_page(
    ctx: &mut QueryContext<'_>,
    state: &Collected,
    blocks: &[Block],
    color: &str,
    hybrid_visible: bool,
    handle_only: bool,
    fell_back: bool,
) -> Result<i32> {
    if let Some(line) = agrep_core::query::meta_notice(state.meta_dropped, state.meta_retained) {
        eprintln!("{line}");
    }
    // 0.4.9 named an empty filter dimension after the page or its zero line.
    let disclose = || {
        if !ctx.input.a.json {
            agrep_core::coverage::disclose(&ctx.coverage);
        }
    };
    if ctx.input.a.probe {
        disclose();
    }
    let _render_span = agrep_core::semantic_profile::span("render");
    let checked = agrep_core::render::checked(ctx.input.no_auto);
    // 0.4.9's `meaning_lane_down` (py/recall.py:2353-2357, 2766-2769): a meaning lane that
    // failed or fell back leaves an empty answer unverified.
    let meaning_down = state
        .lane_runs
        .iter()
        .any(|(_, status, _)| status["fallback_recommended"] == true);
    let exit = if blocks.is_empty() {
        if checked && !state.semantic_unavailable && !meaning_down && !state.model_missing {
            1
        } else {
            2
        }
    } else {
        0
    };
    let notice = ctx
        .policy
        .as_ref()
        .zip(state.excluded)
        .and_then(|(policy, n)| policy.notice(n, "hit"))
        .filter(|_| !ctx.input.a.json);
    if ctx.input.a.probe {
        return emit_probe(ctx, state, blocks, notice.as_deref(), checked);
    }
    // 0.4.9's lane-down disclosure (py/recall.py:2601-2616): recall never goes silently
    // keyword-only.
    if state.model_missing {
        eprintln!(
            "meaning unavailable; keyword-only (semantic model not installed: run agrep setup)"
        );
    }
    let color = color == "always" || (color == "auto" && std::io::stdout().is_terminal());
    let page = page::Page::new(blocks, &ctx.input.queries, ctx.input.a.budget);
    if ctx.input.a.json {
        emit_json(
            ctx,
            state,
            blocks,
            &page,
            hybrid_visible,
            handle_only,
            fell_back,
        )?;
    } else if blocks.is_empty() && state.semantic_unavailable {
        // 0.4.9's line stands alone (py/recall.py:2745-2747).
        let meta = semantic_meta(&state.lane_runs, ctx.input.queries.len() > 1, false, false);
        eprintln!("{}", semantic::unavailable_notice(&meta["semantic_status"]));
        return Ok(exit);
    } else if blocks.is_empty() {
        let n: i64 = ctx
            .c
            .query_row("SELECT count(*) FROM sessions", [], |r| r.get(0))?;
        eprintln!(
            "no hits across {} indexed session{}.",
            surface::number(n.max(0) as usize),
            if n == 1 { "" } else { "s" }
        );
        disclose();
    } else {
        surface::write(&page::text(&page, color)?)?;
        disclose();
    }
    render::note_no_auto(ctx.input.no_auto);
    if let Some(notice) = &notice {
        eprintln!("{notice}");
    }
    Ok(exit)
}
