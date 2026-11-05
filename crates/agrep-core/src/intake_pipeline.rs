use crate::{intake_batch::SourceBatch, tool_store};
use anyhow::Result;
use rayon::prelude::*;
use std::{
    collections::HashMap,
    mem::size_of,
    sync::{mpsc, Arc, Condvar, Mutex},
    time::{Duration, Instant},
};

pub(crate) type Parsed = (SourceBatch, Vec<Option<tool_store::Prepared>>);
const AHEAD_BYTES: u64 = 512 << 20;
const MIN_SOURCE: u64 = 64 << 10;

#[derive(Default)]
struct State {
    used: u64,
    closed: bool,
}
struct Budget {
    limit: u64,
    state: Mutex<State>,
    changed: Condvar,
}
impl Budget {
    fn new(limit: u64) -> Self {
        Self {
            limit,
            state: Mutex::new(State::default()),
            changed: Condvar::new(),
        }
    }
    fn reserve(self: &Arc<Self>, bytes: u64) -> Option<Reservation> {
        let mut state = self.state.lock().unwrap();
        while !state.closed && bytes > self.limit - state.used {
            state = self.changed.wait(state).unwrap();
        }
        if state.closed {
            return None;
        }
        state.used += bytes;
        Some(Reservation {
            budget: self.clone(),
            bytes,
        })
    }
    fn release(&self, bytes: u64) {
        let mut state = self.state.lock().unwrap();
        state.used -= bytes;
        self.changed.notify_one();
    }
    fn close(&self) {
        self.state.lock().unwrap().closed = true;
        self.changed.notify_all();
    }
}
struct Reservation {
    budget: Arc<Budget>,
    bytes: u64,
}
impl Reservation {
    fn retain(&mut self, bytes: u64) {
        let bytes = bytes.clamp(MIN_SOURCE, self.bytes);
        self.budget.release(self.bytes - bytes);
        self.bytes = bytes;
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        self.budget.release(self.bytes);
    }
}
struct Close(Arc<Budget>);
impl Drop for Close {
    fn drop(&mut self) {
        self.0.close();
    }
}

fn json_bytes(value: &serde_json::Value, limit: usize) -> usize {
    let mut bytes = match value {
        serde_json::Value::String(text) => return text.capacity().min(limit),
        serde_json::Value::Array(values) => values.capacity() * size_of::<serde_json::Value>(),
        serde_json::Value::Object(values) => {
            values.len() * (size_of::<String>() + size_of::<serde_json::Value>() + 32)
        }
        _ => return 0,
    };
    if bytes >= limit {
        return limit;
    }
    match value {
        serde_json::Value::Array(values) => {
            for value in values {
                bytes += json_bytes(value, limit - bytes);
                if bytes >= limit {
                    break;
                }
            }
        }
        serde_json::Value::Object(values) => {
            for (key, value) in values {
                bytes += key.capacity().min(limit - bytes);
                bytes += json_bytes(value, limit - bytes);
                if bytes >= limit {
                    break;
                }
            }
        }
        _ => unreachable!(),
    }
    bytes
}

fn retained_bytes((source, prepared): &Parsed, input: u64) -> u64 {
    let limit = (input * 2).div_ceil(3) as usize;
    let mut bytes = size_of::<SourceBatch>() + source.key.capacity() + source.agent.capacity();
    bytes += source.error.as_ref().map_or(0, String::capacity);
    bytes += source.cursor.as_ref().map_or(0, Vec::capacity);
    bytes += source.messages.capacity() * size_of::<crate::model::Message>();
    if bytes >= limit {
        return input;
    }
    for message in &source.messages {
        for text in [
            &message.project,
            &message.session,
            &message.text,
            &message.who,
            &message.model,
            &message.model_source,
            &message.reply,
            &message.parent,
        ] {
            bytes += text.len() + 2 * size_of::<usize>();
        }
        let shape = &message.reply_shape;
        bytes += size_of::<crate::model::ReplyShape>()
            + 2 * size_of::<usize>()
            + shape.breaks.capacity() * size_of::<u32>()
            + shape.worked.capacity()
            + shape.end.capacity();
    }
    if bytes >= limit {
        return input;
    }
    bytes += source.events.capacity() * size_of::<crate::model::Event>();
    if bytes >= limit {
        return input;
    }
    for event in &source.events {
        bytes += [
            &event.session,
            &event.name,
            &event.input,
            &event.output,
            &event.call_id,
            &event.child_session,
            &event.meta,
            &event.state,
        ]
        .into_iter()
        .map(String::capacity)
        .sum::<usize>();
    }
    if bytes >= limit {
        return input;
    }
    bytes += source.nodes.capacity() * size_of::<crate::incremental::Node>();
    if bytes >= limit {
        return input;
    }
    for node in &source.nodes {
        bytes += node.id.capacity()
            + node.parent.as_ref().map_or(0, String::capacity)
            + json_bytes(&node.body, limit.saturating_sub(bytes));
        if bytes >= limit {
            return input;
        }
    }
    if bytes >= limit {
        return input;
    }
    bytes += prepared.capacity() * size_of::<Option<tool_store::Prepared>>();
    if bytes >= limit {
        return input;
    }
    for event in prepared.iter().flatten() {
        bytes += match &event.text {
            tool_store::Text::Plain(text) => text.len(),
            tool_store::Text::Encoded { body, .. } => body.capacity(),
        };
    }
    // Count shared Arcs repeatedly and allow for allocator/map slack before returning credit.
    (bytes as u64).saturating_mul(3).div_ceil(2).min(input)
}

