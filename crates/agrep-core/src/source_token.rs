//! Compact file tokens stay numeric during a census; encoding is only for writes.
use crate::ingest::registry::ChangeToken;
use rusqlite::types::{FromSql, FromSqlError, FromSqlResult, ToSql, ToSqlOutput, Value, ValueRef};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum SourceToken {
    File(ChangeToken),
    Opaque(String),
}
impl Default for SourceToken {
    fn default() -> Self {
        Self::Opaque(String::new())
    }
}
impl std::fmt::Display for SourceToken {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.diagnostic())
    }
}
impl SourceToken {
    pub(crate) fn diagnostic(&self) -> String {
        match self {
            Self::File(value) => serde_json::to_string(value).expect("source token"),
            Self::Opaque(value) => value.clone(),
        }
    }
}
impl ToSql for SourceToken {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        let mut bytes = Vec::new();
        match self {
            Self::File(token) => token.append_key(&mut bytes),
            Self::Opaque(value) => {
                bytes.push(2);
                bytes.extend_from_slice(value.as_bytes());
            }
        }
        Ok(ToSqlOutput::Owned(Value::Blob(bytes)))
    }
}
impl FromSql for SourceToken {
    fn column_result(value: ValueRef<'_>) -> FromSqlResult<Self> {
        let bytes = value.as_blob()?;
        match bytes {
            [0, value @ ..] if value.len() == 8 => Ok(Self::File(ChangeToken::Metadata(
                u64::from_le_bytes(value.try_into().expect("length checked")),
            ))),
            [1, value @ ..] if value.len() == 32 => Ok(Self::File(ChangeToken::ContentSha256(
                value.try_into().expect("length checked"),
            ))),
            [2, value @ ..] => std::str::from_utf8(value)
                .map(|value| Self::Opaque(value.to_owned()))
                .map_err(|error| FromSqlError::Other(Box::new(error))),
            _ => Err(FromSqlError::Other(Box::new(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "invalid source token",
            )))),
        }
    }
}
impl Serialize for SourceToken {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&self.diagnostic())
    }
}
impl<'de> Deserialize<'de> for SourceToken {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = String::deserialize(deserializer)?;
        Ok(match serde_json::from_str::<ChangeToken>(&value) {
            Ok(token) => Self::File(token),
            Err(_) => Self::Opaque(value),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tokens_round_trip_and_preserve_diagnostics() {
        let c = rusqlite::Connection::open_in_memory().unwrap();
        for token in [
            SourceToken::File(ChangeToken::Metadata(0)),
            SourceToken::File(ChangeToken::Metadata(u64::MAX)),
            SourceToken::File(ChangeToken::ContentSha256([71; 32])),
            SourceToken::Opaque(String::new()),
            SourceToken::Opaque("opaque 界 token".into()),
        ] {
            let stored: SourceToken = c.query_row("SELECT ?", [&token], |r| r.get(0)).unwrap();
            assert_eq!(stored, token);
            let output = serde_json::to_value(&token).unwrap();
            assert_eq!(output, token.diagnostic());
        }
        assert_eq!(
            SourceToken::File(ChangeToken::Metadata(42)).diagnostic(),
            r#"{"Metadata":42}"#
        );
        for invalid in [vec![], vec![0], vec![1; 32], vec![2, 255], vec![3]] {
            assert!(c
                .query_row("SELECT ?", [invalid], |r| r.get::<_, SourceToken>(0))
                .is_err());
        }
    }
}
