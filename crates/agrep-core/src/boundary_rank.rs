use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::fmt;

use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use unicode_normalization::UnicodeNormalization;

use crate::unicode_v16;

pub const PROTOCOL_VERSION: u32 = 2;
const PARALLEL_MIN_ITEMS: usize = 512;

pub type Span = [i64; 2];
pub type Stats = HashMap<String, StatValue>;

#[derive(Clone, Debug, Deserialize)]
#[serde(untagged)]
pub enum StatValue {
    Pair([i64; 2]),
    Mapping(HashMap<String, i64>),
}

impl StatValue {
    fn counts(&self) -> (i64, i64) {
        match self {
            Self::Pair([n, s]) => (*n, *s),
            Self::Mapping(values) => (
                values.get("n").copied().unwrap_or(0),
                values.get("s").copied().unwrap_or(0),
            ),
        }
    }
}

#[derive(Clone, Debug)]
struct BoundaryTerm {
    folded: Vec<char>,
    folded_ascii: Option<String>,
    ambiguity: f64,
}

#[derive(Clone, Debug)]
pub struct PreparedQuery {
    terms: Vec<BoundaryTerm>,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct BoundaryScore {
    pub factor: f64,
    pub qualities: Vec<f64>,
    pub match_class: &'static str,
    pub matched: bool,
    pub spans: Vec<Option<Span>>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct BoundaryError(&'static str);

impl fmt::Display for BoundaryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}

impl std::error::Error for BoundaryError {}

#[derive(Debug, Deserialize)]
pub struct BatchRequest {
    pub protocol: u32,
    pub query: String,
    #[serde(default)]
    pub stats: Stats,
    #[serde(default)]
    pub compact: bool,
    #[serde(default)]
    pub decut: bool,
    pub items: Vec<BatchItem>,
}

#[derive(Debug, Deserialize)]
pub struct BatchItem {
    pub text: String,
    #[serde(default)]
    pub spans: Option<Vec<Option<Span>>>,
    #[serde(default = "default_validate_spans")]
    pub validate_spans: bool,
}

fn default_validate_spans() -> bool {
    true
}

#[derive(Debug, Serialize)]
pub struct BatchResponse {
    pub protocol: u32,
    pub results: Vec<BatchEvaluation>,
}

#[derive(Debug, Serialize)]
#[serde(untagged)]
pub enum BatchEvaluation {
    Score(BoundaryScore),
    CompactScore(CompactBoundaryScore),
    Error { error: String },
}

#[derive(Debug, Serialize)]
pub struct CompactBoundaryScore {
    pub factor: f64,
    pub match_class: &'static str,
}

pub fn evaluate_batch(request: BatchRequest) -> Result<BatchResponse, BoundaryError> {
    if request.protocol != PROTOCOL_VERSION {
        return Err(BoundaryError("unsupported boundary-rank protocol"));
    }
    let prepared = prepare_query(&request.query, &request.stats);
    let compact = request.compact;
    let decut = request.decut;
    let results = if compact && request.items.len() >= PARALLEL_MIN_ITEMS {
        request
            .items
            .into_par_iter()
            .map(|item| evaluate_item(&prepared, item, compact, decut))
            .collect()
    } else {
        request
            .items
            .into_iter()
            .map(|item| evaluate_item(&prepared, item, compact, decut))
            .collect()
    };
    Ok(BatchResponse {
        protocol: PROTOCOL_VERSION,
        results,
    })
}

fn evaluate_item(
    prepared: &PreparedQuery,
    item: BatchItem,
    compact: bool,
    decut: bool,
) -> BatchEvaluation {
    let text = if decut {
        decut_text(&item.text, item.spans.is_none())
    } else {
        Cow::Borrowed(item.text.as_str())
    };
    if compact {
        return match prepared.evaluate_compact(&text, item.spans.as_deref(), item.validate_spans) {
            Ok(score) => BatchEvaluation::CompactScore(score),
            Err(error) => BatchEvaluation::Error {
                error: error.to_string(),
            },
        };
    }
    match prepared.evaluate(&text, item.spans.as_deref(), item.validate_spans) {
        Ok(score) => BatchEvaluation::Score(score),
        Err(error) => BatchEvaluation::Error {
            error: error.to_string(),
        },
    }
}

pub(crate) fn decut_text(text: &str, pad_edges: bool) -> Cow<'_, str> {
    if !text.contains('…') {
        return Cow::Borrowed(text);
    }
    let mut chars: Vec<char> = text.chars().collect();
    let last = chars.len() - 1;
    for index in 0..chars.len() {
        if chars[index] != '…' {
            continue;
        }
        let left = index.checked_sub(1).map(|at| chars[at]);
        let right = (index < last).then(|| chars[index + 1]);
        chars[index] = match (left, right) {
            (Some(value), _) if !matches!(value, ' ' | '…') => value,
            (_, Some(value)) if !matches!(value, ' ' | '…') => value,
            _ => ' ',
        };
    }
    if pad_edges {
        if text.starts_with('…') {
            chars.insert(0, chars[0]);
        }
        if text.ends_with('…') {
            chars.push(chars[last + usize::from(text.starts_with('…'))]);
        }
    }
    Cow::Owned(chars.into_iter().collect())
}

pub fn prepare_query(query: &str, stats: &Stats) -> PreparedQuery {
    let mut resolved = HashMap::new();
    let mut terms = Vec::new();
    for original in query_tokens(query) {
        let folded_string = normalize_token(original);
        if folded_string.is_empty() {
            continue;
        }
        let ambiguity = *resolved
            .entry(folded_string.clone())
            .or_insert_with(|| ambiguity(cold_prior(original), stats.get(&folded_string)));
        let folded_ascii = folded_string.is_ascii().then(|| folded_string.clone());
        terms.push(BoundaryTerm {
            folded: folded_string.chars().collect(),
            folded_ascii,
            ambiguity,
        });
    }
    PreparedQuery { terms }
}

pub fn query_tokens(query: &str) -> Vec<&str> {
    query
        .split(|character| unicode_v16::is_space(character) || matches!(character, '-' | '_'))
        .filter(|token| !token.is_empty())
        .collect()
}

pub fn normalize_token(token: &str) -> String {
    let mut out = String::new();
    for cluster in clusters(token) {
        let compatible: String = token[cluster.start_byte..cluster.end_byte].nfkc().collect();
        out.push_str(&unicode_v16::case_fold(&compatible));
    }
    out
}

pub fn cold_prior(token: &str) -> f64 {
    let grouped = clusters(token);
    let alphanumeric: Vec<&Cluster> = grouped
        .iter()
        .filter(|cluster| {
            token[cluster.start_byte..cluster.end_byte]
                .chars()
                .any(unicode_v16::is_alphanumeric)
        })
        .collect();
    let length = if alphanumeric.is_empty() {
        grouped.len()
    } else {
        alphanumeric.len()
    };
    let scripts: HashSet<u8> = alphanumeric
        .iter()
        .filter_map(|cluster| {
            unicode_v16::is_alpha(cluster.base)
                .then(|| unicode_v16::script(cluster.base))
                .filter(|script| *script != 0)
        })
        .collect();
    if !scripts.is_empty()
        && scripts
            .iter()
            .all(|script| unicode_v16::is_unsegmented(*script))
    {
        return 0.0;
    }
    let cased = token.chars().any(unicode_v16::is_cased);
    if alphanumeric.is_empty() {
        return 0.0;
    }
    if cased {
        match length {
            0..=2 => 0.90,
            3 => 0.75,
            4 => 0.40,
            _ => 0.05,
        }
    } else {
        match length {
            0..=2 => 0.60,
            3..=4 => 0.30,
            _ => 0.05,
        }
    }
}

impl PreparedQuery {
    fn evaluate_compact(
        &self,
        text: &str,
        spans: Option<&[Option<Span>]>,
        validate_spans: bool,
    ) -> Result<CompactBoundaryScore, BoundaryError> {
        if spans.is_some_and(|values| values.len() != self.terms.len()) {
            return Err(BoundaryError("one span is required per query token"));
        }
        if text.is_ascii() && self.terms.iter().all(|term| term.folded_ascii.is_some()) {
            return self.evaluate_ascii_compact(text, spans, validate_spans);
        }
        if let Some(spans) = spans.filter(|_| !validate_spans) {
            return self.evaluate_boundary_only_compact(text, spans);
        }
        if spans.is_none()
            && !self.terms.is_empty()
            && self.terms.iter().all(|term| term.folded_ascii.is_some())
        {
            let prepared = BoundaryOnlyText::new(text);
            if prepared.can_score_ascii(&self.terms) {
                return Ok(self.evaluate_boundary_ascii_compact(text, &prepared));
            }
        }
        let score = self.evaluate_unicode(text, spans, validate_spans)?;
        Ok(CompactBoundaryScore {
            factor: score.factor,
            match_class: score.match_class,
        })
    }

