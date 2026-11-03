//! Unicode short-token statistics, computed per conversation family.
use crate::model::Message;
use std::collections::HashSet;
use unicode_normalization::UnicodeNormalization;
use unicode_segmentation::UnicodeSegmentation;
const SHORT_ALPHABET: usize = 36;
const SHORT_TOKEN_COUNT: usize = SHORT_ALPHABET + SHORT_ALPHABET * SHORT_ALPHABET;
const NO_QUALITY: u8 = 3;
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

fn insert_ngrams(run: &[&str], out: &mut HashSet<String>) {
    let normalized = normalize(run);
    let graphemes: Vec<&str> = UnicodeSegmentation::graphemes(normalized.as_str(), true).collect();
    for start in 0..graphemes.len() {
        for width in 1..=4.min(graphemes.len() - start) {
            out.insert(graphemes[start..start + width].concat());
        }
    }
}

fn insert_ascii(bytes: &[u8], out: &mut HashSet<String>) {
    let mut folded = [0u8; 4];
    for (index, byte) in bytes.iter().enumerate() {
        folded[index] = byte.to_ascii_lowercase();
    }
    let token = std::str::from_utf8(&folded[..bytes.len()]).unwrap();
    if !out.contains(token) {
        out.insert(token.to_string());
    }
}

fn insert_ascii_ngrams(run: &[u8], out: &mut HashSet<String>) {
    for start in 0..run.len() {
        for width in 1..=4.min(run.len() - start) {
            let piece = &run[start..start + width];
            if piece.iter().all(u8::is_ascii_alphanumeric) {
                insert_ascii(piece, out);
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

fn collect_ascii_run(run: &[u8], anywhere: &mut HashSet<String>, aligned: &mut HashSet<String>) {
    for chunk in run.split(|byte| *byte == b'\'') {
        if !chunk.is_empty() {
            insert_ascii_ngrams(chunk, anywhere);
        }
    }
    let mut start = 0;
    for index in 1..run.len() {
        if ascii_boundary(run, index) {
            if index - start <= 4 && run[start..index].iter().all(u8::is_ascii_alphanumeric) {
                insert_ascii(&run[start..index], aligned);
            }
            start = index;
        }
    }
    if run.len() - start <= 4 && run[start..].iter().all(u8::is_ascii_alphanumeric) {
        insert_ascii(&run[start..], aligned);
    }
}

fn insert_subtokens(run: &[&str], out: &mut HashSet<String>) {
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
            insert_subtoken(&run[start..index], out);
            start = index;
        }
    }
    insert_subtoken(&run[start..], out);
}

fn insert_subtoken(piece: &[&str], out: &mut HashSet<String>) {
    if piece.is_empty() || piece.iter().any(|g| grapheme_kind(g).is_none()) {
        return;
    }
    let normalized = normalize(piece);
    let width = UnicodeSegmentation::graphemes(normalized.as_str(), true).count();
    if (1..=4).contains(&width) {
        out.insert(normalized);
    }
}

fn collect_unicode_run(text: &str, anywhere: &mut HashSet<String>, aligned: &mut HashSet<String>) {
    let run: Vec<&str> = UnicodeSegmentation::graphemes(text, true).collect();
    if !run.is_empty() {
        let mut chunk_start = 0;
        for cursor in 0..=run.len() {
            if cursor == run.len() || grapheme_kind(run[cursor]).is_none() {
                if chunk_start < cursor {
                    insert_ngrams(&run[chunk_start..cursor], anywhere);
                }
                chunk_start = cursor + 1;
            }
        }
        insert_subtokens(&run, aligned);
    }
}

fn attachment(ch: char) -> bool {
    crate::unicode_v16::is_attachment(ch) || ch == '\u{200d}'
}

fn collect_text(text: &str, anywhere: &mut HashSet<String>, aligned: &mut HashSet<String>) {
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
            collect_run(&text[run_start..index], anywhere, aligned);
        }
        // Attachments stay in-run, so they must not hide the base letter from the
        // apostrophe joiner: NFD cafe\u{301}'s has to join exactly like NFC café's.
        if !attachment(ch) {
            last_alpha = crate::unicode_v16::is_alpha(ch);
        }
    }
    if let Some(run_start) = start {
        collect_run(&text[run_start..], anywhere, aligned);
    }
}

