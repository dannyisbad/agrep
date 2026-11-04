//! Unicode short-token statistics, computed per conversation family.
use std::collections::HashSet;
use unicode_normalization::UnicodeNormalization;
use unicode_segmentation::UnicodeSegmentation;
#[derive(Clone, Copy)]
struct GraphemeKind {
    alpha: bool,
    digit: bool,
    lower: bool,
    upper: bool,
    script: u8,
}
fn grapheme_kind(grapheme: &str) -> Option<GraphemeKind> {
    let base = grapheme
        .chars()
        .find(|ch| crate::unicode_v16::is_alphanumeric(*ch))?;
    Some(GraphemeKind {
        alpha: crate::unicode_v16::is_alpha(base),
        digit: crate::unicode_v16::is_digit(base),
        lower: crate::unicode_v16::is_lower(base),
        upper: crate::unicode_v16::is_upper(base),
        script: crate::unicode_v16::script(base),
    })
}

fn script_boundary(left: GraphemeKind, right: GraphemeKind) -> bool {
    left.alpha
        && right.alpha
        && left.script != right.script
        && left.script != 0
        && right.script != 0
}

fn normalize(graphemes: &[&str]) -> String {
    let mut out = String::new();
    for grapheme in graphemes {
        if grapheme.is_ascii() {
            out.extend(grapheme.bytes().map(|b| char::from(b.to_ascii_lowercase())));
            continue;
        }
        let compatible: String = grapheme.nfkc().collect();
        out.push_str(&crate::unicode_v16::case_fold(&compatible));
    }
    out
}

/// Receives every token `collect_text` finds; a token may arrive more than once.
pub(crate) trait TokenSink {
    fn token(&mut self, aligned: bool, token: &str);
}

struct TokenSets<'a> {
    anywhere: &'a mut HashSet<String>,
    aligned: &'a mut HashSet<String>,
}

impl TokenSink for TokenSets<'_> {
    fn token(&mut self, aligned: bool, token: &str) {
        let set = if aligned {
            &mut *self.aligned
        } else {
            &mut *self.anywhere
        };
        if !set.contains(token) {
            set.insert(token.to_owned());
        }
    }
}

fn insert_ngrams(run: &[&str], sink: &mut impl TokenSink) {
    let normalized = normalize(run);
    let bounds: Vec<usize> = UnicodeSegmentation::grapheme_indices(normalized.as_str(), true)
        .map(|(at, _)| at)
        .chain(std::iter::once(normalized.len()))
        .collect();
    let graphemes = bounds.len() - 1;
    for start in 0..graphemes {
        for width in 1..=4.min(graphemes - start) {
            sink.token(false, &normalized[bounds[start]..bounds[start + width]]);
        }
    }
}

fn insert_ascii(bytes: &[u8], aligned: bool, sink: &mut impl TokenSink) {
    let mut folded = [0u8; 4];
    for (index, byte) in bytes.iter().enumerate() {
        folded[index] = byte.to_ascii_lowercase();
    }
    sink.token(
        aligned,
        std::str::from_utf8(&folded[..bytes.len()]).unwrap(),
    );
}

fn insert_ascii_ngrams(run: &[u8], sink: &mut impl TokenSink) {
    for start in 0..run.len() {
        for width in 1..=4.min(run.len() - start) {
            let piece = &run[start..start + width];
            if piece.iter().all(u8::is_ascii_alphanumeric) {
                insert_ascii(piece, false, sink);
            }
        }
    }
}

fn ascii_boundary(run: &[u8], index: usize) -> bool {
    let left = run[index - 1];
    let right = run[index];
    let acronym_word = left.is_ascii_uppercase()
        && right.is_ascii_uppercase()
        && run
            .get(index + 1)
            .is_some_and(|next| next.is_ascii_lowercase());
    (left.is_ascii_lowercase() && right.is_ascii_uppercase())
        || (left.is_ascii_alphabetic() && right.is_ascii_digit())
        || (left.is_ascii_digit() && right.is_ascii_alphabetic())
        || acronym_word
}

fn collect_ascii_run(run: &[u8], sink: &mut impl TokenSink) {
    for chunk in run.split(|byte| *byte == b'\'') {
        if !chunk.is_empty() {
            insert_ascii_ngrams(chunk, sink);
        }
    }
    let mut start = 0;
    for index in 1..run.len() {
        if ascii_boundary(run, index) {
            if index - start <= 4 && run[start..index].iter().all(u8::is_ascii_alphanumeric) {
                insert_ascii(&run[start..index], true, sink);
            }
            start = index;
        }
    }
    if run.len() - start <= 4 && run[start..].iter().all(u8::is_ascii_alphanumeric) {
        insert_ascii(&run[start..], true, sink);
    }
}