    fn evaluate_ascii_compact(
        &self,
        text: &str,
        spans: Option<&[Option<Span>]>,
        validate_spans: bool,
    ) -> Result<CompactBoundaryScore, BoundaryError> {
        let bytes = text.as_bytes();
        let folded = text.to_ascii_lowercase();
        let mut product = 1.0;
        let mut all_aligned = !self.terms.is_empty();
        let mut all_interior = !self.terms.is_empty();
        for (index, term) in self.terms.iter().enumerate() {
            let needle = term.folded_ascii.as_deref().unwrap();
            let quality = match spans.and_then(|values| values[index]) {
                None if spans.is_some() => 0.0,
                None => best_ascii_quality(bytes, &folded, needle),
                Some([start, end]) => {
                    let (start, end) = checked_span(start, end, bytes.len())?;
                    if spans.is_some()
                        && validate_spans
                        && !folded.as_bytes()[start..end]
                            .windows(needle.len())
                            .any(|window| window == needle.as_bytes())
                    {
                        return Err(BoundaryError("span does not identify its query token"));
                    }
                    ascii_quality(bytes, start, end)
                }
            };
            all_aligned &= quality == 1.0;
            all_interior &= quality == 0.0;
            product *= (1.0 - term.ambiguity * (1.0 - quality)).max(0.12);
        }
        let factor = if self.terms.is_empty() {
            1.0
        } else {
            product.powf(1.0 / self.terms.len() as f64).clamp(0.0, 1.0)
        };
        let match_class = if all_aligned {
            "aligned"
        } else if all_interior {
            "interior"
        } else {
            "partial"
        };
        Ok(CompactBoundaryScore {
            factor,
            match_class,
        })
    }