fn collect_run(text: &str, anywhere: &mut HashSet<String>, aligned: &mut HashSet<String>) {
    if text.is_ascii() {
        collect_ascii_run(text.as_bytes(), anywhere, aligned);
    } else {
        collect_unicode_run(text, anywhere, aligned);
    }
}

fn short_symbol(byte: u8) -> usize {
    let byte = byte.to_ascii_lowercase();
    if byte.is_ascii_digit() {
        usize::from(byte - b'0')
    } else {
        10 + usize::from(byte - b'a')
    }
}

fn symbol_byte(symbol: usize) -> u8 {
    if symbol < 10 {
        b'0' + symbol as u8
    } else {
        b'a' + (symbol - 10) as u8
    }
}

fn raise_quality(token: &[u8], quality: u8, qualities: &mut [u8; SHORT_TOKEN_COUNT]) {
    let first = short_symbol(token[0]);
    let index = if token.len() == 1 {
        first
    } else {
        SHORT_ALPHABET + first * SHORT_ALPHABET + short_symbol(token[1])
    };
    qualities[index] = if qualities[index] == NO_QUALITY {
        quality
    } else {
        qualities[index].max(quality)
    };
}

fn quality_tokens(qualities: [u8; SHORT_TOKEN_COUNT]) -> Vec<(String, u8)> {
    qualities
        .into_iter()
        .enumerate()
        .filter(|(_, quality)| *quality != NO_QUALITY)
        .map(|(index, quality)| {
            let bytes = if index < SHORT_ALPHABET {
                vec![symbol_byte(index)]
            } else {
                let pair = index - SHORT_ALPHABET;
                vec![
                    symbol_byte(pair / SHORT_ALPHABET),
                    symbol_byte(pair % SHORT_ALPHABET),
                ]
            };
            (String::from_utf8(bytes).unwrap(), quality)
        })
        .collect()
}

