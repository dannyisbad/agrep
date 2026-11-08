use anyhow::Result;
use sha2::Digest;
use sha2::Sha256;

pub fn digest(text: &str) -> String {
    hex_digest(Sha256::digest(text.as_bytes()))
}
pub(crate) fn hex_digest(bytes: impl AsRef<[u8]>) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(64);
    for &byte in bytes.as_ref() {
        out.push(HEX[(byte >> 4) as usize] as char);
        out.push(HEX[(byte & 15) as usize] as char);
    }
    out
}
pub fn normalize(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    crate::boundary_rank::normalize_token(text)
}
/// A literal-search key: Python `re.I` equivalences, without compatibility
/// normalization or expansions. Boundary statistics keep their separate NFKC fold.
pub(crate) fn search_fold(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    crate::unicode_v16::sre_fold_text(text, |_, _, _| {})
}

pub(super) fn search_key(text: &str) -> (String, bool) {
    if text.is_ascii() {
        return (text.to_ascii_lowercase(), false);
    }
    let mut aliases = false;
    let key = crate::unicode_v16::sre_fold_text(text, |_, _, mask| aliases |= mask != 0);
    (key, aliases)
}

/// Remove precisely the separators the phrase matcher permits between query terms.
pub fn joined(text: &str) -> String {
    if text.is_ascii() {
        return text
            .bytes()
            .filter(|b| b.is_ascii_alphanumeric())
            .map(char::from)
            .collect();
    }
    static SEPARATORS: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"[\W_]").unwrap());
    SEPARATORS.replace_all(text, "").into_owned()
}
pub fn decode(table: &str, bytes: Vec<u8>) -> Result<String> {
    if table == "tools" {
        crate::tool_store::decode(&bytes)
    } else {
        Ok(String::from_utf8(bytes)?)
    }
}
pub fn encode_tool(text: &str) -> Result<Vec<u8>> {
    crate::tool_store::encode(text)
}