    fn evaluate_boundary_only_compact(
        &self,
        text: &str,
        spans: &[Option<Span>],
    ) -> Result<CompactBoundaryScore, BoundaryError> {
        let prepared = BoundaryOnlyText::new(text);
        let mut product = 1.0;
        let mut all_aligned = !self.terms.is_empty();
        let mut all_interior = !self.terms.is_empty();
        for (term, span) in self.terms.iter().zip(spans) {
            let quality = match span {
                None => 0.0,
                Some([start, end]) => {
                    let (start, end) = checked_span(*start, *end, prepared.char_len)?;
                    prepared.quality(start, end)
                }
            };
            all_aligned &= quality == 1.0;
            all_interior &= quality == 0.0;
            product *= (1.0 - term.ambiguity * (1.0 - quality)).max(0.12);
        }
        let factor = if self.terms.is_empty() {
            1.0
        } else {
            product.powf(1.0 / self.terms.len() as f64).clamp(0.0, 1.0)
        };
        let match_class = if all_aligned {
            "aligned"
        } else if all_interior {
            "interior"
        } else {
            "partial"
        };
        Ok(CompactBoundaryScore {
            factor,
            match_class,
        })
    }

    fn evaluate_boundary_ascii_compact(
        &self,
        text: &str,
        prepared: &BoundaryOnlyText,
    ) -> CompactBoundaryScore {
        let mut product = 1.0;
        let mut all_aligned = true;
        let mut all_interior = true;
        for term in &self.terms {
            let quality = prepared.best_ascii_quality(text, term.folded_ascii.as_deref().unwrap());
            all_aligned &= quality == 1.0;
            all_interior &= quality == 0.0;
            product *= (1.0 - term.ambiguity * (1.0 - quality)).max(0.12);
        }
        CompactBoundaryScore {
            factor: product.powf(1.0 / self.terms.len() as f64).clamp(0.0, 1.0),
            match_class: if all_aligned {
                "aligned"
            } else if all_interior {
                "interior"
            } else {
                "partial"
            },
        }
    }

