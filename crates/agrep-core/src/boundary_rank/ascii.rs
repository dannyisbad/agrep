use super::*;

pub(super) fn ascii_separators_only(text: &str) -> bool {
    text.chars().filter(|c| !c.is_ascii()).all(|c| {
        if matches!(c, '\u{2019}' | '\u{02bc}') || !unicode_v16::is_separator(c) {
            return false;
        }
        let folded = unicode_v16::case_fold(&c.to_string().nfkc().collect::<String>());
        !folded.is_empty()
            && folded.chars().all(|v| {
                !unicode_v16::is_alphanumeric(v)
                    && !unicode_v16::is_attachment(v)
                    && v != '\u{200d}'
            })
    })
}

pub(super) fn aligned_ascii_occurrence(text: &str, needle: &str) -> bool {
    if needle.is_empty() || !needle.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return false;
    }
    let bytes = text.as_bytes();
    let first = needle.as_bytes()[0];
    let mut from = 0;
    while let Some(offset) = memchr::memchr2(
        first.to_ascii_lowercase(),
        first.to_ascii_uppercase(),
        &bytes[from..],
    ) {
        let start = from + offset;
        let end = start + needle.len();
        if end > bytes.len() {
            break;
        }
        if bytes[start..end].eq_ignore_ascii_case(needle.as_bytes())
            && bytes[start.saturating_sub(2)..(end + 2).min(bytes.len())].is_ascii()
            && ascii_boundary(bytes, start)
            && ascii_boundary(bytes, end)
        {
            return true;
        }
        from = start + 1;
    }
    false
}

pub(super) fn checked_span(
    start: i64,
    end: i64,
    length: usize,
) -> Result<(usize, usize), BoundaryError> {
    if start < 0 || end < start || usize::try_from(end).map_or(true, |end| end > length) {
        return Err(BoundaryError("boundary span outside text"));
    }
    Ok((start as usize, end as usize))
}

pub(super) fn best_ascii_span(text: &str, folded: &str, needle: &str) -> Option<Span> {
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

pub(super) fn best_ascii_quality(text: &[u8], folded: &str, needle: &str) -> f64 {
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

pub(super) fn ascii_quality(text: &[u8], start: usize, end: usize) -> f64 {
    (u8::from(ascii_boundary(text, start)) as f64 + u8::from(ascii_boundary(text, end)) as f64)
        / 2.0
}

pub(super) fn ascii_boundary(text: &[u8], index: usize) -> bool {
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
