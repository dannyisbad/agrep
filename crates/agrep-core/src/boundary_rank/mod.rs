use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::fmt;

use serde::Deserialize;
use unicode_normalization::UnicodeNormalization;

use crate::unicode_v16;

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
    variants: Vec<Vec<char>>,
    ascii_variants: Option<Vec<String>>,
    ambiguity: f64,
}

#[derive(Clone, Debug)]
pub struct PreparedQuery {
    terms: Vec<BoundaryTerm>,
}

#[derive(Clone, Debug, PartialEq)]
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

#[derive(Debug)]
pub struct CompactBoundaryScore {
    pub factor: f64,
    pub match_class: &'static str,
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
        let variant_strings = term_variants(original);
        let ascii_variants = variant_strings
            .iter()
            .all(|variant| variant.is_ascii())
            .then(|| variant_strings.clone());
        terms.push(BoundaryTerm {
            variants: variant_strings
                .iter()
                .map(|variant| variant.chars().collect())
                .collect(),
            ascii_variants,
            ambiguity,
        });
    }
    PreparedQuery { terms }
}

impl PreparedQuery {
    /// How many query tokens it grades (tokens that fold to nothing are dropped).
    pub(crate) fn len(&self) -> usize {
        self.terms.len()
    }
    pub(crate) fn evaluate_compact(
        &self,
        text: &str,
        spans: Option<&[Option<Span>]>,
        validate_spans: bool,
    ) -> Result<CompactBoundaryScore, BoundaryError> {
        if spans.is_some_and(|values| values.len() != self.terms.len()) {
            return Err(BoundaryError("one span is required per query token"));
        }
        if text.is_ascii() && self.terms.iter().all(|term| term.ascii_variants.is_some()) {
            return self.evaluate_ascii_compact(text, spans, validate_spans);
        }
        if spans.is_none()
            && !self.terms.is_empty()
            && self.terms.iter().all(|term| {
                term.ascii_variants.as_ref().is_some_and(|variants| {
                    variants
                        .iter()
                        .any(|needle| aligned_ascii_occurrence(text, needle))
                })
            })
        {
            // One certified aligned occurrence is already the maximum quality.
            // Unicode elsewhere in a long row cannot improve that maximum.
            return Ok(CompactBoundaryScore {
                factor: 1.,
                match_class: "aligned",
            });
        }
        if spans.is_none()
            && self.terms.iter().all(|term| {
                term.ascii_variants.as_ref().is_some_and(|variants| {
                    variants
                        .iter()
                        .all(|v| v.bytes().all(|b| b.is_ascii_alphanumeric()))
                })
            })
            && ascii_separators_only(text)
        {
            return self.evaluate_ascii_compact(text, None, validate_spans);
        }
        if let Some(spans) = spans.filter(|_| !validate_spans) {
            return self.evaluate_boundary_only_compact(text, spans);
        }
        if spans.is_none()
            && !self.terms.is_empty()
            && self.terms.iter().all(|term| term.ascii_variants.is_some())
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
            let variants = term.ascii_variants.as_ref().unwrap();
            let quality = match spans.and_then(|values| values[index]) {
                None if spans.is_some() => 0.0,
                None => variants
                    .iter()
                    .map(|needle| best_ascii_quality(bytes, &folded, needle))
                    .fold(0.0, f64::max),
                Some([start, end]) => {
                    let (start, end) = checked_span(start, end, bytes.len())?;
                    if spans.is_some()
                        && validate_spans
                        && !variants.iter().any(|needle| {
                            folded.as_bytes()[start..end]
                                .windows(needle.len())
                                .any(|window| window == needle.as_bytes())
                        })
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
            let quality = term
                .ascii_variants
                .as_ref()
                .unwrap()
                .iter()
                .map(|needle| prepared.best_ascii_quality(text, needle))
                .fold(0.0, f64::max);
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
        if text.is_ascii() && self.terms.iter().all(|term| term.ascii_variants.is_some()) {
            self.evaluate_ascii(text, spans, validate_spans)
        } else if spans.is_none()
            && self.terms.iter().all(|term| {
                term.ascii_variants.as_ref().is_some_and(|variants| {
                    variants
                        .iter()
                        .all(|v| v.bytes().all(|b| b.is_ascii_alphanumeric()))
                })
            })
            && ascii_separators_only(text)
        {
            let mut score = self.evaluate_ascii(text, None, validate_spans)?;
            for span in score.spans.iter_mut().flatten() {
                span[0] = text[..span[0] as usize].chars().count() as i64;
                span[1] = text[..span[1] as usize].chars().count() as i64;
            }
            Ok(score)
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
            let variants = term.ascii_variants.as_ref().unwrap();
            let span = if let Some(values) = spans {
                values[index]
            } else {
                variants.iter().fold(None, |best, needle| {
                    let candidate = best_ascii_span(text, &folded, needle);
                    match (best, candidate) {
                        (None, candidate) => candidate,
                        (best, None) => best,
                        (Some(best), Some(candidate))
                            if ascii_quality(
                                text.as_bytes(),
                                candidate[0] as usize,
                                candidate[1] as usize,
                            ) > ascii_quality(
                                text.as_bytes(),
                                best[0] as usize,
                                best[1] as usize,
                            ) =>
                        {
                            Some(candidate)
                        }
                        (best, Some(_)) => best,
                    }
                })
            };
            let quality = match span {
                None => {
                    matched = false;
                    0.0
                }
                Some([start, end]) => {
                    let (start, end) = checked_span(start, end, text.len())?;
                    if spans.is_some()
                        && validate_spans
                        && !variants
                            .iter()
                            .any(|needle| folded[start..end].contains(needle))
                    {
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
                term.variants.iter().fold(None, |best, variant| {
                    let candidate = prepared.best_span(variant);
                    match (best, candidate) {
                        (None, candidate) => candidate,
                        (best, None) => best,
                        (Some(best), Some(candidate))
                            if prepared.quality(candidate[0] as usize, candidate[1] as usize)
                                > prepared.quality(best[0] as usize, best[1] as usize) =>
                        {
                            Some(candidate)
                        }
                        (best, Some(_)) => best,
                    }
                })
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
                        && !term.variants.iter().any(|variant| {
                            prepared
                                .folded_slice(start, end)
                                .contains(&variant.iter().collect::<String>())
                        })
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

// Separators cannot introduce an ASCII word occurrence or join an ASCII word
// across a normalization boundary. Apostrophe joiners deliberately take the
// full Unicode path. Original byte offsets remain sufficient for ASCII terms.
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

mod ascii;
use ascii::*;
mod normalize;
pub use normalize::{cold_prior, normalize_token, query_tokens, term_anchor, term_variants};
mod text;
use text::{clusters, hangul_kind, BoundaryOnlyText, Cluster, HangulKind, PreparedText};
#[cfg(test)]
#[path = "tests.rs"]
mod checks;