    pub fn evaluate(
        &self,
        text: &str,
        spans: Option<&[Option<Span>]>,
        validate_spans: bool,
    ) -> Result<BoundaryScore, BoundaryError> {
        if spans.is_some_and(|values| values.len() != self.terms.len()) {
            return Err(BoundaryError("one span is required per query token"));
        }
        if text.is_ascii()
            && self
                .terms
                .iter()
                .all(|term| term.folded.iter().all(char::is_ascii))
        {
            self.evaluate_ascii(text, spans, validate_spans)
        } else {
            self.evaluate_unicode(text, spans, validate_spans)
        }
    }

    fn evaluate_ascii(
        &self,
        text: &str,
        spans: Option<&[Option<Span>]>,
        validate_spans: bool,
    ) -> Result<BoundaryScore, BoundaryError> {
        let folded = text.to_ascii_lowercase();
        let mut qualities = Vec::with_capacity(self.terms.len());
        let mut selected = Vec::with_capacity(self.terms.len());
        let mut matched = true;
        for (index, term) in self.terms.iter().enumerate() {
            let needle = term.folded_ascii.as_deref().unwrap();
            let span = if let Some(values) = spans {
                values[index]
            } else {
                best_ascii_span(text, &folded, needle)
            };
            let quality = match span {
                None => {
                    matched = false;
                    0.0
                }
                Some([start, end]) => {
                    let (start, end) = checked_span(start, end, text.len())?;
                    if spans.is_some() && validate_spans && !folded[start..end].contains(needle) {
                        return Err(BoundaryError("span does not identify its query token"));
                    }
                    ascii_quality(text.as_bytes(), start, end)
                }
            };
            selected.push(span);
            qualities.push(quality);
        }
        Ok(finish_score(&self.terms, qualities, selected, matched))
    }

