//! 0.4.9's keyword hit windows. A hit shows its selected match with [`PAD`] characters on
//! each side, whitespace collapsed and cuts marked, and its keyword score counts the query
//! only inside that snippet.
use crate::{boundary_rank, unicode_v16};
use std::cmp::Ordering;
use std::ops::Range;

/// Characters a window keeps on each side of its match.
pub const PAD: usize = 80;
/// The cut marker. Folding turns it into three periods, so no folded token holds it.
pub const CUT: char = '…';

/// Python's `round(value, 4)` for a score: the multiple of 1e-4 nearest the exact binary
/// value, ties to even, as the double nearest that decimal.
pub fn round4(value: f64) -> f64 {
    if !value.is_finite() || value <= 0.0 {
        return value;
    }
    let scaled = value * 1e4;
    let floor = scaled.floor();
    let fraction = scaled - floor;
    // The product errs by about 1e-12 at most, so only a value that close to a tie needs
    // its exact expansion.
    let up = if (fraction - 0.5).abs() > 1e-9 {
        fraction > 0.5
    } else {
        match half_above(value, floor) {
            Ordering::Less => false,
            Ordering::Greater => true,
            Ordering::Equal => floor % 2.0 == 1.0,
        }
    };
    (floor + f64::from(u8::from(up))) / 1e4
}

/// How `value * 10^4` compares with `floor + 0.5`, exactly.
fn half_above(value: f64, floor: f64) -> Ordering {
    let bits = value.to_bits();
    let biased = ((bits >> 52) & 0x7ff) as i64;
    let fraction = u128::from(bits & ((1 << 52) - 1));
    let (mantissa, exponent) = if biased == 0 {
        (fraction, -1074)
    } else {
        (fraction | 1 << 52, biased - 1075)
    };
    // value * 10^4 = mantissa * 625 * 2^(exponent + 4) and floor + 0.5 = (2 floor + 1) / 2.
    let left = mantissa * 625;
    let right = 2 * floor as u128 + 1;
    let shift = exponent + 5;
    if shift >= 0 {
        if shift > 40 {
            return Ordering::Greater;
        }
        (left << shift).cmp(&right)
    } else if shift < -100 {
        Ordering::Less
    } else {
        left.cmp(&(right << -shift))
    }
}

/// A row's folded text (`store::normalize`) and the original bytes each folded byte
/// came from.
pub(crate) struct Folded<'a> {
    pub text: &'a str,
    pub low: String,
    /// Each non-ASCII cluster: folded start and end, original start and end. Bytes
    /// between clusters are ASCII and fold one for one.
    clusters: Vec<[usize; 4]>,
}

impl<'a> Folded<'a> {
    pub fn new(text: &'a str) -> Self {
        if text.is_ascii() {
            return Self {
                text,
                low: text.to_ascii_lowercase(),
                clusters: Vec::new(),
            };
        }
        let (low, clusters) = boundary_rank::normalize_mapped(text);
        Self {
            text,
            low,
            clusters,
        }
    }
    fn cluster(&self, index: usize) -> Option<[usize; 4]> {
        index.checked_sub(1).map(|index| self.clusters[index])
    }
    /// The original byte where folded byte `at` begins.
    pub fn start(&self, at: usize) -> usize {
        match self.cluster(self.clusters.partition_point(|c| c[0] <= at)) {
            Some([_, end, original, _]) if at < end => original,
            Some([_, end, _, original]) => original + (at - end),
            None => at,
        }
    }
    /// The original byte where a folded range ending at `at` ends.
    pub fn end(&self, at: usize) -> usize {
        if at == 0 {
            return 0;
        }
        match self.cluster(self.clusters.partition_point(|c| c[0] < at)) {
            Some([_, end, _, original]) if at <= end => original,
            Some([_, end, _, original]) => original + (at - end),
            None => at,
        }
    }
    /// The folded offset of the first whole cluster at or after original byte `at`.
    fn from(&self, at: usize) -> usize {
        match self.cluster(self.clusters.partition_point(|c| c[2] <= at)) {
            Some([start, _, original, _]) if original == at => start,
            Some([_, end, _, original]) if at < original => end,
            Some([_, end, _, original]) => end + (at - original),
            None => at,
        }
    }
    /// The folded offset where the whole clusters before original byte `at` end.
    fn to(&self, at: usize) -> usize {
        match self.cluster(self.clusters.partition_point(|c| c[2] < at)) {
            Some([start, _, _, original]) if at < original => start,
            Some([_, end, _, original]) => end + (at - original),
            None => at,
        }
    }
}

