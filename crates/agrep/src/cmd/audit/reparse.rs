//! The full audit's reparse of every selected source, source by source: each parse is
//! reduced to its row digests as it completes, so memory holds digests and a bounded
//! window of parses rather than a whole history's texts.
use super::tally::{text_digest, SourceRows, Tally};
use agrep_core::{
    data::DataRoot,
    ingest::registry::ADAPTERS,
    intake_batch::{IntakeBatch, Pending, ReadOutcome},
    model::{Event, Message},
    row_class::{normalize_messages, HarnessPolicy},
    store,
};
use anyhow::Result;
use serde_json::{json, Value};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    panic::AssertUnwindSafe,
    sync::mpsc,
};

/// Source bytes admitted to parse at once; a larger source parses alone. A parse holds
/// less memory than its source's size.
const PARSE_BYTES: u64 = 512 << 20;

pub(super) fn reparse(
    root: &DataRoot,
    live: &[store::Stamp],
    selected: &impl Fn(&str) -> bool,
    errors: &mut Vec<Value>,
    tally: &mut Tally,
) -> Result<()> {
    let policy = HarnessPolicy::parse(
        &std::fs::read_to_string(root.0.join("harness_prefixes.txt")).unwrap_or_default(),
    );
    let sizes: HashMap<&str, u64> = live.iter().map(|s| (s.path.as_str(), s.size)).collect();
    for adapter in ADAPTERS.iter().filter(|a| selected(a.name())) {
        let paths: Vec<std::path::PathBuf> = live
            .iter()
            .filter(|s| s.agent == adapter.name())
            .map(|s| s.path.clone().into())
            .collect();
        let mut batch = IntakeBatch {
            changed: paths.iter().cloned().collect(),
            source_paths: paths,
            force: true,
            defer: true,
            ..Default::default()
        };
        let (messages, events, outcome, issues) = adapter.collect_checked(&mut batch);
        tally.add_source(source_rows(messages, events, &policy));
        for source in std::mem::take(&mut batch.batches) {
            if let Some(error) = source.error {
                errors.push(json!({"path":source.key,"reason":error}));
            }
            tally.add_source(source_rows(source.messages, source.events, &policy));
        }
        let pending = std::mem::take(&mut batch.pending);
        for (path, error) in parse_each(pending, &sizes, &policy, |rows| tally.add_source(rows)) {
            if let Some(error) = error {
                errors.push(json!({"path":path,"reason":error}));
            }
        }
        for issue in issues.into_iter().chain(batch.issues) {
            errors.push(json!({"path":issue.path,"reason":issue.reason}));
        }
        if !matches!(outcome, ReadOutcome::Complete | ReadOutcome::Undone) && errors.is_empty() {
            errors
                .push(json!({"agent":adapter.name(),"reason":format!("source read {outcome:?}")}));
        }
    }
    Ok(())
}

/// A finished parse: its place in the source order, the bytes it held, and its key,
/// error and rows (or the panic that ended it).
type Parsed = (
    usize,
    u64,
    std::thread::Result<(String, Option<String>, SourceRows)>,
);

/// Parses `pending` on the rayon pool within `PARSE_BYTES`, admitting every waiting
/// source that fits, in source order, so a large source waiting for room holds back no
/// smaller one behind it. Hands each source's rows to `fold` as it completes and returns
/// each source's key and error in source order, so the result does not depend on the
/// admission order. The parses never block, since the pool also runs the parsers' own
/// parallel work.
fn parse_each(
    pending: Vec<Pending>,
    sizes: &HashMap<&str, u64>,
    policy: &HarnessPolicy,
    mut fold: impl FnMut(SourceRows),
) -> Vec<(String, Option<String>)> {
    let mut outcomes = vec![(String::new(), None); pending.len()];
    let mut finish = |(at, bytes, parsed): Parsed| {
        let (key, error, rows) = parsed.unwrap_or_else(|panic| std::panic::resume_unwind(panic));
        outcomes[at] = (key, error);
        fold(rows);
        bytes
    };
    let mut waiting: VecDeque<_> = pending
        .into_iter()
        .enumerate()
        .map(|(at, source)| {
            let size = sizes.get(source.path.to_string_lossy().as_ref());
            (at, source, size.copied().unwrap_or(0).min(PARSE_BYTES))
        })
        .collect();
    // Admitted sources wait in the pool's queue holding their bytes, so admit only
    // enough to keep the pool busy.
    let most = 2 * rayon::current_num_threads();
    let (send, parsed) = mpsc::channel::<Parsed>();
    rayon::in_place_scope(|scope| {
        let (mut free, mut running) = (PARSE_BYTES, 0);
        while !waiting.is_empty() || running > 0 {
            let mut next = 0;
            while next < waiting.len() && running < most {
                if waiting[next].2 > free {
                    next += 1;
                    continue;
                }
                let (at, source, bytes) = waiting.remove(next).expect("a waiting source");
                free -= bytes;
                running += 1;
                let send = send.clone();
                scope.spawn(move |_| {
                    let parsed = std::panic::catch_unwind(AssertUnwindSafe(|| {
                        let source = source.parse();
                        let rows = source_rows(source.messages, source.events, policy);
                        (source.key, source.error, rows)
                    }));
                    let _ = send.send((at, bytes, parsed));
                });
            }
            if running > 0 {
                free += finish(parsed.recv().expect("an admitted parse sends its rows"));
                running -= 1;
            }
        }
    });
    outcomes
}

