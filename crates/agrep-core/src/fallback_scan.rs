use std::borrow::Cow;
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap, HashSet};
use std::fmt;
use std::fmt::Write as _;
use std::io::BufRead;
use std::path::Path;
use std::time::UNIX_EPOCH;

use serde::de::IgnoredAny;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const PROTOCOL_VERSION: u32 = 2;
pub const CANDIDATE_BYTES_LIMIT: usize = 16 * 1024 * 1024;
pub const DEFAULT_CANDIDATE_LIMIT: usize = 4096;
const MAX_CANDIDATE_LIMIT: usize = 32_768;
const SCORE_DIGITS: f64 = 10_000.0;
const REFINED_ROUNDING_GUARD: f64 = 1e-6;
const MAX_RESULTS: usize = 512;
const INGEST_GENERATION_MAX_BYTES: u64 = 4096;
const SESSION_ROW_MAX_BYTES: usize = 1024 * 1024;
const SESSION_FILE_MAX_BYTES: u64 = 1024 * 1024 * 1024;
const SESSION_OWNER_MAX: usize = 2_000_000;
const SESSION_ID_MAX_BYTES: usize = 4096;
const SESSION_AGENT_MAX_BYTES: usize = 64;
const DERIVED_PROOF_MAX_BYTES: u64 = 1024 * 1024;