    fn evaluate_unicode(
        &self,
        text: &str,
        spans: Option<&[Option<Span>]>,
        validate_spans: bool,
    ) -> Result<BoundaryScore, BoundaryError> {
        let prepared = PreparedText::new(text);
        let mut qualities = Vec::with_capacity(self.terms.len());
        let mut selected = Vec::with_capacity(self.terms.len());
        let mut matched = true;
        for (index, term) in self.terms.iter().enumerate() {
            let span = if let Some(values) = spans {
                values[index]
            } else {
                prepared.best_span(&term.folded)
            };
            let quality = match span {
                None => {
                    matched = false;
                    0.0
                }
                Some([start, end]) => {
                    let (start, end) = checked_span(start, end, prepared.original.len())?;
                    if spans.is_some()
                        && validate_spans
                        && !prepared
                            .folded_slice(start, end)
                            .contains(&term.folded.iter().collect::<String>())
                    {
                        return Err(BoundaryError("span does not identify its query token"));
                    }
                    prepared.quality(start, end)
                }
            };
            selected.push(span);
            qualities.push(quality);
        }
        Ok(finish_score(&self.terms, qualities, selected, matched))
    }
}

fn checked_span(start: i64, end: i64, length: usize) -> Result<(usize, usize), BoundaryError> {
    if start < 0 || end < start || usize::try_from(end).map_or(true, |end| end > length) {
        return Err(BoundaryError("boundary span outside text"));
    }
    Ok((start as usize, end as usize))
}

fn best_ascii_span(text: &str, folded: &str, needle: &str) -> Option<Span> {
    if needle.is_empty() {
        return None;
    }
    let mut best = None;
    let mut best_quality = -1.0;
    let mut offset = 0;
    while offset <= folded.len() {
        let Some(relative) = folded[offset..].find(needle) else {
            break;
        };
        let start = offset + relative;
        let end = start + needle.len();
        let quality = ascii_quality(text.as_bytes(), start, end);
        if quality > best_quality {
            best = Some([start as i64, end as i64]);
            best_quality = quality;
        }
        offset = start + 1;
    }
    best
}

fn best_ascii_quality(text: &[u8], folded: &str, needle: &str) -> f64 {
    if needle.is_empty() || needle.len() > text.len() {
        return 0.0;
    }
    let mut best: f64 = 0.0;
    let mut offset = 0;
    while offset <= folded.len() {
        let Some(relative) = folded[offset..].find(needle) else {
            break;
        };
        let start = offset + relative;
        let end = start + needle.len();
        best = best.max(ascii_quality(text, start, end));
        if best == 1.0 {
            break;
        }
        offset = start + 1;
    }
    best
}

fn ascii_quality(text: &[u8], start: usize, end: usize) -> f64 {
    (u8::from(ascii_boundary(text, start)) as f64 + u8::from(ascii_boundary(text, end)) as f64)
        / 2.0
}

fn ascii_boundary(text: &[u8], index: usize) -> bool {
    if index == 0 || index == text.len() {
        return true;
    }
    let left = text[index - 1];
    let right = text[index];
    if right == b'\''
        && index + 1 < text.len()
        && left.is_ascii_alphabetic()
        && text[index + 1].is_ascii_alphabetic()
    {
        return false;
    }
    if left == b'\''
        && index >= 2
        && text[index - 2].is_ascii_alphabetic()
        && right.is_ascii_alphabetic()
    {
        return false;
    }
    if !left.is_ascii_alphanumeric() || !right.is_ascii_alphanumeric() {
        return true;
    }
    if left.is_ascii_lowercase() && right.is_ascii_uppercase() {
        return true;
    }
    if left.is_ascii_uppercase()
        && right.is_ascii_uppercase()
        && index + 1 < text.len()
        && text[index + 1].is_ascii_lowercase()
    {
        return true;
    }
    (left.is_ascii_alphabetic() && right.is_ascii_digit())
        || (left.is_ascii_digit() && right.is_ascii_alphabetic())
}

struct PreparedText {
    original: Vec<char>,
    folded: Vec<char>,
    starts: Vec<usize>,
    ends: Vec<usize>,
    boundaries: HashSet<usize>,
}

struct BoundaryOnlyText {
    char_len: usize,
    char_boundaries: HashSet<usize>,
    byte_boundaries: HashSet<usize>,
    unstable_ascii: HashSet<u8>,
    normalization_can_delete: bool,
}

impl BoundaryOnlyText {
    fn new(text: &str) -> Self {
        let grouped = clusters(text);
        let char_len = grouped.last().map_or(0, |cluster| cluster.end);
        let mut char_boundaries = HashSet::from([0, char_len]);
        let mut byte_boundaries = HashSet::from([0, text.len()]);
        let mut unstable_ascii = HashSet::new();
        let mut normalization_can_delete = false;
        for cluster in &grouped {
            let value = &text[cluster.start_byte..cluster.end_byte];
            if value.is_ascii() {
                continue;
            }
            unstable_ascii.extend(
                value
                    .bytes()
                    .filter(u8::is_ascii)
                    .map(|byte| byte.to_ascii_lowercase()),
            );
            let compatible: String = value.nfkc().collect();
            let normalized = unicode_v16::case_fold(&compatible);
            normalization_can_delete |= normalized.is_empty();
            unstable_ascii.extend(
                normalized
                    .bytes()
                    .filter(u8::is_ascii)
                    .map(|byte| byte.to_ascii_lowercase()),
            );
        }
        for index in 1..grouped.len() {
            if boundary_between(text, &grouped, index) {
                char_boundaries.insert(grouped[index].start);
                byte_boundaries.insert(grouped[index].start_byte);
            }
        }
        Self {
            char_len,
            char_boundaries,
            byte_boundaries,
            unstable_ascii,
            normalization_can_delete,
        }
    }

    fn quality(&self, start: usize, end: usize) -> f64 {
        (u8::from(self.char_boundaries.contains(&start)) as f64
            + u8::from(self.char_boundaries.contains(&end)) as f64)
            / 2.0
    }

    fn can_score_ascii(&self, terms: &[BoundaryTerm]) -> bool {
        !self.normalization_can_delete
            && terms.iter().all(|term| {
                term.folded_ascii
                    .as_deref()
                    .unwrap()
                    .bytes()
                    .all(|byte| !self.unstable_ascii.contains(&byte))
            })
    }

