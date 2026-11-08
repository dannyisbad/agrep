use super::*;
struct Record {
    line: String,
    required: bool,
    tool: bool,
    drop_key: (u8, i64, usize),
}
/// 0.4.9's `_cap_line_bytes`: a line cut to `budget` bytes, keeping its recovery pointer.
fn cap_line_bytes(line: &str, budget: i64) -> String {
    if line.len() as i64 <= budget {
        return line.to_owned();
    }
    let markers: Vec<(usize, usize, bool)> = TRUNC_MARKER
        .find_iter(line)
        .map(|m| (m.start(), m.end(), m.as_str().contains("agrep around ")))
        .collect();
    let mut budget = budget;
    let (body, suffix) = match markers.iter().rev().find(|m| m.2) {
        Some(&(start, _, _)) => {
            let suffix = format!(" {}", &line[start..]);
            budget -= suffix.len() as i64;
            (trim_end(&line[..start]), suffix)
        }
        None => (line, String::new()),
    };
    if budget <= 0 {
        return trim_start(&suffix).to_owned();
    }
    let ellipsis = if budget > 3 { "…" } else { "" };
    let mut cut = surface::prefix(body, (budget - ellipsis.len() as i64) as usize).len();
    if let Some(&(start, _, _)) = markers
        .iter()
        .find(|(start, end, _)| *start < cut && cut < *end)
    {
        cut = start;
    }
    let prefix = trim_end(&body[..cut]);
    if prefix.is_empty() {
        trim_start(&suffix).to_owned()
    } else {
        format!("{prefix}{ellipsis}{suffix}")
    }
}
/// 0.4.9's `_cap_required_lines`: exact bytes shared so every required row keeps a prefix;
/// the head takes its whole size first when it fits.
fn cap_required_lines(
    head: &str,
    records: &mut [Record],
    required: &[usize],
    budget: usize,
) -> String {
    let values: Vec<String> = std::iter::once(head.to_owned())
        .chain(required.iter().map(|i| records[*i].line.clone()))
        .collect();
    let mut remaining = budget.saturating_sub(values.len().saturating_sub(1));
    let mut pending: Vec<usize> = (0..values.len()).collect();
    let mut allocations = vec![0; values.len()];
    if head.len() <= remaining {
        allocations[0] = head.len();
        remaining -= head.len();
        pending.remove(0);
    }
    while !pending.is_empty() {
        let share = remaining / pending.len();
        let small: Vec<usize> = pending
            .iter()
            .copied()
            .filter(|i| values[*i].len() <= share)
            .collect();
        if small.is_empty() {
            let extra = remaining % pending.len();
            for (offset, index) in pending.iter().enumerate() {
                allocations[*index] = share + usize::from(offset < extra);
            }
            break;
        }
        for index in small {
            allocations[index] = values[index].len();
            remaining -= values[index].len();
            pending.retain(|p| *p != index);
        }
    }
    let mut values = values
        .iter()
        .zip(&allocations)
        .map(|(value, budget)| cap_line_bytes(value, *budget as i64));
    let head = values.next().expect("the head is always present");
    for (index, value) in required.iter().zip(values) {
        records[*index].line = value;
    }
    head
}
/// 0.4.9's `_fit_recall_records`: a block within `limit` bytes (zero is unbounded) that
/// keeps its required rows, dropping context rows farthest from the hit first.
fn fit_records(
    head: &str,
    records: &mut [Record],
    limit: usize,
    expand: &str,
    hidden_tools: usize,
    markers: &[usize; 3],
) -> String {
    let join = |head: &str, lines: Vec<&str>, tail: &str| {
        let mut out = head.to_owned();
        for line in lines.into_iter().chain((!tail.is_empty()).then_some(tail)) {
            out.push('\n');
            out.push_str(line);
        }
        out
    };
    if limit == 0 {
        let tail = events_tail(hidden_tools, markers, expand);
        return join(
            head,
            records.iter().map(|r| r.line.as_str()).collect(),
            &tail,
        );
    }
    let required: Vec<usize> = (0..records.len())
        .filter(|i| records[*i].required)
        .collect();
    let mut keep = vec![true; records.len()];
    let (mut dropped_tools, mut dropped_context) = (hidden_tools, 0);
    let marker = |dropped_context: usize, dropped_tools: usize| {
        if dropped_context > 0 {
            format!("     [output truncated to --budget - rest at {expand}]")
        } else {
            events_tail(dropped_tools, markers, expand)
        }
    };
    let mut droppable: Vec<usize> = (0..records.len())
        .filter(|i| !records[*i].required)
        .collect();
    droppable.sort_by_key(|i| records[*i].drop_key);
    let mut kept_bytes: usize = records.iter().map(|r| r.line.len()).sum();
    let mut kept_rows = records.len();
    let size = |kept_bytes: usize, kept_rows: usize, tail: &str| {
        head.len() + kept_bytes + tail.len() + kept_rows + usize::from(!tail.is_empty())
    };
    for index in droppable {
        if size(
            kept_bytes,
            kept_rows,
            &marker(dropped_context, dropped_tools),
        ) <= limit
        {
            break;
        }
        keep[index] = false;
        kept_bytes -= records[index].line.len();
        kept_rows -= 1;
        if records[index].tool {
            dropped_tools += 1;
        } else {
            dropped_context += 1;
        }
    }
    let tail = marker(dropped_context, dropped_tools);
    if size(kept_bytes, kept_rows, &tail) <= limit {
        let lines = records
            .iter()
            .zip(&keep)
            .filter(|(_, keep)| **keep)
            .map(|(r, _)| r.line.as_str())
            .collect();
        return join(head, lines, &tail);
    }
    if tail.is_empty() {
        let head = cap_required_lines(head, records, &required, limit);
        return join(
            &head,
            required.iter().map(|i| records[*i].line.as_str()).collect(),
            "",
        );
    }
    let compact = if dropped_context > 0 {
        "     … surrounding context omitted"
    } else if dropped_tools > 0 {
        "       … surrounding tool calls omitted"
    } else {
        "       … surrounding events omitted"
    };
    let tail = if compact.len() < tail.len() {
        compact.to_owned()
    } else {
        tail
    };
    let head = cap_required_lines(
        head,
        records,
        &required,
        limit.saturating_sub(tail.len() + 1),
    );
    join(
        &head,
        required.iter().map(|i| records[*i].line.as_str()).collect(),
        &tail,
    )
}
/// The page a budget too small for its first block keeps: the head (its handle is the
/// recovery route) and the first message row cut plain, or the head alone; `None` when
/// even the head does not fit.
fn tiny_block(head: &str, rows: &[String], room: i64) -> Option<String> {
    if head.len() as i64 > room {
        return None;
    }
    let left = room - head.len() as i64 - 1;
    for line in rows {
        let plain = TRUNC_MARKER.replace_all(line, "");
        let plain = trim_end(&plain);
        let Some(found) = MSG_ROW.captures(plain) else {
            continue;
        };
        let prefix = found
            .get(4)
            .expect("the body group always participates")
            .start();
        if left >= (prefix + TINY_MATCH_MIN) as i64 {
            return Some(format!("{head}\n{}", cap_line_bytes(plain, left)));
        }
        break;
    }
    Some(head.to_owned())
}
fn bare_trailer(count: usize) -> String {
    format!("[+{count} hit(s) over budget - {MORE_HINT}]")
}
/// `[+N hit(s) over budget - h1 h2 +K more (see --json omitted_hits)]` within `limit`
/// bytes (zero is unbounded): the count always, then as many handles as fit.
fn over_budget_trailer(count: usize, cmds: &[String], limit: usize) -> String {
    let mut unique: Vec<&str> = Vec::new();
    for cmd in cmds {
        if !cmd.is_empty() && !unique.contains(&cmd.as_str()) {
            unique.push(cmd);
        }
    }
    let joiner = if !unique.is_empty()
        && unique
            .iter()
            .all(|c| c.starts_with('@') && !c.chars().any(evidence::py_space))
    {
        " "
    } else {
        " · "
    };
    let base = format!("[+{count} hit(s) over budget");
    let fits = |candidate: &str| limit == 0 || candidate.len() <= limit;
    for pointer in [true, false] {
        for shown in (0..=unique.len()).rev() {
            let named = unique[..shown].join(joiner);
            let more = if pointer && count > shown {
                format!("+{} more ({MORE_HINT})", count - shown)
            } else {
                String::new()
            };
            let body = [named, more]
                .into_iter()
                .filter(|part| !part.is_empty())
                .collect::<Vec<_>>()
                .join(" ");
            let candidate = if body.is_empty() {
                format!("{base}]")
            } else {
                format!("{base} - {body}]")
            };
            if (shown > 0 || pointer) && fits(&candidate) {
                return candidate;
            }
        }
    }
    let bare = bare_trailer(count);
    if fits(&bare) {
        bare
    } else {
        format!("{base}]")
    }
}
/// The omitted count and named commands of an over-budget trailer.
fn parse_trailer(trailer: &str) -> Option<(usize, Vec<String>)> {
    let found = TRAILER.captures(trailer)?;
    let tail = MORE.replace(found.get(2).map_or("", |m| m.as_str()), "");
    let words: Vec<&str> = tail
        .split(evidence::py_space)
        .filter(|w| !w.is_empty())
        .collect();
    let cmds: Vec<String> = if !words.is_empty() && words.iter().all(|w| w.starts_with('@')) {
        words.into_iter().map(str::to_owned).collect()
    } else {
        tail.split(" · ")
            .filter(|c| !c.is_empty())
            .map(str::to_owned)
            .collect()
    };
    Some((found[1].parse().ok()?, cmds))
}
/// A page holding only the disclosure: every block folds into the count, lead first.
fn degrade_trailer(trailer: &str, ends: &[(usize, String)], budget: usize) -> String {
    let Some((count, cmds)) = parse_trailer(trailer) else {
        return surface::prefix(trailer, budget).to_owned();
    };
    let named: Vec<String> = ends
        .iter()
        .map(|(_, cmd)| cmd.clone())
        .filter(|cmd| !cmd.is_empty())
        .chain(cmds)
        .collect();
    over_budget_trailer(count + ends.len(), &named, budget)
}
/// Handles of the blocks a cut at byte `cut` drops whole.
fn evicted_handles(cut: usize, ends: &[(usize, String)]) -> Vec<String> {
    let mut start = 0;
    let mut out = Vec::new();
    for (end, cmd) in ends {
        if start >= cut && !cmd.is_empty() {
            out.push(cmd.clone());
        }
        start = end + 2;
    }
    out
}
fn with_evicted(trailer: &str, evicted: &[String], limit: usize) -> String {
    if evicted.is_empty() {
        return trailer.to_owned();
    }
    let (count, cmds) = parse_trailer(trailer).unwrap_or_default();
    let named: Vec<String> = evicted.iter().cloned().chain(cmds).collect();
    over_budget_trailer(count + evicted.len(), &named, limit)
}
fn joined(text: &str, trailer: &str) -> String {
    match (text.is_empty(), trailer.is_empty()) {
        (false, false) => format!("{text}\n\n{trailer}"),
        (true, _) => trailer.to_owned(),
        (false, true) => text.to_owned(),
    }
}
/// 0.4.9's `_fit_text_payload`: `text` and its trailer within `budget` bytes, never cutting
/// inside a marker or a later block's head line; blocks the cut evicts join the trailer.
fn fit_text(
    text: &str,
    budget: usize,
    ends: &[(usize, String)],
    trailer: &str,
    keep: Option<usize>,
) -> String {
    let whole = joined(text, trailer);
    if budget == 0 || whole.len() <= budget {
        return whole;
    }
    let first = ends.first().map_or(text.len(), |(end, _)| *end);
    let limit = match keep {
        Some(keep) => (budget as i64 - keep.min(first) as i64 - 2).max(1) as usize,
        None => (budget as i64 - first as i64 - 2).max(TRAILER_RESERVE as i64) as usize,
    };
    let mut evicted: Vec<String> = Vec::new();
    let mut body = String::new();
    for _ in 0..6 {
        let full = with_evicted(trailer, &evicted, limit);
        let (fitted, cut) = cut_text(text, budget, ends, &full);
        body = fitted;
        let now = cut.map_or_else(Vec::new, |cut| evicted_handles(cut, ends));
        if now == evicted {
            break;
        }
        evicted = now;
    }
    body
}
/// One cut: the fitted page and the byte offset the cut landed at (`None` when the
/// trailer alone fills the page).
fn cut_text(
    text: &str,
    budget: usize,
    ends: &[(usize, String)],
    trailer: &str,
) -> (String, Option<usize>) {
    if !trailer.is_empty() {
        let whole = joined(text, trailer);
        if budget == 0 || whole.len() <= budget {
            return (whole, Some(text.len()));
        }
        let room = budget as i64 - trailer.len() as i64 - 2;
        if room >= 80 {
            let (body, cut) = cut_text(text, room as usize, ends, "");
            return (body + "\n\n" + trailer, cut);
        }
        return (degrade_trailer(trailer, ends, budget), None);
    }
    if budget == 0 || text.len() <= budget {
        return (text.to_owned(), Some(text.len()));
    }
    let generic = "\n[output truncated to --budget; rerun with a larger budget]";
    let mut marker = generic.to_owned();
    for _ in 0..3 {
        let cut = budget as i64 - marker.len() as i64;
        let wanted = ends
            .iter()
            .find(|(end, cmd)| *end as i64 > cut && !cmd.is_empty())
            .map_or_else(
                || generic.to_owned(),
                |(_, cmd)| format!("\n[output truncated to --budget - rest at {cmd}]"),
            );
        if wanted == marker {
            break;
        }
        marker = wanted;
    }
    if marker.len() >= budget {
        marker = generic.to_owned();
    }
    if marker.len() >= budget {
        return (surface::prefix(generic, budget).to_owned(), Some(0));
    }
    let mut cut = surface::prefix(text, budget - marker.len()).len();
    let scan = text[..cut]
        .char_indices()
        .rev()
        .nth(399)
        .map_or(0, |(at, _)| at);
    for m in TRUNC_MARKER.find_iter(&text[scan..]) {
        let (start, end) = (scan + m.start(), scan + m.end());
        if start >= cut {
            break;
        }
        if end > cut {
            cut = start;
            break;
        }
    }
    // A cut inside a later block's head line would slice its handle: the block goes whole.
    let mut start = 0;
    for (end, _) in ends {
        if start < cut && cut < *end {
            let head_end = text[start..].find('\n').map(|at| start + at);
            if start > 0 && head_end.is_none_or(|head_end| cut <= head_end) {
                cut = start;
            }
            break;
        }
        start = end + 2;
    }
    (trim_end(&text[..cut]).to_owned() + &marker, Some(cut))
}
const HEADER: &str = "\x1b[1;36m";
const TURN: &str = "\x1b[33m";
const AGENT: &str = "\x1b[36m";
const DIM: &str = "\x1b[2m";
const FAILED: &str = "\x1b[1;31m";
const RESET: &str = "\x1b[0m";
fn dim_markers(text: &str) -> String {
    let dim = |c: &Captures| format!("{DIM}{}{RESET}", &c[0]);
    let once = TRUNC_MARKER.replace_all(text, dim);
    BUDGET_MARKER.replace_all(&once, dim).into_owned()
}
fn paint_line(line: &str) -> String {
    if line.starts_with("── ") {
        return match line.split_once(SNIP_SEP) {
            Some((head, snippet)) => format!("{HEADER}{head}{RESET} - {}", dim_markers(snippet)),
            None => format!("{HEADER}{line}{RESET}"),
        };
    }
    if let Some(row) = MSG_ROW.captures(line) {
        let speaker = if &row[3] == "agent" { AGENT } else { TURN };
        return format!(
            "{}{TURN}{}{RESET} {speaker}{}:{RESET} {}",
            &row[1],
            &row[2],
            &row[3],
            dim_markers(&row[4])
        );
    }
    if line.starts_with("     ") {
        let code = if line.contains("✗ FAILED") {
            FAILED
        } else {
            DIM
        };
        return format!("{code}{line}{RESET}");
    }
    dim_markers(line)
}
/// The fitted page painted: the budget never counts these bytes.
fn paint(payload: &str) -> String {
    payload
        .split('\n')
        .map(paint_line)
        .collect::<Vec<_>>()
        .join("\n")
        .replace('\x1f', " ")
}
/// 0.4.9's `_render_text_payload`: the fitted page, painted within the budget when color
/// is on, with the final newline counted.
fn render_payload(
    text: &str,
    budget: usize,
    ends: &[(usize, String)],
    trailer: &str,
    color: bool,
    keep: Option<usize>,
) -> String {
    let content = budget.saturating_sub(1);
    if !color {
        return fit_text(text, content, ends, trailer, keep).replace('\x1f', " ");
    }
    if budget == 0 {
        return paint(&fit_text(text, 0, ends, trailer, keep));
    }
    let mut plain_budget = content;
    for _ in 0..12 {
        let painted = paint(&fit_text(text, plain_budget, ends, trailer, keep));
        let excess = painted.len() as i64 - content as i64;
        if excess <= 0 {
            return painted;
        }
        if plain_budget == 0 {
            break;
        }
        let next = (plain_budget as i64 - excess).max(0) as usize;
        plain_budget = (plain_budget - 1).min(next);
    }
    fit_text(text, content, ends, trailer, keep).replace('\x1f', " ")
}
/// One page line ending in a newline, cut to the budget.
/// The text of a block's hit turn, else its first message.
fn center_text<'a>(b: &Block, turns: &[Turn<'a>]) -> &'a str {
    let center = turns
        .iter()
        .find(|t| t.turn == b.window.center)
        .and_then(|t| {
            let reply = reply_text(t);
            let text = if b.hit.who == "agent" && !reply.is_empty() {
                reply
            } else if !t.text.is_empty() {
                t.text
            } else {
                reply
            };
            (!text.is_empty()).then_some(text)
        });
    center.unwrap_or_else(|| {
        turns
            .iter()
            .map(|t| {
                if t.text.is_empty() {
                    reply_text(t)
                } else {
                    t.text
                }
            })
            .find(|text| !text.is_empty())
            .unwrap_or("")
    })
}
/// The text page within `page.budget` bytes (zero is unbounded), 0.4.9's render: weak
/// blocks past the first collapse to one line, every hit turn gets a floor, the spare
/// widens blocks with a double share for the first, and when the page's text exceeds 85%
/// of what is left, messages are capped and each block is fitted to its share.
pub(in crate::cmd::recall) fn text(page: &Page, color: bool) -> Result<String> {
    let TextPlan {
        budget,
        blocks,
        n,
        collapse,
        full,
        full_count,
        heads,
        turns,
        floor,
        room,
        spare_share,
        selections,
        fits,
    } = text_plan(page);
    let trail = |start: usize, used: usize| {
        let rest = &blocks[start..];
        let cmds: Vec<String> = rest.iter().map(|b| b.hit.handle.clone()).collect();
        let left = if budget > 0 {
            budget as i64 - 1 - used as i64 - 2
        } else {
            0
        };
        let pointer = over_budget_trailer(rest.len(), &[], 0);
        let limit = if budget > 0 && pointer.len() as i64 <= left {
            left as usize
        } else {
            0
        };
        over_budget_trailer(rest.len(), &cmds, limit)
    };
    let mut used = 0;
    let mut shown_full = 0;
    let mut out: Vec<String> = Vec::new();
    let mut cmds: Vec<String> = Vec::new();
    let mut trailer = String::new();
    let mut first_block: Option<(String, Vec<String>, String)> = None;
    for (i, b) in blocks.iter().enumerate() {
        if budget > 0 && (used >= budget || (!collapse[i] && floor[i] == 0)) {
            trailer = trail(i, used);
            break;
        }
        if collapse[i] {
            let anchors: Vec<&Anchor> = page.anchors[b.queries[0].min(page.anchors.len() - 1)]
                .iter()
                .collect();
            let snippet = surface::safe(
                &evidence::weak_snippet(center_text(b, &turns[i]), &b.hit.snippet, &anchors),
                false,
            );
            let line = if snippet.is_empty() {
                format!("── {}", heads[i])
            } else {
                format!("── {}{SNIP_SEP}{snippet}", heads[i])
            };
            used += line.len() + 1;
            out.push(line);
            cmds.push(b.hit.handle.clone());
            continue;
        }
        let (block, first) = full_block(BlockContext {
            page,
            b,
            turns: &turns[i],
            selections: &selections[i],
            head: &heads[i],
            floor: floor[i],
            full_count,
            spare_share,
            shown_full,
            fits,
        });
        if let Some(first) = first {
            first_block = Some(first);
        }
        if budget > 0 && shown_full >= room && used + block.len() + 1 > budget {
            // past the block reserve, a block still renders when it fits what is left
            trailer = trail(i, used);
            break;
        }
        used += block.len() + 1;
        out.push(block);
        cmds.push(b.hit.handle.clone());
        shown_full += 1;
    }
    let (out, cmds, trailer) = fit_first_block(out, cmds, trailer, first_block, budget, n, used)?;
    let mut ends = Vec::new();
    let mut offset = 0;
    for (block, cmd) in out.iter().zip(cmds) {
        offset += block.len();
        ends.push((offset, cmd));
        offset += 2;
    }
    let keep = (budget > 0)
        .then(|| full.iter().map(|i| floor[*i]).find(|f| *f > 0))
        .flatten();
    let payload = render_payload(&out.join("\n\n"), budget, &ends, &trailer, color, keep);
    Ok(written(payload, budget))
}

