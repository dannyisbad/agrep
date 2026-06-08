//! Hand-vectorized hot routines with runtime CPU dispatch.
//!
//! For now: `count_ascii_upper`, used by the intensity feature (CAPS-LOCK ranting). The
//! marquee AVX2 kernel — brute-force cosine for `tilt search` over the embedding matrix —
//! lands with the embedding index. The dispatch pattern here is the template: detect at
//! runtime, AVX-512 path stays dormant on Comet Lake (AVX2), scalar fallback everywhere else.

/// Count ASCII uppercase bytes (0x41..=0x5A). UTF-8 continuation bytes have the high bit set
/// and are correctly excluded by the signed-compare AVX2 path (they read as negative).
pub fn count_ascii_upper(bytes: &[u8]) -> usize {
    #[cfg(target_arch = "x86_64")]
    {
        if is_x86_feature_detected!("avx2") {
            // SAFETY: guarded by runtime feature detection.
            return unsafe { count_ascii_upper_avx2(bytes) };
        }
    }
    count_ascii_upper_scalar(bytes)
}

#[inline]
fn count_ascii_upper_scalar(bytes: &[u8]) -> usize {
    bytes.iter().filter(|b| b.is_ascii_uppercase()).count()
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn count_ascii_upper_avx2(bytes: &[u8]) -> usize {
    use std::arch::x86_64::*;
    let mut count = 0usize;
    let lo = _mm256_set1_epi8((b'A' as i8) - 1); // x > 'A'-1  => x >= 'A'
    let hi = _mm256_set1_epi8((b'Z' as i8) + 1); // 'Z'+1 > x  => x <= 'Z'
    let mut chunks = bytes.chunks_exact(32);
    for c in &mut chunks {
        let v = _mm256_loadu_si256(c.as_ptr() as *const __m256i);
        let ge = _mm256_cmpgt_epi8(v, lo); // 0xFF where x >= 'A' (ASCII; high-bit bytes read negative -> excluded)
        let le = _mm256_cmpgt_epi8(hi, v); // 0xFF where x <= 'Z'
        let mask = _mm256_and_si256(ge, le);
        count += (_mm256_movemask_epi8(mask) as u32).count_ones() as usize;
    }
    count + count_ascii_upper_scalar(chunks.remainder())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn avx2_matches_scalar() {
        let cases: &[&str] = &[
            "",
            "all lowercase here",
            "WHY IS THIS STILL BROKEN",
            "Mixed CASE with Ünïcödé and 中文 bytes",
            "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUuVvWwXxYyZz0123456789",
            "shortCAPS",
        ];
        for s in cases {
            let scalar = count_ascii_upper_scalar(s.as_bytes());
            let dispatched = count_ascii_upper(s.as_bytes());
            assert_eq!(scalar, dispatched, "mismatch on {:?}", s);
        }
    }
}
