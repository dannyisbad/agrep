//! Decoding a JSONL record straight into the fields the parser keeps, while still
//! validating everything it discards, from a line, a reader or a spilled file.

use serde::de::DeserializeSeed;

use super::record::{keep_message_field, keep_record_field};

/// Validate discarded fields too: IgnoredAny accepts numbers and escaped strings
/// which a Value would reject. The visitor borrows strings and retains no tree.
struct Validate;
impl<'de> serde::Deserialize<'de> for Validate {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct Visitor;
        impl<'de> serde::de::Visitor<'de> for Visitor {
            type Value = Validate;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a JSON value")
            }
            fn visit_unit<E: serde::de::Error>(self) -> Result<Validate, E> {
                Ok(Validate)
            }
            fn visit_bool<E: serde::de::Error>(self, _: bool) -> Result<Validate, E> {
                Ok(Validate)
            }
            fn visit_i64<E: serde::de::Error>(self, _: i64) -> Result<Validate, E> {
                Ok(Validate)
            }
            fn visit_u64<E: serde::de::Error>(self, _: u64) -> Result<Validate, E> {
                Ok(Validate)
            }
            fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Validate, E> {
                Ok(Validate)
            }
            fn visit_str<E: serde::de::Error>(self, _: &str) -> Result<Validate, E> {
                Ok(Validate)
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut a: A,
            ) -> Result<Validate, A::Error> {
                while a.next_element::<Validate>()?.is_some() {}
                Ok(Validate)
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(
                self,
                mut a: A,
            ) -> Result<Validate, A::Error> {
                while a.next_entry::<Validate, Validate>()?.is_some() {}
                Ok(Validate)
            }
        }
        d.deserialize_any(Visitor)
    }
}

pub(crate) fn validate_record(bytes: &[u8]) -> serde_json::Result<()> {
    serde_json::from_slice::<Validate>(bytes).map(|_| ())
}
pub(crate) fn validate_record_reader(reader: impl std::io::Read) -> serde_json::Result<()> {
    serde_json::from_reader::<_, Validate>(reader).map(|_| ())
}

struct Projection {
    message: bool,
}
impl<'de> serde::de::DeserializeSeed<'de> for Projection {
    type Value = serde_json::Value;
    fn deserialize<D: serde::Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_any(self)
    }
}
impl<'de> serde::de::Visitor<'de> for Projection {
    type Value = serde_json::Value;
    fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("a JSON value")
    }
    fn visit_unit<E: serde::de::Error>(self) -> Result<Self::Value, E> {
        Ok(serde_json::Value::Null)
    }
    fn visit_bool<E: serde::de::Error>(self, v: bool) -> Result<Self::Value, E> {
        Ok(v.into())
    }
    fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Self::Value, E> {
        Ok(v.into())
    }
    fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Self::Value, E> {
        Ok(v.into())
    }
    fn visit_f64<E: serde::de::Error>(self, v: f64) -> Result<Self::Value, E> {
        Ok(v.into())
    }
    fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<Self::Value, E> {
        Ok(v.into())
    }
    fn visit_string<E: serde::de::Error>(self, v: String) -> Result<Self::Value, E> {
        Ok(v.into())
    }
    fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
        let mut values = Vec::new();
        while let Some(value) = a.next_element()? {
            values.push(value);
        }
        Ok(serde_json::Value::Array(values))
    }
    fn visit_map<A: serde::de::MapAccess<'de>>(self, mut a: A) -> Result<Self::Value, A::Error> {
        let mut fields = serde_json::Map::new();
        while let Some(key) = a.next_key::<String>()? {
            let keep = if self.message {
                keep_message_field(&key)
            } else {
                keep_record_field(&key)
            };
            if keep {
                let value = if !self.message && key == "message" {
                    a.next_value_seed(Projection { message: true })?
                } else {
                    a.next_value()?
                };
                fields.insert(key, value);
            } else {
                a.next_value::<Validate>()?;
            }
        }
        Ok(serde_json::Value::Object(fields))
    }
}

/// Discard unused metadata before it becomes an owned JSON tree. In particular,
/// tool-result `details` can be hundreds of MB beside only a few KB of text.
pub(crate) fn projected_record(line: &str) -> serde_json::Result<serde_json::Value> {
    let mut deserializer = serde_json::Deserializer::from_str(line);
    let value = Projection { message: false }.deserialize(&mut deserializer)?;
    deserializer.end()?;
    Ok(value)
}

fn projected_decoded_record_reader(
    reader: impl std::io::Read,
) -> serde_json::Result<serde_json::Value> {
    // serde_json reads one byte at a time; buffer the decoded stream too.
    let decoded = std::io::BufReader::with_capacity(64 << 10, reader);
    let mut deserializer = serde_json::Deserializer::from_reader(decoded);
    let value = Projection { message: false }.deserialize(&mut deserializer)?;
    deserializer.end()?;
    Ok(value)
}

