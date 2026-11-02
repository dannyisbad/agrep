//! The live Board is a glanceable surface; redact credentials and browser capabilities.
use regex::{Captures, Regex};
use std::sync::LazyLock;
pub(super) fn redact(s: &str) -> String {
    static RULES: LazyLock<Vec<Regex>> = LazyLock::new(|| {
        [
        r#"(?i)([?#&]bootstrap(?:[-_]?token)?=)("[^"\r\n]*"|'[^'\r\n]*'|[^&\s"'<>]+)"#,
        r#"(?i)(\bX-Agrep-Bootstrap["']?[ \t]*:[ \t]*)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;"']+)"#,
        r#"(?i)(\b(?:Proxy-)?Authorization["']?[ \t]*:[ \t]*["']?(?:Bearer|Basic|Token)[ \t]+)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;"']+)"#,
        r#"(?i)(\bagrep_ui_[0-9a-f]{12}=)([^;\s"']+)"#,
        r#"(?i)(--?(?:api[-_]?key|access[-_]?token|auth[-_]?token|token|refresh[-_]?token|session[-_]?token|password|passwd|secret|client[-_]?secret|private[-_]?key|signing[-_]?key|secret[-_]?access[-_]?key|database[-_]?url|connection[-_]?string|bootstrap(?:[-_]?token)?)(?:[ \t]*=[ \t]*|[ \t]+))("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]&#]+)"#,
        r#"(?i)((?:"|')?(?:api[-_]?key|access[-_]?token|auth[-_]?token|refresh[-_]?token|session[-_]?token|password|passwd|secret|client[-_]?secret|private[-_]?key|signing[-_]?key|secret[-_]?access[-_]?key|database[-_]?url|connection[-_]?string|bootstrap[-_]?token|[a-z][a-z0-9]*_(?:api_key|access_token|auth_token|refresh_token|session_token|password|secret|private_key|signing_key|secret_access_key|database_url|connection_string))(?:"|')?[ \t]*[:=][ \t]*)("[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]&#]+)"#,
    ].into_iter().map(|s|Regex::new(s).unwrap()).collect()
    });
    let mut text = s.to_owned();
    for (i, rule) in RULES.iter().enumerate() {
        text = rule
            .replace_all(&text, |c: &Captures<'_>| {
                let value = &c[2];
                let quote = value.chars().next().filter(|c| *c == '\'' || *c == '"');
                if i == 5
                    && quote.is_none()
                    && [
                        "bool", "boolean", "bytes", "float", "int", "integer", "none", "null",
                        "number", "optional", "required", "str", "string", "true", "false",
                    ]
                    .contains(&value.to_ascii_lowercase().as_str())
                {
                    return c[0].to_owned();
                }
                format!(
                    "{}{}[redacted]{}",
                    &c[1],
                    quote.map(|c| c.to_string()).unwrap_or_default(),
                    quote.map(|c| c.to_string()).unwrap_or_default()
                )
            })
            .into_owned();
    }
    static CIPHER: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"gAAAAA[A-Za-z0-9_-]{64,}={0,2}").unwrap());
    text = CIPHER
        .replace_all(&text, "[encrypted payload]")
        .into_owned();
    static BEARER: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?i)(\bBearer[ \t]+)([A-Za-z0-9._~+/=-]{8,})").unwrap());
    text = BEARER
        .replace_all(&text, |c: &Captures<'_>| {
            if c[2].chars().all(char::is_alphabetic) && c[2].len() < 20 {
                c[0].to_owned()
            } else {
                format!("{}[redacted]", &c[1])
            }
        })
        .into_owned();
    static URL: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/?#\s@]+)@").unwrap());
    URL.replace_all(&text, |c: &Captures<'_>| {
        if c[2].contains(':') {
            format!("{}[redacted]@", &c[1])
        } else {
            c[0].to_owned()
        }
    })
    .into_owned()
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn capabilities_and_credentials() {
        assert_eq!(redact("http://127.0.0.1:1/#bootstrap=secret&x=1 --api-key 'sk-secret' password=abc"),"http://127.0.0.1:1/#bootstrap=[redacted]&x=1 --api-key '[redacted]' password=[redacted]");
        assert_eq!(
            redact("password=string https://u:p@example.com"),
            "password=string https://[redacted]@example.com"
        );
    }
}
