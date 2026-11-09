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
/// Estimated retained bytes of parsed sources the writer has not taken yet. The writer is
/// usually slower than the parsers, so parsing further ahead only piles rows up in memory.
const WAITING_BYTES: u64 = 128 << 20;
/// A source whose parse outlasts the writer's share of that waiting room. Sources leading
/// up to one parse ahead within the window alone, so its parse starts early enough.
const LARGE_SOURCE: u64 = 16 << 20;
const MIN_SOURCE: u64 = 64 << 10;

#[derive(Default)]
struct State {
    used: u64,
    waiting: u64,
    closed: bool,
}
struct Budget {
    limit: u64,
    waiting_limit: u64,
    state: Mutex<State>,
    changed: Condvar,
}
impl Budget {
    fn new(limit: u64, waiting_limit: u64) -> Self {
        Self {
            limit,
            waiting_limit,
            state: Mutex::new(State::default()),
            changed: Condvar::new(),
        }
    }
    fn admits(&self, state: &State, bytes: u64, gated: bool) -> bool {
        bytes <= self.limit - state.used && (!gated || state.waiting <= self.waiting_limit)
    }
    fn reserve(self: &Arc<Self>, bytes: u64, gated: bool) -> Option<Reservation> {
        let mut state = self.state.lock().unwrap();
        while !state.closed && !self.admits(&state, bytes, gated) {
            state = self.changed.wait(state).unwrap();
        }
        if state.closed {
            return None;
        }
        state.used += bytes;
        Some(Reservation {
            budget: self.clone(),
            bytes,
            waiting: 0,
        })
    }
    fn update(&self, change: impl FnOnce(&mut State)) {
        change(&mut self.state.lock().unwrap());
        self.changed.notify_all();
    }
    fn close(&self) {
        self.state.lock().unwrap().closed = true;
        self.changed.notify_all();
    }
}
struct Reservation {
    budget: Arc<Budget>,
    bytes: u64,
    /// This parsed source's share of the bytes waiting for the writer.
    waiting: u64,
}
impl Reservation {
    /// Keep `bytes` of the input reservation and count `waiting` bytes until the writer takes it.
    fn retain(&mut self, bytes: u64, waiting: u64) {
        let bytes = bytes.clamp(MIN_SOURCE, self.bytes);
        let released = self.bytes - bytes;
        self.bytes = bytes;
        self.waiting = waiting;
        self.budget.update(|state| {
            state.used -= released;
            state.waiting += waiting;
        });
    }
    fn take(&mut self) {
        let waiting = std::mem::take(&mut self.waiting);
        self.budget.update(|state| state.waiting -= waiting);
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        let (bytes, waiting) = (self.bytes, self.waiting);
        self.budget.update(|state| {
            state.used -= bytes;
            state.waiting -= waiting;
        });
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

/// Conservative retained bytes of a parsed source, capped at `cap`; counting stops as soon
/// as the result is certain to be the cap.
fn retained_bytes((source, prepared): &Parsed, cap: u64) -> u64 {
    let limit = (cap * 2).div_ceil(3) as usize;
    let mut bytes = size_of::<SourceBatch>() + source.key.capacity() + source.agent.capacity();
    bytes += source.error.as_ref().map_or(0, String::capacity);
    bytes += source.cursor.as_ref().map_or(0, Vec::capacity);
    bytes += source.messages.capacity() * size_of::<crate::model::Message>();
    if bytes >= limit {
        return cap;
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
        return cap;
    }
    bytes += source.events.capacity() * size_of::<crate::model::Event>();
    if bytes >= limit {
        return cap;
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
        return cap;
    }
    bytes += source.nodes.capacity() * size_of::<crate::incremental::Node>();
    if bytes >= limit {
        return cap;
    }
    for node in &source.nodes {
        bytes += node.id.capacity()
            + node.parent.as_ref().map_or(0, String::capacity)
            + json_bytes(&node.body, limit.saturating_sub(bytes));
        if bytes >= limit {
            return cap;
        }
    }
    if bytes >= limit {
        return cap;
    }
    bytes += prepared.capacity() * size_of::<Option<tool_store::Prepared>>();
    if bytes >= limit {
        return cap;
    }
    for event in prepared.iter().flatten() {
        bytes += match &event.text {
            tool_store::Text::Plain(text) => text.len(),
            tool_store::Text::Encoded { body, .. } => body.capacity(),
        };
    }
    // Count shared Arcs repeatedly and allow for allocator/map slack.
    (bytes as u64).saturating_mul(3).div_ceil(2).min(cap)
}

fn prepare(source: SourceBatch) -> Parsed {
    let event = |event: &crate::model::Event| {
        (source.error.is_none() && event.kind != "control")
            .then(|| tool_store::prepare(event).ok())
            .flatten()
    };
    let small = source.events.len() <= 1
        || source
            .events
            .iter()
            .map(|e| e.input.len() + e.output.len())
            .sum::<usize>()
            <= 64 << 10;
    let prepared = if small {
        source.events.iter().map(event).collect()
    } else {
        source.events.par_iter().map(event).collect()
    };
    (source, prepared)
}

/// Keep active input and completed output inside one byte window, through the write.
/// A feeder can reuse compaction credit while the writer is occupied with earlier rows,
/// but away from large sources admits no source while parsed sources the writer has not
/// taken exceed their own byte bound.
pub(crate) fn parse_ahead(
    parsers: usize,
    pending: Vec<crate::intake_batch::Pending>,
    sizes: &HashMap<&str, u64>,
    mut write: impl FnMut(Parsed) -> Result<()>,
) -> Result<Duration> {
    if pending.len() == 1 {
        let started = Instant::now();
        let parsed = prepare(pending.into_iter().next().expect("one source").parse());
        let waited = started.elapsed();
        write(parsed)?;
        return Ok(waited);
    }
    run(
        parsers,
        pending,
        sizes,
        write,
        Arc::new(Budget::new(AHEAD_BYTES, WAITING_BYTES)),
    )
}
/// Whether a large source starts within `reach` input bytes of each source, itself included.
fn before_large(sizes: &[u64], reach: u64) -> Vec<bool> {
    let mut distance = None;
    let mut near = vec![false; sizes.len()];
    for (index, &size) in sizes.iter().enumerate().rev() {
        distance = if size >= LARGE_SOURCE {
            Some(0)
        } else {
            distance.map(|bytes: u64| bytes.saturating_add(size))
        };
        near[index] = distance.is_some_and(|bytes| bytes <= reach);
    }
    near
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
    // Admit a source only when a parser is free, so the waiting bound applies to every
    // admission rather than to sources queued long before their parse.
    let (queue, jobs) = mpsc::sync_channel::<Job>(0);
    let jobs = Mutex::new(jobs);
    let (ordered, results) = mpsc::channel();
    std::thread::scope(|scope| {
        // Close before scoped threads are joined, including a writer error or panic.
        let _close = Close(budget.clone());
        scope.spawn({
            let budget = budget.clone();
            move || {
                // A compressed source is parsed, and its rows retained, at its decoded size.
                let sizes: Vec<_> = pending
                    .par_iter()
                    .map(|source| {
                        let stored = sizes
                            .get(source.path.to_string_lossy().as_ref())
                            .copied()
                            .unwrap_or(0);
                        crate::ingest::jsonl::decoded_len(&source.path, stored)
                    })
                    .collect();
                let near = before_large(&sizes, budget.limit);
                for ((source, size), near) in pending.into_iter().zip(sizes).zip(near) {
                    let Some(reservation) =
                        budget.reserve(size.clamp(MIN_SOURCE, budget.limit), !near)
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
            let (jobs, budget) = (&jobs, &budget);
            scope.spawn(move || loop {
                let Ok(Ok((source, mut reservation, send))) = jobs.lock().map(|jobs| jobs.recv())
                else {
                    break;
                };
                let parsed = prepare(source.parse());
                let input = reservation.bytes;
                // Estimate past the input only as far as it can still close the waiting window.
                let waiting = retained_bytes(&parsed, input.max(budget.waiting_limit + 1));
                let retained = waiting.min(input);
                reservation.retain(retained, waiting);
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
            let (parsed, mut reservation) = receive
                .recv()
                .map_err(|_| anyhow::anyhow!("source parse ended"))?;
            reservation.take();
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
        numbered_sources(8, parse)
    }

    fn numbered_sources(
        count: usize,
        parse: impl Fn(&Path) -> (Vec<crate::model::Message>, Vec<crate::model::Event>)
            + Send
            + Sync
            + 'static,
    ) -> (Vec<crate::intake_batch::Pending>, Vec<String>) {
        let names: Vec<_> = (0..count).map(|i| format!("source-{i}")).collect();
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
        let budget = Arc::new(Budget::new(1 << 20, WAITING_BYTES));
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
        let budget = Arc::new(Budget::new(1 << 20, WAITING_BYTES));
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
        let budget = Arc::new(Budget::new(1 << 20, WAITING_BYTES));
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

    #[test]
    fn sources_waiting_for_the_writer_close_admission_until_it_takes_them() {
        let budget = Arc::new(Budget::new(1 << 30, 1 << 20));
        let admits = |gated| budget.admits(&budget.state.lock().unwrap(), MIN_SOURCE, gated);
        let mut parsed = budget.reserve(640 << 10, true).unwrap();
        parsed.retain(640 << 10, 2 << 20);
        assert!(!admits(true));
        assert!(admits(false));
        parsed.take();
        assert!(admits(true));
        let mut second = budget.reserve(MIN_SOURCE, true).unwrap();
        second.retain(MIN_SOURCE, 2 << 20);
        drop(second);
        drop(parsed);
        let state = budget.state.lock().unwrap();
        assert_eq!((state.used, state.waiting), (0, 0));
    }

    #[test]
    fn sources_leading_up_to_a_large_one_parse_ahead_within_the_window() {
        let mb = 1 << 20;
        let sizes = [mb, 3 * mb, mb, 16 * mb, 4 * mb, mb, 20 * mb, mb];
        assert_eq!(
            before_large(&sizes, 4 * mb),
            [false, true, true, true, false, true, true, false]
        );
    }

    #[test]
    fn parsed_sources_waiting_for_a_stalled_writer_stay_bounded() {
        let (pending, names) = numbered_sources(32, |_| (vec![message()], Vec::new()));
        let reserved = 640 << 10;
        let sizes = names.iter().map(|name| (name.as_str(), reserved)).collect();
        let (parsers, waiting_limit) = (2, 1 << 20);
        let budget = Arc::new(Budget::new(1 << 30, waiting_limit));
        // Each admission saw at most the limit waiting; the window then closes once the
        // sources already handed to parsers, plus the feeder's next one, complete.
        let bound = waiting_limit + (parsers as u64 + 1) * (waiting_limit + 1);
        let mut each = None;
        let mut written = Vec::new();
        run(
            parsers,
            pending,
            &sizes,
            |parsed| {
                // Sources differ only in key length; the first has the shortest key.
                let each = *each.get_or_insert_with(|| retained_bytes(&parsed, waiting_limit + 1));
                // Before each write, let the parsers run until they must stop: every admitted
                // source parsed, and the window closed or nothing left to admit.
                let settled = |state: &State| {
                    let admitted = state.used / reserved;
                    state.waiting / each + 1 == admitted
                        && (written.len() as u64 + admitted == names.len() as u64
                            || !budget.admits(state, reserved, true))
                };
                let (waiting, timed_out) = budget
                    .changed
                    .wait_timeout_while(
                        budget.state.lock().unwrap(),
                        Duration::from_secs(30),
                        |state| !settled(state),
                    )
                    .map(|(state, wait)| (state.waiting, wait.timed_out()))
                    .unwrap();
                assert!(!timed_out);
                assert!(waiting <= bound);
                written.push(parsed.0.key);
                Ok(())
            },
            budget.clone(),
        )
        .unwrap();
        assert_eq!(written, names);
        let state = budget.state.lock().unwrap();
        assert_eq!((state.used, state.waiting), (0, 0));
    }
}