struct TextPlan<'a> {
    budget: usize,
    blocks: &'a [Block],
    n: usize,
    collapse: Vec<bool>,
    full: Vec<usize>,
    full_count: usize,
    heads: Vec<String>,
    turns: Vec<Vec<Turn<'a>>>,
    floor: Vec<usize>,
    room: usize,
    spare_share: usize,
    selections: Vec<Vec<(&'a Event, ToolMatch)>>,
    fits: bool,
}
fn text_plan<'a>(page: &'a Page<'_>) -> TextPlan<'a> {
    let budget = page.budget;
    let blocks = page.blocks;
    let n = blocks.len();
    let collapse: Vec<bool> = blocks
        .iter()
        .enumerate()
        .map(|(i, b)| i > 0 && weak(&b.hit))
        .collect();
    let full: Vec<usize> = (0..n).filter(|i| !collapse[*i]).collect();
    let full_count = full.len();
    let mut content_budget = if budget > 0 {
        budget.saturating_sub(WEAK_LINE_COST * (n - full_count))
    } else {
        0
    };
    let heads: Vec<String> = blocks.iter().map(|b| head(&b.hit)).collect();
    let turns: Vec<Vec<Turn>> = blocks.iter().map(|b| turns(&b.window.rows)).collect();
    let head_costs: Vec<usize> = full
        .iter()
        .map(|i| format!("── {}", heads[*i]).len() + 5)
        .collect();
    let needs: Vec<usize> = full
        .iter()
        .zip(&head_costs)
        .map(|(i, cost)| hit_need(&turns[*i], &blocks[*i].hit_turns) + cost)
        .collect();
    let mins: Vec<usize> = full
        .iter()
        .map(|i| hit_min(&turns[*i], &blocks[*i].hit_turns, &blocks[*i].window.target))
        .collect();
    let mut floors = hit_floors(&needs, &head_costs, &mins, content_budget);
    if budget > 0 && floors.contains(&0) {
        // omitted hits are named in a trailer the first block must leave room for
        content_budget = content_budget.saturating_sub(TRAILER_RESERVE);
        floors = hit_floors(&needs, &head_costs, &mins, content_budget);
    }
    let mut floor = vec![0; n];
    for (i, f) in full.iter().zip(&floors) {
        floor[*i] = *f;
    }
    let room = if budget > 0 {
        full.iter().filter(|i| floor[**i] > 0).count()
    } else {
        full_count
    }
    .max(1);
    let spare = if budget > 0 {
        content_budget.saturating_sub(floors.iter().sum())
    } else {
        0
    };
    let spare_share = spare / (full_count.min(room) + usize::from(full_count > 1)).max(1);
    let claims: Vec<_> = blocks.iter().map(tool_claim).collect();
    let selections: Vec<Vec<(&Event, ToolMatch)>> = blocks
        .iter()
        .zip(&claims)
        .map(|(b, claim)| matches(b, claim))
        .collect();
    let raw: usize = full
        .iter()
        .map(|i| {
            turns[*i]
                .iter()
                .map(|t| t.text.len() + reply_text(t).len())
                .sum::<usize>()
                + selections[*i]
                    .iter()
                    .filter(|(_, m)| m.selected)
                    .map(|(e, _)| event_cost(e))
                    .sum::<usize>()
        })
        .sum();
    let fits = budget == 0 || raw <= (content_budget as f64 * 0.85) as usize;
    TextPlan {
        budget,
        blocks,
        n,
        collapse,
        full,
        full_count,
        heads,
        turns,
        floor,
        room,
        spare_share,
        selections,
        fits,
    }
}

