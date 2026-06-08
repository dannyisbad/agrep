//! Multi-pattern lexical scan. Aho-Corasick (itself SIMD-accelerated) does the matching;
//! we post-filter to word boundaries so "ass" doesn't fire inside "class" / "assert".

use aho_corasick::{AhoCorasick, MatchKind};

use crate::lexicon::{BLAME, FRUSTRATION, POSITIVE, POSITIVE_PHRASES, SEVERITY};

#[derive(Clone, Copy, Debug)]
enum Cat {
    Vulgar(u8),
    Frust(f32),
    Blame,
    Positive(f32),
}

/// What a single message yielded.
#[derive(Default, Debug, Clone)]
pub struct Hits {
    pub vulgarity: u32,
    pub frust: f32,
    pub blame: u32,
    pub pos: f32,
    /// (lowercased swear, severity) for the vocabulary breakdown.
    pub swears: Vec<(String, u8)>,
}

pub struct Scanner {
    ac: AhoCorasick,
    cats: Vec<Cat>,
}

impl Default for Scanner {
    fn default() -> Self {
        Self::new()
    }
}

impl Scanner {
    pub fn new() -> Self {
        let mut pats: Vec<&str> = Vec::new();
        let mut cats: Vec<Cat> = Vec::new();
        for (w, s) in SEVERITY {
            pats.push(w);
            cats.push(Cat::Vulgar(*s));
        }
        for (w, f) in FRUSTRATION {
            pats.push(w);
            cats.push(Cat::Frust(*f));
        }
        for w in BLAME {
            pats.push(w);
            cats.push(Cat::Blame);
        }
        for (w, f) in POSITIVE {
            pats.push(w);
            cats.push(Cat::Positive(*f));
        }
        for w in POSITIVE_PHRASES {
            pats.push(w);
            cats.push(Cat::Positive(1.3));
        }
        let ac = AhoCorasick::builder()
            .ascii_case_insensitive(true)
            .match_kind(MatchKind::Standard) // Standard => overlapping iter allowed
            .build(&pats)
            .expect("lexicon automaton builds");
        Scanner { ac, cats }
    }

    pub fn scan(&self, text: &str) -> Hits {
        let bytes = text.as_bytes();
        let mut h = Hits::default();
        for m in self.ac.find_overlapping_iter(text) {
            let (s, e) = (m.start(), m.end());
            // Word-boundary check on both ends (applies to single tokens and phrases alike;
            // phrases keep their internal spaces, only the outer edges must be boundaries).
            let lb = s == 0 || !bytes[s - 1].is_ascii_alphanumeric();
            let rb = e == bytes.len() || !bytes[e].is_ascii_alphanumeric();
            if !(lb && rb) {
                continue;
            }
            match self.cats[m.pattern().as_usize()] {
                Cat::Vulgar(sev) => {
                    h.vulgarity += sev as u32;
                    h.swears.push((text[s..e].to_ascii_lowercase(), sev));
                }
                Cat::Frust(f) => h.frust += f,
                Cat::Blame => h.blame += 1,
                Cat::Positive(f) => h.pos += f,
            }
        }
        h
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn boundaries_and_categories() {
        let sc = Scanner::new();
        // "class" must NOT trip "ass"; "assert" must NOT trip "ass".
        assert_eq!(sc.scan("the class asserts a value").vulgarity, 0);
        // real profanity counts at severity.
        assert_eq!(sc.scan("what the fuck").vulgarity, 3);
        // hype phrase registers as positive, not frustration.
        let h = sc.scan("this is sick as fuck");
        assert!(h.pos > 0.0);
        // directed blame.
        assert_eq!(sc.scan("you broke it again").blame, 1);
    }
}
