//! Signed int8 SIMD dot product core for future in-process semantic retrieval.
pub fn dot_scalar(left: &[i8], right: &[i8]) -> i32 {
    left.iter()
        .zip(right)
        .map(|(a, b)| i32::from(*a) * i32::from(*b))
        .sum()
}

pub fn dot(left: &[i8], right: &[i8]) -> i32 {
    debug_assert_eq!(left.len(), right.len());
    selected_dot(right)(left, right)
}

type DotProduct = fn(&[i8], &[i8]) -> i32;

fn selected_dot(_right: &[i8]) -> DotProduct {
    #[cfg(target_arch = "x86_64")]
    if std::arch::is_x86_feature_detected!("avx2") {
        return if _right.contains(&i8::MIN) {
            dot_avx2_safe
        } else {
            dot_avx2_q8_safe
        };
    }
    #[cfg(target_arch = "aarch64")]
    if std::arch::is_aarch64_feature_detected!("neon") {
        return dot_neon_safe;
    }
    dot_scalar
}

#[cfg(target_arch = "x86_64")]
fn dot_avx2_safe(left: &[i8], right: &[i8]) -> i32 {
    unsafe { dot_avx2(left, right) }
}

#[cfg(target_arch = "x86_64")]
fn dot_avx2_q8_safe(left: &[i8], right: &[i8]) -> i32 {
    unsafe { dot_avx2_q8(left, right) }
}

