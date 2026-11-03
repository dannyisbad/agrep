//! The encoder remains miniz for size; both decoders read standard zlib streams.
use anyhow::{ensure, Result};

pub(crate) fn inflate(bytes: &[u8]) -> Result<Vec<u8>> {
    let mut decoder = zlib_rs::Inflate::new(true, 15);
    let mut output = Vec::new();
    let step = bytes.len().saturating_mul(4).clamp(256, 16 * 1024);
    loop {
        let before_in = decoder.total_in();
        let before_out = output.len();
        output.resize(before_out + step, 0);
        let status = decoder
            .decompress(
                &bytes[before_in as usize..],
                &mut output[before_out..],
                zlib_rs::InflateFlush::NoFlush,
            )
            .map_err(|e| anyhow::anyhow!("invalid compressed data: {}", e.as_str()))?;
        output.truncate(decoder.total_out() as usize);
        if status == zlib_rs::Status::StreamEnd {
            return Ok(output);
        }
        ensure!(
            decoder.total_in() != before_in || output.len() != before_out,
            "truncated compressed data"
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    #[test]
    fn standard_zlib_round_trips_and_rejects_truncation() {
        for text in [
            String::new(),
            "ordinary café 界".into(),
            "long repeated content".repeat(10_000),
        ] {
            let mut e = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::new(1));
            e.write_all(text.as_bytes()).unwrap();
            let bytes = e.finish().unwrap();
            assert_eq!(inflate(&bytes).unwrap(), text.as_bytes());
            assert!(inflate(&bytes[..bytes.len() - 1]).is_err());
        }
        assert!(inflate(b"invalid stream").is_err());
    }
}