#[derive(Clone, Debug, Deserialize, Eq, Hash, PartialEq, Serialize)]
pub struct SessionOwner {
    pub agent: String,
    pub session: String,
    #[serde(default)]
    pub project: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
pub struct ScoreContract {
    pub half_life_days: f64,
    pub who_tool: f64,
    pub source_tool: f64,
    pub meta_min: f64,
    pub boundary_min: f64,
}

impl Default for ScoreContract {
    fn default() -> Self {
        Self {
            half_life_days: 14.0,
            who_tool: 0.4,
            source_tool: 0.55,
            meta_min: 0.45,
            boundary_min: 0.12,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ScanRequest {
    pub protocol: u32,
    pub expected_ingest_generation: String,
    pub expected_event_generation: String,
    pub query: String,
    pub boundary_context: BoundaryContext,
    pub now_ms: f64,
    pub limit: usize,
    #[serde(default = "default_candidate_limit")]
    pub candidate_limit: usize,
    #[serde(default)]
    pub after: Option<EnvelopeCursor>,
    #[serde(default)]
    pub eligible_sessions: Vec<SessionOwner>,
    #[serde(default)]
    pub eligibility: EligibilityMode,
    #[serde(default)]
    pub owner_filter: PublishedOwnerFilter,
    #[serde(default)]
    pub caller_event_window: Option<CallerEventWindow>,
    #[serde(default)]
    pub since_ms: Option<i64>,
    #[serde(default)]
    pub until_ms: Option<i64>,
    pub score_contract: ScoreContract,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BoundaryContext {
    ColdPrior,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct CallerEventWindow {
    pub session: String,
    pub boundary: i64,
    #[serde(default)]
    pub marks: Vec<EventTurnMark>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
pub struct EventTurnMark {
    pub ts: i64,
    pub turn: i64,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
pub struct PublishedOwnerFilter {
    #[serde(default)]
    pub agent_contains: String,
    #[serde(default)]
    pub project_contains: String,
    #[serde(default)]
    pub chat_prefix: String,
    #[serde(default)]
    pub excluded_sessions: Vec<String>,
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EligibilityMode {
    #[default]
    Explicit,
    PublishedSessions,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct RequestError(&'static str);

impl fmt::Display for RequestError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for RequestError {}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ScanState {
    Ok,
    Unsupported,
    GenerationMoved,
    IntegrityError,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct ScanTotals {
    pub sessions: u64,
    pub events: u64,
    pub bytes: u64,
    pub candidate_sessions: u64,
    pub candidate_events: u64,
    pub candidate_bytes: u64,
    pub refined_matches: u64,
    pub conservative_matches: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct MatchTotals {
    pub tools: u64,
    pub phrase_tools: u64,
    pub all_terms_tools: u64,
    pub all_terms_additions: u64,
    pub matched_sessions: u64,
    pub eligible_sessions: u64,
    pub matched_owner_bitmap: String,
    pub phrase_owner_bitmap: String,
    pub owner_order_sha256: String,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MatchLane {
    Phrase,
    AllTerms,
}

#[derive(Clone, Debug, Serialize)]
pub struct Candidate {
    pub agent: String,
    pub session: String,
    pub ordinal: u64,
    pub event_ordinal: u64,
    pub ts: i64,
    pub matched: MatchLane,
    pub occurrences: u64,
    pub upper_score: f64,
    pub lower_score: f64,
    pub refined_score: bool,
    #[serde(skip)]
    estimated_bytes: usize,
}

#[derive(Clone, Debug)]
struct EnvelopeCandidate(Candidate);

impl PartialEq for EnvelopeCandidate {
    fn eq(&self, other: &Self) -> bool {
        optimistic_candidate_cmp(&self.0, &other.0) == Ordering::Equal
    }
}

impl Eq for EnvelopeCandidate {}

impl PartialOrd for EnvelopeCandidate {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for EnvelopeCandidate {
    fn cmp(&self, other: &Self) -> Ordering {
        optimistic_candidate_cmp(&self.0, &other.0)
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct EnvelopeCursor {
    pub matched: MatchLane,
    pub upper_score: f64,
    pub ts: i64,
    pub session: String,
    pub ordinal: u64,
}

#[derive(Clone, Debug, Serialize)]
pub struct ScanResponse {
    pub protocol: u32,
    pub state: ScanState,
    pub ingest_generation: String,
    pub event_generation: String,
    pub scanned: ScanTotals,
    pub matches: MatchTotals,
    pub candidates: Vec<Candidate>,
    pub best_omitted: Option<EnvelopeCursor>,
    pub next_after: Option<EnvelopeCursor>,
    pub envelope_complete: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

pub struct EventSession<'a> {
    pub name: &'a str,
    pub agent: &'a str,
    pub session: &'a str,
    pub n_events: u64,
    pub payload: &'a [u8],
}

#[derive(Clone, Copy)]
struct ScoreInputs {
    ts: i64,
    meta_scale: f64,
    now_ms: f64,
    contract: ScoreContract,
}

#[derive(Clone, Copy)]
struct CandidateObservation {
    owner_ordinal: usize,
    ts: i64,
    lane: MatchLane,
    occurrences: u64,
    first_phrase_span: Option<usize>,
    boundary_factors: (f64, f64),
    ordinal: u64,
    event_ordinal: u64,
    refined_score: Option<(f64, f64)>,
}

fn default_candidate_limit() -> usize {
    DEFAULT_CANDIDATE_LIMIT
}

#[derive(Clone, Debug)]
struct LiteralMatcher {
    query: Vec<u8>,
}

#[derive(Clone, Debug)]
enum QueryPlan {
    Single(LiteralMatcher),
    Multi {
        phrase: Vec<Vec<u8>>,
        terms: Vec<LiteralMatcher>,
    },
}

impl QueryPlan {
    fn min_phrase_len(&self) -> usize {
        match self {
            Self::Single(matcher) => matcher.query.len(),
            Self::Multi { phrase, .. } => phrase.iter().map(Vec::len).sum(),
        }
    }
}

#[derive(Deserialize)]
#[serde(untagged)]
enum LooseText<'a> {
    Text(#[serde(borrow)] Cow<'a, str>),
    Other(IgnoredAny),
}

impl Default for LooseText<'_> {
    fn default() -> Self {
        Self::Other(IgnoredAny)
    }
}

impl LooseText<'_> {
    fn get(&self) -> &str {
        match self {
            Self::Text(value) => value.as_ref(),
            Self::Other(_) => "",
        }
    }
}

#[derive(Deserialize)]
#[serde(untagged)]
enum LooseBool {
    Bool(bool),
    Other(IgnoredAny),
}

impl Default for LooseBool {
    fn default() -> Self {
        Self::Other(IgnoredAny)
    }
}

#[derive(Deserialize)]
#[serde(untagged)]
enum LooseI64 {
    Integer(i64),
    Other(IgnoredAny),
}

impl Default for LooseI64 {
    fn default() -> Self {
        Self::Other(IgnoredAny)
    }
}

#[derive(Default, Deserialize)]
struct EventView<'a> {
    #[serde(default)]
    ts: LooseI64,
    #[serde(borrow, default)]
    kind: LooseText<'a>,
    #[serde(borrow, default)]
    name: LooseText<'a>,
    #[serde(borrow, default)]
    input: LooseText<'a>,
    #[serde(borrow, default)]
    output: LooseText<'a>,
    #[serde(default)]
    ok: LooseBool,
}

impl EventView<'_> {
    fn timestamp(&self) -> i64 {
        match self.ts {
            LooseI64::Integer(value) if value != i64::MIN => value,
            LooseI64::Integer(_) | LooseI64::Other(_) => 0,
        }
    }

    fn failed(&self) -> bool {
        matches!(self.ok, LooseBool::Bool(false))
    }
}

impl LiteralMatcher {
    fn new(query: &str) -> Self {
        let query = query.to_ascii_lowercase().into_bytes();
        Self { query }
    }

    fn count(&self, text: &str) -> usize {
        self.match_stats(text, false).0
    }

    fn first_span(&self, text: &str) -> Option<crate::boundary_rank::Span> {
        self.match_stats(text, true).1
    }

    fn match_stats(
        &self,
        text: &str,
        first_only: bool,
    ) -> (usize, Option<crate::boundary_rank::Span>) {
        if text.is_ascii() {
            return self.match_ascii(text.as_bytes(), first_only);
        }
        let folded: Vec<u8> = text.chars().map(python_regex_ascii_fold).collect();
        let mut count = 0;
        let mut first = None;
        let mut offset = 0;
        while offset + self.query.len() <= folded.len() {
            let Some(relative) = folded[offset..]
                .windows(self.query.len())
                .position(|window| window == self.query)
            else {
                break;
            };
            let start = offset + relative;
            let end = start + self.query.len();
            count += 1;
            first.get_or_insert([start as i64, end as i64]);
            if first_only {
                break;
            }
            offset = end;
        }
        (count, first)
    }

    fn match_ascii(
        &self,
        text: &[u8],
        first_only: bool,
    ) -> (usize, Option<crate::boundary_rank::Span>) {
        let mut count = 0;
        let mut first = None;
        let mut offset = 0;
        let needle_first = self.query[0];
        let needle_other = if needle_first.is_ascii_alphabetic() {
            needle_first.to_ascii_uppercase()
        } else {
            needle_first
        };
        while offset + self.query.len() <= text.len() {
            let found = if needle_first == needle_other {
                memchr::memchr(needle_first, &text[offset..])
            } else {
                memchr::memchr2(needle_first, needle_other, &text[offset..])
            };
            let Some(relative) = found else {
                break;
            };
            let start = offset + relative;
            let end = start + self.query.len();
            if end <= text.len() && text[start..end].eq_ignore_ascii_case(&self.query) {
                count += 1;
                first.get_or_insert([start as i64, end as i64]);
                if first_only {
                    break;
                }
                offset = end;
            } else {
                offset = start + 1;
            }
        }
        (count, first)
    }
}

pub struct Scanner {
    request: ScanRequest,
    query: QueryPlan,
    boundary: crate::boundary_rank::PreparedQuery,
    raw_anchors: Vec<Vec<u8>>,
    owners: Vec<SessionOwner>,
    owners_by_name: HashMap<String, usize>,
    owners_by_identity: HashMap<String, HashMap<String, usize>>,
    published_owners: Option<Vec<SessionOwner>>,
    published_by_name: HashMap<String, usize>,
    published_by_identity: HashMap<String, HashMap<String, usize>>,
    scanned: ScanTotals,
    match_count: u64,
    phrase_count: u64,
    all_terms_count: u64,
    all_terms_additions: u64,
    matched_owner_bits: Vec<u8>,
    phrase_owner_bits: Vec<u8>,
    candidates: BinaryHeap<EnvelopeCandidate>,
    best_omitted: Option<EnvelopeCursor>,
    candidate_bytes: usize,
    candidate_bytes_limit: usize,
    unsupported_payload: bool,
    owner_integrity_error: Option<String>,
}

impl Scanner {
    pub fn new(request: ScanRequest) -> Result<Self, RequestError> {
        Self::with_candidate_limit(request, CANDIDATE_BYTES_LIMIT, None)
    }

    fn with_candidate_limit(
        mut request: ScanRequest,
        candidate_bytes_limit: usize,
        published_owners: Option<Vec<SessionOwner>>,
    ) -> Result<Self, RequestError> {
        validate_request(&request)?;
        let owners = std::mem::take(&mut request.eligible_sessions);
        let owner_count = owners.len();
        let owners_by_name = owners
            .iter()
            .enumerate()
            .map(|(ordinal, owner)| {
                (
                    crate::cache::event_fname(&owner.agent, &owner.session),
                    ordinal,
                )
            })
            .collect();
        let mut owners_by_identity: HashMap<String, HashMap<String, usize>> = HashMap::new();
        for (ordinal, owner) in owners.iter().enumerate() {
            owners_by_identity
                .entry(owner.agent.clone())
                .or_default()
                .insert(owner.session.clone(), ordinal);
        }
        let mut published_by_name = HashMap::new();
        let mut published_by_identity: HashMap<String, HashMap<String, usize>> = HashMap::new();
        if let Some(census) = published_owners.as_ref() {
            for (ordinal, owner) in census.iter().enumerate() {
                let name = crate::cache::event_fname(&owner.agent, &owner.session);
                if published_by_name.insert(name, ordinal).is_some()
                    || published_by_identity
                        .entry(owner.agent.clone())
                        .or_default()
                        .insert(owner.session.clone(), ordinal)
                        .is_some()
                {
                    return Err(RequestError("published event owners collide"));
                }
            }
        }
        if let Some(window) = request.caller_event_window.as_mut() {
            window.marks.retain(|mark| mark.ts != 0);
            window.marks.sort_by_key(|mark| (mark.ts, mark.turn));
        }
        let tokens = keyword_tokens(&request.query);
        let query = if tokens.len() == 1 {
            QueryPlan::Single(LiteralMatcher::new(tokens[0]))
        } else {
            QueryPlan::Multi {
                phrase: tokens
                    .iter()
                    .map(|token| token.to_ascii_lowercase().into_bytes())
                    .collect(),
                terms: tokens
                    .iter()
                    .map(|token| LiteralMatcher::new(token))
                    .collect(),
            }
        };
        let raw_anchors = raw_anchors(&tokens);
        let boundary = crate::boundary_rank::prepare_query(
            &request.query,
            &crate::boundary_rank::Stats::new(),
        );
        Ok(Self {
            request,
            query,
            boundary,
            raw_anchors,
            owners,
            owners_by_name,
            owners_by_identity,
            published_owners,
            published_by_name,
            published_by_identity,
            scanned: ScanTotals::default(),
            match_count: 0,
            phrase_count: 0,
            all_terms_count: 0,
            all_terms_additions: 0,
            matched_owner_bits: vec![0; owner_count.div_ceil(8)],
            phrase_owner_bits: vec![0; owner_count.div_ceil(8)],
            candidates: BinaryHeap::new(),
            best_omitted: None,
            candidate_bytes: 0,
            candidate_bytes_limit,
            unsupported_payload: false,
            owner_integrity_error: None,
        })
    }

    pub fn visit(&mut self, row: EventSession<'_>) {
        self.scanned.sessions = self.scanned.sessions.saturating_add(1);
        self.scanned.events = self.scanned.events.saturating_add(row.n_events);
        self.scanned.bytes = self.scanned.bytes.saturating_add(row.payload.len() as u64);

        let owner_ordinal = match self.owner_for(&row) {
            Ok(Some(ordinal)) => ordinal,
            Ok(None) => return,
            Err(detail) => {
                self.owner_integrity_error
                    .get_or_insert_with(|| detail.into());
                return;
            }
        };
        if !raw_payload_maybe_matches(row.payload, &self.raw_anchors) {
            return;
        }
        self.scanned.candidate_sessions = self.scanned.candidate_sessions.saturating_add(1);
        self.scanned.candidate_events = self.scanned.candidate_events.saturating_add(row.n_events);
        self.scanned.candidate_bytes = self
            .scanned
            .candidate_bytes
            .saturating_add(row.payload.len() as u64);
        let mut start = 0;
        let mut event_ordinal = 0u64;
        for end in memchr::memchr_iter(b'\n', row.payload).chain(std::iter::once(row.payload.len()))
        {
            let raw = &row.payload[start..end];
            start = end.saturating_add(1);
            if raw.is_empty() {
                continue;
            }
            let current_event_ordinal = event_ordinal;
            event_ordinal = event_ordinal.saturating_add(1);
            if !raw_maybe_matches(raw, &self.raw_anchors) {
                continue;
            }
            let Ok(event) = serde_json::from_slice::<EventView<'_>>(raw) else {
                self.unsupported_payload |= raw_has_unpaired_surrogate_escape(raw);
                continue;
            };
            let ts = event.timestamp();
            if self.request.since_ms.is_some_and(|since| ts < since)
                || self.request.until_ms.is_some_and(|until| ts >= until)
                || self.event_is_window_excluded(owner_ordinal, ts)
            {
                continue;
            }
            let matched = match &self.query {
                QueryPlan::Single(matcher) => {
                    let count = canonical_occurrences(&event, matcher);
                    (count > 0).then_some({
                        (
                            MatchLane::Phrase,
                            count as u64,
                            false,
                            Some(matcher.query.len()),
                            None,
                        )
                    })
                }
                QueryPlan::Multi { phrase, terms } => {
                    let Some(text) = canonical_text(&event) else {
                        continue;
                    };
                    let phrase_stats = phrase_match_stats(&text.text, phrase);
                    let all_terms = terms
                        .iter()
                        .all(|term| term.first_span(&text.text).is_some());
                    (phrase_stats.count > 0 || all_terms).then(|| {
                        let phrase_matched = phrase_stats.count > 0;
                        (
                            if phrase_matched {
                                MatchLane::Phrase
                            } else {
                                MatchLane::AllTerms
                            },
                            phrase_stats.count as u64,
                            all_terms,
                            phrase_stats.first_span,
                            Some(text),
                        )
                    })
                }
            };
            if let Some((lane, occurrences, all_terms, first_phrase_span, rendered)) = matched {
                let refined_score = rendered.as_ref().map_or_else(
                    || self.exact_event_score(owner_ordinal, &event, ts, lane),
                    |rendered| self.exact_rendered_score(owner_ordinal, rendered, ts, lane),
                );
                if refined_score.is_some() {
                    self.scanned.refined_matches = self.scanned.refined_matches.saturating_add(1);
                } else {
                    self.scanned.conservative_matches =
                        self.scanned.conservative_matches.saturating_add(1);
                }
                let ordinal = self.record_match(owner_ordinal, lane, all_terms);
                self.retain_match(CandidateObservation {
                    owner_ordinal,
                    ts,
                    lane,
                    occurrences,
                    first_phrase_span,
                    boundary_factors: (self.request.score_contract.boundary_min, 1.0),
                    ordinal,
                    event_ordinal: current_event_ordinal,
                    refined_score,
                });
            }
        }
    }

    pub fn finish(self, ingest_generation: String, event_generation: String) -> ScanResponse {
        let matches = self.match_totals();
        if let Some(detail) = self.owner_integrity_error {
            return ScanResponse {
                protocol: PROTOCOL_VERSION,
                state: ScanState::IntegrityError,
                ingest_generation,
                event_generation,
                scanned: self.scanned,
                matches,
                candidates: Vec::new(),
                best_omitted: None,
                next_after: None,
                envelope_complete: false,
                detail: Some(detail),
            };
        }
        if self.unsupported_payload {
            return ScanResponse {
                protocol: PROTOCOL_VERSION,
                state: ScanState::Unsupported,
                ingest_generation,
                event_generation,
                scanned: self.scanned,
                matches,
                candidates: Vec::new(),
                best_omitted: None,
                next_after: None,
                envelope_complete: false,
                detail: Some("the event store contains legacy surrogate escapes".into()),
            };
        }
        let mut candidates: Vec<_> = self
            .candidates
            .into_iter()
            .map(|candidate| candidate.0)
            .collect();
        candidates.sort_by(optimistic_candidate_cmp);
        let best_omitted = self.best_omitted;
        let next_after = best_omitted
            .as_ref()
            .and_then(|_| candidates.last().map(candidate_cursor));
        let envelope_complete = best_omitted.is_none();
        ScanResponse {
            protocol: PROTOCOL_VERSION,
            state: ScanState::Ok,
            ingest_generation,
            event_generation,
            scanned: self.scanned,
            matches,
            candidates,
            best_omitted,
            next_after,
            envelope_complete,
            detail: None,
        }
    }

    fn owner_for(&self, row: &EventSession<'_>) -> Result<Option<usize>, &'static str> {
        if row.session.is_empty() {
            if let Some(census) = self.published_owners.as_ref() {
                let Some(full_ordinal) = self.published_by_name.get(row.name).copied() else {
                    return Err("legacy event filename has no published owner");
                };
                if census.get(full_ordinal).is_none() {
                    return Err("legacy event owner census is inconsistent");
                }
            }
            return Ok(self.owners_by_name.get(row.name).copied());
        }
        if row.name != crate::cache::event_fname(row.agent, row.session) {
            return Err("event row filename does not match its owner identity");
        }
        if let Some(census) = self.published_owners.as_ref() {
            let Some(full_ordinal) = self
                .published_by_identity
                .get(row.agent)
                .and_then(|sessions| sessions.get(row.session))
                .copied()
            else {
                return Err("event row identity has no published owner");
            };
            if self.published_by_name.get(row.name).copied() != Some(full_ordinal)
                || census
                    .get(full_ordinal)
                    .is_none_or(|owner| owner.agent != row.agent || owner.session != row.session)
            {
                return Err("event row owner disagrees with the published census");
            }
        }
        Ok(self
            .owners_by_identity
            .get(row.agent)
            .and_then(|sessions| sessions.get(row.session))
            .copied())
    }

    fn event_is_window_excluded(&self, owner_ordinal: usize, ts: i64) -> bool {
        let Some(window) = self.request.caller_event_window.as_ref() else {
            return false;
        };
        if self.owners[owner_ordinal].session != window.session {
            return false;
        }
        let Some(first) = window.marks.first() else {
            return 0 >= window.boundary;
        };
        let turn = if ts == 0 {
            first.turn
        } else {
            let next = window.marks.partition_point(|mark| mark.ts <= ts);
            window.marks[next.saturating_sub(1)].turn
        };
        turn >= window.boundary
    }

    fn retain_match(&mut self, observation: CandidateObservation) {
        let CandidateObservation {
            owner_ordinal,
            ts,
            lane,
            occurrences,
            first_phrase_span,
            boundary_factors,
            ordinal,
            event_ordinal,
            refined_score,
        } = observation;
        let (lower_score, upper_score, refined_score) = match refined_score {
            Some((lower, upper)) => (lower, upper, true),
            None => {
                let (lower, upper) = self.score_bounds_for(
                    owner_ordinal,
                    ts,
                    lane,
                    occurrences,
                    first_phrase_span,
                    boundary_factors,
                );
                (lower, upper, false)
            }
        };
        let owner = &self.owners[owner_ordinal];
        let mut candidate = Candidate {
            agent: owner.agent.clone(),
            session: owner.session.clone(),
            ordinal,
            event_ordinal,
            ts,
            matched: lane,
            occurrences,
            upper_score,
            lower_score,
            refined_score,
            estimated_bytes: 0,
        };
        let estimated_bytes = candidate
            .agent
            .len()
            .saturating_add(candidate.session.len())
            .saturating_add(384);
        candidate.estimated_bytes = estimated_bytes;
        let cursor = candidate_cursor(&candidate);
        if self
            .request
            .after
            .as_ref()
            .is_some_and(|after| envelope_cursor_cmp(&cursor, after) != Ordering::Greater)
        {
            return;
        }
        let omitted =
            if self.candidates.len() < self.request.candidate_limit {
                self.candidate_bytes = self.candidate_bytes.saturating_add(estimated_bytes);
                self.candidates.push(EnvelopeCandidate(candidate));
                None
            } else if self.candidates.peek().is_some_and(|worst| {
                optimistic_candidate_cmp(&candidate, &worst.0) == Ordering::Less
            }) {
                let removed = self.candidates.pop().unwrap().0;
                self.candidate_bytes = self
                    .candidate_bytes
                    .saturating_sub(removed.estimated_bytes)
                    .saturating_add(estimated_bytes);
                self.candidates.push(EnvelopeCandidate(candidate));
                Some(removed)
            } else {
                Some(candidate)
            };
        if let Some(omitted) = omitted.as_ref() {
            self.note_omitted(omitted);
        }
        while self.candidate_bytes > self.candidate_bytes_limit && self.candidates.len() > 1 {
            let removed = self.candidates.pop().unwrap().0;
            self.candidate_bytes = self.candidate_bytes.saturating_sub(removed.estimated_bytes);
            self.note_omitted(&removed);
        }
    }

    fn record_match(&mut self, owner_ordinal: usize, lane: MatchLane, all_terms: bool) -> u64 {
        self.match_count = self.match_count.saturating_add(1);
        let ordinal = self.match_count - 1;
        self.matched_owner_bits[owner_ordinal / 8] |= 1 << (owner_ordinal % 8);
        if lane == MatchLane::Phrase {
            self.phrase_owner_bits[owner_ordinal / 8] |= 1 << (owner_ordinal % 8);
            self.phrase_count = self.phrase_count.saturating_add(1);
        }
        if all_terms {
            self.all_terms_count = self.all_terms_count.saturating_add(1);
            if lane == MatchLane::AllTerms {
                self.all_terms_additions = self.all_terms_additions.saturating_add(1);
            }
        }
        ordinal
    }

    fn score_bounds_for(
        &self,
        _owner_ordinal: usize,
        ts: i64,
        lane: MatchLane,
        occurrences: u64,
        first_phrase_span: Option<usize>,
        boundary_factors: (f64, f64),
    ) -> (f64, f64) {
        let inputs = ScoreInputs {
            ts,
            meta_scale: 1.0,
            now_ms: self.request.now_ms,
            contract: self.request.score_contract,
        };
        if lane == MatchLane::Phrase {
            score_bounds(
                inputs,
                occurrences.max(1),
                first_phrase_span,
                self.query.min_phrase_len(),
                boundary_factors,
            )
        } else {
            all_terms_score_bounds(inputs, boundary_factors)
        }
    }

    fn exact_event_score(
        &self,
        owner_ordinal: usize,
        event: &EventView<'_>,
        ts: i64,
        lane: MatchLane,
    ) -> Option<(f64, f64)> {
        let rendered = canonical_text(event)?;
        self.exact_rendered_score(owner_ordinal, &rendered, ts, lane)
    }

    fn exact_rendered_score(
        &self,
        _owner_ordinal: usize,
        rendered: &CanonicalText,
        ts: i64,
        lane: MatchLane,
    ) -> Option<(f64, f64)> {
        if rendered.unsupported_whitespace {
            return None;
        }
        exact_rank_score(
            rendered,
            &self.query,
            &self.boundary,
            lane,
            ScoreInputs {
                ts,
                meta_scale: 1.0,
                now_ms: self.request.now_ms,
                contract: self.request.score_contract,
            },
        )
    }

    fn note_omitted(&mut self, candidate: &Candidate) {
        let value = candidate_cursor(candidate);
        if self
            .best_omitted
            .as_ref()
            .is_none_or(|current| envelope_cursor_cmp(&value, current) == Ordering::Less)
        {
            self.best_omitted = Some(value);
        }
    }

    fn match_totals(&self) -> MatchTotals {
        let matched_sessions = self
            .matched_owner_bits
            .iter()
            .map(|byte| byte.count_ones() as u64)
            .sum();
        let mut matched_owner_bitmap = String::with_capacity(self.matched_owner_bits.len() * 2);
        for byte in &self.matched_owner_bits {
            write!(&mut matched_owner_bitmap, "{byte:02x}").expect("writing to String cannot fail");
        }
        let mut phrase_owner_bitmap = String::with_capacity(self.phrase_owner_bits.len() * 2);
        for byte in &self.phrase_owner_bits {
            write!(&mut phrase_owner_bitmap, "{byte:02x}").expect("writing to String cannot fail");
        }
        let mut owner_order = Sha256::new();
        for owner in &self.owners {
            for value in [&owner.agent, &owner.session, &owner.project] {
                owner_order.update((value.len() as u64).to_le_bytes());
                owner_order.update(value.as_bytes());
            }
        }
        let owner_order_sha256 = owner_order
            .finalize()
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        MatchTotals {
            tools: self.match_count,
            phrase_tools: self.phrase_count,
            all_terms_tools: self.all_terms_count,
            all_terms_additions: self.all_terms_additions,
            matched_sessions,
            eligible_sessions: self.owners.len() as u64,
            matched_owner_bitmap,
            phrase_owner_bitmap,
            owner_order_sha256,
        }
    }
}

pub fn run_verified_scan(data_dir: &Path, mut request: ScanRequest) -> ScanResponse {
    let expected_ingest = request.expected_ingest_generation.clone();
    let expected_event = request.expected_event_generation.clone();
    if let Err(error) = validate_request(&request) {
        return failure_response(
            ScanState::Unsupported,
            error.to_string(),
            expected_ingest,
            expected_event,
        );
    }
    let ingest_before = match ingest_generation(data_dir) {
        Ok(generation) if generation == expected_ingest => generation,
        Ok(_) => {
            return failure_response(
                ScanState::GenerationMoved,
                "transcript generation moved before the event scan",
                expected_ingest,
                expected_event,
            )
        }
        Err(detail) => {
            return failure_response(
                ScanState::IntegrityError,
                detail,
                expected_ingest,
                expected_event,
            )
        }
    };
    let mut published_census = None;
    let session_snapshot = if request.eligibility == EligibilityMode::PublishedSessions {
        match published_session_owners(data_dir, &request.owner_filter) {
            Ok((owners, census, snapshot)) => {
                request.eligible_sessions = owners;
                published_census = Some(census);
                Some(snapshot)
            }
            Err(detail) => {
                return failure_response(
                    ScanState::Unsupported,
                    detail,
                    ingest_before,
                    expected_event,
                )
            }
        }
    } else {
        None
    };
    request
        .eligible_sessions
        .sort_by(|left, right| (&left.session, &left.agent).cmp(&(&right.session, &right.agent)));
    if request
        .eligible_sessions
        .windows(2)
        .any(|pair| pair[0].session == pair[1].session)
    {
        return failure_response(
            ScanState::Unsupported,
            "eligible sessions contain duplicate owners",
            ingest_before,
            expected_event,
        );
    }
    request
        .eligible_sessions
        .sort_by(|left, right| (&left.agent, &left.session).cmp(&(&right.agent, &right.session)));
    let agents: Vec<String> = request
        .eligible_sessions
        .iter()
        .map(|owner| owner.agent.clone())
        .collect::<HashSet<_>>()
        .into_iter()
        .collect();
    let agent_refs: Vec<_> = agents.iter().map(String::as_str).collect();
    let mut scanner =
        match Scanner::with_candidate_limit(request, CANDIDATE_BYTES_LIMIT, published_census) {
            Ok(scanner) => scanner,
            Err(error) => {
                return failure_response(
                    ScanState::Unsupported,
                    error.to_string(),
                    expected_ingest,
                    expected_event,
                )
            }
        };
    if agent_refs.is_empty() {
        let event_path = data_dir.join("events").join(".generation");
        let event_before = crate::ingest::registry::regular_file_edge_snapshot(&event_path, 512);
        let event_body = crate::ingest::registry::read_bounded_regular_file(&event_path, 4096);
        let event_after = crate::ingest::registry::regular_file_edge_snapshot(&event_path, 512);
        let event_pinned = matches!(
            (event_before, event_body, event_after),
            (Ok(Some(before)), Ok(Some(body)), Ok(Some(after)))
                if before == after && body == expected_event.as_bytes()
        );
        let sessions_pinned = session_snapshot.as_ref().is_none_or(|before| {
            matches!(
                crate::ingest::registry::regular_file_edge_snapshot(
                    &data_dir.join("sessions.jsonl"),
                    512,
                ),
                Ok(Some(ref observed)) if observed == before
            )
        });
        let ingest_pinned = matches!(
            ingest_generation(data_dir),
            Ok(ref generation) if generation == &ingest_before
        );
        if !event_pinned || !sessions_pinned || !ingest_pinned {
            return failure_response(
                ScanState::GenerationMoved,
                "published generations moved during the empty event scan",
                ingest_before,
                expected_event,
            );
        }
        return scanner.finish(ingest_before, expected_event);
    }
    let verified = crate::cache::scan_verified_event_generation_candidates(
        &data_dir.join("events"),
        expected_event.as_bytes(),
        &agent_refs,
        |row| {
            scanner.visit(EventSession {
                name: row.name,
                agent: row.agent,
                session: row.session,
                n_events: row.n_events,
                payload: row.payload,
            });
        },
    );
    let summary = match verified {
        Ok(summary) => summary,
        Err(error) => {
            let state = match error.kind {
                crate::cache::VerifiedEventScanFailure::MissingOrUnsupported => {
                    ScanState::Unsupported
                }
                crate::cache::VerifiedEventScanFailure::GenerationMoved => {
                    ScanState::GenerationMoved
                }
                crate::cache::VerifiedEventScanFailure::Integrity => ScanState::IntegrityError,
            };
            return failure_response(state, error.detail, ingest_before, expected_event);
        }
    };
    if let Some(before) = session_snapshot {
        let after = crate::ingest::registry::regular_file_edge_snapshot(
            &data_dir.join("sessions.jsonl"),
            512,
        );
        if !matches!(after, Ok(Some(ref observed)) if observed == &before) {
            return failure_response(
                ScanState::GenerationMoved,
                "published session metadata moved during the event scan",
                ingest_before,
                expected_event,
            );
        }
    }
    let ingest_after = match ingest_generation(data_dir) {
        Ok(generation) if generation == ingest_before => generation,
        Ok(_) => {
            return failure_response(
                ScanState::GenerationMoved,
                "transcript generation moved during the event scan",
                ingest_before,
                expected_event,
            )
        }
        Err(detail) => {
            return failure_response(
                ScanState::IntegrityError,
                detail,
                ingest_before,
                expected_event,
            )
        }
    };
    let event_generation = match String::from_utf8(summary.generation) {
        Ok(generation) => generation,
        Err(_) => {
            return failure_response(
                ScanState::IntegrityError,
                "event generation is not UTF-8",
                ingest_after,
                expected_event,
            )
        }
    };
    let response = scanner.finish(ingest_after.clone(), event_generation);
    if response.scanned.sessions != summary.sessions
        || response.scanned.events != summary.events
        || response.scanned.bytes != summary.bytes
    {
        return failure_response(
            ScanState::IntegrityError,
            "event verifier and matcher scan totals disagree",
            ingest_after,
            expected_event,
        );
    }
    response
}

#[derive(Deserialize)]
struct PublishedSessionOwner {
    session: String,
    agent: String,
    #[serde(default)]
    project: String,
}

#[derive(Deserialize, Eq, PartialEq)]
#[serde(deny_unknown_fields)]
struct DerivedFileProof {
    name: String,
    len: u64,
    modified_ns: u64,
    change_token: crate::ingest::registry::ChangeToken,
    edge_hash: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DerivedProof {
    version: u32,
    signature: String,
    files: Vec<DerivedFileProof>,
}

fn session_file_proof(
    snapshot: &crate::ingest::registry::RegularFileEdgeSnapshot,
) -> Result<DerivedFileProof, String> {
    let modified_ns = snapshot
        .modified
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("published sessions have an invalid timestamp: {error}"))?
        .as_nanos()
        .min(u64::MAX as u128) as u64;
    let mut edge_hash = 0xcbf29ce484222325_u64;
    for byte in snapshot
        .len
        .to_le_bytes()
        .iter()
        .chain(&snapshot.head)
        .chain(&snapshot.tail)
    {
        edge_hash ^= *byte as u64;
        edge_hash = edge_hash.wrapping_mul(0x100000001b3);
    }
    Ok(DerivedFileProof {
        name: "sessions.jsonl".into(),
        len: snapshot.len,
        modified_ns,
        change_token: snapshot.change_token.clone(),
        edge_hash,
    })
}

fn expected_session_file_proof(data_dir: &Path) -> Result<DerivedFileProof, String> {
    let signature = crate::ingest::registry::read_bounded_regular_file(
        &data_dir.join(".ingest.sig"),
        INGEST_GENERATION_MAX_BYTES,
    )
    .map_err(|error| format!("transcript generation cannot be read: {error}"))?
    .ok_or_else(|| "transcript generation is missing".to_string())?;
    let signature = std::str::from_utf8(&signature)
        .map_err(|_| "transcript generation is not UTF-8".to_string())?
        .trim();
    let body = crate::ingest::registry::read_bounded_regular_file(
        &data_dir.join(".derived_generation.json"),
        DERIVED_PROOF_MAX_BYTES,
    )
    .map_err(|error| format!("derived generation proof cannot be read: {error}"))?
    .ok_or_else(|| "derived generation proof is missing".to_string())?;
    let proof: DerivedProof = serde_json::from_slice(&body)
        .map_err(|error| format!("derived generation proof is invalid: {error}"))?;
    let expected_names = [
        "messages.jsonl",
        "replies.jsonl",
        "sessions.jsonl",
        crate::cache::SESSION_FAMILY_META_FILE,
        crate::boundary_stats::FILE_NAME,
        crate::boundary_stats::CACHE_FILE_NAME,
        "event_stats.json",
    ];
    let names: HashSet<_> = proof.files.iter().map(|file| file.name.as_str()).collect();
    if proof.version != 6
        || proof.signature != signature
        || proof.files.len() != expected_names.len()
        || names.len() != expected_names.len()
        || expected_names.iter().any(|name| !names.contains(name))
    {
        return Err("derived generation proof does not authorize published sessions".into());
    }
    proof
        .files
        .into_iter()
        .find(|file| file.name == "sessions.jsonl")
        .ok_or_else(|| "derived generation proof omits published sessions".into())
}

fn published_session_owners(
    data_dir: &Path,
    filter: &PublishedOwnerFilter,
) -> Result<
    (
        Vec<SessionOwner>,
        Vec<SessionOwner>,
        crate::ingest::registry::RegularFileEdgeSnapshot,
    ),
    String,
> {
    let path = data_dir.join("sessions.jsonl");
    let expected_proof = expected_session_file_proof(data_dir)?;
    let before = crate::ingest::registry::regular_file_edge_snapshot(&path, 512)
        .map_err(|error| format!("published sessions cannot be inspected: {error}"))?
        .ok_or_else(|| "published sessions are missing".to_string())?;
    if session_file_proof(&before)? != expected_proof {
        return Err("published sessions do not match the committed generation".into());
    }
    if before.len > SESSION_FILE_MAX_BYTES {
        return Err("published sessions exceed the native owner limit".into());
    }
    let agent = filter.agent_contains.to_lowercase();
    let project = filter.project_contains.to_lowercase();
    let chat = filter.chat_prefix.to_lowercase();
    let excluded: HashSet<_> = filter
        .excluded_sessions
        .iter()
        .map(String::as_str)
        .collect();
    let owners = crate::ingest::registry::with_regular_file_snapshot(&path, |file| {
        let mut reader = std::io::BufReader::new(file);
        let mut line = Vec::new();
        let mut owners = Vec::new();
        loop {
            line.clear();
            let read = reader.read_until(b'\n', &mut line)?;
            if read == 0 {
                break;
            }
            if line.len() > SESSION_ROW_MAX_BYTES {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "published session row exceeds its size limit",
                ));
            }
            if line.iter().all(u8::is_ascii_whitespace) {
                continue;
            }
            let row: PublishedSessionOwner = serde_json::from_slice(&line)
                .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))?;
            if row.session.is_empty()
                || row.session.len() > SESSION_ID_MAX_BYTES
                || row.agent.is_empty()
                || row.agent.len() > SESSION_AGENT_MAX_BYTES
            {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "published session owner is empty",
                ));
            }
            owners.push(SessionOwner {
                agent: row.agent,
                session: row.session,
                project: row.project,
            });
            if owners.len() > SESSION_OWNER_MAX {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::InvalidData,
                    "published sessions exceed the native owner count limit",
                ));
            }
        }
        Ok(owners)
    })
    .map_err(|error| format!("published sessions cannot be read: {error}"))?
    .ok_or_else(|| "published sessions are missing".to_string())?;
    let after = crate::ingest::registry::regular_file_edge_snapshot(&path, 512)
        .map_err(|error| format!("published sessions cannot be confirmed: {error}"))?
        .ok_or_else(|| "published sessions disappeared".to_string())?;
    if after != before {
        return Err("published sessions moved while deriving event owners".into());
    }
    if session_file_proof(&after)? != expected_proof {
        return Err("published sessions left the committed generation".into());
    }
    let mut all_owners = owners;
    all_owners
        .sort_by(|left, right| (&left.session, &left.agent).cmp(&(&right.session, &right.agent)));
    if all_owners
        .windows(2)
        .any(|pair| pair[0].session == pair[1].session)
    {
        return Err("published sessions contain duplicate owners".into());
    }
    let mut eligible: Vec<_> = all_owners
        .iter()
        .filter(|owner| {
            !excluded.contains(owner.session.as_str())
                && (chat.is_empty() || owner.session.to_lowercase().starts_with(&chat))
                && (agent.is_empty() || owner.agent.to_lowercase().contains(&agent))
                && (project.is_empty() || owner.project.to_lowercase().contains(&project))
        })
        .cloned()
        .collect();
    all_owners
        .sort_by(|left, right| (&left.agent, &left.session).cmp(&(&right.agent, &right.session)));
    eligible
        .sort_by(|left, right| (&left.agent, &left.session).cmp(&(&right.agent, &right.session)));
    Ok((eligible, all_owners, before))
}

fn ingest_generation(data_dir: &Path) -> Result<String, String> {
    let path = data_dir.join(".ingest.sig");
    let body =
        crate::ingest::registry::read_bounded_regular_file(&path, INGEST_GENERATION_MAX_BYTES)
            .map_err(|error| format!("transcript generation cannot be read: {error}"))?
            .ok_or_else(|| "transcript generation is missing".to_string())?;
    let digest = Sha256::digest(body);
    let mut rendered = String::with_capacity(digest.len() * 2);
    for byte in digest {
        write!(&mut rendered, "{byte:02x}").expect("writing to String cannot fail");
    }
    Ok(rendered)
}

pub fn failure_response(
    state: ScanState,
    detail: impl Into<String>,
    ingest_generation: String,
    event_generation: String,
) -> ScanResponse {
    debug_assert!(state != ScanState::Ok);
    ScanResponse {
        protocol: PROTOCOL_VERSION,
        state,
        ingest_generation,
        event_generation,
        scanned: ScanTotals::default(),
        matches: MatchTotals::default(),
        candidates: Vec::new(),
        best_omitted: None,
        next_after: None,
        envelope_complete: false,
        detail: Some(detail.into()),
    }
}

fn validate_request(request: &ScanRequest) -> Result<(), RequestError> {
    let query = request.query.trim();
    let tokens = keyword_tokens(query);
    if request.protocol != PROTOCOL_VERSION {
        return Err(RequestError("unsupported fallback-scan protocol"));
    }
    if !query.is_ascii()
        || query.bytes().any(|byte| {
            !byte.is_ascii_alphanumeric()
                && !byte.is_ascii_whitespace()
                && !matches!(byte, b'-' | b'_')
        })
        || tokens.is_empty()
        || tokens.len() > 16
        || (tokens.len() == 1 && tokens[0].len() < 3)
    {
        return Err(RequestError(
            "fallback-scan requires supported ASCII keyword tokens",
        ));
    }
    if request.limit == 0 || request.limit > MAX_RESULTS {
        return Err(RequestError(
            "fallback-scan limit is outside its exact frontier",
        ));
    }
    if request.candidate_limit == 0 || request.candidate_limit > MAX_CANDIDATE_LIMIT {
        return Err(RequestError(
            "fallback-scan candidate limit is outside its bounded page",
        ));
    }
    if request.after.as_ref().is_some_and(|cursor| {
        !cursor.upper_score.is_finite()
            || !(0.0..=1.0).contains(&cursor.upper_score)
            || cursor.session.is_empty()
            || cursor.session.len() > SESSION_ID_MAX_BYTES
    }) {
        return Err(RequestError("fallback-scan continuation is invalid"));
    }
    if !request.now_ms.is_finite() {
        return Err(RequestError("fallback-scan now_ms must be finite"));
    }
    if request.expected_ingest_generation.is_empty() || request.expected_event_generation.is_empty()
    {
        return Err(RequestError("fallback-scan requires pinned generations"));
    }
    if request
        .since_ms
        .zip(request.until_ms)
        .is_some_and(|(since, until)| since > until)
    {
        return Err(RequestError("fallback-scan time range is inverted"));
    }
    if request
        .caller_event_window
        .as_ref()
        .is_some_and(|window| window.session.is_empty() || window.marks.len() > 1_000_000)
    {
        return Err(RequestError("fallback-scan caller window is invalid"));
    }
    if !score_contract_matches(request.score_contract) {
        return Err(RequestError(
            "fallback-scan score contract does not match this binary",
        ));
    }
    Ok(())
}

fn keyword_tokens(query: &str) -> Vec<&str> {
    query
        .trim()
        .split(|character: char| character.is_ascii_whitespace() || matches!(character, '-' | '_'))
        .filter(|token| !token.is_empty())
        .collect()
}

fn score_contract_matches(contract: ScoreContract) -> bool {
    let expected = ScoreContract::default();
    [
        (contract.half_life_days, expected.half_life_days),
        (contract.who_tool, expected.who_tool),
        (contract.source_tool, expected.source_tool),
        (contract.meta_min, expected.meta_min),
        (contract.boundary_min, expected.boundary_min),
    ]
    .into_iter()
    .all(|(actual, expected)| actual.is_finite() && (actual - expected).abs() <= 1e-12)
}

fn optimistic_candidate_cmp(left: &Candidate, right: &Candidate) -> Ordering {
    lane_order(left.matched)
        .cmp(&lane_order(right.matched))
        .then_with(|| right.upper_score.total_cmp(&left.upper_score))
        .then_with(|| right.ts.cmp(&left.ts))
        .then_with(|| left.session.cmp(&right.session))
        .then_with(|| left.ordinal.cmp(&right.ordinal))
}

fn envelope_cursor_cmp(left: &EnvelopeCursor, right: &EnvelopeCursor) -> Ordering {
    lane_order(left.matched)
        .cmp(&lane_order(right.matched))
        .then_with(|| right.upper_score.total_cmp(&left.upper_score))
        .then_with(|| right.ts.cmp(&left.ts))
        .then_with(|| left.session.cmp(&right.session))
        .then_with(|| left.ordinal.cmp(&right.ordinal))
}

fn lane_order(lane: MatchLane) -> u8 {
    match lane {
        MatchLane::Phrase => 0,
        MatchLane::AllTerms => 1,
    }
}

fn candidate_cursor(candidate: &Candidate) -> EnvelopeCursor {
    EnvelopeCursor {
        matched: candidate.matched,
        upper_score: candidate.upper_score,
        ts: candidate.ts,
        session: candidate.session.clone(),
        ordinal: candidate.ordinal,
    }
}

fn score_bounds(
    inputs: ScoreInputs,
    occurrences: u64,
    first_span: Option<usize>,
    min_phrase_len: usize,
    boundary_factors: (f64, f64),
) -> (f64, f64) {
    let age_days = (inputs.now_ms - inputs.ts as f64).max(0.0) / 86_400_000.0;
    let recency = 0.5f64.powf(age_days / inputs.contract.half_life_days);
    let common = recency * inputs.contract.who_tool * inputs.contract.source_tool;
    let tightness = first_span
        .filter(|span| *span > 0)
        .map_or(0.0, |span| (min_phrase_len as f64 / span as f64).min(1.0));
    let lower = 0.5 * tightness * common * inputs.meta_scale * boundary_factors.0;
    let match_upper = 1.0 - 0.5f64.powf(occurrences.max(1) as f64);
    let upper = match_upper * common * inputs.meta_scale * boundary_factors.1;
    (round_down_score(lower), round_up_score(upper))
}

fn all_terms_score_bounds(inputs: ScoreInputs, boundary_factors: (f64, f64)) -> (f64, f64) {
    let age_days = (inputs.now_ms - inputs.ts as f64).max(0.0) / 86_400_000.0;
    let recency = 0.5f64.powf(age_days / inputs.contract.half_life_days);
    let common =
        recency * inputs.contract.who_tool * inputs.contract.source_tool * inputs.meta_scale;
    let lower = 0.0;
    let upper = common * boundary_factors.1;
    (round_down_score(lower), round_up_score(upper))
}

fn round_up_score(score: f64) -> f64 {
    ((score * SCORE_DIGITS + 0.5 + 1e-9).floor() / SCORE_DIGITS).clamp(0.0, 1.0)
}

fn round_down_score(score: f64) -> f64 {
    ((score * SCORE_DIGITS).floor() / SCORE_DIGITS).clamp(0.0, 1.0)
}

fn canonical_occurrences(event: &EventView<'_>, matcher: &LiteralMatcher) -> usize {
    let kind = event.kind.get();
    if kind == "control" {
        return 0;
    }
    let mut count = matcher.count(event.name.get().trim());
    if kind == "subagent_start" {
        count = count.saturating_add(matcher.count("subagent"));
    } else if kind == "subagent_result" {
        count = count.saturating_add(matcher.count("subagent result"));
    }
    if event.failed() {
        count = count.saturating_add(matcher.count("failed"));
    }
    count
        .saturating_add(matcher.count(event.input.get().trim()))
        .saturating_add(matcher.count(event.output.get()))
}

struct CanonicalText {
    text: String,
    output_bounds: Option<(usize, usize)>,
    unsupported_whitespace: bool,
}

fn canonical_text(event: &EventView<'_>) -> Option<CanonicalText> {
    let kind = event.kind.get();
    if kind == "control" {
        return None;
    }
    let unsupported_whitespace = [
        event.kind.get(),
        event.name.get(),
        event.input.get(),
        event.output.get(),
    ]
    .into_iter()
    .flat_map(str::chars)
    .any(|character| !character.is_ascii() && character.is_whitespace());
    let name = event.name.get().trim();
    let mut head = match kind {
        "subagent_start" if name.is_empty() => "subagent".to_string(),
        "subagent_start" => format!("subagent {name}"),
        "subagent_result" if name.is_empty() => "subagent result".to_string(),
        "subagent_result" => format!("subagent result {name}"),
        _ => name.to_string(),
    };
    if event.failed() {
        head.push_str(" [failed]");
    }
    let input = event.input.get().trim();
    let mut text = match (head.is_empty(), input.is_empty()) {
        (false, false) => format!("{head}: {input}"),
        (false, true) => head,
        (true, false) => input.to_string(),
        (true, true) => String::new(),
    };
    let output = event.output.get();
    if output.is_empty() {
        return Some(CanonicalText {
            text,
            output_bounds: None,
            unsupported_whitespace,
        });
    }
    let output_start_untrimmed = text.chars().count().saturating_add(1);
    text.push('\n');
    text.push_str(output);
    let leading = text
        .chars()
        .take_while(|character| character.is_whitespace())
        .count();
    let trailing = text
        .chars()
        .rev()
        .take_while(|character| character.is_whitespace())
        .count();
    let end = text.chars().count().saturating_sub(trailing);
    let rendered: String = text
        .chars()
        .skip(leading)
        .take(end.saturating_sub(leading))
        .collect();
    let output_start = output_start_untrimmed.max(leading).saturating_sub(leading);
    let output_end = end.saturating_sub(leading);
    Some(CanonicalText {
        text: rendered,
        output_bounds: (output_start < output_end).then_some((output_start, output_end)),
        unsupported_whitespace,
    })
}

#[derive(Clone, Debug)]
struct PhraseOccurrence {
    start: usize,
    end: usize,
    spans: Vec<Option<crate::boundary_rank::Span>>,
}

fn exact_rank_score(
    rendered: &CanonicalText,
    query: &QueryPlan,
    boundary: &crate::boundary_rank::PreparedQuery,
    lane: MatchLane,
    inputs: ScoreInputs,
) -> Option<(f64, f64)> {
    let tokens: Vec<&[u8]> = match query {
        QueryPlan::Single(matcher) => vec![matcher.query.as_slice()],
        QueryPlan::Multi { phrase, .. } => phrase.iter().map(Vec::as_slice).collect(),
    };
    let snippet = exact_snippet(rendered, query, lane, &tokens)?;
    let phrase = phrase_occurrences(&snippet, &tokens);
    let qlen = query.min_phrase_len();
    let mut strength = phrase_strength(&phrase, qlen);
    if lane == MatchLane::AllTerms {
        let terms = match query {
            QueryPlan::Multi { terms, .. } => terms,
            QueryPlan::Single(_) => return None,
        };
        strength = strength.max(terms_proximity(&snippet, terms, qlen));
    }
    let boundary_factor = match lane {
        MatchLane::Phrase => {
            let decut = crate::boundary_rank::decut_text(&snippet, false);
            phrase
                .iter()
                .filter_map(|found| {
                    boundary
                        .evaluate(&decut, Some(&found.spans), false)
                        .ok()
                        .map(|score| (score.factor, found.end - found.start))
                })
                .max_by(|left, right| {
                    left.0
                        .total_cmp(&right.0)
                        .then_with(|| right.1.cmp(&left.1))
                })?
                .0
        }
        MatchLane::AllTerms => {
            let decut = crate::boundary_rank::decut_text(&snippet, true);
            boundary.evaluate(&decut, None, true).ok()?.factor
        }
    };
    let age_days = (inputs.now_ms - inputs.ts as f64).max(0.0) / 86_400_000.0;
    let recency = 0.5f64.powf(age_days / inputs.contract.half_life_days);
    let score = strength
        * recency
        * inputs.contract.who_tool
        * boundary_factor
        * inputs.contract.source_tool
        * inputs.meta_scale;
    Some(refined_score_interval(score))
}

fn refined_score_interval(score: f64) -> (f64, f64) {
    let scaled = score * SCORE_DIGITS;
    let floor = scaled.floor();
    let lower = (floor / SCORE_DIGITS).clamp(0.0, 1.0);
    let upper = (scaled.ceil() / SCORE_DIGITS).clamp(0.0, 1.0);
    if lower == upper || (scaled - floor - 0.5).abs() <= REFINED_ROUNDING_GUARD {
        return (lower, upper);
    }
    let rounded = if scaled - floor < 0.5 { lower } else { upper };
    (rounded, rounded)
}

fn exact_snippet(
    rendered: &CanonicalText,
    query: &QueryPlan,
    lane: MatchLane,
    tokens: &[&[u8]],
) -> Option<String> {
    let snippet = match lane {
        MatchLane::Phrase => {
            let first = first_phrase_occurrence(&rendered.text, tokens)?;
            payload_snip_supported(
                &rendered.text,
                first.start,
                first.end,
                rendered.output_bounds,
            )?
        }
        MatchLane::AllTerms => {
            let terms = match query {
                QueryPlan::Multi { terms, .. } => terms,
                QueryPlan::Single(_) => return None,
            };
            let spans: Option<Vec<_>> = terms
                .iter()
                .map(|term| {
                    term.first_span(&rendered.text)
                        .map(|[start, end]| (start as usize, end as usize))
                })
                .collect();
            snip_spans_supported(&rendered.text, &spans?)?
        }
    };
    Some(snippet)
}

fn phrase_strength(found: &[PhraseOccurrence], qlen: usize) -> f64 {
    let Some(best) = found.iter().map(|item| item.end - item.start).min() else {
        return 0.0;
    };
    let tightness = if qlen > 0 && best > 0 {
        (qlen as f64 / best as f64).min(1.0)
    } else {
        0.0
    };
    tightness * (1.0 - 0.5f64.powi(found.len().min(i32::MAX as usize) as i32))
}

fn terms_proximity(snippet: &str, terms: &[LiteralMatcher], qlen: usize) -> f64 {
    let mut found = 0usize;
    let mut first = 0usize;
    let mut last = 0usize;
    for term in terms {
        let Some([start, end]) = term.first_span(snippet) else {
            continue;
        };
        let (start, end) = (start as usize, end as usize);
        if found == 0 {
            first = start;
            last = end;
        } else {
            first = first.min(start);
            last = last.max(end);
        }
        found += 1;
    }
    let fraction = found as f64 / terms.len() as f64;
    if found < 2 {
        return fraction;
    }
    let cuts = snippet
        .chars()
        .skip(first)
        .take(last.saturating_sub(first))
        .filter(|character| *character == '…')
        .count();
    let spread = last.saturating_sub(first).saturating_add(80 * cuts);
    fraction * (0.5 + 0.5 * (qlen as f64 / spread.max(1) as f64).min(1.0))
}

fn first_phrase_occurrence(text: &str, tokens: &[&[u8]]) -> Option<PhraseOccurrence> {
    let units = phrase_units(text);
    next_phrase_occurrence(&units, tokens, 0)
}

fn phrase_occurrences(text: &str, tokens: &[&[u8]]) -> Vec<PhraseOccurrence> {
    let units = phrase_units(text);
    let mut out = Vec::new();
    let mut offset = 0;
    while let Some(found) = next_phrase_occurrence(&units, tokens, offset) {
        offset = found.end;
        out.push(found);
    }
    out
}

fn phrase_units(text: &str) -> Vec<PhraseUnit> {
    text.chars()
        .map(|character| PhraseUnit {
            folded: python_regex_ascii_fold(character),
            bridge: !character.is_alphanumeric(),
        })
        .collect()
}

fn next_phrase_occurrence(
    units: &[PhraseUnit],
    tokens: &[&[u8]],
    offset: usize,
) -> Option<PhraseOccurrence> {
    for start in offset..units.len() {
        let mut cursor = start;
        let mut spans = Vec::with_capacity(tokens.len());
        let mut matched = true;
        for (index, token) in tokens.iter().enumerate() {
            if index > 0 {
                while cursor < units.len() && units[cursor].bridge {
                    cursor += 1;
                }
            }
            let token_start = cursor;
            if cursor + token.len() > units.len()
                || !units[cursor..cursor + token.len()]
                    .iter()
                    .zip(*token)
                    .all(|(unit, byte)| unit.folded == *byte)
            {
                matched = false;
                break;
            }
            cursor += token.len();
            spans.push(Some([token_start as i64, cursor as i64]));
        }
        if matched {
            return Some(PhraseOccurrence {
                start,
                end: cursor,
                spans,
            });
        }
    }
    None
}

fn snip_at_supported(text: &str, start: usize, end: usize) -> Option<String> {
    let text_len = text.chars().count();
    let a = start.saturating_sub(80);
    let b = end.saturating_add(80).min(text_len);
    let mut value = String::new();
    if a > 0 {
        value.push('…');
    }
    value.push_str(&slice_chars(text, a, b)?);
    if b < text_len {
        value.push('…');
    }
    let rendered = one_line_supported(&value)?;
    supported_render(&rendered).then_some(rendered)
}

fn snip_spans_supported(text: &str, spans: &[(usize, usize)]) -> Option<String> {
    let text_len = text.chars().count();
    let mut windows: Vec<(usize, usize)> = Vec::new();
    let mut ordered = spans.to_vec();
    ordered.sort_unstable();
    for (start, end) in ordered {
        let a = start.saturating_sub(80);
        let b = end.saturating_add(80).min(text_len);
        if let Some(last) = windows.last_mut().filter(|last| a <= last.1) {
            last.1 = last.1.max(b);
        } else {
            windows.push((a, b));
        }
    }
    let mut parts = Vec::with_capacity(windows.len());
    for (a, b) in windows {
        let mut value = String::new();
        if a > 0 {
            value.push('…');
        }
        value.push_str(&slice_chars(text, a, b)?);
        if b < text_len {
            value.push('…');
        }
        let rendered = one_line_supported(&value)?;
        if !supported_render(&rendered) {
            return None;
        }
        if !rendered.is_empty() {
            parts.push(rendered);
        }
    }
    Some(parts.join(" "))
}

fn payload_snip_supported(
    text: &str,
    start: usize,
    end: usize,
    payload_bounds: Option<(usize, usize)>,
) -> Option<String> {
    let Some((payload_start, payload_end)) = payload_bounds else {
        return snip_at_supported(text, start, end);
    };
    let text_len = text.chars().count();
    let (left, source_end, source, local_start, local_end) = if payload_start <= start {
        (
            payload_start,
            payload_end,
            slice_chars(text, payload_start, payload_end)?,
            start - payload_start,
            end - payload_start,
        )
    } else {
        let payload = one_line_supported(&slice_chars(text, payload_start, payload_end)?)?;
        let mut source = slice_chars(text, start, end)?;
        if !payload.is_empty() {
            source.push_str(" · ");
            source.push_str(&payload);
        }
        (start, payload_end, source, 0, end - start)
    };
    let (source, local_start, local_end) =
        one_line_with_span_supported(&source, local_start, local_end)?;
    let budget = local_end.saturating_sub(local_start).saturating_add(160);
    let mut a = local_start.saturating_sub(80);
    let source_len = source.chars().count();
    let mut b = local_end.saturating_add(80).min(source_len);
    let mut missing = budget.saturating_sub(b - a);
    if missing > 0 {
        let take = missing.min(source_len - b);
        b += take;
        missing -= take;
    }
    if missing > 0 {
        a -= missing.min(a);
    }
    let mut rendered = String::new();
    if left > 0 || a > 0 {
        rendered.push('…');
    }
    rendered.push_str(&slice_chars(&source, a, b)?);
    if source_end < text_len || b < source_len {
        rendered.push('…');
    }
    supported_render(&rendered).then_some(rendered)
}

fn one_line_supported(value: &str) -> Option<String> {
    value
        .chars()
        .all(|character| character.is_ascii() || !character.is_whitespace())
        .then(|| value.split_whitespace().collect::<Vec<_>>().join(" "))
}

fn supported_render(value: &str) -> bool {
    value
        .chars()
        .all(|character| character.is_ascii() || matches!(character, '…' | '·'))
}

fn one_line_with_span_supported(
    value: &str,
    start: usize,
    end: usize,
) -> Option<(String, usize, usize)> {
    let chars: Vec<char> = value.chars().collect();
    if end < start
        || end > chars.len()
        || chars
            .iter()
            .any(|character| !character.is_ascii() && character.is_whitespace())
    {
        return None;
    }
    let mut out = Vec::with_capacity(chars.len());
    let mut boundaries = vec![0usize; chars.len() + 1];
    let mut index = 0;
    while index < chars.len() {
        boundaries[index] = out.len();
        if chars[index].is_whitespace() {
            let run_start = index;
            while index < chars.len() && chars[index].is_whitespace() {
                index += 1;
            }
            if !out.is_empty() && index < chars.len() {
                out.push(' ');
            }
            boundaries[run_start + 1..=index].fill(out.len());
        } else {
            out.push(chars[index]);
            index += 1;
            boundaries[index] = out.len();
        }
    }
    Some((
        out.into_iter().collect(),
        boundaries[start],
        boundaries[end],
    ))
}

fn slice_chars(value: &str, start: usize, end: usize) -> Option<String> {
    if end < start {
        return None;
    }
    let out: String = value.chars().skip(start).take(end - start).collect();
    (out.chars().count() == end - start).then_some(out)
}

#[derive(Clone, Copy)]
struct PhraseUnit {
    folded: u8,
    bridge: bool,
}

struct PhraseMatchStats {
    count: usize,
    first_span: Option<usize>,
}

fn phrase_match_stats(text: &str, tokens: &[Vec<u8>]) -> PhraseMatchStats {
    if tokens.is_empty() {
        return PhraseMatchStats {
            count: 0,
            first_span: None,
        };
    }
    let units: Vec<_> = text
        .chars()
        .map(|character| PhraseUnit {
            folded: python_regex_ascii_fold(character),
            bridge: !character.is_alphanumeric(),
        })
        .collect();
    let mut count = 0;
    let mut offset = 0;
    let mut first_span = None;
    while let Some((start, end)) = next_phrase_span(&units, tokens, offset) {
        count += 1;
        first_span.get_or_insert(end - start);
        offset = end;
    }
    PhraseMatchStats { count, first_span }
}

fn next_phrase_span(
    units: &[PhraseUnit],
    tokens: &[Vec<u8>],
    offset: usize,
) -> Option<(usize, usize)> {
    for start in offset..units.len() {
        let mut cursor = start;
        let mut matched = true;
        for (index, token) in tokens.iter().enumerate() {
            if index > 0 {
                while cursor < units.len() && units[cursor].bridge {
                    cursor += 1;
                }
            }
            if cursor + token.len() > units.len()
                || !units[cursor..cursor + token.len()]
                    .iter()
                    .zip(token)
                    .all(|(unit, byte)| unit.folded == *byte)
            {
                matched = false;
                break;
            }
            cursor += token.len();
        }
        if matched {
            return Some((start, cursor));
        }
    }
    None
}

fn python_regex_ascii_fold(character: char) -> u8 {
    match character {
        'A'..='Z' => character as u8 + (b'a' - b'A'),
        'a'..='z' | '0'..='9' => character as u8,
        '\u{130}' | '\u{131}' => b'i',
        '\u{17f}' => b's',
        '\u{212a}' => b'k',
        _ => 0xff,
    }
}

fn raw_anchors(tokens: &[&str]) -> Vec<Vec<u8>> {
    const GENERATED: &str = "subagent result failed";
    let mut anchors: Vec<Vec<u8>> = tokens
        .iter()
        .filter_map(|token| {
            token
                .split(|character: char| matches!(character.to_ascii_lowercase(), 'i' | 's' | 'k'))
                .filter(|run| run.len() >= 3)
                .map(str::to_ascii_lowercase)
                .filter(|run| !GENERATED.contains(run))
                .max_by(|left, right| left.len().cmp(&right.len()).then_with(|| left.cmp(right)))
        })
        .map(String::into_bytes)
        .collect();
    anchors.sort_by(|left, right| right.len().cmp(&left.len()).then_with(|| left.cmp(right)));
    anchors.dedup();
    anchors
}

fn raw_has_unpaired_surrogate_escape(raw: &[u8]) -> bool {
    fn hex_quad(raw: &[u8], start: usize) -> Option<u16> {
        let digits = raw.get(start..start + 4)?;
        digits.iter().try_fold(0u16, |value, digit| {
            let nibble = match digit {
                b'0'..=b'9' => digit - b'0',
                b'a'..=b'f' => digit - b'a' + 10,
                b'A'..=b'F' => digit - b'A' + 10,
                _ => return None,
            };
            Some((value << 4) | u16::from(nibble))
        })
    }

    let mut in_string = false;
    let mut index = 0;
    while index < raw.len() {
        match raw[index] {
            b'"' => {
                in_string = !in_string;
                index += 1;
            }
            b'\\' if in_string => {
                if raw.get(index + 1) != Some(&b'u') {
                    index = index.saturating_add(2);
                    continue;
                }
                let Some(codepoint) = hex_quad(raw, index + 2) else {
                    index += 2;
                    continue;
                };
                if (0xdc00..=0xdfff).contains(&codepoint) {
                    return true;
                }
                if (0xd800..=0xdbff).contains(&codepoint) {
                    if raw.get(index + 6..index + 8) != Some(br#"\u"#)
                        || !hex_quad(raw, index + 8)
                            .is_some_and(|low| (0xdc00..=0xdfff).contains(&low))
                    {
                        return true;
                    }
                    index += 12;
                    continue;
                }
                index += 6;
            }
            _ => index += 1,
        }
    }
    false
}

fn raw_maybe_matches(raw: &[u8], anchors: &[Vec<u8>]) -> bool {
    anchors
        .iter()
        .all(|anchor| contains_ascii_case_insensitive(raw, anchor))
        || memchr::memmem::find(raw, b"\\u").is_some()
}

fn raw_payload_maybe_matches(raw: &[u8], anchors: &[Vec<u8>]) -> bool {
    anchors
        .first()
        .is_none_or(|anchor| contains_ascii_case_insensitive(raw, anchor))
        || memchr::memmem::find(raw, b"\\u").is_some()
}

fn contains_ascii_case_insensitive(haystack: &[u8], needle: &[u8]) -> bool {
    if needle.is_empty() || needle.len() > haystack.len() {
        return false;
    }
    let first = needle[0].to_ascii_lowercase();
    let alternate = first.to_ascii_uppercase();
    let mut offset = 0;
    while offset + needle.len() <= haystack.len() {
        let searchable = &haystack[offset..=haystack.len() - needle.len()];
        let relative = if first == alternate {
            memchr::memchr(first, searchable)
        } else {
            memchr::memchr2(first, alternate, searchable)
        };
        let Some(relative) = relative else {
            return false;
        };
        offset += relative;
        if haystack[offset..offset + needle.len()].eq_ignore_ascii_case(needle) {
            return true;
        }
        offset += 1;
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::Event;
    use serde_json::Value;

    fn owner(agent: &str, session: &str) -> SessionOwner {
        SessionOwner {
            agent: agent.into(),
            session: session.into(),
            project: String::new(),
        }
    }

    fn request(query: &str, limit: usize) -> ScanRequest {
        ScanRequest {
            protocol: PROTOCOL_VERSION,
            expected_ingest_generation: "ingest-a".into(),
            expected_event_generation: "events-a".into(),
            query: query.into(),
            boundary_context: BoundaryContext::ColdPrior,
            now_ms: 1_000.0,
            limit,
            candidate_limit: DEFAULT_CANDIDATE_LIMIT,
            after: None,
            eligible_sessions: vec![owner("codex", "one"), owner("claude", "two")],
            eligibility: EligibilityMode::Explicit,
            owner_filter: PublishedOwnerFilter::default(),
            caller_event_window: None,
            since_ms: None,
            until_ms: None,
            score_contract: ScoreContract::default(),
        }
    }

    fn scanner_with_census(
        query: &str,
        eligible: Vec<SessionOwner>,
        census: Vec<SessionOwner>,
    ) -> Scanner {
        let mut req = request(query, 10);
        req.eligible_sessions = eligible;
        Scanner::with_candidate_limit(req, CANDIDATE_BYTES_LIMIT, Some(census)).unwrap()
    }

    fn temp_root(label: &str) -> std::path::PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!(
            "agrep-fallback-scan-{label}-{}-{nonce}",
            std::process::id()
        ))
    }

    fn visit(scanner: &mut Scanner, agent: &str, session: &str, payload: &[u8]) {
        let name = crate::cache::event_fname(agent, session);
        scanner.visit(EventSession {
            name: &name,
            agent,
            session,
            n_events: payload
                .split(|byte| *byte == b'\n')
                .filter(|row| !row.is_empty())
                .count() as u64,
            payload,
        });
    }

    #[test]
    fn canonical_tool_fields_and_controls_match_python_contract() {
        let payload = br#"{"ts":1000,"kind":"tool","name":"Run","input":"needle","output":"needle","ok":false}
{"ts":1000,"kind":"subagent_result","name":"Needle","output":""}
{"ts":1000,"kind":"control","name":"needle","input":"needle","output":"needle"}
"#;
        let mut scanner = Scanner::new(request("needle", 10)).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        let response = scanner.finish("ingest-a".into(), "events-a".into());
        assert_eq!(response.state, ScanState::Ok, "{:?}", response.detail);
        assert_eq!(response.matches.tools, 2);
        assert_eq!(response.candidates.len(), 2);
        assert_eq!(response.candidates[0].occurrences, 2);
        assert_eq!(response.candidates[1].occurrences, 1);
    }

    #[test]
    fn synthetic_prefixes_and_failed_marker_are_searchable() {
        let payload = br#"{"ts":1000,"kind":"subagent_start","name":"","ok":true}
{"ts":1000,"kind":"tool","name":"Run","ok":false}
"#;
        let mut subagent = Scanner::new(request("subagent", 10)).unwrap();
        visit(&mut subagent, "codex", "one", payload);
        assert_eq!(subagent.finish("i".into(), "e".into()).matches.tools, 1);
        let mut failed = Scanner::new(request("failed", 10)).unwrap();
        visit(&mut failed, "codex", "one", payload);
        assert_eq!(failed.finish("i".into(), "e".into()).matches.tools, 1);
    }

    #[test]
    fn unicode_and_json_escapes_follow_python_re_ignorecase() {
        let matcher = LiteralMatcher::new("isk");
        assert_eq!(matcher.count("\u{130}\u{17f}\u{212a}"), 1);
        let matcher = LiteralMatcher::new("xi");
        assert_eq!(matcher.count("x\u{130} x\u{131}"), 2);
        let matcher = LiteralMatcher::new("akd");
        assert_eq!(matcher.count("peakDetect AKD"), 2);

        let payload =
            br#"{"ts":1000,"kind":"tool","name":"x\u0130i x\u0131i","input":"","output":""}
"#;
        let mut scanner = Scanner::new(request("xii", 10)).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.matches.tools, 1);
        assert_eq!(response.candidates[0].occurrences, 2);
    }

    #[test]
    fn legacy_surrogate_escape_requires_the_exact_python_fallback() {
        let payload = br#"{"kind":"tool","name":"\ud800","output":"needle"}
"#;
        let mut scanner = Scanner::new(request("needle", 10)).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.state, ScanState::Unsupported);
        assert!(response.candidates.is_empty());
        assert!(!response.envelope_complete);

        let literal = br#"{"kind":"tool","name":"character '\\udce9'","output":"deadlock"}
"#;
        let mut scanner = Scanner::new(request("deadlock", 10)).unwrap();
        visit(&mut scanner, "codex", "one", literal);
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.state, ScanState::Ok);
        assert_eq!(response.matches.tools, 1);

        assert!(raw_has_unpaired_surrogate_escape(br#"{"x":"\udce9"}"#));
        assert!(!raw_has_unpaired_surrogate_escape(
            br#"{"x":"\ud83d\ude00"}"#
        ));
    }

    #[test]
    fn ordinary_malformed_json_is_skipped_without_disabling_native_scan() {
        let payload = br#"not-json needle
{"kind":"tool","name":"needle"}
"#;
        let mut scanner = Scanner::new(request("needle", 10)).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.state, ScanState::Ok);
        assert_eq!(response.matches.tools, 1);
    }

    #[test]
    fn raw_anchors_are_mandatory_and_longest_first() {
        let anchors = raw_anchors(&["deadlock", "condition", "alpha"]);
        assert_eq!(
            anchors,
            vec![b"deadloc".to_vec(), b"alpha".to_vec(), b"cond".to_vec()]
        );
        assert!(raw_maybe_matches(b"alpha DEADLOCK condition", &anchors));
        assert!(!raw_maybe_matches(b"alpha deadlock", &anchors));
        assert!(raw_payload_maybe_matches(
            b"different line\nalpha DEADLOCK condition",
            &anchors
        ));
        assert!(!raw_payload_maybe_matches(b"alpha condition", &anchors));
    }

    #[test]
    fn python_tool_text_oracle_matches() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../py/fixtures/fallback_scan_conformance.json");
        let fixture: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        assert_eq!(fixture["schema"], 2);
        for case in fixture["cases"].as_array().unwrap() {
            let matcher = LiteralMatcher::new(case["query"].as_str().unwrap());
            let raw = serde_json::to_vec(&case["event"]).unwrap();
            let event: EventView<'_> = serde_json::from_slice(&raw).unwrap();
            let actual = canonical_occurrences(&event, &matcher);
            assert_eq!(
                actual as u64,
                case["occurrences"].as_u64().unwrap(),
                "{}",
                case["name"].as_str().unwrap()
            );
        }
        for case in fixture["encoded_cases"].as_array().unwrap() {
            let matcher = LiteralMatcher::new(case["query"].as_str().unwrap());
            let raw = case["event_json"].as_str().unwrap().as_bytes();
            assert!(raw_maybe_matches(raw, std::slice::from_ref(&matcher.query)));
            let event: EventView<'_> = serde_json::from_slice(raw).unwrap();
            assert_eq!(
                canonical_occurrences(&event, &matcher) as u64,
                case["occurrences"].as_u64().unwrap(),
                "{}",
                case["name"].as_str().unwrap()
            );
        }
        for case in fixture["multi_cases"].as_array().unwrap() {
            let query = case["query"].as_str().unwrap();
            let tokens = keyword_tokens(query);
            let phrase: Vec<_> = tokens
                .iter()
                .map(|token| token.to_ascii_lowercase().into_bytes())
                .collect();
            let terms: Vec<_> = tokens
                .iter()
                .map(|token| LiteralMatcher::new(token))
                .collect();
            let raw = serde_json::to_vec(&case["event"]).unwrap();
            let event: EventView<'_> = serde_json::from_slice(&raw).unwrap();
            let text = canonical_text(&event);
            let actual_phrase = text
                .as_ref()
                .map(|text| phrase_match_stats(&text.text, &phrase).count)
                .unwrap_or(0);
            let actual_terms = text
                .as_ref()
                .is_some_and(|text| terms.iter().all(|term| term.count(&text.text) > 0));
            assert_eq!(
                actual_phrase as u64,
                case["phrase_occurrences"].as_u64().unwrap(),
                "{} phrase",
                case["name"].as_str().unwrap()
            );
            assert_eq!(
                actual_terms,
                case["all_terms"].as_bool().unwrap(),
                "{} terms",
                case["name"].as_str().unwrap()
            );
        }
    }

    #[test]
    fn python_exact_score_oracle_matches() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../py/fixtures/fallback_scan_conformance.json");
        let fixture: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        for case in fixture["exact_score_cases"].as_array().unwrap() {
            let query = case["query"].as_str().unwrap();
            let tokens = keyword_tokens(query);
            let plan = if tokens.len() == 1 {
                QueryPlan::Single(LiteralMatcher::new(tokens[0]))
            } else {
                QueryPlan::Multi {
                    phrase: tokens
                        .iter()
                        .map(|token| token.to_ascii_lowercase().into_bytes())
                        .collect(),
                    terms: tokens
                        .iter()
                        .map(|token| LiteralMatcher::new(token))
                        .collect(),
                }
            };
            let raw = case["event_json"].as_str().map_or_else(
                || serde_json::to_vec(&case["event"]).unwrap(),
                |encoded| encoded.as_bytes().to_vec(),
            );
            let event: EventView<'_> = serde_json::from_slice(&raw).unwrap();
            let rendered = canonical_text(&event).unwrap();
            let lane = if case["lane"] == "phrase" {
                MatchLane::Phrase
            } else {
                MatchLane::AllTerms
            };
            let phrase_tokens: Vec<&[u8]> = match &plan {
                QueryPlan::Single(matcher) => vec![matcher.query.as_slice()],
                QueryPlan::Multi { phrase, .. } => phrase.iter().map(Vec::as_slice).collect(),
            };
            assert_eq!(
                exact_snippet(&rendered, &plan, lane, &phrase_tokens).unwrap(),
                case["snippet"].as_str().unwrap(),
                "{}",
                case["name"].as_str().unwrap(),
            );
            let boundary =
                crate::boundary_rank::prepare_query(query, &crate::boundary_rank::Stats::new());
            let (lower, upper) = exact_rank_score(
                &rendered,
                &plan,
                &boundary,
                lane,
                ScoreInputs {
                    ts: event.timestamp(),
                    meta_scale: 1.0,
                    now_ms: case["now_ms"].as_f64().unwrap_or(1_000.0),
                    contract: ScoreContract::default(),
                },
            )
            .unwrap();
            let expected = case["score"].as_f64().unwrap();
            assert!(lower <= expected, "{}", case["name"].as_str().unwrap());
            assert!(expected <= upper, "{}", case["name"].as_str().unwrap());
            if case["name"]
                .as_str()
                .unwrap()
                .contains("near-rounding-half")
            {
                assert!(lower < upper, "{}", case["name"].as_str().unwrap());
            }
        }
    }

