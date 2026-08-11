use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::Path;

use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use unicode_normalization::UnicodeNormalization;
use unicode_segmentation::UnicodeSegmentation;

use crate::cache::write_bytes_atomic;
use crate::model::Message;

pub const FILE_NAME: &str = "boundary_stats.json";
pub const CACHE_FILE_NAME: &str = ".boundary_stats.bin";
const FILE_SCHEMA: u32 = 2;
const CACHE_SCHEMA: u32 = 2;
const BUILD_ID: &str = "nfkc-u16-casefold-code-boundaries-v6-capped-quality";
const SHORT_ALPHABET: usize = 36;
const SHORT_TOKEN_COUNT: usize = SHORT_ALPHABET + SHORT_ALPHABET * SHORT_ALPHABET;
const NO_QUALITY: u8 = 3;
// Compaction is linear in family count; require at least 512 dead tokens and 25% waste.
const COMPACT_MIN_DEAD_TOKENS: usize = 512;

type SessionKey = (String, String);

#[derive(Clone, Copy, Default, Deserialize, Serialize)]
struct Counts {
    n: u32,
    s: u32,
    quality: [u32; 3],
}

#[derive(Default, Deserialize, Serialize)]
struct FamilyBits {
    anywhere: Vec<u64>,
    aligned: Vec<u64>,
    quality: [Vec<u64>; 3],
}

#[derive(Deserialize, Serialize)]
struct BoundaryCache {
    schema: u32,
    build_id: String,
    generation: String,
    tokens: Vec<String>,
    counts: Vec<Counts>,
    families: HashMap<SessionKey, FamilyBits>,
    sessions: HashMap<SessionKey, SessionKey>,
}

#[derive(Serialize)]
struct BoundaryStats {
    schema: u32,
    generation: String,
    families: usize,
    tokens: BTreeMap<String, [u32; 3]>,
}

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

fn root_for(
    key: &SessionKey,
    parents: &HashMap<SessionKey, SessionKey>,
    memo: &mut HashMap<SessionKey, SessionKey>,
) -> SessionKey {
    if let Some(root) = memo.get(key) {
        return root.clone();
    }
    let mut trail = Vec::new();
    let mut seen = HashMap::new();
    let mut current = key.clone();
    let root = loop {
        if let Some(root) = memo.get(&current) {
            break root.clone();
        }
        if let Some(&cycle_start) = seen.get(&current) {
            break trail[cycle_start..]
                .iter()
                .min()
                .cloned()
                .unwrap_or(current);
        }
        seen.insert(current.clone(), trail.len());
        trail.push(current.clone());
        let Some(parent) = parents.get(&current) else {
            break current;
        };
        current = parent.clone();
    };
    for member in trail {
        memo.insert(member, root.clone());
    }
    root
}

fn layout(
    msgs: &[Message],
) -> (
    HashMap<SessionKey, SessionKey>,
    HashMap<SessionKey, Vec<&Message>>,
) {
    let mut parents = HashMap::new();
    for message in msgs.iter().filter(|message| !message.parent.is_empty()) {
        parents.insert(
            (message.agent.to_string(), message.session.to_string()),
            (message.agent.to_string(), message.parent.to_string()),
        );
    }
    let mut memo = HashMap::new();
    let mut sessions = HashMap::new();
    let mut families: HashMap<SessionKey, Vec<&Message>> = HashMap::new();
    for message in msgs {
        let key = (message.agent.to_string(), message.session.to_string());
        let root = root_for(&key, &parents, &mut memo);
        sessions.insert(key, root.clone());
        families.entry(root).or_default().push(message);
    }
    (sessions, families)
}

/// Message.text is uncapped at ingest (replies get REPLY_CAP), and token ids are
/// permanent: bound what one pasted multi-MB blob can mint into the global table.
fn ngram_source(text: &str) -> &str {
    match text.char_indices().nth(crate::ingest::REPLY_CAP) {
        Some((cut, _)) => &text[..cut],
        None => text,
    }
}

