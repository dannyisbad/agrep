//! What finds a query's matches in a row or a snippet. Only a user's `-E` expression runs
//! on fancy-regex, which backtracks, at most 100,000 times per search, when the
//! expression needs look-around, a backreference or another feature the regex crate
//! lacks. Keyword phrases and terms run on the linear-time regex crate and `-w` on a
//! literal search, so neither can reach that limit.
use crate::unicode_v16;
use anyhow::Result;
use std::ops::Range;

#[derive(Clone)]
pub enum Pattern {
    /// Keyword phrases and terms.
    Linear(regex::Regex),
    /// `-w`.
    Word(WholeWord),
    /// `-E`.
    Fancy(fancy_regex::Regex),
}

impl Pattern {
    /// A case-insensitive expression the regex crate supports.
    pub fn linear(pattern: &str) -> Result<Self> {
        Ok(Self::Linear(regex::Regex::new(&format!("(?i:{pattern})"))?))
    }
    /// A user's `-E` expression, case-insensitive.
    pub fn regex(pattern: &str) -> Result<Self> {
        let mut b = fancy_regex::RegexBuilder::new(&format!("(?i:{pattern})"));
        b.backtrack_limit(100_000);
        Ok(Self::Fancy(b.build()?))
    }
    /// A `-w` term, which must not be empty.
    pub fn word(term: &str) -> Result<Self> {
        Ok(Self::Word(WholeWord::new(term)?))
    }
    /// The first match.
    pub fn find(&self, text: &str) -> Result<Option<Range<usize>>> {
        Ok(match self {
            Self::Linear(re) => re.find(text).map(|m| m.range()),
            Self::Word(word) => word.find_at(text, 0),
            Self::Fancy(re) => re.find(text)?.map(|m| m.range()),
        })
    }
    /// Each match from the previous one's end, as Python's `finditer` and `sub` take them.
    pub fn each(&self, text: &str, mut visit: impl FnMut(Range<usize>)) -> Result<()> {
        match self {
            Self::Linear(re) => re.find_iter(text).for_each(|m| visit(m.range())),
            Self::Word(word) => {
                let mut at = 0;
                while let Some(found) = word.find_at(text, at) {
                    at = found.end;
                    visit(found);
                }
            }
            Self::Fancy(re) => {
                for m in re.find_iter(text) {
                    visit(m?.range());
                }
            }
        }
        Ok(())
    }
}

/// 0.4.9's `-w`: `(?<!\w)` + `re.escape(term)` + `(?!\w)` under `re.I`
/// (py/console.py:285-287). The literal matches each term character as `re`'s
/// IGNORECASE does, and a match stands where neither neighbor is `\w`, which for `re` is an
/// alphanumeric character or `_`. The literal is found by the regex crate and the two
/// neighbors are checked after, so no search backtracks.
#[derive(Clone)]
pub struct WholeWord {
    literal: regex::Regex,
}

impl WholeWord {
    fn new(term: &str) -> Result<Self, regex::Error> {
        debug_assert!(!term.is_empty());
        let mut pattern = String::new();
        let mut buffer = [0; 4];
        for character in term.chars() {
            let class = unicode_v16::sre_case_class(character);
            if class.len() > 1 {
                pattern.push('[');
            }
            for &member in &class {
                pattern.push_str(&regex::escape(member.encode_utf8(&mut buffer)));
            }
            if class.len() > 1 {
                pattern.push(']');
            }
        }
        Ok(Self {
            literal: regex::Regex::new(&pattern)?,
        })
    }
    /// The first match starting at or after `at`. The character before `at` still bounds
    /// a match there, as when `re` searches from a position.
    fn find_at(&self, text: &str, mut at: usize) -> Option<Range<usize>> {
        while let Some(m) = self.literal.find_at(text, at) {
            if !text[..m.start()].chars().next_back().is_some_and(is_word)
                && !text[m.end()..].chars().next().is_some_and(is_word)
            {
                return Some(m.range());
            }
            at = m.start() + text[m.start()..].chars().next()?.len_utf8();
        }
        None
    }
}