/// 0.4.9's `_ascii_boundary`, with Python's character classes: whether a word boundary
/// falls at byte `at` of `text`.
fn boundary(text: &str, at: usize) -> bool {
    if at == 0 || at == text.len() {
        return true;
    }
    let mut before = text[..at].chars().rev();
    let mut after = text[at..].chars();
    let (Some(left), Some(right)) = (before.next(), after.next()) else {
        return true;
    };
    let (left2, right2) = (before.next(), after.next());
    let alpha = unicode_v16::is_alpha;
    if right == '\'' && right2.is_some_and(alpha) && alpha(left) {
        return false;
    }
    if left == '\'' && left2.is_some_and(alpha) && alpha(right) {
        return false;
    }
    if !unicode_v16::is_alphanumeric(left) || !unicode_v16::is_alphanumeric(right) {
        return true;
    }
    if unicode_v16::is_lower(left) && unicode_v16::is_upper(right) {
        return true;
    }
    if unicode_v16::is_upper(left)
        && unicode_v16::is_upper(right)
        && right2.is_some_and(unicode_v16::is_lower)
    {
        return true;
    }
    (alpha(left) && unicode_v16::is_digit(right)) || (unicode_v16::is_digit(left) && alpha(right))
}

/// 0.4.9's `insensitive_span` over folded text: of the occurrences of each spelling in
/// turn, the first aligned at both ends, else the first of the best quality, as an
/// original byte range.
pub(crate) fn insensitive_span(row: &Folded, spellings: &[String]) -> Option<Range<usize>> {
    let mut best = None;
    let mut best_quality = -1.0;
    for spelling in spellings.iter().filter(|s| !s.is_empty()) {
        let mut from = 0;
        while let Some(found) = row.low[from..].find(spelling.as_str()) {
            let at = from + found;
            let span = row.start(at)..row.end(at + spelling.len());
            let quality = (f64::from(u8::from(boundary(row.text, span.start)))
                + f64::from(u8::from(boundary(row.text, span.end))))
                / 2.0;
            if quality > best_quality {
                best_quality = quality;
                best = Some(span);
                if quality == 1.0 {
                    return best;
                }
            }
            from = at + row.low[at..].chars().next().map_or(1, char::len_utf8);
        }
    }
    best
}

/// The byte `count` characters before `at`, or the start.
fn back(text: &str, at: usize, count: usize) -> usize {
    text[..at]
        .char_indices()
        .rev()
        .nth(count - 1)
        .map_or(0, |(index, _)| index)
}

/// The byte `count` characters after `at`, or the end.
fn forward(text: &str, at: usize, count: usize) -> usize {
    text[at..]
        .char_indices()
        .nth(count)
        .map_or(text.len(), |(index, _)| at + index)
}

/// One window of a snippet.
struct Piece {
    /// The original bytes it shows.
    body: Range<usize>,
    /// Snippet character index of each body byte offset (meaningful on character
    /// boundaries), and of the body's end at `body.len()`.
    at: Vec<usize>,
}

/// A hit's snippet: 0.4.9's `snip_spans` of the original text, which is `snip_at` for one
/// span.
pub(crate) struct Snip {
    pub text: String,
    /// Its length in characters.
    pub chars: usize,
    pieces: Vec<Piece>,
}

