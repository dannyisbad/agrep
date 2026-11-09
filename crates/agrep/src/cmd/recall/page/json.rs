use super::*;
/// The compact JSON size of `value`.
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
    json_output::to_writer(&mut count, value)?;
    Ok(count.0)
}
/// A window row as fitted: its value, compact size and whether it must stay.
struct JsonRow {
    value: Value,
    size: usize,
    required: bool,
}
/// A hit as fitted: the hit object with an empty window, its size, and its rows.
struct JsonHit<'a> {
    frame: Value,
    frame_size: usize,
    rows: Vec<JsonRow>,
    target: &'a str,
    anchors: Vec<&'a Anchor>,
}
impl JsonHit<'_> {
    fn size(&self) -> usize {
        self.frame_size
            + self.rows.iter().map(|r| r.size).sum::<usize>()
            + self.rows.len().saturating_sub(1)
    }
}
/// A tool row's identity as 0.4.9 keyed it: kind, session, turn, timestamp, name.
fn tool_key(row: &Value) -> [Value; 5] {
    ["kind", "session", "turn", "ts", "name"]
        .map(|key| row.get(key).cloned().unwrap_or(Value::Null))
}
/// The JSON page within `page.budget` bytes (zero is unbounded): 0.4.9 capped messages by
/// share when the page's text exceeded 85% of the budget, kept only the selected event of
/// a tool hit, counted omitted events, then shrank, dropped and omitted until it fit.
pub(in crate::cmd::recall) fn json(page: &Page, envelope: &mut Value) -> Result<String> {
    let budget = page.budget;
    let blocks = page.blocks;
    let n = blocks.len();
    let room = if budget > 0 {
        (budget / JUDGEABLE_BLOCK).max(1)
    } else {
        n
    };
    let share = if budget > 0 {
        budget / n.min(room).max(1)
    } else {
        0
    };
    let turns: Vec<Vec<Turn>> = blocks.iter().map(|b| turns(&b.window.rows)).collect();
    let raw: usize = turns
        .iter()
        .flatten()
        .map(|t| t.text.len() + reply_text(t).len())
        .sum();
    let fits = budget == 0 || raw <= (budget as f64 * 0.85) as usize;
    let mut hits = Vec::new();
    for (b, turns) in blocks.iter().zip(&turns) {
        let w = &b.window;
        let claim = tool_claim(b);
        let cap = if fits { 0 } else { msg_cap(share, turns, 260) };
        let anchors = if fits {
            Vec::new()
        } else {
            page.block_anchors(b)
        };
        let mut events: BTreeMap<i64, Vec<&Event>> = BTreeMap::new();
        for e in &w.events {
            events.entry(e.turn).or_default().push(e);
        }
        let mut rows = Vec::new();
        let (mut size, mut cut) = (0, 0);
        let mut required_tools = HashSet::new();
        for t in turns {
            let hit_turn = b.hit_turns.contains(&t.turn);
            let tcap = if fits {
                0
            } else if hit_turn {
                cap.max(2400.min(share))
            } else {
                cap
            };
            for (who, text, reply) in [(t.who, t.text, None), ("agent", reply_text(t), t.reply)] {
                if text.is_empty() {
                    continue;
                }
                let expand = evidence::expand_command(&w.target, t.turn, who);
                let (capped, omitted) = if !anchors.is_empty() && hit_turn {
                    evidence::anchored_cap(text, tcap, &expand, &anchors)
                } else {
                    evidence::cap(text, tcap, &expand)
                };
                size += capped.len() + 140;
                let mut row = json!({"kind":"msg","session":w.session,"project":w.project,"turn":t.turn,"who":who,"ts":t.ts,"text":capped,"omitted_chars":omitted});
                if let Some(reply) = reply {
                    row["reply_chars"] = json!(reply.source_chars);
                    row["reply_truncated"] = json!(reply.reply_truncated());
                }
                rows.push(row);
            }
            for e in events.get(&t.turn).into_iter().flatten() {
                let m = evidence::selected_tool_match(e, claim.0.as_deref(), claim.1);
                if !m.selected && (claim.0.is_some() || (share > 0 && size >= share)) {
                    cut += 1;
                    continue;
                }
                let mut input = e.input.clone();
                if input.chars().count() > 120 {
                    input = input.chars().take(120).collect::<String>() + "…";
                }
                size += input.len() + 160;
                let mut row = json!({"kind":e.kind,"session":w.session,"project":w.project,"turn":e.turn,"ts":e.ts,"name":e.name,"input":input,"ok":e.ok,"input_chars":e.input_chars,"output_chars":e.output_chars,"output_bytes":e.output_bytes,"input_truncated":e.input_truncated(),"output_truncated":e.output_truncated()});
                if m.selected {
                    if let Some(preview) = m.preview.filter(|p| !p.is_empty()) {
                        row["match_preview"] = json!(preview);
                    }
                    required_tools.insert(tool_key(&row));
                }
                rows.push(row);
            }
        }
        if cut > 0 {
            rows.push(json!({"kind":"events_omitted","session":w.session,"project":w.project,"n":cut,"expand":window_command(b, turns)}));
        }
        let h = &b.hit;
        // 0.4.9 labeled a meaning hit's lane and cosine, rounded to six places, and scored
        // it with the keyword formula (py/recall.py:2373, 2394; py/ask.py:2491;
        // py/search.py:1938-1942).
        let meaning = h.matched == "semantic";
        let score = if meaning {
            agrep_core::query::meaning_score(h, agrep_core::clock::now_ms())
        } else {
            h.score
        };
        let mut frame = json!({"session":w.session,"turn":w.center,"ts":h.ts,"agent":w.agent,"project":w.project,"score":score,"sem_score":meaning.then(|| agrep_core::window::round6(h.score)),"score_kind":meaning.then_some("cosine"),"matched":h.matched,"handle":h.handle,"self":h.self_marker,"window":[]});
        if meaning {
            frame["lane"] = json!("semantic");
        }
        let rows = rows
            .into_iter()
            .map(|value| {
                let required = (value["kind"] == "msg" && value["turn"] == json!(w.center))
                    || required_tools.contains(&tool_key(&value));
                Ok(JsonRow {
                    size: json_len(&value)?,
                    value,
                    required,
                })
            })
            .collect::<Result<_>>()?;
        hits.push(JsonHit {
            frame_size: json_len(&frame)?,
            frame,
            rows,
            target: &w.target,
            anchors: page.block_anchors(b),
        });
    }
    let fitted = fit_json(envelope, hits, budget.saturating_sub(1), page)?;
    Ok(written(fitted, budget))
}
fn assemble(envelope: &mut Value, hits: Vec<JsonHit>) -> Result<String> {
    envelope["hits"] = Value::Array(
        hits.into_iter()
            .map(|h| {
                let mut frame = h.frame;
                frame["window"] = Value::Array(h.rows.into_iter().map(|r| r.value).collect());
                frame
            })
            .collect(),
    );
    Ok(json_output::to_string(envelope)?)
}
/// 0.4.9's `_shrink_row`: a message row's text halved to `cap`'s marker, sliding to its
/// anchors when halving would drop the match; false when the marker eats the savings.
fn shrink_row(row: &mut Value, target: &str, anchors: &[&Anchor]) -> bool {
    let old = row["text"].as_str().unwrap_or_default().to_owned();
    let body = CAP_MARKER.replace(&old, "").into_owned();
    let slid = SLID_HEAD
        .find(&body)
        .map(|m| body[..m.end()].chars().count());
    let length = body.chars().count();
    let keep = slid
        .map_or(16, |head| head + 192)
        .max(((length as i64 - 1).div_euclid(2)).max(0) as usize);
    let turn = row["turn"].as_i64().unwrap_or_default();
    let expand = evidence::expand_command(target, turn, row["who"].as_str().unwrap_or("user"));
    let prior = row["omitted_chars"].as_u64().unwrap_or_default() as usize;
    let (new, omitted) = if !anchors.is_empty() && slid.is_none() {
        let (new_body, cut) = evidence::anchored_cap(&body, keep, &expand, anchors);
        let omitted = prior + cut;
        let new = if cut > 0 {
            carry_omission(&new_body, omitted, prior, &expand)
        } else {
            new_body
        };
        (new, omitted)
    } else {
        let end = body
            .char_indices()
            .nth(keep)
            .map_or(body.len(), |(at, _)| at);
        let mut cut = &body[..end];
        let window = cut.char_indices().rev().nth(199).map_or(0, |(at, _)| at);
        if cut[window..].contains(' ') {
            cut = &cut[..cut.rfind(' ').expect("a space was found")];
        }
        let omitted = prior + length - cut.chars().count();
        (
            format!("{cut} [+{} chars - {expand}]", surface::number(omitted)),
            omitted,
        )
    };
    if new.chars().count() as i64 > old.chars().count() as i64 - 8 {
        return false;
    }
    row["text"] = json!(new);
    row["omitted_chars"] = json!(omitted);
    true
}
/// 0.4.9's `_carry_omission`: a re-shrunk row's marker counts every character it no
/// longer shows, the earlier cut included.
fn carry_omission(text: &str, omitted: usize, prior: usize, expand: &str) -> String {
    if CAP_MARKER.is_match(text) {
        return format!(
            "{} [+{} chars - {expand}]",
            CAP_MARKER.replace(text, ""),
            surface::number(omitted)
        );
    }
    if prior == 0 {
        return text.to_owned();
    }
    match SNIPPET_TAIL.captures(text) {
        None => format!("{text} [+{} chars - {expand}]", surface::number(omitted)),
        Some(tail) => {
            let gap = tail
                .get(1)
                .map_or(0, |g| g.as_str().replace(',', "").parse().unwrap_or(0))
                + prior;
            format!(
                "{} [{} chars] [snippet - open: {expand}]",
                &text[..tail.get(0).expect("whole match").start()],
                surface::number(gap)
            )
        }
    }
}
/// 0.4.9's `_fit_json_payload`: the envelope and its hits within `budget` bytes, or the
/// envelope's state alone when no hit fits.
fn fit_json<'a>(
    envelope: &mut Value,
    mut hits: Vec<JsonHit<'a>>,
    budget: usize,
    page: &'a Page,
) -> Result<String> {
    let total_of = |envelope_size: usize, hits: &[JsonHit]| {
        envelope_size + hits.iter().map(JsonHit::size).sum::<usize>() + hits.len().saturating_sub(1)
    };
    envelope["hits"] = json!([]);
    let mut envelope_size = json_len(envelope)?;
    if budget == 0 || total_of(envelope_size, &hits) <= budget {
        return assemble(envelope, hits);
    }
    let original = hits.len();
    envelope["truncated"] = json!(true);
    envelope["omitted_hits"] = json!(0);
    match &mut envelope["query"] {
        Value::String(q) if q.chars().count() > 256 => {
            *q = q.chars().take(240).collect::<String>() + "…";
        }
        Value::Array(items) => {
            items.truncate(8);
            for item in items.iter_mut() {
                let text = item.as_str().unwrap_or_default();
                if text.chars().count() > 120 {
                    *item = json!(text.chars().take(120).collect::<String>() + "…");
                }
            }
        }
        _ => {}
    }
    envelope_size = json_len(envelope)?;
    // Query anchors after each hit's own: exact phrases outrank term fallbacks.
    let named: Vec<&Vec<Anchor>> = page
        .queries
        .iter()
        .zip(&page.anchors)
        .filter(|(q, _)| !q.is_empty() && !q.starts_with('@'))
        .map(|(_, anchors)| anchors)
        .collect();
    let query_anchors: Vec<&Anchor> = named
        .iter()
        .flat_map(|a| a.iter().take(1))
        .chain(named.iter().flat_map(|a| a.iter().skip(1)))
        .collect();
    for h in &mut hits {
        h.anchors.extend(query_anchors.iter().copied());
    }
    let mut total = total_of(envelope_size, &hits);
    // Long message bodies are the normal overflow: halve the longest that can still shrink.
    // A row that cannot shrink never can again, since only shrinking changes it.
    let mut longest: BinaryHeap<(usize, Reverse<usize>, Reverse<usize>)> = BinaryHeap::new();
    for (hi, h) in hits.iter().enumerate() {
        for (ri, row) in h.rows.iter().enumerate() {
            if let Some(text) = row.value["text"]
                .as_str()
                .filter(|_| row.value["kind"] == "msg")
            {
                let chars = text.chars().count();
                if chars > 48 {
                    longest.push((chars, Reverse(hi), Reverse(ri)));
                }
            }
        }
    }
    while total > budget {
        let Some((_, Reverse(hi), Reverse(ri))) = longest.pop() else {
            break;
        };
        let h = &mut hits[hi];
        if !shrink_row(&mut h.rows[ri].value, h.target, &h.anchors) {
            continue;
        }
        let row = &mut h.rows[ri];
        let size = json_len(&row.value)?;
        total = total + size - row.size;
        row.size = size;
        let chars = row.value["text"]
            .as_str()
            .unwrap_or_default()
            .chars()
            .count();
        if chars > 48 {
            longest.push((chars, Reverse(hi), Reverse(ri)));
        }
    }
    // Optional narration goes before a hit's selected exchange or tool event.
    while total > budget {
        let Some((hi, ri)) = hits
            .iter()
            .enumerate()
            .rev()
            .find_map(|(hi, h)| h.rows.iter().rposition(|r| !r.required).map(|ri| (hi, ri)))
        else {
            break;
        };
        let h = &mut hits[hi];
        let before = h.size();
        h.rows.remove(ri);
        if h.frame.get("window_truncated").is_none() {
            h.frame["window_truncated"] = json!(true);
            h.frame_size = json_len(&h.frame)?;
        }
        total = total - before + h.size();
    }
    while total > budget && !hits.is_empty() {
        hits.pop();
        envelope["omitted_hits"] = json!(original - hits.len());
        envelope_size = json_len(envelope)?;
        total = total_of(envelope_size, &hits);
    }
    if total <= budget {
        return assemble(envelope, hits);
    }
    Ok(json_output::to_string(&minimal(envelope, original))?)
}
/// What a page too small for any hit keeps of its envelope.
fn minimal(out: &Value, original: usize) -> Value {
    let fields = |name: &str, keys: &[&str]| {
        let Some(map) = out.get(name).and_then(Value::as_object) else {
            return Value::Null;
        };
        let mut picked = Map::new();
        for key in keys {
            if let Some(item) = map.get(*key) {
                let item = match item {
                    Value::String(s) => json!(surface::prefix(s, 96)),
                    other => other.clone(),
                };
                picked.insert((*key).into(), item);
            }
        }
        Value::Object(picked)
    };
    let query = match out.get("query") {
        Some(Value::Array(items)) => json!(items
            .iter()
            .take(4)
            .map(|item| surface::prefix(item.as_str().unwrap_or_default(), 48))
            .collect::<Vec<_>>()),
        other => json!(surface::prefix(
            other.and_then(Value::as_str).unwrap_or_default(),
            96
        )),
    };
    let mut minimal = json!({
        "query": query,
        "engine": surface::prefix(out["engine"].as_str().unwrap_or_default(), 64),
        "hits": [],
        "error": fields("error", &["code", "reason"]),
        "self_exclusion": fields("self_exclusion", &["active", "reason", "scope", "session", "excluded_hits", "excluded_hits_known", "postfilter_excluded_hits", "from_turn"]),
        "freshness": fields("freshness", &["state", "failing", "checked", "may_be_stale", "code", "reason", "consecutive_failures"]),
        "semantic_coverage": fields("semantic_coverage", &["indexed", "total", "pending", "fraction", "complete", "order"]),
        "truncated": true,
        "omitted_hits": original,
    });
    if out.get("tools_excluded").is_some_and(Value::is_object) {
        minimal["tools_excluded"] = fields("tools_excluded", &["reason"]);
    }
    minimal
}