#[cfg(target_arch = "aarch64")]
fn dot_neon_safe(left: &[i8], right: &[i8]) -> i32 {
    unsafe { dot_neon(left, right) }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn dot_avx2(left: &[i8], right: &[i8]) -> i32 {
    use std::arch::x86_64::*;
    let mut sum = _mm256_setzero_si256();
    let mut offset = 0usize;
    while offset + 32 <= left.len() {
        let a = _mm256_loadu_si256(left.as_ptr().add(offset).cast());
        let b = _mm256_loadu_si256(right.as_ptr().add(offset).cast());
        let a_low = _mm256_cvtepi8_epi16(_mm256_castsi256_si128(a));
        let b_low = _mm256_cvtepi8_epi16(_mm256_castsi256_si128(b));
        let a_high = _mm256_cvtepi8_epi16(_mm256_extracti128_si256(a, 1));
        let b_high = _mm256_cvtepi8_epi16(_mm256_extracti128_si256(b, 1));
        sum = _mm256_add_epi32(sum, _mm256_madd_epi16(a_low, b_low));
        sum = _mm256_add_epi32(sum, _mm256_madd_epi16(a_high, b_high));
        offset += 32;
    }
    let high = _mm256_extracti128_si256(sum, 1);
    let low = _mm256_castsi256_si128(sum);
    let mut total = _mm_add_epi32(low, high);
    total = _mm_hadd_epi32(total, total);
    total = _mm_hadd_epi32(total, total);
    _mm_cvtsi128_si32(total) + dot_scalar(&left[offset..], &right[offset..])
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2")]
unsafe fn dot_avx2_q8(left: &[i8], right: &[i8]) -> i32 {
    use std::arch::x86_64::*;
    let ones = _mm256_set1_epi16(1);
    let mut sum0 = _mm256_setzero_si256();
    let mut sum1 = _mm256_setzero_si256();
    let mut sum2 = _mm256_setzero_si256();
    let mut sum3 = _mm256_setzero_si256();
    let mut offset = 0usize;
    while offset + 128 <= left.len() {
        for (lane, sum) in [
            (0usize, &mut sum0),
            (32, &mut sum1),
            (64, &mut sum2),
            (96, &mut sum3),
        ] {
            let a = _mm256_loadu_si256(left.as_ptr().add(offset + lane).cast());
            let b = _mm256_loadu_si256(right.as_ptr().add(offset + lane).cast());
            let pairs = _mm256_maddubs_epi16(_mm256_abs_epi8(a), _mm256_sign_epi8(b, a));
            *sum = _mm256_add_epi32(*sum, _mm256_madd_epi16(pairs, ones));
        }
        offset += 128;
    }
    while offset + 32 <= left.len() {
        let a = _mm256_loadu_si256(left.as_ptr().add(offset).cast());
        let b = _mm256_loadu_si256(right.as_ptr().add(offset).cast());
        let pairs = _mm256_maddubs_epi16(_mm256_abs_epi8(a), _mm256_sign_epi8(b, a));
        sum0 = _mm256_add_epi32(sum0, _mm256_madd_epi16(pairs, ones));
        offset += 32;
    }
    let sum = _mm256_add_epi32(_mm256_add_epi32(sum0, sum1), _mm256_add_epi32(sum2, sum3));
    let high = _mm256_extracti128_si256(sum, 1);
    let low = _mm256_castsi256_si128(sum);
    let mut total = _mm_add_epi32(low, high);
    total = _mm_hadd_epi32(total, total);
    total = _mm_hadd_epi32(total, total);
    _mm_cvtsi128_si32(total) + dot_scalar(&left[offset..], &right[offset..])
}

#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn dot_neon(left: &[i8], right: &[i8]) -> i32 {
    use std::arch::aarch64::*;
    let mut sum0 = vdupq_n_s32(0);
    let mut sum1 = vdupq_n_s32(0);
    let mut sum2 = vdupq_n_s32(0);
    let mut sum3 = vdupq_n_s32(0);
    let mut offset = 0usize;
    while offset + 64 <= left.len() {
        for (lane, sum) in [
            (0usize, &mut sum0),
            (16, &mut sum1),
            (32, &mut sum2),
            (48, &mut sum3),
        ] {
            let a = vld1q_s8(left.as_ptr().add(offset + lane));
            let b = vld1q_s8(right.as_ptr().add(offset + lane));
            *sum = vpadalq_s16(*sum, vmull_s8(vget_low_s8(a), vget_low_s8(b)));
            *sum = vpadalq_s16(*sum, vmull_s8(vget_high_s8(a), vget_high_s8(b)));
        }
        offset += 64;
    }
    while offset + 16 <= left.len() {
        let a = vld1q_s8(left.as_ptr().add(offset));
        let b = vld1q_s8(right.as_ptr().add(offset));
        sum0 = vpadalq_s16(sum0, vmull_s8(vget_low_s8(a), vget_low_s8(b)));
        sum0 = vpadalq_s16(sum0, vmull_s8(vget_high_s8(a), vget_high_s8(b)));
        offset += 16;
    }
    let sum = vaddq_s32(vaddq_s32(sum0, sum1), vaddq_s32(sum2, sum3));
    vaddvq_s32(sum) + dot_scalar(&left[offset..], &right[offset..])
}

/// Scan row-scaled signed int8 vectors with a bounded top-k heap.
/// The query and rows use the same dimensionality; ties keep the lower row index.
pub fn scan(
    rows: &[i8],
    scales: &[f32],
    query: &[i8],
    query_scale: f32,
    k: usize,
) -> Vec<(usize, f32)> {
    use std::{cmp::Ordering, collections::BinaryHeap};
    #[derive(Clone, Copy, PartialEq)]
    struct Candidate(usize, f32);
    impl Eq for Candidate {}
    impl Ord for Candidate {
        fn cmp(&self, other: &Self) -> Ordering {
            other.1.total_cmp(&self.1).then(self.0.cmp(&other.0))
        }
    }
    impl PartialOrd for Candidate {
        fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
            Some(self.cmp(other))
        }
    }
    assert!(!query.is_empty() && rows.len() == scales.len() * query.len());
    let mut heap = BinaryHeap::new();
    if k == 0 {
        return vec![];
    }
    for (i, row) in rows.chunks_exact(query.len()).enumerate() {
        let score = dot(row, query) as f32 * scales[i] * query_scale;
        let next = Candidate(i, score);
        if heap.len() < k {
            heap.push(next)
        } else if next < *heap.peek().unwrap() {
            heap.pop();
            heap.push(next);
        }
    }
    let mut result: Vec<_> = heap.into_iter().map(|c| (c.0, c.1)).collect();
    result.sort_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
    result
}
#[cfg(test)]
mod tests {
    #[test]
    fn scanner_matches_signed_reference() {
        let data: Vec<i8> = (0..384 * 23).map(|i| (i % 256) as u8 as i8).collect();
        let query: Vec<i8> = (0..384).map(|i| ((i * 13) % 256) as u8 as i8).collect();
        let scales = vec![0.001; 23];
        let mut oracle: Vec<_> = data
            .chunks(384)
            .enumerate()
            .map(|(i, r)| (i, super::dot_scalar(r, &query) as f32 * 0.001 * 0.002))
            .collect();
        oracle.sort_by(|a, b| b.1.total_cmp(&a.1).then(a.0.cmp(&b.0)));
        oracle.truncate(7);
        assert_eq!(super::scan(&data, &scales, &query, 0.002, 7), oracle);
    }
}