struct RecordContext<'a> {
    b: &'a Block,
    turns: &'a [Turn<'a>],
    selections: &'a [(&'a Event, ToolMatch)],
    anchors: Vec<&'a Anchor>,
    cap: usize,
    center_cap: usize,
}
fn block_records<'a>(ctx: RecordContext<'a>) -> (Vec<Record>, Vec<&'a Event>) {
    let RecordContext {
        b,
        turns,
        selections,
        anchors,
        cap,
        center_cap,
    } = ctx;
    let mut by_turn: BTreeMap<i64, Vec<&(&Event, ToolMatch)>> = BTreeMap::new();
    for selection in selections {
        by_turn.entry(selection.0.turn).or_default().push(selection);
    }
    let target = &b.window.target;
    let center = b.window.center;
    let row_body = |text: &str, cap: usize, expand: &str| {
        let flat = evidence::one_line(text);
        let (body, _) = if anchors.is_empty() {
            evidence::cap(&flat, cap, expand)
        } else {
            evidence::anchored_cap(&flat, cap, expand, &anchors)
        };
        surface::safe(&body, false)
    };
    let mut records = Vec::new();
    let mut hidden: Vec<&Event> = Vec::new();
    let mut previous = None;
    for t in turns {
        if let Some(previous) = previous.filter(|p| t.turn > p + 1) {
            records.push(Record {
                line: gap_line(previous, t.turn),
                required: true,
                tool: false,
                drop_key: (3, 0, records.len()),
            });
        }
        previous = Some(t.turn);
        let hit_turn = b.hit_turns.contains(&t.turn);
        let tcap = if hit_turn { center_cap } else { cap };
        let distance = -(t.turn - center).abs();
        if !t.text.is_empty() {
            let body = row_body(
                t.text,
                tcap,
                &evidence::expand_command(target, t.turn, t.who),
            );
            records.push(Record {
                line: format!("  {:>4} {}: {body}", t.turn, surface::safe(t.who, false)),
                required: hit_turn,
                tool: false,
                drop_key: (2, distance, records.len()),
            });
        }
        for (e, m) in by_turn.get(&t.turn).into_iter().flatten() {
            if !m.selected {
                hidden.push(e);
                continue;
            }
            let line = format!(
                "     {}",
                evidence::tool_line(e, m.output_span, m.preview.as_deref())
            );
            records.push(Record {
                line: surface::safe(&line, false),
                required: true,
                tool: true,
                drop_key: (u8::from(e.ok != Some(true)), distance, records.len()),
            });
        }
        if let Some(reply) = t.reply.filter(|r| !r.text.is_empty()) {
            let mut body = row_body(
                &reply.text,
                tcap,
                &evidence::expand_command(target, t.turn, "agent"),
            );
            if reply.reply_truncated() {
                body += &format!(
                    " [source truncated at ingest; original reply {} chars]",
                    surface::number(reply.source_chars)
                );
            }
            records.push(Record {
                line: format!("  {:>4} agent: {body}", t.turn),
                required: hit_turn,
                tool: false,
                drop_key: (2, distance, records.len()),
            });
        }
    }
    (records, hidden)
}