/// Keep active input and completed output inside one byte window, through the write.
/// A feeder can reuse compaction credit while the writer is occupied with earlier rows.
pub(crate) fn parse_ahead(
    parsers: usize,
    pending: Vec<crate::intake_batch::Pending>,
    sizes: &HashMap<&str, u64>,
    write: impl FnMut(Parsed) -> Result<()>,
) -> Result<Duration> {
    run(
        parsers,
        pending,
        sizes,
        write,
        Arc::new(Budget::new(AHEAD_BYTES)),
    )
}
fn run(
    parsers: usize,
    pending: Vec<crate::intake_batch::Pending>,
    sizes: &HashMap<&str, u64>,
    mut write: impl FnMut(Parsed) -> Result<()>,
    budget: Arc<Budget>,
) -> Result<Duration> {
    if pending.is_empty() {
        return Ok(Duration::ZERO);
    }
    let parsers = parsers.clamp(1, pending.len());
    type Job = (
        crate::intake_batch::Pending,
        Reservation,
        mpsc::SyncSender<(Parsed, Reservation)>,
    );
    let (queue, jobs) = mpsc::channel::<Job>();
    let jobs = Mutex::new(jobs);
    let (ordered, results) = mpsc::channel();
    std::thread::scope(|scope| {
        // Close before scoped threads are joined, including a writer error or panic.
        let _close = Close(budget.clone());
        scope.spawn({
            let budget = budget.clone();
            move || {
                for source in pending {
                    let size = sizes
                        .get(source.path.to_string_lossy().as_ref())
                        .copied()
                        .unwrap_or(0);
                    let Some(reservation) = budget.reserve(size.clamp(MIN_SOURCE, budget.limit))
                    else {
                        break;
                    };
                    let (send, receive) = mpsc::sync_channel(1);
                    if ordered.send(receive).is_err()
                        || queue.send((source, reservation, send)).is_err()
                    {
                        break;
                    }
                }
            }
        });
        for _ in 0..parsers {
            let jobs = &jobs;
            scope.spawn(move || loop {
                let Ok(Ok((source, mut reservation, send))) = jobs.lock().map(|jobs| jobs.recv())
                else {
                    break;
                };
                let source = source.parse();
                let prepared = source
                    .events
                    .par_iter()
                    .map(|event| {
                        (source.error.is_none() && event.kind != "control")
                            .then(|| tool_store::prepare(event).ok())
                            .flatten()
                    })
                    .collect();
                let parsed = (source, prepared);
                let input = reservation.bytes;
                let retained = retained_bytes(&parsed, input);
                reservation.retain(retained);
                if crate::semantic_profile::timing_enabled() && input >= 64 << 20 {
                    crate::semantic_profile::event(serde_json::json!({"parse_credit": {
                        "input_bytes": input, "retained_limit_bytes": retained,
                        "charged_bytes": reservation.bytes, "parse_ms": parsed.0.parse_ms
                    }}));
                }
                let _ = send.send((parsed, reservation));
            });
        }
        let mut waited = Duration::ZERO;
        loop {
            let tick = Instant::now();
            let Ok(receive) = results.recv() else { break };
            let (parsed, reservation) = receive
                .recv()
                .map_err(|_| anyhow::anyhow!("source parse ended"))?;
            let elapsed = tick.elapsed();
            waited += elapsed;
            if crate::semantic_profile::timing_enabled() && elapsed.as_millis() >= 100 {
                crate::semantic_profile::event(serde_json::json!({"parse_wait": {
                    "input_bytes": parsed.0.bytes_read, "wait_ms": elapsed.as_secs_f64() * 1000.,
                    "parse_ms": parsed.0.parse_ms, "charged_bytes": reservation.bytes
                }}));
            }
            write(parsed)?;
            drop(reservation);
        }
        Ok(waited)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::intake_batch::{collect_cached_for, IntakeBatch};
    use std::{
        path::{Path, PathBuf},
        sync::atomic::{AtomicUsize, Ordering},
    };

    fn sources(
        parse: impl Fn(&Path) -> (Vec<crate::model::Message>, Vec<crate::model::Event>)
            + Send
            + Sync
            + 'static,
    ) -> (Vec<crate::intake_batch::Pending>, Vec<String>) {
        let names: Vec<_> = (0..8).map(|i| format!("source-{i}")).collect();
        let paths: Vec<_> = names.iter().map(PathBuf::from).collect();
        let mut batch = IntakeBatch {
            changed: paths.iter().cloned().collect(),
            defer: true,
            ..Default::default()
        };
        collect_cached_for(&mut batch, "test", &paths, parse);
        (batch.pending, names)
    }

    #[test]
    fn compacted_sources_admit_more_work_before_the_writer_finishes() {
        let (started, second) = mpsc::channel();
        let (pending, names) = sources(move |path| {
            if path == Path::new("source-1") {
                started.send(()).unwrap();
            }
            (Vec::new(), Vec::new())
        });
        let sizes = names
            .iter()
            .map(|name| (name.as_str(), 640 << 10))
            .collect();
        let budget = Arc::new(Budget::new(1 << 20));
        let mut written = Vec::new();
        run(
            8,
            pending,
            &sizes,
            |(source, _)| {
                if written.is_empty() {
                    second.recv_timeout(Duration::from_secs(30)).unwrap();
                }
                assert!(budget.state.lock().unwrap().used <= budget.limit);
                written.push(source.key);
                Ok(())
            },
            budget.clone(),
        )
        .unwrap();
        assert_eq!(written, names);
        assert_eq!(budget.state.lock().unwrap().used, 0);
    }

    fn message() -> crate::model::Message {
        crate::model::RawMessage {
            agent: "test",
            project: String::new(),
            session: "test".into(),
            ts: 0,
            turn: 0,
            text: "x".repeat(512 << 10),
            model: String::new(),
            reply: String::new(),
            reply_chars: 0,
            reply_shape: Default::default(),
            side: false,
            parent: String::new(),
        }
        .freeze()
    }

    #[test]
    fn retained_rows_stay_reserved_through_the_write() {
        let live = Arc::new(AtomicUsize::new(0));
        let parsing = live.clone();
        let (pending, names) = sources(move |_| {
            assert_eq!(parsing.fetch_add(1, Ordering::SeqCst), 0);
            (vec![message()], Vec::new())
        });
        let sizes = names
            .iter()
            .map(|name| (name.as_str(), 640 << 10))
            .collect();
        let budget = Arc::new(Budget::new(1 << 20));
        let mut written = Vec::new();
        run(
            8,
            pending,
            &sizes,
            |(source, _)| {
                assert_eq!(live.fetch_sub(1, Ordering::SeqCst), 1);
                assert_eq!(budget.state.lock().unwrap().used, 640 << 10);
                written.push(source.key);
                Ok(())
            },
            budget.clone(),
        )
        .unwrap();
        assert_eq!(written, names);
        assert_eq!(live.load(Ordering::SeqCst), 0);
        assert_eq!(budget.state.lock().unwrap().used, 0);
    }

    #[test]
    fn writer_failure_cancels_a_feeder_waiting_for_credit() {
        let (pending, names) = sources(|_| (vec![message()], Vec::new()));
        let sizes = names
            .iter()
            .map(|name| (name.as_str(), 640 << 10))
            .collect();
        let budget = Arc::new(Budget::new(1 << 20));
        let result = run(
            8,
            pending,
            &sizes,
            |_| anyhow::bail!("writer stopped"),
            budget.clone(),
        );
        assert_eq!(result.unwrap_err().to_string(), "writer stopped");
        assert!(budget.state.lock().unwrap().closed);
        assert_eq!(budget.state.lock().unwrap().used, 0);
    }
}
