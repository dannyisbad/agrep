//! A transient parse pass. Fingerprints and rows live only in SQLite.
use crate::model::{Event, Message};
use rayon::prelude::*;
use std::{
    collections::{HashMap, HashSet},
    path::{Path, PathBuf},
    sync::Arc,
    time::SystemTime,
};
#[derive(Debug, Clone)]
pub struct SourceReadIssue {
    pub agent: &'static str,
    pub path: PathBuf,
    pub kind: &'static str,
    pub reason: String,
    /// The conversation of a token store whose parse this issue records; None for a failure the
    /// collector cannot tie to one, which a reader must then attribute to no chat.
    pub session: Option<String>,
    /// A failure a retry may clear (a lock, a journal, an I/O error), though it may persist.
    pub retryable: bool,
    /// The source changed under the read: it never fails the same way twice.
    pub moving: bool,
}

impl SourceReadIssue {
    pub(crate) fn new(
        agent: &'static str,
        path: impl Into<PathBuf>,
        kind: &'static str,
        reason: impl Into<String>,
    ) -> Self {
        Self {
            agent,
            path: path.into(),
            kind,
            reason: reason.into(),
            session: None,
            retryable: false,
            moving: false,
        }
    }

    pub(crate) fn for_session(mut self, session: impl Into<String>) -> Self {
        self.session = Some(session.into());
        self
    }

    pub(crate) fn retryable(mut self) -> Self {
        self.retryable = true;
        self
    }

    pub(crate) fn moving(mut self) -> Self {
        self.moving = true;
        self
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReadOutcome {
    Complete,
    Invalid,
    Skipped,
    /// Trustworthy rows, but some scope inside the source went unobserved. Too incomplete to
    /// license deletion, so last-good wins; with no last-good there is nothing to lose and
    /// what parsed is published.
    Partial,
    /// A complete read that came back empty because the source itself undid what it held: rolled
    /// back or rewound in records the read saw whole. Where an empty read of a changed file may be
    /// torn, this one proves the emptiness, so a file that only grew may publish it over last-good.
    Undone,
    /// The source could not be opened or read at this moment (a writer's lock or journal, an I/O
    /// error), which a retry may clear or which may persist. It vouches for nothing.
    Unavailable,
    /// The source changed under the read (its generation moved, it shrank or was replaced): a
    /// later pass reads another source, so this failure never counts as persistent.
    Moved,
}

impl ReadOutcome {
    pub fn merge(self, other: Self) -> Self {
        match (self, other) {
            (Self::Moved, _) | (_, Self::Moved) => Self::Moved,
            (Self::Unavailable, _) | (_, Self::Unavailable) => Self::Unavailable,
            (Self::Invalid, _) | (_, Self::Invalid) => Self::Invalid,
            (Self::Skipped, _) | (_, Self::Skipped) => Self::Skipped,
            (Self::Partial, _) | (_, Self::Partial) => Self::Partial,
            _ => Self::Complete,
        }
    }

    /// Whether this read may replace a cached entry wholesale (and so publish deletions).
    fn licenses_replacement(self) -> bool {
        matches!(self, Self::Complete | Self::Undone)
    }
}

pub trait IntoParsed {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>);
}

impl IntoParsed for (Vec<Message>, Vec<Event>) {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>) {
        (self.0, self.1, None)
    }
}

impl IntoParsed for (Vec<Message>, Vec<Event>, bool) {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>) {
        let outcome = if self.2 {
            ReadOutcome::Complete
        } else {
            ReadOutcome::Invalid
        };
        (self.0, self.1, Some(outcome))
    }
}

impl IntoParsed for (Vec<Message>, Vec<Event>, ReadOutcome) {
    fn into_parsed(self) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>) {
        (self.0, self.1, Some(self.2))
    }
}