    fn best_ascii_quality(&self, text: &str, needle: &str) -> f64 {
        if needle.is_empty() || needle.len() > text.len() {
            return 0.0;
        }
        let mut best: f64 = 0.0;
        for (start, window) in text.as_bytes().windows(needle.len()).enumerate() {
            if !window.eq_ignore_ascii_case(needle.as_bytes()) {
                continue;
            }
            let end = start + needle.len();
            let quality = (u8::from(self.byte_boundaries.contains(&start)) as f64
                + u8::from(self.byte_boundaries.contains(&end)) as f64)
                / 2.0;
            best = best.max(quality);
            if best == 1.0 {
                break;
            }
        }
        best
    }
}

impl PreparedText {
    fn new(text: &str) -> Self {
        let grouped = clusters(text);
        let original: Vec<char> = text.chars().collect();
        let mut folded = Vec::new();
        let mut starts = Vec::new();
        let mut ends = Vec::new();
        for cluster in &grouped {
            let compatible: String = text[cluster.start_byte..cluster.end_byte].nfkc().collect();
            let normalized = unicode_v16::case_fold(&compatible);
            let length = normalized.chars().count();
            folded.extend(normalized.chars());
            starts.extend(std::iter::repeat_n(cluster.start, length));
            ends.extend(std::iter::repeat_n(cluster.end, length));
        }
        let mut boundaries = HashSet::from([0, original.len()]);
        for index in 1..grouped.len() {
            if boundary_between(text, &grouped, index) {
                boundaries.insert(grouped[index].start);
            }
        }
        Self {
            original,
            folded,
            starts,
            ends,
            boundaries,
        }
    }

    fn best_span(&self, needle: &[char]) -> Option<Span> {
        if needle.is_empty() || needle.len() > self.folded.len() {
            return None;
        }
        let mut seen = HashSet::new();
        let mut best = None;
        let mut best_quality = -1.0;
        for index in 0..=self.folded.len() - needle.len() {
            if self.folded[index..index + needle.len()] != *needle {
                continue;
            }
            let start = self.starts[index];
            let end = self.ends[index + needle.len() - 1];
            if !seen.insert((start, end)) {
                continue;
            }
            let quality = self.quality(start, end);
            if quality > best_quality {
                best = Some([start as i64, end as i64]);
                best_quality = quality;
            }
        }
        best
    }

    fn quality(&self, start: usize, end: usize) -> f64 {
        (u8::from(self.boundaries.contains(&start)) as f64
            + u8::from(self.boundaries.contains(&end)) as f64)
            / 2.0
    }