    #[test]
    fn multi_token_lanes_run_independently_and_return_the_complete_union() {
        let payload = br#"{"ts":1000,"kind":"tool","name":"cyber_filter"}
{"ts":1000,"kind":"tool","name":"cyberfilter"}
{"ts":1000,"kind":"tool","name":"cyberXfilter"}
{"ts":1000,"kind":"tool","name":"filter cyber"}
{"ts":1000,"kind":"control","name":"cyber_filter"}
"#;
        let mut scanner = Scanner::new(request("cyber filter", 1)).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.matches.tools, 4);
        assert_eq!(response.matches.phrase_tools, 2);
        assert_eq!(response.matches.all_terms_tools, 4);
        assert_eq!(response.matches.all_terms_additions, 2);
        assert_eq!(response.candidates.len(), 4);
        assert_eq!(
            response
                .candidates
                .iter()
                .filter(|candidate| candidate.matched == MatchLane::Phrase)
                .count(),
            2
        );
    }

    #[test]
    fn totals_and_sessions_are_exact_before_candidate_pruning() {
        let payload = br#"{"ts":1000,"kind":"tool","name":"needle"}
{"ts":1000,"kind":"tool","name":"needle needle"}
not-json
"#;
        let mut req = request("needle", 1);
        req.candidate_limit = 1;
        let mut scanner = Scanner::new(req).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        visit(
            &mut scanner,
            "claude",
            "two",
            br#"{"ts":1000,"kind":"tool","output":"needle"}
"#,
        );
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.matches.tools, 3);
        assert_eq!(response.matches.matched_sessions, 2);
        assert_eq!(response.matches.matched_owner_bitmap, "03");
        assert_eq!(response.candidates.len(), 1);
        assert!(response.best_omitted.is_some());
        assert!(response.next_after.is_some());
        assert!(!response.envelope_complete);
        assert_eq!(response.scanned.sessions, 2);
        assert_eq!(response.scanned.events, 4);
    }

    #[test]
    fn bounded_page_reports_the_best_omitted_cursor() {
        let payload = br#"{"ts":1000,"kind":"tool","name":"needle"}
{"ts":1000,"kind":"tool","name":"needle needle"}
"#;
        let mut req = request("needle", 3);
        req.candidate_limit = 1;
        let mut scanner = Scanner::new(req).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.candidates.len(), 1);
        assert!(response.best_omitted.is_some());
        assert_eq!(
            response.next_after.as_ref().unwrap().ordinal,
            response.candidates[0].ordinal
        );
        assert!(!response.envelope_complete);
    }

    #[test]
    fn snippet_sensitive_bounds_ignore_project_names() {
        let mut aligned = Scanner::new(request("the", 10)).unwrap();
        visit(
            &mut aligned,
            "codex",
            "one",
            br#"{"ts":1000,"kind":"tool","name":"the"}
"#,
        );
        let aligned = aligned.finish("i".into(), "e".into());
        assert!(aligned.candidates[0].refined_score);
        assert!(aligned.candidates[0].lower_score <= 0.11);
        assert!(aligned.candidates[0].upper_score >= 0.11);

        let mut output_start = Scanner::new(request("the", 10)).unwrap();
        visit(
            &mut output_start,
            "codex",
            "one",
            br#"{"ts":1000,"kind":"tool","name":"run","output":"the rest"}
"#,
        );
        let output_start = output_start.finish("i".into(), "e".into());
        assert!(output_start.candidates[0].refined_score);
        assert!(output_start.candidates[0].lower_score <= 0.0688);
        assert!(output_start.candidates[0].upper_score >= 0.0688);

        let mut named_request = request("the", 10);
        named_request.eligible_sessions[0].project = "/repo/bench".into();
        let mut named = Scanner::new(named_request).unwrap();
        visit(
            &mut named,
            "codex",
            "one",
            br#"{"ts":1000,"kind":"tool","name":"the"}
"#,
        );
        let named = named.finish("i".into(), "e".into());
        assert!(named.candidates[0].refined_score);
        assert_eq!(
            (
                named.candidates[0].lower_score,
                named.candidates[0].upper_score
            ),
            (
                aligned.candidates[0].lower_score,
                aligned.candidates[0].upper_score
            )
        );
    }

    #[test]
    fn owner_digest_binds_project_metadata() {
        let product = Scanner::new(request("needle", 10)).unwrap().match_totals();
        let mut fixture_request = request("needle", 10);
        fixture_request.eligible_sessions[0].project = "fixtures".into();
        let fixture = Scanner::new(fixture_request).unwrap().match_totals();
        assert_ne!(product.owner_order_sha256, fixture.owner_order_sha256);
    }

    #[test]
    fn rendered_snippet_counterexamples_do_not_tighten_boundary_bounds() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../py/fixtures/fallback_scan_conformance.json");
        let fixture: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        for case in fixture["bound_cases"].as_array().unwrap() {
            let query = case["query"].as_str().unwrap();
            let payload = format!("{}\n", serde_json::to_string(&case["event"]).unwrap());
            let mut scanner = Scanner::new(request(query, 10)).unwrap();
            visit(&mut scanner, "codex", "one", payload.as_bytes());
            let response = scanner.finish("i".into(), "e".into());
            assert_eq!(response.candidates.len(), 1, "{query}");
            let candidate = &response.candidates[0];
            assert_eq!(
                candidate.matched,
                if case["lane"] == "phrase" {
                    MatchLane::Phrase
                } else {
                    MatchLane::AllTerms
                },
                "{query}"
            );
            let global_lower = case["lower"].as_f64().unwrap();
            let global_upper = case["upper"].as_f64().unwrap();
            if candidate.refined_score {
                assert!(global_lower <= candidate.lower_score, "{query}");
                assert!(candidate.upper_score <= global_upper, "{query}");
            } else {
                assert_eq!(candidate.lower_score, global_lower, "{query}");
                assert_eq!(candidate.upper_score, global_upper, "{query}");
            }
        }
    }

    #[test]
    fn outward_score_bounds_cover_every_exact_component_choice() {
        let contract = ScoreContract::default();
        let (lower, upper) = score_bounds(
            ScoreInputs {
                ts: 1_000,
                meta_scale: 1.0,
                now_ms: 1_000.0,
                contract,
            },
            3,
            Some(6),
            6,
            (contract.boundary_min, 1.0),
        );
        for snippet_occurrences in 1..=3 {
            for boundary in [contract.boundary_min, 0.4, 1.0] {
                let exact = (1.0 - 0.5f64.powi(snippet_occurrences))
                    * contract.who_tool
                    * contract.source_tool
                    * boundary;
                let rounded = (exact * SCORE_DIGITS).round() / SCORE_DIGITS;
                assert!(lower <= rounded);
                assert!(upper >= rounded);
            }
        }
        assert_eq!(upper, 0.1925);
    }

    #[test]
    fn refined_score_interval_contains_python_half_decimal_rounding() {
        let (lower, upper) = refined_score_interval(0.01485);
        assert_eq!(lower, 0.0148);
        assert_eq!(upper, 0.0149);
        assert!(lower <= 0.0149 && 0.0149 <= upper);
    }

    #[test]
    fn refined_score_interval_certifies_unambiguous_rounding_cells() {
        assert_eq!(refined_score_interval(0.06874), (0.0687, 0.0687));
        assert_eq!(refined_score_interval(0.06876), (0.0688, 0.0688));
        assert_eq!(refined_score_interval(0.5), (0.5, 0.5));
    }

    #[test]
    fn refined_score_interval_keeps_a_guard_around_half_cells() {
        let epsilon = REFINED_ROUNDING_GUARD / (2.0 * SCORE_DIGITS);
        assert_eq!(refined_score_interval(0.06875 - epsilon), (0.0687, 0.0688));
        assert_eq!(refined_score_interval(0.06875 + epsilon), (0.0687, 0.0688));
    }

    #[test]
    fn candidate_byte_budget_becomes_a_continuation_boundary() {
        let payload = br#"{"ts":1000,"kind":"tool","name":"needle","output":"large result"}
{"ts":999,"kind":"tool","name":"needle","output":"other result"}
"#;
        let mut scanner = Scanner::with_candidate_limit(request("needle", 10), 1, None).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.state, ScanState::Ok);
        assert_eq!(response.matches.tools, 2);
        assert_eq!(response.candidates.len(), 1);
        assert!(response.best_omitted.is_some());
        assert!(response.next_after.is_some());
        assert!(!response.envelope_complete);
    }

    #[test]
    fn continuation_pages_have_no_gaps_or_duplicates() {
        let payload = br#"{"ts":1000,"kind":"tool","name":"needle one"}
{"ts":3000,"kind":"tool","name":"needle two"}
{"ts":2000,"kind":"tool","name":"needle three"}
"#;
        let mut after = None;
        let mut ordinals = Vec::new();
        loop {
            let mut req = request("needle", 10);
            req.candidate_limit = 1;
            req.after = after;
            let mut scanner = Scanner::new(req).unwrap();
            visit(&mut scanner, "codex", "one", payload);
            let response = scanner.finish("i".into(), "e".into());
            assert_eq!(response.matches.tools, 3);
            ordinals.extend(
                response
                    .candidates
                    .iter()
                    .map(|candidate| candidate.ordinal),
            );
            let Some(next) = response.next_after else {
                assert!(response.envelope_complete);
                break;
            };
            assert!(!response.envelope_complete);
            after = Some(next);
        }
        assert_eq!(ordinals, [1, 2, 0]);
    }

    #[test]
    fn continuation_order_keeps_phrase_ahead_of_newer_all_terms() {
        let payload = br#"{"ts":3000,"kind":"tool","name":"cyber x filter"}
{"ts":1000,"kind":"tool","name":"cyber filter"}
"#;
        let mut first_request = request("cyber filter", 10);
        first_request.candidate_limit = 1;
        let mut first = Scanner::new(first_request).unwrap();
        visit(&mut first, "codex", "one", payload);
        let first = first.finish("i".into(), "e".into());
        assert_eq!(first.candidates[0].matched, MatchLane::Phrase);
        assert_eq!(first.matches.phrase_tools, 1);
        assert_eq!(first.matches.all_terms_additions, 1);

        let mut second_request = request("cyber filter", 10);
        second_request.candidate_limit = 1;
        second_request.after = first.next_after;
        let mut second = Scanner::new(second_request).unwrap();
        visit(&mut second, "codex", "one", payload);
        let second = second.finish("i".into(), "e".into());
        assert_eq!(second.candidates[0].matched, MatchLane::AllTerms);
        assert!(second.envelope_complete);
    }

    #[test]
    fn blank_legacy_session_resolves_only_through_the_pinned_owner() {
        let payload = br#"{"ts":1000,"kind":"tool","name":"needle"}
"#;
        let eligible = vec![owner("codex", "one")];
        let census = vec![owner("codex", "one"), owner("codex", "filtered")];
        let mut scanner = scanner_with_census("needle", eligible.clone(), census.clone());
        let name = crate::cache::event_fname("codex", "one");
        scanner.visit(EventSession {
            name: &name,
            agent: "codex",
            session: "",
            n_events: 1,
            payload,
        });
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.matches.matched_sessions, 1);
        assert_eq!(response.matches.matched_owner_bitmap, "01");

        let filtered_name = crate::cache::event_fname("codex", "filtered");
        let mut filtered = scanner_with_census("needle", eligible.clone(), census.clone());
        filtered.visit(EventSession {
            name: &filtered_name,
            agent: "legacy",
            session: "",
            n_events: 1,
            payload,
        });
        let filtered = filtered.finish("i".into(), "e".into());
        assert_eq!(filtered.state, ScanState::Ok);
        assert_eq!(filtered.matches.tools, 0);

        let mut orphan = scanner_with_census("needle", eligible, census);
        orphan.visit(EventSession {
            name: "unknown.jsonl",
            agent: "legacy",
            session: "",
            n_events: 1,
            payload,
        });
        assert_eq!(
            orphan.finish("i".into(), "e".into()).state,
            ScanState::IntegrityError,
        );
    }

    #[test]
    fn time_and_owner_filters_apply_before_matching() {
        let payload = br#"{"ts":9,"kind":"tool","name":"needle"}
{"ts":10,"kind":"tool","name":"needle"}
{"ts":20,"kind":"tool","name":"needle"}
"#;
        let mut req = request("needle", 10);
        req.since_ms = Some(10);
        req.until_ms = Some(20);
        let mut scanner = Scanner::new(req).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        visit(&mut scanner, "codex", "excluded", payload);
        assert_eq!(scanner.finish("i".into(), "e".into()).matches.tools, 1);
    }

    #[test]
    fn caller_window_uses_the_same_timestamp_to_turn_policy_as_python() {
        let payload = br#"{"ts":0,"kind":"tool","name":"needle"}
{"ts":1500,"kind":"tool","name":"needle"}
{"ts":2000,"kind":"tool","name":"needle"}
"#;
        let mut req = request("needle", 10);
        req.caller_event_window = Some(CallerEventWindow {
            session: "one".into(),
            boundary: 3,
            marks: vec![
                EventTurnMark { ts: 2_000, turn: 3 },
                EventTurnMark { ts: 1_000, turn: 1 },
            ],
        });
        let mut scanner = Scanner::new(req).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        assert_eq!(scanner.finish("i".into(), "e".into()).matches.tools, 2);
    }

    #[test]
    fn populated_owner_requires_its_canonical_filename() {
        let payload = br#"{"ts":1,"kind":"tool","name":"needle"}
"#;
        let eligible = vec![owner("codex", "one")];
        let mut scanner = scanner_with_census("needle", eligible.clone(), eligible);
        scanner.visit(EventSession {
            name: "noncanonical.jsonl",
            agent: "codex",
            session: "one",
            n_events: 1,
            payload,
        });
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.state, ScanState::IntegrityError);
        assert_eq!(response.matches.tools, 0);
    }

    #[test]
    fn published_census_distinguishes_filtered_and_orphan_event_rows() {
        let payload = br#"{"ts":1,"kind":"tool","name":"needle"}
"#;
        let eligible = vec![owner("codex", "one")];
        let census = vec![owner("codex", "one"), owner("codex", "filtered")];
        let mut filtered = scanner_with_census("needle", eligible.clone(), census.clone());
        visit(&mut filtered, "codex", "filtered", payload);
        let filtered = filtered.finish("i".into(), "e".into());
        assert_eq!(filtered.state, ScanState::Ok);
        assert_eq!(filtered.matches.tools, 0);

        let mut orphan = scanner_with_census("needle", eligible, census);
        visit(&mut orphan, "codex", "orphan", payload);
        let orphan = orphan.finish("i".into(), "e".into());
        assert_eq!(orphan.state, ScanState::IntegrityError);
        assert!(orphan.detail.unwrap().contains("no published owner"));
    }

    #[test]
    fn known_filename_with_orphan_columns_is_an_integrity_error() {
        let payload = br#"{"ts":1,"kind":"tool","name":"needle"}
"#;
        let eligible = vec![owner("codex", "one")];
        let mut scanner = scanner_with_census("needle", eligible.clone(), eligible);
        let known_name = crate::cache::event_fname("codex", "one");
        scanner.visit(EventSession {
            name: &known_name,
            agent: "codex",
            session: "orphan",
            n_events: 1,
            payload,
        });
        let response = scanner.finish("i".into(), "e".into());
        assert_eq!(response.state, ScanState::IntegrityError);
        assert!(response.detail.unwrap().contains("filename"));
    }

    #[test]
    fn minimum_i64_event_timestamp_canonicalizes_to_zero() {
        let payload = br#"{"ts":-9223372036854775808,"kind":"tool","name":"needle"}
"#;
        let mut req = request("needle", 10);
        req.since_ms = Some(1);
        let mut scanner = Scanner::new(req).unwrap();
        visit(&mut scanner, "codex", "one", payload);
        assert_eq!(scanner.finish("i".into(), "e".into()).matches.tools, 0);
    }

    #[test]
    fn invalid_shapes_fail_closed() {
        for query in ["hi", "two words?", "café words"] {
            assert!(Scanner::new(request(query, 10)).is_err());
        }
        assert!(Scanner::new(request("two words", 10)).is_ok());
        let mut wrong = request("needle", 10);
        wrong.protocol += 1;
        assert!(Scanner::new(wrong).is_err());
    }

    #[test]
    fn refined_scoring_requires_the_cold_prior_protocol_context() {
        let value = serde_json::to_value(request("needle", 10)).unwrap();
        assert_eq!(value["boundary_context"], "cold_prior");
        let mut missing = value.clone();
        missing.as_object_mut().unwrap().remove("boundary_context");
        assert!(serde_json::from_value::<ScanRequest>(missing).is_err());
        let mut unsupported = value;
        unsupported["boundary_context"] = Value::String("sidecar".into());
        assert!(serde_json::from_value::<ScanRequest>(unsupported).is_err());
    }

    #[test]
    fn failure_states_carry_no_partial_results() {
        let response = failure_response(
            ScanState::IntegrityError,
            "event payload digest mismatch",
            "i".into(),
            "e".into(),
        );
        assert!(response.candidates.is_empty());
        assert_eq!(response.matches.tools, 0);
        assert!(!response.envelope_complete);
    }

    #[test]
    fn verified_bridge_withholds_rows_until_both_generations_are_pinned() {
        let root = temp_root("verified-bridge");
        let events_dir = root.join("events");
        std::fs::create_dir_all(&events_dir).unwrap();
        std::fs::write(root.join(".ingest.sig"), b"one-event-generation").unwrap();
        let events = vec![Event {
            agent: "codex",
            session: "one".into(),
            ts: 1_000,
            kind: "tool",
            name: "agent".into(),
            input: "got_stuck".into(),
            output: "retrying same command".into(),
            input_chars: 9,
            output_chars: 21,
            output_bytes: 21,
            ok: Some(false),
            call_id: "call-1".into(),
            child_session: String::new(),
        }];
        let keep = HashSet::from([crate::cache::event_fname("codex", "one")]);
        crate::cache::write_events(
            &events,
            &events_dir,
            &keep,
            &["codex"],
            true,
            &HashSet::new(),
        )
        .unwrap();
        assert!(crate::cache::publish_events_complete(&events_dir, &["codex"]).unwrap());
        assert!(crate::cache::events_complete(&events_dir, &["codex"]).unwrap());
        let event_generation =
            String::from_utf8(std::fs::read(events_dir.join(".generation")).unwrap()).unwrap();
        let mut req = request("agent got stuck retrying same command", 10);
        req.eligible_sessions.retain(|owner| owner.agent == "codex");
        req.expected_ingest_generation = ingest_generation(&root).unwrap();
        req.expected_event_generation = event_generation;
        let response = run_verified_scan(&root, req.clone());
        assert_eq!(response.state, ScanState::Ok, "{:?}", response.detail);
        assert_eq!(response.matches.tools, 1);
        assert_eq!(response.candidates.len(), 1);

        std::fs::write(root.join(".ingest.sig"), b"moved-generation").unwrap();
        let moved = run_verified_scan(&root, req);
        assert_eq!(moved.state, ScanState::GenerationMoved);
        assert_eq!(moved.matches.tools, 0);
        assert!(moved.candidates.is_empty());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn empty_owner_set_is_proven_without_opening_an_event_store() {
        let root = temp_root("empty-owner");
        std::fs::create_dir_all(root.join("events")).unwrap();
        std::fs::write(root.join(".ingest.sig"), b"empty-ingest").unwrap();
        std::fs::write(root.join("events/.generation"), b"empty-events").unwrap();
        let mut req = request("deadlock", 10);
        req.eligible_sessions.clear();
        req.expected_ingest_generation = ingest_generation(&root).unwrap();
        req.expected_event_generation = "empty-events".into();
        let response = run_verified_scan(&root, req);
        assert_eq!(response.state, ScanState::Ok, "{:?}", response.detail);
        assert_eq!(response.matches.tools, 0);
        assert_eq!(response.matches.eligible_sessions, 0);
        let _ = std::fs::remove_dir_all(root);
    }
}
