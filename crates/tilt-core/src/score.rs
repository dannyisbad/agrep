//! Tier-0 lexical scoring: turn raw lexicon hits + text features into vulgarity / rage / vibe.
//! Deliberately separates *cussing* (vulgarity, exact) from *being pissed* (rage, contextual),
//! so hype-swearing ("sick as fuck") doesn't read as anger.
//!
//! DECISION (lexical reliability): the keyword lexicon is EXACT-VULGARITY-ONLY. It can tell you
//! *that* a word was cussed, but it is NOT a trustworthy rage signal — "damn that's sick asf" is
//! hype, "this is broken" with no swearing can be pure fury. So the `rage`/`valence`/`tag` outputs
//! below are a ROUGH INSTANT ESTIMATE only: a cheap tier-0 heuristic to render something the moment
//! a transcript lands. The authoritative affect read comes from the GPU semantic sidecar
//! (ModernBERT-GoEmotions gate -> Qwen judge), which writes back into the cache and OVERRIDES these
//! lexical fields. Treat lexical `rage` as a placeholder, not ground truth. `vulgarity` stays
//! lexical because exact profanity is a lexical fact, not an inference.

use crate::scan::Hits;
use crate::simd;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Tag {
    Fuming,
    Annoyed,
    Irked,
    Hype,
    Casual,
    Neutral,
}

impl Tag {
    pub fn as_str(self) -> &'static str {
        match self {
            Tag::Fuming => "fuming",
            Tag::Annoyed => "annoyed",
            Tag::Irked => "irked",
            Tag::Hype => "hype",
            Tag::Casual => "casual",
            Tag::Neutral => "neutral",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Score {
    /// Severity-weighted profanity count. Exact, lexical, trustworthy — a lexical fact.
    pub vulgarity: u32,
    /// ROUGH INSTANT ESTIMATE of anger from keyword heuristics. NOT a reliable rage signal
    /// (vulgarity != anger: hype swears inflate it, swear-free fury reads as zero). Kept so the
    /// UI has something to show on first ingest; the semantic sidecar OVERRIDES this in the cache.
    pub rage: f32,
    /// ROUGH INSTANT ESTIMATE of positive-vs-negative tilt from lexicon hits. Same caveat as
    /// `rage`: a cheap placeholder superseded by the semantic sidecar.
    pub valence: f32,
    /// ROUGH INSTANT register label derived from `rage`/`vulgarity`. Placeholder until the
    /// semantic sidecar writes the authoritative affect read back into the cache.
    pub tag: Tag,
}

/// Tier-0 lexical scorer. Produces an exact `vulgarity` count plus a ROUGH INSTANT ESTIMATE of
/// `rage`/`valence`/`tag`. The rage estimate is a cheap heuristic, not a reliable anger signal —
/// the semantic sidecar overrides it. See the module/`Score` docs for the reliability decision.
pub fn score(text: &str, h: &Hits) -> Score {
    let caps = simd::count_ascii_upper(text.as_bytes());
    let len = text.len().max(1);
    let caps_ratio = caps as f32 / len as f32;
    // shouting = many capitals AND a high ratio (so a lone acronym doesn't count).
    let caps_signal = if caps >= 3 && caps_ratio > 0.30 {
        (caps as f32).min(20.0) * 0.1
    } else {
        0.0
    };
    let elong = count_elongations(text) as f32;
    let punct = count_punct_bursts(text) as f32;
    let intensity = caps_signal + elong * 0.5 + punct * 0.8;

    // ROUGH INSTANT ESTIMATE follows: a keyword heuristic, not a reliable rage signal. The
    // semantic sidecar overrides `rage`/`valence`/`tag` in the cache; this is a placeholder.
    let base = h.frust + h.blame as f32 * 2.5 + intensity;
    let amp = 1.0 + (h.vulgarity.min(6) as f32) * 0.25;
    let mut rage = base * amp;
    if base < 0.001 {
        // bare cussing with no negative context barely registers as rage.
        rage = h.vulgarity as f32 * 0.15;
    }
    if h.pos > 0.001 && base < 1.0 {
        // positive markers + no real negativity => hype, not anger.
        rage *= 0.1;
    }
    let rage = rage.min(25.0);

    let valence = h.pos - (h.frust + h.blame as f32);

    let tag = if rage >= 8.0 {
        Tag::Fuming
    } else if rage >= 3.5 {
        Tag::Annoyed
    } else if h.vulgarity > 0 && h.pos > 0.0 && rage < 2.0 {
        Tag::Hype
    } else if rage >= 1.2 {
        Tag::Irked
    } else if h.vulgarity > 0 {
        Tag::Casual
    } else {
        Tag::Neutral
    };

    Score {
        vulgarity: h.vulgarity,
        rage,
        valence,
        tag,
    }
}

/// 3+ of the same alphabetic char in a row ("fuuuck", "noooo", "ughhh").
fn count_elongations(text: &str) -> usize {
    let b = text.as_bytes();
    let mut n = 0;
    let mut run = 1usize;
    for i in 1..b.len() {
        if b[i] == b[i - 1] && b[i].is_ascii_alphabetic() {
            run += 1;
            if run == 3 {
                n += 1;
            }
        } else {
            run = 1;
        }
    }
    n
}

/// Runs of 2+ of [! ?] ("!!!", "??", "?!?").
fn count_punct_bursts(text: &str) -> usize {
    let b = text.as_bytes();
    let mut n = 0;
    let mut in_run = false;
    let mut run = 0usize;
    for &c in b {
        if c == b'!' || c == b'?' {
            run += 1;
            if run == 2 && !in_run {
                n += 1;
                in_run = true;
            }
        } else {
            run = 0;
            in_run = false;
        }
    }
    n
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scan::Scanner;

    #[test]
    fn hype_is_not_rage() {
        let sc = Scanner::new();
        let s = score("this shit is crazy good", &sc.scan("this shit is crazy good"));
        assert!(s.rage < 2.0, "hype scored as rage: {:?}", s);
    }

    #[test]
    fn directed_frustration_rages() {
        let sc = Scanner::new();
        let t = "why is this STILL fucking broken, you keep breaking it";
        let s = score(t, &sc.scan(t));
        assert!(s.rage >= 4.0, "directed rage too low: {:?}", s);
    }
}
