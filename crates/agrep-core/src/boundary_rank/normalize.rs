use super::*;

pub fn query_tokens(query: &str) -> Vec<&str> {
    query
        .split(|character| unicode_v16::is_space(character) || matches!(character, '-' | '_'))
        .filter(|token| !token.is_empty())
        .collect()
}

pub fn normalize_token(token: &str) -> String {
    if token.is_ascii() {
        return token.to_ascii_lowercase();
    }
    let mut out = Vec::with_capacity(token.len());
    fold(token, &mut out, |_, _| {});
    String::from_utf8(out).expect("normalized clusters preserve UTF-8")
}

pub(super) fn fold(
    token: &str,
    out: &mut Vec<u8>,
    mut cluster: impl FnMut(std::ops::Range<usize>, std::ops::Range<usize>),
) {
    let mut at = 0;
    while at < token.len() {
        // Leave the final ASCII character with the Unicode tail: an attachment
        // or ZWJ may make it part of the next normalization cluster.
        let ascii = token.as_bytes()[at..]
            .iter()
            .take_while(|b| b.is_ascii())
            .count();
        let safe = if at + ascii < token.len() {
            ascii.saturating_sub(1)
        } else {
            ascii
        };
        if safe > 0 {
            out.extend(
                token.as_bytes()[at..at + safe]
                    .iter()
                    .map(u8::to_ascii_lowercase),
            );
            at += safe;
            continue;
        }
        let mut chars = token[at..].char_indices().peekable();
        let (_, head) = chars.next().unwrap();
        let mut hangul = hangul_kind(head);
        while let Some(&(_, character)) = chars.peek() {
            if unicode_v16::is_attachment(character) {
                chars.next();
                continue;
            }
            let next = hangul_kind(character);
            if hangul == HangulKind::L && next == HangulKind::V {
                hangul = HangulKind::Lv;
                chars.next();
                continue;
            }
            if hangul == HangulKind::Lv && next == HangulKind::T {
                hangul = HangulKind::Lvt;
                chars.next();
                continue;
            }
            if character == '\u{200d}' && chars.clone().nth(1).is_some() {
                chars.next();
                chars.next();
                continue;
            }
            break;
        }
        let end = at + chars.peek().map_or(token.len() - at, |&(byte, _)| byte);
        let original = &token[at..end];
        if original.is_ascii() {
            out.extend(original.bytes().map(|b| b.to_ascii_lowercase()));
        } else {
            let start = out.len();
            let compatible: String = original.nfkc().collect();
            out.extend_from_slice(unicode_v16::case_fold(&compatible).as_bytes());
            cluster(start..out.len(), at..end);
        }
        at = end;
    }
}

pub fn term_variants(token: &str) -> Vec<String> {
    let folded = normalize_token(token);
    if folded.is_empty() {
        return Vec::new();
    }
    if !folded.is_ascii() || !folded.bytes().all(|byte| byte.is_ascii_alphabetic()) {
        return vec![folded];
    }
    let mut variants = vec![folded.clone()];
    let hissing = |value: &str| {
        ["s", "x", "z", "ch", "sh"]
            .iter()
            .any(|suffix| value.ends_with(suffix))
    };
    if folded.len() >= 5 && folded.ends_with("ies") {
        variants.push(format!("{}y", &folded[..folded.len() - 3]));
    } else if folded.len() >= 5
        && folded.ends_with("es")
        && (["x", "z", "ch", "sh", "ss"]
            .iter()
            .any(|suffix| folded[..folded.len() - 2].ends_with(suffix))
            || (folded.len() >= 7
                && ["us", "is"]
                    .iter()
                    .any(|suffix| folded[..folded.len() - 2].ends_with(suffix))))
    {
        variants.push(folded[..folded.len() - 2].to_string());
    } else if folded.len() >= 5
        && folded.ends_with('s')
        && !["ss", "us", "is"]
            .iter()
            .any(|suffix| folded.ends_with(suffix))
    {
        variants.push(folded[..folded.len() - 1].to_string());
    } else if folded.len() >= 4
        && folded.ends_with('y')
        && !matches!(
            folded.as_bytes()[folded.len() - 2],
            b'a' | b'e' | b'i' | b'o' | b'u'
        )
    {
        variants.push(format!("{}ies", &folded[..folded.len() - 1]));
    } else if folded.len() >= 4 && !hissing(&folded) {
        variants.push(format!("{folded}s"));
    } else if folded.len() >= 4
        && ["x", "z", "ch", "sh", "ss", "us"]
            .iter()
            .any(|suffix| folded.ends_with(suffix))
    {
        variants.push(format!("{folded}es"));
    }
    variants.retain(|variant| variant == &folded || variant.len() >= 3);
    variants.dedup();
    variants
}

pub fn term_anchor(token: &str) -> String {
    let variants = term_variants(token);
    let Some(first) = variants.first() else {
        return String::new();
    };
    let mut prefix = first.clone();
    for variant in variants.iter().skip(1) {
        while !prefix.is_empty() && !variant.starts_with(&prefix) {
            prefix.pop();
        }
    }
    if prefix.len() >= 3 {
        prefix
    } else {
        first.clone()
    }
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