    fn folded_slice(&self, start: usize, end: usize) -> String {
        normalize_token(&self.original[start..end].iter().collect::<String>())
    }
}

#[derive(Clone, Copy)]
struct Cluster {
    start: usize,
    end: usize,
    start_byte: usize,
    end_byte: usize,
    base: char,
}

fn clusters(text: &str) -> Vec<Cluster> {
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let mut out = Vec::new();
    let mut index = 0;
    while index < chars.len() {
        let start = index;
        let mut hangul = hangul_kind(chars[index].1);
        index += 1;
        while index < chars.len() {
            let character = chars[index].1;
            if unicode_v16::is_attachment(character) {
                index += 1;
                continue;
            }
            let next_hangul = hangul_kind(character);
            if hangul == HangulKind::L && next_hangul == HangulKind::V {
                hangul = HangulKind::Lv;
                index += 1;
                continue;
            }
            if hangul == HangulKind::Lv && next_hangul == HangulKind::T {
                hangul = HangulKind::Lvt;
                index += 1;
                continue;
            }
            if character == '\u{200d}' && index + 1 < chars.len() {
                index += 2;
                continue;
            }
            break;
        }
        let base = chars[start..index]
            .iter()
            .find_map(|(_, character)| {
                (!unicode_v16::is_attachment(*character) && *character != '\u{200d}')
                    .then_some(*character)
            })
            .unwrap_or(chars[start].1);
        out.push(Cluster {
            start,
            end: index,
            start_byte: chars[start].0,
            end_byte: chars.get(index).map_or(text.len(), |(byte, _)| *byte),
            base,
        });
    }
    out
}

#[derive(Clone, Copy, Eq, PartialEq)]
enum HangulKind {
    None,
    L,
    V,
    T,
    Lv,
    Lvt,
}

fn hangul_kind(character: char) -> HangulKind {
    match character as u32 {
        0x1100..=0x115f | 0xa960..=0xa97c => HangulKind::L,
        0x1160..=0x11a7 | 0xd7b0..=0xd7c6 => HangulKind::V,
        0x11a8..=0x11ff | 0xd7cb..=0xd7fb => HangulKind::T,
        _ => HangulKind::None,
    }
}

fn boundary_between(text: &str, grouped: &[Cluster], index: usize) -> bool {
    if apostrophe_joins(text, grouped, index) {
        return false;
    }
    let left = grouped[index - 1].base;
    let right = grouped[index].base;
    if unicode_v16::is_separator(left) || unicode_v16::is_separator(right) {
        return true;
    }
    if unicode_v16::is_lower(left) && unicode_v16::is_upper(right) {
        return true;
    }
    if unicode_v16::is_upper(left)
        && unicode_v16::is_upper(right)
        && index + 1 < grouped.len()
        && unicode_v16::is_lower(grouped[index + 1].base)
    {
        return true;
    }
    if (unicode_v16::is_alpha(left) && unicode_v16::is_digit(right))
        || (unicode_v16::is_digit(left) && unicode_v16::is_alpha(right))
    {
        return true;
    }
    let left_script = unicode_v16::script(left);
    let right_script = unicode_v16::script(right);
    unicode_v16::is_alpha(left)
        && unicode_v16::is_alpha(right)
        && left_script != 0
        && right_script != 0
        && left_script != right_script
}

fn apostrophe_joins(text: &str, grouped: &[Cluster], index: usize) -> bool {
    let left = &grouped[index - 1];
    let right = &grouped[index];
    let right_text = &text[right.start_byte..right.end_byte];
    if matches!(right_text, "'" | "\u{2019}" | "\u{02bc}") && index + 1 < grouped.len() {
        return unicode_v16::is_alpha(left.base) && unicode_v16::is_alpha(grouped[index + 1].base);
    }
    let left_text = &text[left.start_byte..left.end_byte];
    if matches!(left_text, "'" | "\u{2019}" | "\u{02bc}") && index >= 2 {
        return unicode_v16::is_alpha(grouped[index - 2].base) && unicode_v16::is_alpha(right.base);
    }
    false
}

fn ambiguity(prior: f64, value: Option<&StatValue>) -> f64 {
    let Some(value) = value else {
        return prior;
    };
    let (n, s) = value.counts();
    if n <= 0 {
        return prior;
    }
    let s = s.clamp(0, n);
    ((n - s) as f64 + 32.0 * prior) / (n as f64 + 32.0)
}

fn finish_score(
    terms: &[BoundaryTerm],
    qualities: Vec<f64>,
    spans: Vec<Option<Span>>,
    matched: bool,
) -> BoundaryScore {
    let penalties: Vec<f64> = terms
        .iter()
        .zip(&qualities)
        .map(|(term, quality)| (1.0 - term.ambiguity * (1.0 - quality)).max(0.12))
        .collect();
    let factor = if penalties.is_empty() {
        1.0
    } else {
        penalties
            .iter()
            .product::<f64>()
            .powf(1.0 / penalties.len() as f64)
    }
    .clamp(0.0, 1.0);
    let match_class = if !qualities.is_empty() && qualities.iter().all(|quality| *quality == 1.0) {
        "aligned"
    } else if !qualities.is_empty() && qualities.iter().all(|quality| *quality == 0.0) {
        "interior"
    } else {
        "partial"
    };
    BoundaryScore {
        factor,
        qualities,
        match_class,
        matched,
        spans,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        cold_prior, decut_text, normalize_token, prepare_query, query_tokens, BoundaryScore, Stats,
    };

    #[test]
    fn query_shape_and_normalization_match_python() {
        assert_eq!(
            query_tokens(" cyber-filter_thing "),
            ["cyber", "filter", "thing"]
        );
        assert_eq!(normalize_token("Straße"), "strasse");
        assert_eq!(normalize_token("e\u{301}"), "é");
        assert_eq!(cold_prior("id"), 0.90);
        assert_eq!(cold_prior("東京"), 0.0);
    }

    #[test]
    fn evaluates_code_boundaries() {
        let score = prepare_query("http", &Stats::new())
            .evaluate("myHTTPServer", None, true)
            .unwrap();
        assert_eq!(score.match_class, "aligned");
        assert_eq!(score.spans, [Some([2, 6])]);
    }

    #[test]
    fn python_oracle_matches() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../py/fixtures/boundary_conformance.json");
        let fixture: serde_json::Value =
            serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
        assert_eq!(fixture["schema"], 2);
        assert_eq!(fixture["python_unicode_version"], "16.0.0");
        for case in fixture["evaluations"].as_array().unwrap() {
            let name = case["name"].as_str().unwrap();
            let stats: Stats = case
                .get("stats")
                .cloned()
                .map(serde_json::from_value)
                .transpose()
                .unwrap()
                .unwrap_or_default();
            let spans = case
                .get("spans")
                .cloned()
                .map(serde_json::from_value::<Vec<Option<[i64; 2]>>>)
                .transpose()
                .unwrap();
            let result = prepare_query(case["query"].as_str().unwrap(), &stats).evaluate(
                case["text"].as_str().unwrap(),
                spans.as_deref(),
                case.get("validate_spans")
                    .and_then(|value| value.as_bool())
                    .unwrap_or(true),
            );
            let expected = &case["expected"];
            if let Some(error) = expected.get("error") {
                assert_eq!(
                    result.unwrap_err().to_string(),
                    error.as_str().unwrap(),
                    "{name}"
                );
                continue;
            }
            assert_score(name, &result.unwrap(), expected);
        }
    }

