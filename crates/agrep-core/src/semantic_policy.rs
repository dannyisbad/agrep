//! Eligibility and lane merge ported from py/search.py, without runtime policy states.
pub const FLOOR: f64 = 0.82;
/// 0.4.9's strong band: a meaning row below it is a weak neighbor.
pub const STRONG: f64 = 0.84;
const STOP: &str = "a an and are as at be been but by can could did do does for from had has have how i if in is it its me my not of on or our should so that the their them they this these those to was we were what when where which who why will with would you your whats what's im i'm dont don't isnt isn't didnt didn't";
fn wordlike(letters: &str) -> bool {
    const PAIRS: &str = "th he in er an re on at en nd ti es or te of ed is it al ar st to nt ng se ha as ou io le ve co me de hi ri ro ic ne ea ra ce li ch ll be ma si om ur ca el ta la ns di fo ho pe ec pr no ct us ac ot il tr ly nc et ut ss so rs un lo wa ge ie wh ee wi em ad ol rt po we na ul ni ts mo ow pa im mi ai sh do ck ke eb gr ph ap ry cr pt";
    let low = letters.to_ascii_lowercase();
    if low.len() < 7 {
        return true;
    }
    let pairs: Vec<_> = low.as_bytes().windows(2).collect();
    let sequential = pairs.iter().filter(|p| p[0].abs_diff(p[1]) == 1).count();
    if sequential >= 4.max((pairs.len() as f64 * 0.6) as usize) {
        return false;
    }
    if ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
        .iter()
        .any(|row| row.contains(&low) || row.chars().rev().collect::<String>().contains(&low))
    {
        return false;
    }
    let common = pairs
        .iter()
        .filter(|p| PAIRS.split(' ').any(|s| s.as_bytes() == **p))
        .count();
    common as f64 / pairs.len() as f64 >= 0.25
}
fn obvious_identifier(q: &str) -> bool {
    static IDENT: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
        regex::Regex::new(r"[/\\:@_]|[A-Z][a-z]+[A-Z]|[a-z][A-Z]|^(?:0x)?[0-9a-fA-F]{8,}$|^[A-Za-z0-9_.-]+\.[A-Za-z][A-Za-z0-9]{1,7}$|^[vV]\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$").unwrap()
    });
    IDENT.is_match(q) || (q.len() >= 4 && q == q.to_uppercase())
}
fn long_token(q: &str) -> bool {
    q.len() >= 24
        && !q
            .chars()
            .all(|c| c.is_ascii_alphabetic() || "'-".contains(c))
}
fn identifier(q: &str) -> bool {
    obvious_identifier(q) || long_token(q)
}
/// 0.4.9's `semantic_query_policy` (py/search.py:358-405): why a query is no request for
/// meaning search, in its order of checks, or `None` when it is one.
pub fn rejection(query: &str) -> Option<&'static str> {
    let q = query.trim();
    if q.is_empty() {
        return Some("empty-query");
    }
    if q.chars().any(|c| c.is_control()) {
        return Some("control-characters");
    }
    if !q.chars().any(|c| c.is_ascii_alphabetic()) {
        return Some("non-language-query");
    }
    if q.split_whitespace().count() != 1 {
        return None;
    }
    let letters: String = q.chars().filter(char::is_ascii_alphabetic).collect();
    if obvious_identifier(q) {
        return Some("identifier-query");
    }
    if letters.len() >= 7
        && (!letters
            .to_lowercase()
            .contains(['a', 'e', 'i', 'o', 'u', 'y'])
            || !wordlike(&letters))
    {
        return Some("gibberish-query");
    }
    long_token(q).then_some("identifier-query")
}
fn policy(q: &str) -> bool {
    rejection(q).is_none()
}
pub fn eligible(query: &str) -> bool {
    if !policy(query) {
        return false;
    }
    let (mut natural, mut support) = (0, 0);
    for token in query.split_whitespace() {
        let cleaned = token.trim_matches(|c| ".,;:!?()[]{}<>\"'".contains(c));
        let letters: String = cleaned.chars().filter(char::is_ascii_alphabetic).collect();
        if cleaned.is_empty() || STOP.split(' ').any(|s| s == cleaned.to_lowercase()) {
            continue;
        }
        if policy(cleaned) && letters.len() >= 4 {
            natural += 1;
        } else if (letters == letters.to_uppercase() && (2..=6).contains(&letters.len()))
            || identifier(cleaned)
            || letters.is_empty()
        {
            support += 1;
        }
    }
    natural >= 2 || (natural >= 1 && support >= 1)
}
/// 0.4.9's `semantic_query_terms`: the content words a meaning row is expected to carry,
/// lowercased, deduped, at least three characters and never a stopword.
pub fn query_terms(query: &str) -> Vec<String> {
    let mut words: Vec<String> = Vec::new();
    for word in query.split(|c: char| !c.is_ascii_alphanumeric()) {
        let low = word.to_ascii_lowercase();
        if low.len() >= 3 && !STOP.split(' ').any(|s| s == low) && !words.contains(&low) {
            words.push(low);
        }
    }
    words
}
/// 0.4.9's `_sem_term_weak` with its equal term weights: the row carries less than half of
/// `terms`.
pub fn term_weak(terms: &[String], text: &str) -> bool {
    let low = text.to_lowercase();
    let matched = terms.iter().filter(|t| low.contains(t.as_str())).count();
    !terms.is_empty() && matched * 2 < terms.len()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn eligibility() {
        for q in [
            "connection pool timeout",
            "fix Rust borrow checker",
            "PostgreSQL ECONNRESET failure",
            "how did we resolve migration failure?",
        ] {
            assert!(eligible(q), "{q}");
        }
        for q in [
            "pool",
            "foo_bar",
            "foo/bar.rs",
            "deadbeef",
            "v1.2.3",
            "the and",
            "hash zzzxxxvvv",
            "connection\nreset",
        ] {
            assert!(!eligible(q), "{q}");
        }
    }
    #[test]
    fn rejection_reasons_match_0_4_9() {
        // py/search.py:358-405 `semantic_query_policy(q)["reason"]` for each query.
        for (q, reason) in [
            ("", Some("empty-query")),
            ("   ", Some("empty-query")),
            ("a\x07b", Some("control-characters")),
            ("1234 5678", Some("non-language-query")),
            ("foo_bar", Some("identifier-query")),
            ("foo/bar.rs", Some("identifier-query")),
            ("deadbeef", Some("identifier-query")),
            ("v1.2.3", Some("identifier-query")),
            ("HTTPS", Some("identifier-query")),
            ("server.yaml", Some("identifier-query")),
            ("getUserName", Some("identifier-query")),
            ("bcdfghjklm", Some("gibberish-query")),
            ("qwertyuiop", Some("gibberish-query")),
            ("abcdefghij", Some("gibberish-query")),
            ("bcdfghjklmnpqrstvwxz1234", Some("gibberish-query")),
            ("aaaaaaaaaaaaaaaaaaaaaaa1", Some("identifier-query")),
            ("migration", None),
            ("database migration failure", None),
        ] {
            assert_eq!(rejection(q), reason, "{q:?}");
            assert_eq!(policy(q), reason.is_none(), "{q:?}");
        }
    }
    #[test]
    fn meaning_rows_are_weak_below_half_the_query_terms_as_0_4_9() {
        // py/search.py:411-435
        let terms = query_terms("how do we rotate secret credentials?");
        assert_eq!(terms, ["rotate", "secret", "credentials"]);
        assert_eq!(query_terms("go to it, a b"), Vec::<String>::new());
        assert_eq!(query_terms("foo.bar foo"), ["foo", "bar"]);
        assert!(term_weak(&terms, "updates the Secret store"));
        assert!(!term_weak(
            &terms,
            "deploy CREDENTIALS and the secret store"
        ));
        assert!(!term_weak(&[], "anything"));
    }
}