type FirstBlock = (String, Vec<String>, String);
struct BlockContext<'a> {
    page: &'a Page<'a>,
    b: &'a Block,
    turns: &'a [Turn<'a>],
    selections: &'a [(&'a Event, ToolMatch)],
    head: &'a str,
    floor: usize,
    full_count: usize,
    spare_share: usize,
    shown_full: usize,
    fits: bool,
}
fn full_block(ctx: BlockContext<'_>) -> (String, Option<FirstBlock>) {
    let BlockContext {
        page,
        b,
        turns,
        selections,
        head,
        floor,
        full_count,
        spare_share,
        shown_full,
        fits,
    } = ctx;
    let budget = page.budget;
    let mut first_block = None;
    let share = if budget > 0 {
        floor
            + spare_share
                * if shown_full == 0 && full_count > 1 {
                    2
                } else {
                    1
                }
    } else {
        0
    };
    let cap = if fits { 0 } else { msg_cap(share, turns, 120) };
    let anchors = if fits {
        Vec::new()
    } else {
        page.block_anchors(b)
    };
    let head_line = format!("── {}", head);
    let rows_in = |hit: bool| {
        turns
            .iter()
            .filter(|t| b.hit_turns.contains(&t.turn) == hit)
            .map(|t| usize::from(!t.text.is_empty()) + usize::from(!reply_text(t).is_empty()))
            .sum::<usize>()
    };
    let (center_rows, context_rows) = (rows_in(true), rows_in(false));
    let mut center_cap = cap;
    if share > 0 && !fits && center_rows > 0 {
        let context_reserve = context_rows * (cap + 80);
        let tool_reserve = if selections.iter().any(|(_, m)| m.selected) {
            360
        } else {
            0
        };
        let center_room = (80 * center_rows as i64)
            .max(share as i64 - head_line.len() as i64 - context_reserve as i64 - tool_reserve);
        center_cap = (center_room as usize / center_rows)
            .saturating_sub(80)
            .clamp(80, 2400);
    }
    let (mut records, hidden) = block_records(RecordContext {
        b,
        turns,
        selections,
        anchors,
        cap,
        center_cap,
    });
    let markers = marker_counts(&hidden);
    if shown_full == 0 {
        first_block = Some((
            head_line.clone(),
            records
                .iter()
                .filter(|r| r.required)
                .map(|r| r.line.clone())
                .collect(),
            b.hit.handle.clone(),
        ));
    }
    let limit = if share > 0 && !fits {
        share.saturating_sub(4).max(80)
    } else {
        0
    };
    let block = fit_records(
        &head_line,
        &mut records,
        limit,
        &window_command(b, turns),
        hidden.len() - markers.iter().sum::<usize>(),
        &markers,
    );
    (block, first_block)
}