impl Snip {
    pub fn new(text: &str, spans: &[Range<usize>]) -> Self {
        Self::padded(text, spans, PAD)
    }
    /// `snip_spans` with `pad` characters kept on each side of a span.
    pub fn padded(text: &str, spans: &[Range<usize>], pad: usize) -> Self {
        let mut sorted = spans.to_vec();
        sorted.sort_by_key(|span| (span.start, span.end));
        let mut windows: Vec<Range<usize>> = Vec::new();
        for span in sorted {
            let (a, b) = (back(text, span.start, pad), forward(text, span.end, pad));
            match windows.last_mut() {
                Some(last) if a <= last.end => last.end = last.end.max(b),
                _ => windows.push(a..b),
            }
        }
        let mut snip = Self {
            text: String::new(),
            chars: 0,
            pieces: Vec::new(),
        };
        for window in windows {
            let mut part = String::new();
            let (mut piece, count) = render(text, window, &mut part);
            if count == 0 {
                continue;
            }
            let base = if snip.chars == 0 {
                0
            } else {
                snip.text.push(' ');
                snip.chars + 1
            };
            for index in &mut piece.at {
                *index += base;
            }
            snip.text.push_str(&part);
            snip.chars = base + count;
            snip.pieces.push(piece);
        }
        snip
    }
    /// The snippet character index of original byte `at` in window `piece`.
    fn index(&self, piece: usize, at: usize) -> usize {
        let piece = &self.pieces[piece];
        piece.at[at - piece.body.start]
    }
}

/// `one_line(cut + text[body] + cut)`, with its length in characters and each body
/// byte's character index.
fn render(text: &str, body: Range<usize>, out: &mut String) -> (Piece, usize) {
    let mut count = 0;
    let mut pending = false;
    if body.start > 0 {
        out.push(CUT);
        count += 1;
    }
    let mut at = vec![0; body.len() + 1];
    for (offset, c) in text[body.clone()].char_indices() {
        if unicode_v16::is_space(c) {
            at[offset] = count;
            pending |= count > 0;
            continue;
        }
        if pending {
            out.push(' ');
            count += 1;
            pending = false;
        }
        at[offset] = count;
        out.push(c);
        count += 1;
    }
    at[body.len()] = count;
    if body.end < text.len() {
        if pending {
            out.push(' ');
            count += 1;
            at[body.len()] = count;
        }
        out.push(CUT);
        count += 1;
    }
    (Piece { body, at }, count)
}

/// The folded text a snippet's matches are found in: each window's whole clusters, with
/// its cut markers and the joining spaces kept as they are, so no token matches them.
pub(crate) struct View {
    pub text: String,
    /// For each folded run: its view offset, folded offset, length and window.
    runs: Vec<[usize; 4]>,
}

impl View {
    pub fn new(row: &Folded, snip: &Snip) -> Self {
        let mut text = String::new();
        let mut runs = Vec::new();
        for (index, piece) in snip.pieces.iter().enumerate() {
            if index > 0 {
                text.push(' ');
            }
            if piece.body.start > 0 {
                text.push(CUT);
            }
            let (from, to) = (row.from(piece.body.start), row.to(piece.body.end));
            if from < to {
                runs.push([text.len(), from, to - from, index]);
                text.push_str(&row.low[from..to]);
            }
            if piece.body.end < row.text.len() {
                text.push(CUT);
            }
        }
        Self { text, runs }
    }
    /// The snippet characters of the view range `range`, which starts and ends inside
    /// folded runs because tokens never match the markers or spaces between them.
    pub fn span(&self, row: &Folded, snip: &Snip, range: Range<usize>) -> Range<usize> {
        let locate = |at: usize, end: bool| -> usize {
            let index = self
                .runs
                .partition_point(|run| if end { run[0] < at } else { run[0] <= at })
                .saturating_sub(1);
            let [view, folded, length, piece] = self.runs[index];
            let offset = (at.max(view) - view).min(length);
            let original = if end {
                row.end(folded + offset)
            } else {
                row.start(folded + offset)
            };
            snip.index(piece, original)
        };
        if self.runs.is_empty() {
            return 0..0;
        }
        locate(range.start, false)..locate(range.end, true)
    }
}