/// `\w` in a Python `re` pattern on `str`.
fn is_word(character: char) -> bool {
    character == '_' || unicode_v16::is_alphanumeric(character)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every match, as character spans.
    fn word_spans(term: &str, text: &str) -> Vec<(usize, usize)> {
        let chars = |byte: usize| text[..byte].chars().count();
        let mut spans = vec![];
        Pattern::word(term)
            .unwrap()
            .each(text, |m| spans.push((chars(m.start), chars(m.end))))
            .unwrap();
        spans
    }

    // Python 3.14's `re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.I)`, the
    // pattern 0.4.9 built, gives these spans with `finditer`.
    #[test]
    fn a_whole_word_matches_as_python_re_does() {
        for (term, text, spans) in [
            // `re`'s `\w` is `str.isalnum()` or `_`, unlike the regex crate's: combining
            // marks, connector punctuation, joiners and circled letters are not word
            // characters, while superscript and circled digits are.
            ("test", "\u{301}test", vec![(1, 5)]),
            ("test", "e\u{301} test", vec![(3, 7)]),
            ("test", "test\u{203f}x", vec![(0, 4)]),
            ("test", "test\u{200d}x", vec![(0, 4)]),
            ("test", "\u{24b6}test", vec![(1, 5)]),
            ("test", "x\u{b2}test", vec![]),
            ("test", "\u{2460}test", vec![]),
            ("test", "\u{661}test", vec![]),
            ("test", "\u{3007}test", vec![]),
            ("test", "\u{aa}test", vec![]),
            ("test", "_test", vec![]),
            ("test", "test_", vec![]),
            ("test", "testing latest tests", vec![]),
            ("TEST", "a Test b", vec![(2, 6)]),
            // IGNORECASE compares `re`'s lowercase and equates i with dotless ı and s
            // with long ſ; it never expands ß.
            ("is", "\u{130}S", vec![(0, 2)]),
            ("is", "\u{131}s", vec![(0, 2)]),
            ("is", "\u{130}\u{17f}", vec![(0, 2)]),
            ("kelvin", "\u{212a}ELVIN", vec![(0, 6)]),
            ("ss", "\u{df}", vec![]),
            ("\u{3c3}", "\u{3c2}", vec![(0, 1)]),
            (
                "\u{3c3}\u{3bf}\u{3c6}\u{3b9}\u{3b1}",
                "\u{3a3}\u{39f}\u{3a6}\u{399}\u{391}",
                vec![(0, 5)],
            ),
            // The term is literal: its spaces, and its punctuation edges, whose outer
            // neighbors must still be no word characters.
            ("flaky windows", "a flaky windows b", vec![(2, 15)]),
            ("flaky windows", "flaky  windows", vec![]),
            ("flaky windows", "flaky-windows", vec![]),
            ("c++", "use c++ now", vec![(4, 7)]),
            ("c++", "c++", vec![(0, 3)]),
            ("c++", "c++x", vec![]),
            ("c++", "xc++", vec![]),
            (".env", "cp .env here", vec![(3, 7)]),
            (".env", "x.env", vec![]),
            ("foo.", "foo.bar foo. end", vec![(8, 12)]),
            ("-v", "a -v", vec![(2, 4)]),
            ("-v", "a-v", vec![]),
            // A rejected occurrence does not hide one overlapping it, and each match
            // starts after the previous one ends.
            ("aa", "aaa aa", vec![(4, 6)]),
            ("ab ab", "xab ab ab", vec![(4, 9)]),
            (".a.", ".a..a.", vec![(0, 3), (3, 6)]),
        ] {
            assert_eq!(word_spans(term, text), spans, "{term:?} in {text:?}");
        }
    }

    // fancy-regex charges a backtrack for each start position it rejects: the expression
    // `-w` used to compile, still what `-E` runs, exceeds its limit of 100,000 on a long row
    // without the whole word.
    #[test]
    fn a_long_row_without_the_word_never_exhausts_a_search() {
        let row = "testing the latest tests ".repeat(10_000);
        assert!(Pattern::regex(r"(?<!\w)(?:test)(?!\w)")
            .unwrap()
            .find(&row)
            .is_err());
        assert!(Pattern::word("test").unwrap().find(&row).unwrap().is_none());
        let row = format!("{row}test");
        let found = Pattern::word("test").unwrap().find(&row).unwrap();
        assert_eq!(found, Some(row.len() - 4..row.len()));
    }
}