fn source_rows(messages: Vec<Message>, events: Vec<Event>, policy: &HarnessPolicy) -> SourceRows {
    let mut rows = SourceRows::new();
    let mut add = |agent: &'static str, session: &str, who: &str, text: &str| {
        *rows
            .entry((agent, session.into()))
            .or_default()
            .entry((who.into(), text_digest(text)))
            .or_default() += 1;
    };
    for m in normalize_messages(messages, policy) {
        for (who, text) in [
            (m.who.as_ref(), m.text.as_ref()),
            ("agent", m.reply.as_ref()),
        ] {
            if !text.is_empty() {
                add(m.agent, &m.session, who, text);
            }
        }
    }
    let mut seen_events = HashSet::new();
    for e in events {
        if e.kind == "control"
            || !seen_events.insert((e.agent, e.session.clone(), e.call_id.clone()))
        {
            continue;
        }
        let mut head = match e.kind {
            "subagent_start" => format!("subagent {}", e.name.trim()),
            "subagent_result" => format!("subagent result {}", e.name.trim()),
            _ => e.name.trim().into(),
        };
        if e.ok == Some(false) {
            head.push_str(" [failed]");
        }
        let mut text = if head.is_empty() {
            e.input.trim().to_string()
        } else if e.input.trim().is_empty() {
            head
        } else {
            format!("{head}: {}", e.input.trim())
        };
        if !e.output.is_empty() {
            text.push('\n');
            text.push_str(&e.output);
        }
        add(e.agent, &e.session, "tool", text.trim());
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use agrep_core::{intake_batch::collect_cached_for, model::RawMessage};
    use std::{
        path::PathBuf,
        sync::{
            atomic::{AtomicUsize, Ordering::SeqCst},
            Arc,
        },
    };

    #[test]
    fn sources_filling_the_budget_parse_one_at_a_time_in_order() {
        let names: Vec<_> = (0..24).map(|i| format!("source-{i}")).collect();
        let paths: Vec<_> = names.iter().map(PathBuf::from).collect();
        let (running, most) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicUsize::new(0)));
        let mut batch = IntakeBatch {
            changed: paths.iter().cloned().collect(),
            defer: true,
            ..Default::default()
        };
        let (now, peak) = (running.clone(), most.clone());
        collect_cached_for(&mut batch, "test", &paths, move |path| {
            peak.fetch_max(now.fetch_add(1, SeqCst) + 1, SeqCst);
            // A window in which a second admitted parse would be seen running.
            for _ in 0..200 {
                std::thread::yield_now();
            }
            now.fetch_sub(1, SeqCst);
            let message = RawMessage {
                agent: "test",
                project: String::new(),
                session: path.to_string_lossy().into(),
                ts: 0,
                turn: 0,
                text: "x".into(),
                model: String::new(),
                reply: String::new(),
                reply_chars: 0,
                reply_shape: Default::default(),
                side: false,
                parent: String::new(),
            };
            (vec![message.freeze()], Vec::new())
        });
        let sizes = names.iter().map(|n| (n.as_str(), PARSE_BYTES)).collect();
        let mut chats = 0;
        let outcomes = parse_each(batch.pending, &sizes, &HarnessPolicy::parse(""), |rows| {
            chats += rows.len()
        });
        assert_eq!(most.load(SeqCst), 1);
        assert_eq!(chats, names.len());
        let keys: Vec<_> = outcomes.into_iter().map(|(key, _)| key).collect();
        assert_eq!(keys, names);
    }
}