fn ascii_text_boundary(text: &[u8], index: usize) -> bool {
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

fn apostrophe(character: char) -> bool {
    matches!(character, '\'' | '\u{2019}' | '\u{02bc}')
}

fn short_boundary(text: &str, index: usize, ascii: bool) -> bool {
    if index == 0 || index == text.len() {
        return true;
    }
    if ascii {
        return ascii_text_boundary(text.as_bytes(), index);
    }
    let left = text[..index].chars().next_back().unwrap();
    let right = text[index..].chars().next().unwrap();
    if apostrophe(right) {
        let after = text[index + right.len_utf8()..].chars().next();
        if crate::unicode_v16::is_alpha(left) && after.is_some_and(crate::unicode_v16::is_alpha) {
            return false;
        }
    }
    if apostrophe(left) {
        let before = text[..index - left.len_utf8()].chars().next_back();
        if before.is_some_and(crate::unicode_v16::is_alpha) && crate::unicode_v16::is_alpha(right) {
            return false;
        }
    }
    if !left.is_ascii_alphanumeric() || !right.is_ascii_alphanumeric() {
        return true;
    }
    if left.is_ascii_lowercase() && right.is_ascii_uppercase() {
        return true;
    }
    if left.is_ascii_uppercase() && right.is_ascii_uppercase() {
        let after = text[index + right.len_utf8()..].chars().next();
        if after.is_some_and(|character| !character.is_ascii() || character.is_ascii_lowercase()) {
            return true;
        }
    }
    (left.is_ascii_alphabetic() && right.is_ascii_digit())
        || (left.is_ascii_digit() && right.is_ascii_alphabetic())
}

fn collect_raw_short_quality(text: &str, ascii: bool, qualities: &mut [u8; SHORT_TOKEN_COUNT]) {
    let bytes = text.as_bytes();
    for start in 0..bytes.len() {
        if !bytes[start].is_ascii_alphanumeric() {
            continue;
        }
        let end = start + 1;
        let starts = short_boundary(text, start, ascii);
        let quality = u8::from(starts) + u8::from(short_boundary(text, end, ascii));
        raise_quality(&bytes[start..end], quality, qualities);
        if end < bytes.len() && bytes[end].is_ascii_alphanumeric() {
            let pair_end = end + 1;
            let quality = u8::from(starts) + u8::from(short_boundary(text, pair_end, ascii));
            raise_quality(&bytes[start..pair_end], quality, qualities);
        }
    }
}

fn consume_folded_short(
    character: char,
    uncertain: bool,
    previous: &mut Option<(u8, bool)>,
    uncertain_gap: &mut bool,
    qualities: &mut [u8; SHORT_TOKEN_COUNT],
) {
    if !character.is_ascii_alphanumeric() {
        *previous = None;
        *uncertain_gap = false;
        return;
    }
    let byte = (character as u8).to_ascii_lowercase();
    if uncertain {
        raise_quality(&[byte], 2, qualities);
    }
    if let Some((left, left_uncertain)) = *previous {
        if uncertain || left_uncertain || *uncertain_gap {
            raise_quality(&[left, byte], 2, qualities);
        }
    }
    *previous = Some((byte, uncertain));
    *uncertain_gap = false;
}

fn collect_uncertain_short_quality(text: &str, qualities: &mut [u8; SHORT_TOKEN_COUNT]) {
    let mut previous = None;
    let mut uncertain_gap = false;
    for grapheme in UnicodeSegmentation::graphemes(text, true) {
        if grapheme.is_ascii() {
            for character in grapheme.chars() {
                consume_folded_short(
                    character,
                    false,
                    &mut previous,
                    &mut uncertain_gap,
                    qualities,
                );
            }
            continue;
        }
        let compatible: String = grapheme.nfkc().collect();
        let normalized = crate::unicode_v16::case_fold(&compatible);
        if normalized.is_empty() {
            uncertain_gap = true;
            continue;
        }
        for character in normalized.chars() {
            let character = if character == '\u{0131}' {
                'i'
            } else {
                character
            };
            consume_folded_short(
                character,
                true,
                &mut previous,
                &mut uncertain_gap,
                qualities,
            );
        }
    }
}

fn collect_short_quality(text: &str, qualities: &mut [u8; SHORT_TOKEN_COUNT]) {
    let ascii = text.is_ascii();
    collect_raw_short_quality(text, ascii, qualities);
    if !ascii {
        collect_uncertain_short_quality(text, qualities);
    }
}

fn ngram_source(text: &str) -> &str {
    match text.char_indices().nth(crate::ingest::REPLY_CAP) {
        Some((cut, _)) => &text[..cut],
        None => text,
    }
}

pub fn family_tokens(
    messages: &[&Message],
) -> (HashSet<String>, HashSet<String>, Vec<(String, u8)>) {
    let mut anywhere = HashSet::new();
    let mut aligned = HashSet::new();
    let mut qualities = [NO_QUALITY; SHORT_TOKEN_COUNT];
    for message in messages {
        let text = ngram_source(&message.text);
        let reply = ngram_source(&message.reply);
        collect_text(text, &mut anywhere, &mut aligned);
        collect_text(reply, &mut anywhere, &mut aligned);
        collect_short_quality(text, &mut qualities);
        collect_short_quality(reply, &mut qualities);
    }
    aligned.retain(|token| anywhere.contains(token));
    (anywhere, aligned, quality_tokens(qualities))
}

/// Per-family n/s contribution; repeats and echoes count once.
pub fn text_tokens<'a>(texts: impl Iterator<Item = &'a str>) -> (HashSet<String>, HashSet<String>) {
    let mut any = HashSet::new();
    let mut aligned = HashSet::new();
    for text in texts {
        collect_text(ngram_source(text), &mut any, &mut aligned);
    }
    (any, aligned)
}
