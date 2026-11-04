//! The encoder remains miniz for size; both decoders read standard zlib streams.
use anyhow::{ensure, Result};
use std::cell::RefCell;

thread_local! {
    // A zlib compressor's state is a few hundred KiB. Allocating one per page or
    // tool text made Windows first builds 45x slower than macOS (its heap
    // decommits and re-faults blocks that size), so each thread reuses one.
    static DEFLATE: RefCell<flate2::Compress> =
        RefCell::new(flate2::Compress::new(flate2::Compression::new(1), true));
}

/// `prefix` followed by the level-1 zlib stream of `input`.
pub(crate) fn deflate(prefix: &[u8], input: &[u8]) -> Result<Vec<u8>> {
    DEFLATE.with(|compressor| {
        let mut compressor = compressor.borrow_mut();
        compressor.reset();
        let mut output = Vec::with_capacity(prefix.len() + input.len() / 2 + 64);
        output.extend_from_slice(prefix);
        loop {
            let consumed = compressor.total_in() as usize;
            let status = compressor.compress_vec(
                &input[consumed..],
                &mut output,
                flate2::FlushCompress::Finish,
            )?;
            if status == flate2::Status::StreamEnd {
                return Ok(output);
            }
            output.reserve(output.capacity().max(1024));
        }
    })
}

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

    #[test]
    fn reused_compressor_round_trips_after_every_reset() {
        let incompressible: String = (0..70_000u32)
            .map(|i| char::from_u32(0x4e00 + (i.wrapping_mul(2_654_435_761) % 20_000)).unwrap())
            .collect();
        let inputs = [
            String::new(),
            "ordinary café 界".into(),
            "long repeated content".repeat(10_000),
            incompressible,
        ];
        let first: Vec<_> = inputs
            .iter()
            .map(|text| deflate(b"G2", text.as_bytes()).unwrap())
            .collect();
        // A second pass runs on a compressor reset after unrelated input.
        for (text, earlier) in inputs.iter().zip(&first) {
            let bytes = deflate(b"G2", text.as_bytes()).unwrap();
            assert_eq!(&bytes, earlier);
            assert_eq!(&bytes[..2], b"G2");
            assert_eq!(inflate(&bytes[2..]).unwrap(), text.as_bytes());
        }
        assert!(first[2].len() < inputs[2].len() / 50);
    }
}
