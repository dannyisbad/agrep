use super::*;

pub(super) struct PreparedText {
    pub(super) original: Vec<char>,
    pub(super) folded: Vec<char>,
    pub(super) starts: Vec<usize>,
    pub(super) ends: Vec<usize>,
    pub(super) boundaries: HashSet<usize>,
}

pub(super) struct BoundaryOnlyText {
    pub(super) char_len: usize,
    pub(super) char_boundaries: HashSet<usize>,
    pub(super) byte_boundaries: HashSet<usize>,
    pub(super) unstable_ascii: HashSet<u8>,
    pub(super) normalization_can_delete: bool,
}

impl BoundaryOnlyText {
    pub(super) fn new(text: &str) -> Self {
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

    pub(super) fn quality(&self, start: usize, end: usize) -> f64 {
        (u8::from(self.char_boundaries.contains(&start)) as f64
            + u8::from(self.char_boundaries.contains(&end)) as f64)
            / 2.0
    }

    pub(super) fn can_score_ascii(&self, terms: &[BoundaryTerm]) -> bool {
        !self.normalization_can_delete
            && terms.iter().all(|term| {
                term.ascii_variants.as_ref().unwrap().iter().all(|variant| {
                    variant
                        .bytes()
                        .all(|byte| !self.unstable_ascii.contains(&byte))
                })
            })
    }

    pub(super) fn best_ascii_quality(&self, text: &str, needle: &str) -> f64 {
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
    pub(super) fn new(text: &str) -> Self {
        let grouped = clusters(text);
        let original: Vec<char> = text.chars().collect();
        let mut folded = Vec::new();
        let mut starts = Vec::new();
        let mut ends = Vec::new();
        for cluster in &grouped {
            let original = &text[cluster.start_byte..cluster.end_byte];
            if original.is_ascii() {
                folded.extend(original.bytes().map(|b| char::from(b.to_ascii_lowercase())));
                starts.extend(std::iter::repeat_n(cluster.start, original.len()));
                ends.extend(std::iter::repeat_n(cluster.end, original.len()));
                continue;
            }
            let compatible: String = original.nfkc().collect();
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

    pub(super) fn best_span(&self, needle: &[char]) -> Option<Span> {
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

    pub(super) fn quality(&self, start: usize, end: usize) -> f64 {
        (u8::from(self.boundaries.contains(&start)) as f64
            + u8::from(self.boundaries.contains(&end)) as f64)
            / 2.0
    }

    pub(super) fn folded_slice(&self, start: usize, end: usize) -> String {
        normalize_token(&self.original[start..end].iter().collect::<String>())
    }
}

#[derive(Clone, Copy)]
pub(super) struct Cluster {
    pub(super) start: usize,
    pub(super) end: usize,
    pub(super) start_byte: usize,
    pub(super) end_byte: usize,
    pub(super) base: char,
}

pub(super) fn clusters(text: &str) -> Vec<Cluster> {
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
pub(super) enum HangulKind {
    None,
    L,
    V,
    T,
    Lv,
    Lvt,
}

pub(super) fn hangul_kind(character: char) -> HangulKind {
    match character as u32 {
        0x1100..=0x115f | 0xa960..=0xa97c => HangulKind::L,
        0x1160..=0x11a7 | 0xd7b0..=0xd7c6 => HangulKind::V,
        0x11a8..=0x11ff | 0xd7cb..=0xd7fb => HangulKind::T,
        _ => HangulKind::None,
    }
}

pub(super) fn boundary_between(text: &str, grouped: &[Cluster], index: usize) -> bool {
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

pub(super) fn apostrophe_joins(text: &str, grouped: &[Cluster], index: usize) -> bool {
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