    #[test]
    fn compact_path_matches_full_unicode_scoring() {
        let chunks = [
            "a", "A", "b", "_", "'", "é", "e\u{301}", "😀", "東京", "ß", "\u{200d}", "-",
        ];
        let queries = ["a", "ab", "e", "ss", "id", "東京"];
        let mut state = 0x9e37_79b9_u32;
        for case in 0..500 {
            let mut text = String::new();
            for _ in 0..12 {
                state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                text.push_str(chunks[(state as usize) % chunks.len()]);
            }
            let query = queries[case % queries.len()];
            let prepared = prepare_query(query, &Stats::new());
            let full = prepared.evaluate(&text, None, true).unwrap();
            let compact = prepared.evaluate_compact(&text, None, true).unwrap();
            assert!(
                (full.factor - compact.factor).abs() <= 1e-12,
                "query={query:?} text={text:?}"
            );
            assert_eq!(full.match_class, compact.match_class);
        }

        let prepared = prepare_query("a", &Stats::new());
        let full = prepared.evaluate("a\u{301}", None, true).unwrap();
        let compact = prepared.evaluate_compact("a\u{301}", None, true).unwrap();
        assert!(!full.matched);
        assert_eq!(compact.factor, full.factor);
        assert_eq!(compact.match_class, full.match_class);
    }

    #[test]
    fn native_decut_matches_snippet_edge_rules() {
        assert_eq!(decut_text("plain", true), "plain");
        assert_eq!(decut_text("a…b", false), "aab");
        assert_eq!(decut_text("……x", false), " xx");
        assert_eq!(decut_text("…abc", true), "aaabc");
        assert_eq!(decut_text("abc…", true), "abccc");
        assert_eq!(decut_text("…a…", true), "aaaaa");
    }

    fn assert_score(name: &str, actual: &BoundaryScore, expected: &serde_json::Value) {
        let expected_factor = expected["factor"].as_f64().unwrap();
        assert!(
            (actual.factor - expected_factor).abs() <= 1e-12,
            "{name}: factor"
        );
        assert_eq!(
            actual.qualities,
            serde_json::from_value::<Vec<f64>>(expected["qualities"].clone()).unwrap(),
            "{name}"
        );
        assert_eq!(
            actual.match_class,
            expected["match_class"].as_str().unwrap(),
            "{name}"
        );
        assert_eq!(
            actual.matched,
            expected["matched"].as_bool().unwrap(),
            "{name}"
        );
        assert_eq!(
            actual.spans,
            serde_json::from_value::<Vec<Option<[i64; 2]>>>(expected["spans"].clone()).unwrap(),
            "{name}"
        );
    }
}