/// 0.4.9's match strength: `tight * (1 - 0.5^n)` for `n` matches, `tight` from the
/// shortest, `qlen` the query's characters without separators.
pub fn strength(n: u32, best: usize, qlen: usize) -> f64 {
    let tight = if n > 0 && qlen > 0 && best > 0 {
        (qlen as f64 / best as f64).min(1.0)
    } else if n > 0 {
        1.0
    } else {
        0.0
    };
    tight * (1.0 - 0.5f64.powi(n.min(1100) as i32))
}

/// 0.4.9's `_terms_proximity` from the boundary scorer's spans and qualities, in snippet
/// characters.
pub fn proximity(snippet: &str, spans: &[Option<[i64; 2]>], qualities: &[f64], qlen: usize) -> f64 {
    let selected: Vec<([i64; 2], f64)> = spans
        .iter()
        .zip(qualities)
        .filter_map(|(span, quality)| span.map(|span| (span, *quality)))
        .collect();
    let fraction = if qualities.is_empty() {
        1.0
    } else {
        selected.len() as f64 / qualities.len() as f64
    };
    if selected.len() < 2 {
        return fraction;
    }
    let first = selected.iter().map(|(span, _)| span[0]).min().unwrap_or(0);
    let last = selected.iter().map(|(span, _)| span[1]).max().unwrap_or(0);
    let spread = last - first + 80 * cuts(snippet, first, last);
    let scale = selected
        .iter()
        .map(|(_, quality)| (2.0 * quality - 1.0).max(0.0))
        .fold(f64::INFINITY, f64::min);
    fraction * (0.5 + 0.5 * (qlen as f64 / spread.max(1) as f64).min(1.0) * scale)
}