fn optional_projected_record_reader(
    reader: impl std::io::Read,
) -> serde_json::Result<Option<serde_json::Value>> {
    let mut decoded = crate::ingest::jsonl::LossyReader::new(reader);
    let value = projected_decoded_record_reader(&mut decoded);
    if value.is_err() && decoded.only_whitespace() {
        std::io::copy(&mut decoded, &mut std::io::sink()).map_err(serde_json::Error::io)?;
        if decoded.only_whitespace() {
            return Ok(None);
        }
    }
    value.map(Some)
}

fn restore_string(text: &mut String, file: &mut std::fs::File) -> std::io::Result<()> {
    use std::io::{Read, Seek, SeekFrom};
    let Some(marker) = text.strip_prefix(crate::ingest::jsonl::STRING_MARKER) else {
        return Ok(());
    };
    let (offset, length) = marker
        .split_once(':')
        .ok_or(std::io::ErrorKind::InvalidData)?;
    let offset: u64 = offset
        .parse()
        .map_err(|_| std::io::ErrorKind::InvalidData)?;
    let length: usize = length
        .parse()
        .map_err(|_| std::io::ErrorKind::InvalidData)?;
    let end = offset
        .checked_add(length as u64)
        .ok_or(std::io::ErrorKind::InvalidData)?;
    if end > file.metadata()?.len() {
        return Err(std::io::ErrorKind::InvalidData.into());
    }
    file.seek(SeekFrom::Start(offset))?;
    let mut bytes = vec![0; length];
    file.read_exact(&mut bytes)?;
    *text = String::from_utf8(bytes).map_err(|_| std::io::ErrorKind::InvalidData)?;
    Ok(())
}
fn restore_strings(value: &mut serde_json::Value, file: &mut std::fs::File) -> std::io::Result<()> {
    match value {
        serde_json::Value::String(text) => restore_string(text, file)?,
        serde_json::Value::Array(values) => {
            for value in values {
                restore_strings(value, file)?;
            }
        }
        serde_json::Value::Object(values) => {
            if values
                .keys()
                .any(|key| key.starts_with(crate::ingest::jsonl::STRING_MARKER))
            {
                for (mut key, mut value) in std::mem::take(values) {
                    restore_string(&mut key, file)?;
                    restore_strings(&mut value, file)?;
                    values.insert(key, value);
                }
            } else {
                for value in values.values_mut() {
                    restore_strings(value, file)?;
                }
            }
        }
        _ => {}
    }
    Ok(())
}
const MAPPED_RECORD_BYTES: u64 = 256 << 20;
static RECORD_VIEW: std::sync::Mutex<()> = std::sync::Mutex::new(());

pub(crate) fn optional_projected_record_file(
    file: &mut std::fs::File,
    length: u64,
) -> serde_json::Result<Option<serde_json::Value>> {
    projected_record_file(file, length, MAPPED_RECORD_BYTES)
}

fn projected_record_file(
    file: &mut std::fs::File,
    length: u64,
    mapped_limit: u64,
) -> serde_json::Result<Option<serde_json::Value>> {
    use std::io::{Read, Seek};
    file.rewind().map_err(serde_json::Error::io)?;
    if length > 0
        && length <= mapped_limit
        && length <= file.metadata().map_err(serde_json::Error::io)?.len()
    {
        if let Some(value) = mapped_projection(file, length) {
            return Ok(value);
        }
    }
    let value = projected_decoded_record_reader(crate::ingest::jsonl::LargeStrings::new(
        std::io::BufReader::with_capacity(64 << 10, (&mut *file).take(length)),
    ))
    .and_then(|mut value| {
        restore_strings(&mut value, file).map_err(serde_json::Error::io)?;
        Ok(value)
    });
    match value {
        Ok(value) => Ok(Some(value)),
        Err(_) => {
            // Preserve lossy UTF-8, whitespace handling and original JSON error positions.
            file.rewind().map_err(serde_json::Error::io)?;
            optional_projected_record_reader(file.take(length))
        }
    }
}

/// The record read through a memory map, or None when the view cannot serve it
/// (no map, invalid UTF-8 or a parse error) and the streamed path must decide.
/// Values own their strings, so the capped view is released before row extraction.
fn mapped_projection(file: &std::fs::File, length: u64) -> Option<Option<serde_json::Value>> {
    let _view = RECORD_VIEW
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    // SAFETY: callers own a completed private spill with no other writer or pathname.
    let map = unsafe { memmap2::MmapOptions::new().len(length as usize).map(file) };
    map.ok().and_then(|map| {
        let text = std::str::from_utf8(&map).ok()?;
        if text.trim().is_empty() {
            Some(None)
        } else {
            projected_record(text).ok().map(Some)
        }
    })
}

#[cfg(test)]
mod tests;