fn family_tokens(messages: &[&Message]) -> (HashSet<String>, HashSet<String>, Vec<(String, u8)>) {
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

fn empty_cache(generation: &str, sessions: HashMap<SessionKey, SessionKey>) -> BoundaryCache {
    BoundaryCache {
        schema: CACHE_SCHEMA,
        build_id: BUILD_ID.to_string(),
        generation: generation.to_string(),
        tokens: Vec::new(),
        counts: Vec::new(),
        families: HashMap::new(),
        sessions,
    }
}

fn set_bit(bits: &mut Vec<u64>, index: usize) {
    if bits.len() <= index / 64 {
        bits.resize(index / 64 + 1, 0);
    }
    bits[index / 64] |= 1 << (index % 64);
}

fn visit_bits(bits: Vec<u64>, mut visit: impl FnMut(usize)) {
    for (word_index, word) in bits.into_iter().enumerate() {
        let mut live = word;
        while live != 0 {
            let bit = live.trailing_zeros() as usize;
            visit(word_index * 64 + bit);
            live &= live - 1;
        }
    }
}

fn remove_family(cache: &mut BoundaryCache, family: &SessionKey) {
    let Some(bits) = cache.families.remove(family) else {
        return;
    };
    visit_bits(bits.anywhere, |index| {
        if let Some(count) = cache.counts.get_mut(index) {
            count.n = count.n.saturating_sub(1);
        }
    });
    visit_bits(bits.aligned, |index| {
        if let Some(count) = cache.counts.get_mut(index) {
            count.s = count.s.saturating_sub(1);
        }
    });
    for (quality, quality_bits) in bits.quality.into_iter().enumerate() {
        visit_bits(quality_bits, |index| {
            if let Some(count) = cache.counts.get_mut(index) {
                count.quality[quality] = count.quality[quality].saturating_sub(1);
            }
        });
    }
}

fn counts_live(counts: &Counts) -> bool {
    counts.n > 0 || counts.s > 0 || counts.quality.iter().any(|count| *count > 0)
}

fn remap_bits(bits: &mut Vec<u64>, remap: &[Option<usize>]) {
    let old = std::mem::take(bits);
    visit_bits(old, |old_index| {
        if let Some(Some(new_index)) = remap.get(old_index) {
            set_bit(bits, *new_index);
        }
    });
}

fn compact_dead_tokens(cache: &mut BoundaryCache) {
    let dead = cache
        .counts
        .iter()
        .filter(|counts| !counts_live(counts))
        .count();
    if dead < COMPACT_MIN_DEAD_TOKENS || dead.saturating_mul(4) < cache.tokens.len() {
        return;
    }

    let old_tokens = std::mem::take(&mut cache.tokens);
    let old_counts = std::mem::take(&mut cache.counts);
    let mut remap = vec![None; old_tokens.len()];
    cache.tokens.reserve(old_tokens.len().saturating_sub(dead));
    cache.counts.reserve(old_counts.len().saturating_sub(dead));
    for (old_index, (token, counts)) in old_tokens.into_iter().zip(old_counts).enumerate() {
        if counts_live(&counts) {
            remap[old_index] = Some(cache.tokens.len());
            cache.tokens.push(token);
            cache.counts.push(counts);
        }
    }
    for bits in cache.families.values_mut() {
        remap_bits(&mut bits.anywhere, &remap);
        remap_bits(&mut bits.aligned, &remap);
        for quality in &mut bits.quality {
            remap_bits(quality, &remap);
        }
    }
}

fn token_index(
    cache: &mut BoundaryCache,
    token: String,
    token_ids: &mut HashMap<String, usize>,
) -> usize {
    if let Some(&index) = token_ids.get(token.as_str()) {
        index
    } else {
        let index = cache.tokens.len();
        cache.tokens.push(token.clone());
        cache.counts.push(Counts::default());
        token_ids.insert(token, index);
        index
    }
}

fn add_family(
    cache: &mut BoundaryCache,
    family: SessionKey,
    anywhere: HashSet<String>,
    aligned: HashSet<String>,
    qualities: Vec<(String, u8)>,
    token_ids: &mut HashMap<String, usize>,
) {
    let mut bits = FamilyBits::default();
    for token in anywhere {
        let index = token_index(cache, token, token_ids);
        cache.counts[index].n += 1;
        set_bit(&mut bits.anywhere, index);
    }
    for token in aligned {
        let index = token_ids[&token];
        cache.counts[index].s += 1;
        set_bit(&mut bits.aligned, index);
    }
    for (token, quality) in qualities {
        let quality = usize::from(quality.min(2));
        let index = token_index(cache, token, token_ids);
        cache.counts[index].quality[quality] += 1;
        set_bit(&mut bits.quality[quality], index);
    }
    cache.families.insert(family, bits);
}

fn quality_ceiling(token: &str, counts: &Counts) -> u32 {
    if token.len() > 2 || !token.bytes().all(|byte| byte.is_ascii_alphanumeric()) {
        return 2;
    }
    for quality in (0..=2).rev() {
        if counts.quality[quality] > 0 {
            return quality as u32;
        }
    }
    2
}

fn rebuild_cache(msgs: &[Message], generation: &str) -> BoundaryCache {
    let (sessions, families) = layout(msgs);
    let collected: Vec<_> = families
        .into_par_iter()
        .map(|(family, messages)| (family, family_tokens(&messages)))
        .collect();
    let mut cache = empty_cache(generation, sessions);
    let mut token_ids = HashMap::new();
    for (family, (anywhere, aligned, qualities)) in collected {
        add_family(
            &mut cache,
            family,
            anywhere,
            aligned,
            qualities,
            &mut token_ids,
        );
    }
    cache
}

fn stats_from_cache(cache: &BoundaryCache) -> BoundaryStats {
    let tokens = cache
        .tokens
        .iter()
        .zip(&cache.counts)
        .filter(|(_, counts)| counts_live(counts))
        .map(|(token, counts)| {
            (
                token.clone(),
                [
                    counts.n,
                    counts.s.min(counts.n),
                    quality_ceiling(token, counts),
                ],
            )
        })
        .collect();
    BoundaryStats {
        schema: FILE_SCHEMA,
        generation: cache.generation.clone(),
        families: cache.families.len(),
        tokens,
    }
}

#[cfg(test)]
fn build(msgs: &[Message], generation: &str) -> BoundaryStats {
    stats_from_cache(&rebuild_cache(msgs, generation))
}

pub fn write(
    msgs: &[Message],
    path: &Path,
    cache_path: &Path,
    generation: &str,
    prior_generation: Option<&str>,
    touched: &HashSet<String>,
    force: bool,
) -> anyhow::Result<usize> {
    let cached = std::fs::read(cache_path)
        .ok()
        .and_then(|bytes| bincode::deserialize::<BoundaryCache>(&bytes).ok())
        .filter(|cache| {
            !force
                && cache.schema == CACHE_SCHEMA
                && cache.build_id == BUILD_ID
                && prior_generation.is_some_and(|prior| cache.generation == prior)
        });
    let mut cache = if let Some(mut cache) = cached {
        let (sessions, families) = layout(msgs);
        let mut affected = HashSet::new();
        for (session, old_family) in &cache.sessions {
            if sessions.get(session) != Some(old_family) || touched.contains(&session.1) {
                affected.insert(old_family.clone());
                if let Some(new_family) = sessions.get(session) {
                    affected.insert(new_family.clone());
                }
            }
        }
        for (session, family) in &sessions {
            if cache.sessions.get(session) != Some(family) || touched.contains(&session.1) {
                affected.insert(family.clone());
            }
        }
        if affected.is_empty() && cache.generation != generation {
            rebuild_cache(msgs, generation)
        } else {
            let replacements: Vec<_> = affected
                .par_iter()
                .filter_map(|family| {
                    families
                        .get(family)
                        .map(|messages| (family.clone(), family_tokens(messages)))
                })
                .collect();
            for family in &affected {
                remove_family(&mut cache, family);
            }
            let mut token_ids: HashMap<String, usize> = cache
                .tokens
                .iter()
                .cloned()
                .enumerate()
                .map(|(index, token)| (token, index))
                .collect();
            for (family, (anywhere, aligned, qualities)) in replacements {
                add_family(
                    &mut cache,
                    family,
                    anywhere,
                    aligned,
                    qualities,
                    &mut token_ids,
                );
            }
            cache.sessions = sessions;
            cache
        }
    } else {
        rebuild_cache(msgs, generation)
    };
    cache.generation = generation.to_string();
    compact_dead_tokens(&mut cache);
    let stats = stats_from_cache(&cache);
    let families = stats.families;
    let (cache_result, stats_result) = rayon::join(
        || -> anyhow::Result<()> { write_bytes_atomic(cache_path, &bincode::serialize(&cache)?) },
        || -> anyhow::Result<()> { write_bytes_atomic(path, &serde_json::to_vec(&stats)?) },
    );
    cache_result?;
    stats_result?;
    Ok(families)
}

#[cfg(test)]
mod tests {
    use std::collections::HashSet;
    use std::sync::Arc;

    use super::{build, collect_text, counts_live, layout, write, BoundaryCache};
    use crate::model::Message;

    fn message(session: &str, parent: &str, text: &str) -> Message {
        Message {
            agent: "codex",
            project: Arc::from("repo"),
            session: Arc::from(session),
            ts: 1,
            turn: 0,
            text: Arc::from(text),
            who: Arc::from("user"),
            model: Arc::from(""),
            model_source: Arc::from("unknown"),
            reply: Arc::from(""),
            reply_chars: 0,
            side: !parent.is_empty(),
            parent: Arc::from(parent),
        }
    }

    fn base36_token(mut value: usize) -> String {
        const ALPHABET: &[u8] = b"0123456789abcdefghijklmnopqrstuvwxyz";
        let mut token = [b'0'; 4];
        for place in (0..token.len()).rev() {
            token[place] = ALPHABET[value % ALPHABET.len()];
            value /= ALPHABET.len();
        }
        String::from_utf8(token.to_vec()).unwrap()
    }

    #[test]
    fn counts_each_root_family_once() {
        let rows = vec![
            message("root", "", "akd akd"),
            message("child", "root", "akd"),
            message("other", "", "zzakdzz"),
        ];
        let stats = build(&rows, "3:abc");
        assert_eq!(stats.families, 2);
        assert_eq!(stats.tokens.get("akd"), Some(&[2, 1, 2]));
        assert_eq!(stats.generation, "3:abc");
    }

    #[test]
    fn recognizes_code_boundaries_without_promoting_apostrophes() {
        let mut anywhere = std::collections::HashSet::new();
        let mut aligned = std::collections::HashSet::new();
        collect_text(
            "fooBar HTTPServer sha256 request_id don't JSON\u{89e3}\u{6790}",
            &mut anywhere,
            &mut aligned,
        );
        for token in ["foo", "bar", "http", "sha", "256", "id", "json"] {
            assert!(aligned.contains(token), "missing {token}");
        }
        assert!(!aligned.contains("t"));
        assert!(anywhere.contains("t"));
        assert!(aligned.contains("解析"));
    }

    #[test]
    fn keys_use_nfkc_and_full_casefold() {
        let mut anywhere = std::collections::HashSet::new();
        let mut aligned = std::collections::HashSet::new();
        collect_text("K ß", &mut anywhere, &mut aligned);
        assert!(aligned.contains("k"));
        assert!(aligned.contains("ss"));
        assert!(anywhere.contains("ss"));
    }

    #[test]
    fn acronym_and_nested_families_are_stable() {
        let rows = vec![
            message("grand", "", "HTTPServer"),
            message("parent", "grand", "http"),
            message("child", "parent", "http"),
        ];
        let stats = build(&rows, "3:def");
        assert_eq!(stats.families, 1);
        assert_eq!(stats.tokens.get("http"), Some(&[1, 1, 2]));
    }

    #[test]
    fn unrelated_roots_chains_and_cycles_do_not_collapse() {
        let rows = vec![
            message("root-a", "", "alpha"),
            message("child-a", "root-a", "alpha"),
            message("grandchild-a", "child-a", "alpha"),
            message("root-b", "", "bravo"),
            message("cycle-a", "cycle-b", "cycle"),
            message("cycle-b", "cycle-a", "cycle"),
        ];
        let (sessions, families) = layout(&rows);
        assert_eq!(families.len(), 3);
        assert_eq!(
            sessions[&("codex".to_string(), "root-a".to_string())].1,
            "root-a"
        );
        assert_eq!(
            sessions[&("codex".to_string(), "root-b".to_string())].1,
            "root-b"
        );
        assert_eq!(
            sessions[&("codex".to_string(), "grandchild-a".to_string())].1,
            "root-a"
        );
        assert_eq!(
            sessions[&("codex".to_string(), "cycle-a".to_string())].1,
            "cycle-a"
        );
        assert_eq!(
            sessions[&("codex".to_string(), "cycle-b".to_string())].1,
            "cycle-a"
        );
    }

    #[test]
    fn conformance_fixture_aligned_tokens_match() {
        // Shared contract with py/boundary_rank.py: both segmenters must produce these
        // exact NFKC-folded aligned sets, or sidecar stats keys silently go unmatched.
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../py/fixtures/boundary_conformance.json");
        let body = std::fs::read_to_string(&path).unwrap();
        let fixture: serde_json::Value = serde_json::from_str(&body).unwrap();
        let cases = fixture["segmentation"].as_array().unwrap();
        assert!(!cases.is_empty());
        for case in cases {
            let text = case["text"].as_str().unwrap();
            let mut expected: Vec<String> = case["aligned"]
                .as_array()
                .unwrap()
                .iter()
                .map(|token| token.as_str().unwrap().to_string())
                .collect();
            expected.sort();
            let mut anywhere = HashSet::new();
            let mut aligned = HashSet::new();
            collect_text(text, &mut anywhere, &mut aligned);
            let mut actual: Vec<String> = aligned.into_iter().collect();
            actual.sort();
            assert_eq!(actual, expected, "aligned mismatch for {text:?}");
        }
    }

    #[test]
    fn pasted_blob_ngrams_are_capped() {
        // Message.text is uncapped at ingest; tokens past the cap must not be minted.
        let text = format!("akd {} zzmarkerzz", "x".repeat(70_000));
        let rows = vec![message("root", "", &text)];
        let stats = build(&rows, "3:cap");
        assert!(stats.tokens.contains_key("akd"));
        assert!(!stats.tokens.contains_key("zzma"));
    }

    #[test]
    fn short_quality_tracks_interior_partial_aligned_and_apostrophe_hits() {
        let interior = build(&[message("root", "", "zaxb")], "3:interior");
        assert_eq!(interior.tokens.get("a"), Some(&[1, 0, 0]));

        let partial = build(&[message("root", "", "abx")], "3:partial");
        assert_eq!(partial.tokens.get("ab"), Some(&[1, 0, 1]));

        let aligned = build(&[message("root", "", "xId")], "3:aligned");
        assert_eq!(aligned.tokens.get("id"), Some(&[1, 1, 2]));

        let joined = build(&[message("root", "", "don't")], "3:joined");
        assert_eq!(joined.tokens.get("t"), Some(&[1, 0, 1]));

        let unicode_joined = build(&[message("root", "", "don’t")], "3:unicode-joined");
        assert_eq!(unicode_joined.tokens.get("t"), Some(&[1, 0, 1]));
    }

    #[test]
    fn short_quality_respects_the_paste_cap() {
        let mut row = message(
            "root",
            "",
            &format!("{} id", "x".repeat(crate::ingest::REPLY_CAP + 8)),
        );
        row.reply = Arc::from(format!("{} db", "y".repeat(crate::ingest::REPLY_CAP + 8)));
        row.reply_chars = row.reply.chars().count();
        let stats = build(&[row], "3:capped-quality");
        assert!(!stats.tokens.contains_key("id"));
        assert!(!stats.tokens.contains_key("db"));
    }

    #[test]
    fn uncertain_unicode_folds_only_raise_quality() {
        let stats = build(&[message("root", "", "xK ß ıd")], "3:folds");
        assert_eq!(stats.tokens.get("k"), Some(&[1, 1, 2]));
        assert_eq!(stats.tokens.get("ss"), Some(&[1, 1, 2]));
        assert_eq!(stats.tokens.get("id"), Some(&[0, 0, 2]));
    }

    #[test]
    fn incremental_family_replacement_preserves_other_counts() {
        let dir = std::env::temp_dir().join(format!(
            "agrep-boundary-stats-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("boundary_stats.json");
        let cache = dir.join(".boundary_stats.bin");
        let first = vec![
            message("root", "", "akd id"),
            message("child", "root", "akd"),
            message("other", "", "zzakdzz xidq"),
        ];
        write(&first, &path, &cache, "3:one", None, &HashSet::new(), false).unwrap();
        let initial: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(initial["tokens"]["id"], serde_json::json!([2, 1, 2]));

        let second = vec![
            message("root", "", "xyz"),
            message("child", "root", "xyz"),
            message("other", "", "zzakdzz xidq"),
        ];
        write(
            &second,
            &path,
            &cache,
            "3:two",
            Some("3:one"),
            &HashSet::from(["child".to_string()]),
            false,
        )
        .unwrap();
        let body: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(body["schema"], 2);
        assert_eq!(body["generation"], "3:two");
        assert_eq!(body["tokens"]["akd"], serde_json::json!([1, 0, 2]));
        assert_eq!(body["tokens"]["xyz"], serde_json::json!([1, 1, 2]));
        assert_eq!(body["tokens"]["id"], serde_json::json!([1, 0, 0]));
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn incremental_churn_compacts_dead_token_ids() {
        let dir = std::env::temp_dir().join(format!(
            "agrep-boundary-churn-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("boundary_stats.json");
        let cache_path = dir.join(".boundary_stats.bin");
        let first: Vec<_> = (0..600)
            .map(|index| message(&format!("session-{index}"), "", &base36_token(index)))
            .collect();
        write(
            &first,
            &path,
            &cache_path,
            "3:churn-one",
            None,
            &HashSet::new(),
            false,
        )
        .unwrap();
        let before: BoundaryCache =
            bincode::deserialize(&std::fs::read(&cache_path).unwrap()).unwrap();
        assert!(before.tokens.len() > 512);

        let second = vec![message("session-0", "", &base36_token(0))];
        write(
            &second,
            &path,
            &cache_path,
            "3:churn-two",
            Some("3:churn-one"),
            &HashSet::new(),
            false,
        )
        .unwrap();
        let after: BoundaryCache =
            bincode::deserialize(&std::fs::read(&cache_path).unwrap()).unwrap();
        assert_eq!(after.families.len(), 1);
        assert!(after.tokens.len() < before.tokens.len());
        assert!(after.counts.iter().all(counts_live));
        let live_words = after.tokens.len().div_ceil(64);
        let live_bits = after.tokens.len() % 64;
        let tail_mask = if live_bits == 0 {
            u64::MAX
        } else {
            (1u64 << live_bits) - 1
        };
        for family in after.families.values() {
            for bits in std::iter::once(&family.anywhere)
                .chain(std::iter::once(&family.aligned))
                .chain(family.quality.iter())
            {
                assert!(bits.len() <= live_words);
                if bits.len() == live_words {
                    assert_eq!(bits.last().copied().unwrap_or(0) & !tail_mask, 0);
                }
            }
        }
        write(
            &[],
            &path,
            &cache_path,
            "3:churn-three",
            Some("3:churn-two"),
            &HashSet::from(["session-0".to_string()]),
            false,
        )
        .unwrap();
        let empty: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        assert_eq!(empty["families"], 0);
        assert_eq!(empty["tokens"], serde_json::json!({}));
        let _ = std::fs::remove_dir_all(dir);
    }
}