fn fit_first_block(
    mut out: Vec<String>,
    mut cmds: Vec<String>,
    mut trailer: String,
    first_block: Option<FirstBlock>,
    budget: usize,
    n: usize,
    used: usize,
) -> Result<(Vec<String>, Vec<String>, String)> {
    if let Some((head, rows, handle)) = first_block.filter(|_| budget > 0) {
        let content = budget - 1;
        let (over, bare, reserve) = if trailer.is_empty() {
            let marker = format!("\n[output truncated to --budget - rest at {handle}]");
            let over = used > content && content as i64 - marker.len() as i64 <= head.len() as i64;
            (over, String::new(), 0)
        } else {
            let bare = bare_trailer(n - 1);
            let reserve = bare.len() + 2;
            (used + 2 + trailer.len() > content, bare, reserve)
        };
        if over {
            let Some(tiny) = tiny_block(&head, &rows, content as i64 - reserve as i64) else {
                let need = head.len() + reserve + 1;
                let what = if bare.is_empty() {
                    "its hit block's head line"
                } else {
                    "its first hit block and the over-budget line"
                };
                bail!("--budget {budget} is too small for this page: {what} need at least {need} bytes");
            };
            (out, cmds, trailer) = (vec![tiny], vec![handle], bare);
        }
    }
    Ok((out, cmds, trailer))
}