#[derive(Default)]
pub struct Pass {
    pub messages: Vec<Message>,
    pub events: Vec<Event>,
}
#[derive(Default)]
pub struct SourceBatch {
    pub key: String,
    pub agent: String,
    pub messages: Vec<Message>,
    pub events: Vec<Event>,
    pub error: Option<String>,
    pub cursor: Option<Vec<u8>>,
    pub nodes: Vec<crate::incremental::Node>,
    pub append: bool,
    pub reset_nodes: bool,
    pub bytes_read: usize,
    pub bytes_parsed: usize,
    pub parse_ms: f64,
}
#[derive(Default)]
pub struct IntakeBatch {
    pub changed: HashSet<PathBuf>,
    pub force: bool,
    pub retired: HashSet<String>,
    pub source_paths: Vec<PathBuf>,
    pub projects: HashMap<String, String>,
    pub seen_tokens: HashSet<String>,
    pub batches: Vec<SourceBatch>,
    pub issues: Vec<SourceReadIssue>,
    pub tokens: HashMap<String, String>,
    pub cursors: HashMap<String, Vec<u8>>,
    pub database: Option<PathBuf>,
    /// File sources go to `pending` unparsed, for a caller that parses each one only
    /// as it is ready to write it.
    pub defer: bool,
    pub pending: Vec<Pending>,
}
type Parse = dyn Fn(&Path) -> (Vec<Message>, Vec<Event>, Option<ReadOutcome>) + Send + Sync;
/// A changed file source and everything its parse needs.
pub struct Pending {
    pub path: PathBuf,
    agent: &'static str,
    cursor: Option<Vec<u8>>,
    database: Option<PathBuf>,
    parse: Arc<Parse>,
}
impl Pending {
    pub fn parse(self) -> SourceBatch {
        parse_source(
            self.agent,
            &self.path,
            self.cursor.as_deref(),
            self.database.as_deref(),
            &*self.parse,
        )
    }
}
impl IntakeBatch {
    pub fn preflight_source_paths(&self, root: &Path) -> Option<Vec<PathBuf>> {
        Some(
            self.source_paths
                .iter()
                .filter(|p| p.starts_with(root))
                .cloned()
                .collect(),
        )
    }
    pub fn record_read_issue(&mut self, i: SourceReadIssue) {
        self.issues.push(i)
    }
    pub fn record_source_read_issue(
        &mut self,
        a: &'static str,
        p: &Path,
        k: &'static str,
        r: impl Into<String>,
    ) {
        self.record_read_issue(SourceReadIssue::new(a, p, k, r))
    }
    pub fn token_projects(&self, _: &str) -> HashMap<String, String> {
        self.projects.clone()
    }
    pub fn collect_token_cached_keyed_partial<F, R>(
        &mut self,
        agent: &str,
        tokens: Option<Vec<(String, String, String)>>,
        _: &[String],
        _: bool,
        _: bool,
        parse: F,
    ) -> Pass
    where
        F: Fn(&str, &str) -> R,
        R: IntoParsed,
    {
        for (id, sid, token) in tokens.unwrap_or_default() {
            let key = format!("token:{agent}:{id}");
            self.seen_tokens.insert(key.clone());
            if !self.force && self.tokens.get(&key) == Some(&token) {
                continue;
            }
            let (m, e, o) = parse(&id, &sid).into_parsed();
            let error = o
                .filter(|o| !o.licenses_replacement())
                .map(|o| format!("parser returned {o:?}"));
            self.tokens.insert(key.clone(), token);

            self.batches.push(SourceBatch {
                key,
                agent: agent.into(),
                messages: m,
                events: e,
                error,
                ..Default::default()
            });
        }
        Pass::default()
    }
}
pub fn epoch_ns(t: SystemTime) -> Option<i64> {
    t.duration_since(SystemTime::UNIX_EPOCH)
        .ok()
        .and_then(|d| i64::try_from(d.as_nanos()).ok())
}
pub fn racy_stamp(stamp: i64, start: i64, now: i64) -> bool {
    let mut granule = 1;
    while granule < 1_000_000_000 && stamp % (granule * 10) == 0 {
        granule *= 10;
    }
    let margin = granule.max(20_000_000);
    stamp > start.saturating_sub(margin) && stamp <= now.saturating_add(2_000_000_000)
}
pub fn collect_cached_retiring_for<F, R>(
    c: &mut IntakeBatch,
    a: &'static str,
    f: &[PathBuf],
    retired: &HashSet<PathBuf>,
    p: F,
) -> Pass
where
    F: Fn(&Path) -> R + Send + Sync + 'static,
    R: IntoParsed + 'static,
{
    c.retired
        .extend(retired.iter().map(|p| p.to_string_lossy().into_owned()));
    collect_cached_for(c, a, f, p)
}
pub fn collect_cached_for<F, R>(c: &mut IntakeBatch, a: &'static str, f: &[PathBuf], p: F) -> Pass
where
    F: Fn(&Path) -> R + Send + Sync + 'static,
    R: IntoParsed + 'static,
{
    let parse: Arc<Parse> = Arc::new(move |path: &Path| p(path).into_parsed());
    let cursor = |path: &Path| c.cursors.get(path.to_string_lossy().as_ref());
    if c.defer {
        let pending: Vec<_> = f
            .iter()
            .filter(|p| c.changed.contains(*p))
            .map(|path| Pending {
                path: path.clone(),
                agent: a,
                cursor: cursor(path).cloned(),
                database: c.database.clone(),
                parse: parse.clone(),
            })
            .collect();
        c.pending.extend(pending);
        return Pass::default();
    }
    let batches: Vec<_> = f
        .par_iter()
        .filter(|p| c.changed.contains(*p))
        .map(|path| {
            parse_source(
                a,
                path,
                cursor(path).map(Vec::as_slice),
                c.database.as_deref(),
                &*parse,
            )
        })
        .collect();
    c.batches.extend(batches);
    Pass::default()
}
fn parse_source(
    a: &'static str,
    path: &Path,
    cursor: Option<&[u8]>,
    database: Option<&Path>,
    p: &Parse,
) -> SourceBatch {
    let started = std::time::Instant::now();
    crate::intake::take_diagnostics();
    let mut delta = None;
    let ((m, e, o), bad) = crate::intake::scoped(|| {
        match crate::ingest::parse_guard::isolate(a, path, || {
            if crate::incremental::supports(a, path) {
                match crate::incremental::parse(a, path, cursor, database) {
                    Ok(mut parsed) => {
                        let rows = (
                            std::mem::take(&mut parsed.messages),
                            std::mem::take(&mut parsed.events),
                            Some(parsed.outcome),
                        );
                        delta = Some(parsed);
                        rows
                    }
                    Err(e) => {
                        crate::intake::diagnostic(format_args!("source parse failed: {e}"));
                        (vec![], vec![], Some(ReadOutcome::Invalid))
                    }
                }
            } else {
                p(path)
            }
        }) {
            Ok(v) => v,
            Err(_) => (vec![], vec![], Some(ReadOutcome::Invalid)),
        }
    });
    let diagnostics = crate::intake::take_diagnostics();
    // A complete, nonempty parse can account for bad records without losing its good ones.
    let error = if !diagnostics.is_empty()
        || (crate::intake::errors(&bad)
            && (o != Some(ReadOutcome::Complete) || (m.is_empty() && e.is_empty())))
        || o.is_some_and(|o| !o.licenses_replacement())
    {
        Some(
            crate::ingest::take_read_failure(path)
                .unwrap_or_else(|| format!("source parse incomplete: {}", path.display())),
        )
    } else {
        None
    };
    let mut batch = SourceBatch {
        key: path.to_string_lossy().into(),
        agent: a.into(),
        messages: m,
        events: e,
        error,
        parse_ms: started.elapsed().as_secs_f64() * 1000.,
        ..Default::default()
    };
    if batch.error.is_none() {
        if let Some(delta) = delta {
            batch.cursor = Some(delta.cursor);
            batch.nodes = delta.nodes;
            batch.append = delta.append;
            batch.reset_nodes = delta.reset_nodes;
            batch.bytes_read = delta.bytes_read;
            batch.bytes_parsed = delta.bytes_parsed;
        }
    }
    batch
}

/// Reconcile an adapter-wide dependency unit before it reaches the row store.
pub fn canonicalize_messages(mut messages: Vec<Message>) -> Vec<Message> {
    fn quality(a: &Message, b: &Message) -> std::cmp::Ordering {
        a.reply_chars
            .cmp(&b.reply_chars)
            .then(a.reply.len().cmp(&b.reply.len()))
            .then((!a.model.is_empty()).cmp(&(!b.model.is_empty())))
            .then((!a.project.is_empty()).cmp(&(!b.project.is_empty())))
            .then((a.project.as_ref() != a.agent).cmp(&(b.project.as_ref() != b.agent)))
            .then((!a.parent.is_empty()).cmp(&(!b.parent.is_empty())))
            .then(a.side.cmp(&b.side))
            .then_with(|| match (a.ts > 0, b.ts > 0) {
                (true, false) => std::cmp::Ordering::Greater,
                (false, true) => std::cmp::Ordering::Less,
                (true, true) => b.ts.cmp(&a.ts),
                _ => std::cmp::Ordering::Equal,
            })
            .then(a.reply.cmp(&b.reply))
            .then(a.model.cmp(&b.model))
            .then(a.project.cmp(&b.project))
            .then(a.parent.cmp(&b.parent))
            .then(a.who.cmp(&b.who))
            .then(a.model_source.cmp(&b.model_source))
    }
    messages.sort_by(|a, b| {
        (a.agent, &a.session, a.turn, &a.text)
            .cmp(&(b.agent, &b.session, b.turn, &b.text))
            .then_with(|| quality(b, a))
    });
    messages.dedup_by(|a, b| {
        a.agent == b.agent && a.session == b.session && a.turn == b.turn && a.text == b.text
    });
    let mut positions = HashSet::new();
    let mut affected = HashSet::new();
    for m in &messages {
        if !positions.insert((m.agent, m.session.clone(), m.turn)) {
            affected.insert((m.agent, m.session.clone()));
        }
    }
    let mut groups: HashMap<_, Vec<usize>> = HashMap::new();
    for (i, m) in messages.iter().enumerate() {
        let key = (m.agent, m.session.clone());
        if affected.contains(&key) {
            groups.entry(key).or_default().push(i);
        }
    }
    for indices in groups.values_mut() {
        indices.sort_by_key(|i| {
            let m = &messages[*i];
            (m.ts <= 0, m.ts, m.turn, m.text.clone())
        });
        for (turn, index) in indices.iter().enumerate() {
            messages[*index].turn = u32::try_from(turn).expect("session turn count fits u32");
        }
    }
    messages
}

#[cfg(test)]
mod replay_tests {
    fn message(text: &str, reply: &str, ts: i64) -> crate::model::Message {
        crate::model::RawMessage {
            agent: "test",
            project: "test".into(),
            session: "s".into(),
            ts,
            turn: 0,
            text: text.into(),
            model: String::new(),
            reply: reply.into(),
            reply_chars: reply.chars().count(),
            reply_shape: Default::default(),
            side: false,
            parent: String::new(),
        }
        .freeze()
    }
    #[test]
    fn dependency_units_choose_complete_replies_and_repair_collisions() {
        let rows = super::canonicalize_messages(vec![
            message("same", "short", 1),
            message("same", "longer answer", 1),
            message("different", "answer", 2),
        ]);
        assert_eq!(rows.len(), 2);
        let same = rows.iter().find(|r| r.text.as_ref() == "same").unwrap();
        assert_eq!(same.reply.as_ref(), "longer answer");
        assert_eq!(same.turn, 0);
        assert_eq!(
            rows.iter()
                .find(|r| r.text.as_ref() == "different")
                .unwrap()
                .turn,
            1
        );
    }
}