fn insert_subtokens(run: &[&str], sink: &mut impl TokenSink) {
    let kinds: Vec<Option<GraphemeKind>> = run.iter().map(|g| grapheme_kind(g)).collect();
    let mut start = 0;
    for index in 1..run.len() {
        let Some(left) = kinds[index - 1] else {
            continue;
        };
        let Some(right) = kinds[index] else {
            continue;
        };
        let acronym_word = left.upper
            && right.upper
            && kinds
                .get(index + 1)
                .and_then(|kind| *kind)
                .is_some_and(|next| next.lower);
        let boundary = (left.lower && right.upper)
            || (left.alpha && right.digit)
            || (left.digit && right.alpha)
            || acronym_word
            || script_boundary(left, right);
        if boundary {
            insert_subtoken(&run[start..index], sink);
            start = index;
        }
    }
    insert_subtoken(&run[start..], sink);
}

fn insert_subtoken(piece: &[&str], sink: &mut impl TokenSink) {
    if piece.is_empty() || piece.iter().any(|g| grapheme_kind(g).is_none()) {
        return;
    }
    let normalized = normalize(piece);
    let width = UnicodeSegmentation::graphemes(normalized.as_str(), true).count();
    if (1..=4).contains(&width) {
        sink.token(true, &normalized);
    }
}

fn collect_unicode_run(text: &str, sink: &mut impl TokenSink) {
    let run: Vec<&str> = UnicodeSegmentation::graphemes(text, true).collect();
    if !run.is_empty() {
        let mut chunk_start = 0;
        for cursor in 0..=run.len() {
            if cursor == run.len() || grapheme_kind(run[cursor]).is_none() {
                if chunk_start < cursor {
                    insert_ngrams(&run[chunk_start..cursor], sink);
                }
                chunk_start = cursor + 1;
            }
        }
        insert_subtokens(&run, sink);
    }
}

fn attachment(ch: char) -> bool {
    crate::unicode_v16::is_attachment(ch) || ch == '\u{200d}'
}

fn collect_text(text: &str, sink: &mut impl TokenSink) {
    let mut start = None;
    let mut last_alpha = false;
    for (index, ch) in text.char_indices() {
        let next_alpha = text[index + ch.len_utf8()..]
            .chars()
            .next()
            .is_some_and(crate::unicode_v16::is_alpha);
        let apostrophe = matches!(ch, '\'' | '\u{2019}' | '\u{02bc}') && last_alpha && next_alpha;
        let in_run = !crate::unicode_v16::is_separator(ch) || apostrophe;
        if in_run {
            start.get_or_insert(index);
        } else if let Some(run_start) = start.take() {
            collect_run(&text[run_start..index], sink);
        }
        // Attachments stay in-run, so they must not hide the base letter from the
        // apostrophe joiner: NFD cafe\u{301}'s has to join exactly like NFC café's.
        if !attachment(ch) {
            last_alpha = crate::unicode_v16::is_alpha(ch);
        }
    }
    if let Some(run_start) = start {
        collect_run(&text[run_start..], sink);
    }
}

fn collect_run(text: &str, sink: &mut impl TokenSink) {
    if text.is_ascii() {
        collect_ascii_run(text.as_bytes(), sink);
    } else {
        collect_unicode_run(text, sink);
    }
}

fn ngram_source(text: &str) -> &str {
    match text.char_indices().nth(crate::ingest::REPLY_CAP) {
        Some((cut, _)) => &text[..cut],
        None => text,
    }
}

/// Per-family n/s contribution; repeats and echoes count once.
pub fn text_tokens<'a>(texts: impl Iterator<Item = &'a str>) -> (HashSet<String>, HashSet<String>) {
    let mut any = HashSet::new();
    let mut aligned = HashSet::new();
    let mut sets = TokenSets {
        anywhere: &mut any,
        aligned: &mut aligned,
    };
    for text in texts {
        collect_text(ngram_source(text), &mut sets);
    }
    (any, aligned)
}

/// Feeds one row's n/s tokens to `sink`, exactly those `text_tokens` collects.
pub(crate) fn row_tokens(text: &str, sink: &mut impl TokenSink) {
    collect_text(ngram_source(text), sink);
}
