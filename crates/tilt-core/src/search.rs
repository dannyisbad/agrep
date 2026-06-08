//! The marquee AVX2 kernel: brute-force cosine search over the embedding matrix.
//!
//! Embedding rows are L2-normalized on the Python side (Matryoshka-truncated to D=256, then
//! renormalized), and the query vector is normalized too — so cosine similarity collapses to a
//! plain dot product. `top_k` is the whole semantic-search hot loop: one `dot` per row, rayon
//! across row chunks, partial-sort to the best `k`. The dispatch shape mirrors `simd.rs`
//! (runtime `is_x86_feature_detected!`, `#[target_feature]` unsafe inner fn, scalar fallback).

use rayon::prelude::*;

/// Dot product of two equal-length f32 slices. With L2-normalized inputs this is cosine sim.
///
/// AVX2 path when available (8 lanes/iter via FMA), scalar fallback otherwise. Lengths must match;
/// a debug assert guards the contract and the scalar tail handles any non-multiple-of-8 remainder.
pub fn dot(a: &[f32], b: &[f32]) -> f32 {
    debug_assert_eq!(a.len(), b.len(), "dot: length mismatch");
    #[cfg(target_arch = "x86_64")]
    {
        if is_x86_feature_detected!("avx2") {
            // SAFETY: guarded by runtime feature detection; inner fn only uses AVX2/FMA which are
            // both implied by the "avx2" probe on x86_64 (FMA shipped alongside AVX2 on Haswell+).
            return unsafe { dot_avx2(a, b) };
        }
    }
    dot_scalar(a, b)
}