/// Python's `snippet.count("…", first, last)`, with its slice semantics.
fn cuts(snippet: &str, first: i64, last: i64) -> i64 {
    let length = snippet.chars().count() as i64;
    let clamp = |index: i64| {
        if index < 0 {
            (index + length).max(0)
        } else {
            index.min(length)
        }
    };
    let (from, to) = (clamp(first), clamp(last));
    if from >= to {
        return 0;
    }
    snippet
        .chars()
        .skip(from as usize)
        .take((to - from) as usize)
        .filter(|&c| c == CUT)
        .count() as i64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scores_round_half_even_on_the_exact_binary_value() {
        // Expected values are Python's round(x, 4).
        for (value, rounded) in [
            (0.03125, 0.0312),
            (0.09375, 0.0938),
            (0.46875, 0.4688),
            (0.15625, 0.1562),
            (0.4, 0.4),
            (0.123449999999, 0.1234),
            (0.12345, 0.1235),
            (0.43215, 0.4321),
            (0.00005, 0.0001),
            (0.000049999, 0.0),
            (0.99995, 1.0),
            (0.5 * 0.8 * 0.85, 0.34),
            (1.0, 1.0),
            (0.0, 0.0),
        ] {
            assert_eq!(round4(value), rounded, "{value}");
        }
    }

    fn snip(text: &str, start: usize, end: usize) -> String {
        Snip::new(text, std::slice::from_ref(&(start..end))).text
    }

    #[test]
    fn snippets_follow_snip_at_and_snip_spans() {
        // Expected strings are 0.4.9's console.snip_at / snip_spans.
        let text = format!("{}needle  in\n\nhay{}", "a ".repeat(50), " b".repeat(50));
        let at = text.find("needle").unwrap();
        assert_eq!(
            snip(&text, at, at + 6),
            format!("…{}needle in hay{} …", "a ".repeat(40), " b".repeat(35))
        );
        assert_eq!(snip("  x  ", 2, 3), "x");
        let long = "z".repeat(400);
        let both = Snip::new(&long, &[10..11, 300..301]).text;
        assert_eq!(both, format!("{}… …{}…", "z".repeat(91), "z".repeat(161)));
        let joined = Snip::new(&long, &[10..11, 170..171]).text;
        assert_eq!(joined, format!("{}…", "z".repeat(251)));
    }

    #[test]
    fn folded_offsets_map_back_to_original_clusters() {
        let text = "Café e\u{301}x ﬁle Σ";
        let row = Folded::new(text);
        assert_eq!(row.low, "café éx file σ");
        let at = row.low.find("éx").unwrap();
        assert_eq!(&text[row.start(at)..row.end(at + "éx".len())], "e\u{301}x");
        let at = row.low.find("fi").unwrap();
        assert_eq!(&text[row.start(at)..row.end(at + 2)], "ﬁ");
        assert_eq!(&text[row.start(at + 1)..row.end(at + 2)], "ﬁ");
        // A window edge inside a cluster drops the partial cluster.
        let inside = text.find('\u{301}').unwrap();
        assert_eq!(&row.low[row.from(inside)..row.to(text.len())], "x file σ");
        assert_eq!(&row.low[row.from(0)..row.to(inside)], "café ");
    }

    #[test]
    fn insensitive_span_prefers_the_first_aligned_occurrence() {
        // Expected spans are 0.4.9's console.insensitive_span, here in bytes.
        let span = |text: &str, token: &str| insensitive_span(&Folded::new(text), &[token.into()]);
        assert_eq!(span("xFooBar foo-bar Foo", "foo"), Some(1..4));
        assert_eq!(span("xfoox yfooy", "foo"), Some(1..4));
        assert_eq!(span("xfoox foo", "foo"), Some(6..9));
        assert_eq!(span("don't", "t"), Some(4..5));
        assert_eq!(span("Ünïcode xfoo Foo", "foo"), Some(15..18));
    }

    #[test]
    fn view_spans_count_collapsed_snippet_characters() {
        let text = format!("{}Alpha   \t beta{}", "q".repeat(100), "r".repeat(100));
        let row = Folded::new(&text);
        let at = text.find("Alpha").unwrap();
        let end = text.find("beta").unwrap() + 4;
        let snip = Snip::new(&text, std::slice::from_ref(&(at..end)));
        let view = View::new(&row, &snip);
        let found = regex::Regex::new(r"alpha[\W_]*beta").unwrap();
        let m = found.find(&view.text).unwrap();
        let span = view.span(&row, &snip, m.range());
        let chars: Vec<char> = snip.text.chars().collect();
        assert_eq!(chars[span.clone()].iter().collect::<String>(), "Alpha beta");
        assert_eq!(span.len(), 10);
    }

    #[test]
    fn proximity_counts_cuts_with_python_slices() {
        assert_eq!(cuts("a…b…c", 0, 5), 2);
        assert_eq!(cuts("a…b…c", -1, 3), 0);
        assert_eq!(cuts("a…b…c", 1, 2), 1);
        // Expected values are 0.4.9's search._terms_proximity.
        let spans = [Some([0, 3]), Some([4, 8])];
        assert_eq!(proximity("abc defg", &spans, &[1.0, 1.0], 7), 0.9375);
        assert_eq!(
            proximity("abc…defg", &spans, &[1.0, 1.0], 7),
            0.5397727272727273
        );
        assert_eq!(proximity("abc defg", &spans, &[1.0, 0.75], 7), 0.71875);
        assert_eq!(proximity("abc defg", &spans, &[1.0, 0.5], 7), 0.5);
        assert_eq!(
            proximity("abc defg", &[Some([0, 3]), None], &[1.0, 0.0], 7),
            0.5
        );
    }
}