#[cfg(test)]
mod tests {
    use super::*;
    // Expected values were produced by agrep 0.4.9's own `_over_budget_trailer`,
    // `_cap_line_bytes`, `_tiny_block` and `_fit_text_payload` (py/recall.py).
    #[test]
    fn trailers_line_caps_and_payload_cuts_match_0_4_9() {
        let handles: Vec<String> = ["@a:1.ffff", "@b:2.eeee", "@c:3.dddd"]
            .map(Into::into)
            .to_vec();
        let named = "[+3 hit(s) over budget - @a:1.ffff @b:2.eeee @c:3.dddd]";
        assert_eq!(over_budget_trailer(3, &handles, 0), named);
        assert_eq!(over_budget_trailer(3, &handles, 60), named);
        assert_eq!(
            over_budget_trailer(3, &handles, 30),
            "[+3 hit(s) over budget]"
        );
        let line =
            "    4 user: some words here [+1,234 chars - agrep around abc 4 -C 0 --max-chars 0]";
        let pointer = "[+1,234 chars - agrep around abc 4 -C 0 --max-chars 0]";
        assert_eq!(cap_line_bytes(line, 70), format!("    4 user:… {pointer}"));
        assert_eq!(cap_line_bytes(line, 40), pointer);
        assert_eq!(cap_line_bytes(line, 90), line);
        let head = "── @h:1.abcd · pi · x";
        let row = "    1 user: hello there [+5 chars - agrep around h 1 -C 0 --max-chars 0]";
        assert_eq!(tiny_block(head, &[row.into()], 40).as_deref(), Some(head));
        assert_eq!(
            tiny_block(head, &["    1 user: hello there".into()], 26),
            None
        );
        let blocks = [
            format!("── @a:1.abcd\n    1 user: {}", "alpha ".repeat(30)),
            format!("── @b:2.abcd\n    2 user: {}", "beta ".repeat(30)),
        ];
        let text = blocks.join("\n\n");
        let ends = vec![
            (blocks[0].len(), "@a:1.abcd".to_owned()),
            (
                blocks[0].len() + 2 + blocks[1].len(),
                "@b:2.abcd".to_owned(),
            ),
        ];
        let over = "[+1 hit(s) over budget - @c:3.abcd]";
        assert_eq!(
            fit_text(&text, 250, &ends, "", Some(150)),
            format!("── @a:1.abcd\n    1 user: {}a\n[output truncated to --budget - rest at @a:1.abcd]\n\n[+1 hit(s) over budget - @b:2.abcd]", "alpha ".repeat(22))
        );
        assert_eq!(
            fit_text(&text, 120, &ends, over, Some(90)),
            "── @a:1.abcd\n    1 user: alpha alpha alp\n[output truncated to --budget - rest at @a:1.abcd]\n\n[+2 hit(s) over budget]"
        );
        assert_eq!(
            fit_text(&text, 70, &ends, over, Some(50)),
            "[+3 hit(s) over budget - @a:1.abcd @b:2.abcd @c:3.abcd]"
        );
    }
}