#[inline]
fn dot_scalar(a: &[f32], b: &[f32]) -> f32 {
    let n = a.len().min(b.len());
    let mut acc = 0.0f32;
    for i in 0..n {
        acc += a[i] * b[i];
    }
    acc
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2", enable = "fma")]
unsafe fn dot_avx2(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::x86_64::*;
    let n = a.len().min(b.len());
    let mut acc = _mm256_setzero_ps();
    let mut i = 0usize;
    // Main loop: 8 floats per iteration, fused multiply-add into the accumulator.
    while i + 8 <= n {
        let va = _mm256_loadu_ps(a.as_ptr().add(i));
        let vb = _mm256_loadu_ps(b.as_ptr().add(i));
        acc = _mm256_fmadd_ps(va, vb, acc);
        i += 8;
    }
    // Horizontal sum of the 8 lanes: fold high 128 into low 128, then reduce 4 -> 2 -> 1.
    let lo = _mm256_castps256_ps128(acc);
    let hi = _mm256_extractf128_ps(acc, 1);
    let sum128 = _mm_add_ps(lo, hi);
    let shuf = _mm_movehdup_ps(sum128); // [a1,a1,a3,a3]
    let sums = _mm_add_ps(sum128, shuf); // [a0+a1, _, a2+a3, _]
    let shuf2 = _mm_movehl_ps(shuf, sums); // bring a2+a3 down to lane 0
    let sums = _mm_add_ss(sums, shuf2);
    let mut total = _mm_cvtss_f32(sums);
    // Scalar tail for the trailing < 8 elements.
    while i < n {
        total += *a.get_unchecked(i) * *b.get_unchecked(i);
        i += 1;
    }
    total
}

/// Brute-force vector search: dot(query, row) for every row of a row-major `dim`-wide matrix,
/// returning the top-`k` `(row_idx, score)` pairs in descending score order.
///
/// This is the marquee AVX2 flex — the entire semantic-retrieval hot path. No ANN index, no
/// quantization: just `dot` over the whole matrix, parallelized across row chunks with rayon, then
/// a partial sort to the best `k`. At our corpus size the dense scan is fast enough that the
/// vectorized dot is what matters; the chunking keeps per-task overhead off the critical path.
pub fn top_k(query: &[f32], matrix: &[f32], dim: usize, k: usize) -> Vec<(usize, f32)> {
    if dim == 0 || query.len() != dim || matrix.is_empty() || k == 0 {
        return Vec::new();
    }
    let rows = matrix.len() / dim;
    if rows == 0 {
        return Vec::new();
    }

    // Score every row in parallel. Chunk the row range so each rayon task does a contiguous,
    // cache-friendly sweep rather than one task per row.
    const CHUNK: usize = 1024;
    let mut scored: Vec<(usize, f32)> = (0..rows)
        .into_par_iter()
        .with_min_len(CHUNK)
        .map(|r| {
            let start = r * dim;
            let row = &matrix[start..start + dim];
            (r, dot(query, row))
        })
        .collect();

    // Partial sort: pull the top-k to the front, then order just those descending.
    let k = k.min(scored.len());
    scored.select_nth_unstable_by(k - 1, |a, b| b.1.total_cmp(&a.1));
    scored.truncate(k);
    scored.sort_unstable_by(|a, b| b.1.total_cmp(&a.1));
    scored
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Deterministic pseudo-vector: values derived from the index alone (no rng, no clock) so the
    /// AVX2-vs-scalar comparison is reproducible. Mixes a couple of irrational-ish ramps to avoid
    /// trivially-equal lanes.
    fn vec_at(seed: usize, len: usize) -> Vec<f32> {
        (0..len)
            .map(|i| {
                let x = (i as f32 + 1.0) * 0.137 + seed as f32 * 0.911;
                (x.sin() * 1.3) + (x * 0.25).cos() - 0.5
            })
            .collect()
    }

    fn l2_normalize(v: &mut [f32]) {
        let norm = v.iter().map(|x| x * x).sum::<f32>().sqrt();
        if norm > 0.0 {
            for x in v.iter_mut() {
                *x /= norm;
            }
        }
    }

    #[test]
    fn avx2_matches_scalar() {
        // Vary lengths to exercise the 8-wide main loop *and* the scalar tail (29, 31, 33 ...).
        for &len in &[0usize, 1, 7, 8, 9, 16, 29, 31, 33, 64, 100, 256, 257] {
            let a = vec_at(1, len);
            let b = vec_at(2, len);
            let scalar = dot_scalar(&a, &b);
            let dispatched = dot(&a, &b);
            let diff = (scalar - dispatched).abs();
            assert!(
                diff < 1e-4,
                "len={len}: dispatched={dispatched} scalar={scalar} diff={diff}"
            );
        }
    }

    #[test]
    fn top_k_returns_nearest_first() {
        let dim = 256;
        let rows = 200;
        // Build a matrix of normalized rows; the query is row 137's vector, also normalized.
        let mut matrix = Vec::with_capacity(rows * dim);
        for r in 0..rows {
            let mut row = vec_at(r, dim);
            l2_normalize(&mut row);
            matrix.extend_from_slice(&row);
        }
        let target = 137usize;
        let mut query = vec_at(target, dim);
        l2_normalize(&mut query);

        let hits = top_k(&query, &matrix, dim, 5);
        assert_eq!(hits.len(), 5);
        // The identical row must be the nearest neighbour (self-cosine ~= 1.0).
        assert_eq!(hits[0].0, target, "nearest row should be the query's own row");
        assert!(
            (hits[0].1 - 1.0).abs() < 1e-3,
            "self-cosine should be ~1.0, got {}",
            hits[0].1
        );
        // Scores must be in non-increasing order.
        for w in hits.windows(2) {
            assert!(w[0].1 >= w[1].1, "top_k not sorted descending: {hits:?}");
        }
    }

    #[test]
    fn top_k_degenerate_inputs() {
        assert!(top_k(&[1.0, 0.0], &[], 2, 5).is_empty());
        assert!(top_k(&[1.0, 0.0], &[1.0, 0.0], 2, 0).is_empty());
        // query/dim mismatch -> empty, no panic.
        assert!(top_k(&[1.0], &[1.0, 0.0], 2, 5).is_empty());
    }

    /// In-process latency of the brute-force kernel over a few corpus sizes. Ignored by default
    /// (it's a benchmark, not a correctness gate). Run with:
    ///   cargo test -p tilt-core --release -- --ignored --nocapture bench_top_k_latency
    #[test]
    #[ignore]
    fn bench_top_k_latency() {
        use std::time::Instant;
        let dim = 256;
        for &rows in &[200usize, 50_000, 200_000] {
            let mut matrix = Vec::with_capacity(rows * dim);
            for r in 0..rows {
                let mut row = vec_at(r, dim);
                l2_normalize(&mut row);
                matrix.extend_from_slice(&row);
            }
            let mut query = vec_at(rows / 2, dim);
            l2_normalize(&mut query);

            // Warm up (rayon thread pool spin-up, page-in) so we time steady state.
            let _ = top_k(&query, &matrix, dim, 5);

            let iters = 50;
            let t0 = Instant::now();
            let mut sink = 0usize;
            for _ in 0..iters {
                let hits = top_k(&query, &matrix, dim, 5);
                sink ^= hits.len();
            }
            let per = t0.elapsed().as_secs_f64() * 1000.0 / iters as f64;
            // Sanity: planted row must still win, so the bench isn't measuring a no-op.
            let hits = top_k(&query, &matrix, dim, 5);
            assert_eq!(hits[0].0, rows / 2);
            println!(
                "bench top_k: rows={rows:>7} dim={dim} k=5 -> {per:.4} ms/search  (sink={sink})"
            );
        }
    }
}
