//! Tiled inference-only attention. Scores live in a small stack tile, never S².
use candle_core::{CpuStorage, CustomOp3, Layout, Result, Shape, Tensor};

pub(crate) fn kind() -> u8 {
    std::env::var("AGREP_SEM_FUSED_ATTN")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(if cfg!(target_os = "macos") { 3 } else { 0 })
}

struct Attention<'a> {
    lengths: &'a [usize],
    radius: Option<usize>,
    scale: f32,
    vector_exp: bool,
    blas_tiles: bool,
}

struct Rows<'a> {
    values: &'a [f32],
    strides: &'a [usize],
    start: usize,
    width: usize,
}
impl<'a> Rows<'a> {
    fn new(storage: &'a CpuStorage, layout: &'a Layout) -> Result<Self> {
        let CpuStorage::F32(values) = storage else {
            candle_core::bail!("fused CPU attention requires F32");
        };
        if layout.dims().len() != 4 || layout.stride()[3] != 1 {
            candle_core::bail!("attention requires contiguous head rows");
        }
        Ok(Self {
            values,
            strides: layout.stride(),
            start: layout.start_offset(),
            width: layout.dims()[3],
        })
    }
    fn row(&self, batch: usize, head: usize, token: usize) -> &[f32] {
        let offset =
            self.start + batch * self.strides[0] + head * self.strides[1] + token * self.strides[2];
        &self.values[offset..offset + self.width]
    }
}

// Accelerate can use the matrix unit for a bounded score tile. Its rows share
// the union of their local windows; masked entries are normalized to zero.
// Scratch is at most 32*S floats per head job, not a full S*S score matrix.
#[cfg(target_os = "macos")]
fn blas_tiles(
    q: &Rows<'_>,
    k: &Rows<'_>,
    v: &Rows<'_>,
    dims: [usize; 4],
    lengths: &[usize],
    radius: Option<usize>,
    scale: f32,
) -> Result<Vec<f32>> {
    use rayon::prelude::*;
    unsafe extern "C" {
        fn cblas_sgemm(
            order: i32,
            ta: i32,
            tb: i32,
            m: i32,
            n: i32,
            k: i32,
            alpha: f32,
            a: *const f32,
            lda: i32,
            b: *const f32,
            ldb: i32,
            beta: f32,
            c: *mut f32,
            ldc: i32,
        );
        fn vvexpf(y: *mut f32, x: *const f32, n: *const i32);
    }
    let [batch, heads, seq, width] = dims;
    let head = |job: usize| {
        let b = job / heads;
        let h = job % heads;
        let n = lengths[b];
        let mut queries = vec![0.; n * width];
        let mut values = queries.clone();
        let mut keys = queries.clone();
        for i in 0..n {
            for d in 0..width {
                queries[i * width + d] = q.row(b, h, i)[d] * scale;
            }
            keys[i * width..(i + 1) * width].copy_from_slice(k.row(b, h, i));
            values[i * width..(i + 1) * width].copy_from_slice(v.row(b, h, i));
        }
        let mut output = vec![0.; seq * width];
        let mut scores = vec![0.; 32 * n];
        for start in (0..n).step_by(32) {
            let rows = (n - start).min(32);
            let low = radius.map_or(0, |r| start.saturating_sub(r));
            let high = radius.map_or(n, |r| (start + rows + r).min(n));
            let columns = high - low;
            unsafe {
                cblas_sgemm(
                    101,
                    111,
                    112,
                    rows as i32,
                    columns as i32,
                    width as i32,
                    1.,
                    queries.as_ptr().add(start * width),
                    width as i32,
                    keys.as_ptr().add(low * width),
                    width as i32,
                    0.,
                    scores.as_mut_ptr(),
                    columns as i32,
                );
            }
            for (i, row) in scores[..rows * columns].chunks_mut(columns).enumerate() {
                let mut maximum = f32::NEG_INFINITY;
                for (j, score) in row.iter_mut().enumerate() {
                    if radius.is_some_and(|r| (start + i).abs_diff(low + j) > r) {
                        *score = f32::NEG_INFINITY;
                    }
                    maximum = maximum.max(*score);
                }
                for score in row.iter_mut() {
                    *score -= maximum;
                }
                unsafe {
                    vvexpf(row.as_mut_ptr(), row.as_ptr(), &(columns as i32));
                }
                let sum: f32 = row.iter().sum();
                for score in row.iter_mut() {
                    *score /= sum;
                }
            }
            unsafe {
                cblas_sgemm(
                    101,
                    111,
                    111,
                    rows as i32,
                    width as i32,
                    columns as i32,
                    1.,
                    scores.as_ptr(),
                    columns as i32,
                    values.as_ptr().add(low * width),
                    width as i32,
                    0.,
                    output.as_mut_ptr().add(start * width),
                    width as i32,
                );
            }
        }
        output
    };
    let parts: Vec<_> = if batch * heads * seq < 256 {
        (0..batch * heads).map(head).collect()
    } else {
        (0..batch * heads).into_par_iter().map(head).collect()
    };
    let mut output = vec![0.; batch * heads * seq * width];
    for (job, part) in parts.iter().enumerate() {
        let b = job / heads;
        let h = job % heads;
        for pos in 0..seq {
            output
                [((b * seq + pos) * heads + h) * width..((b * seq + pos) * heads + h + 1) * width]
                .copy_from_slice(&part[pos * width..(pos + 1) * width]);
        }
    }
    Ok(output)
}

#[inline]
fn dot(a: &[f32], b: &[f32]) -> f32 {
    #[cfg(target_arch = "aarch64")]
    unsafe {
        dot_neon(a, b)
    }
    #[cfg(not(target_arch = "aarch64"))]
    a.iter().zip(b).map(|(x, y)| x * y).sum()
}
#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn dot_neon(a: &[f32], b: &[f32]) -> f32 {
    use std::arch::aarch64::*;
    let mut sums = [vdupq_n_f32(0.); 4];
    let mut i = 0;
    while i + 16 <= a.len() {
        for (n, sum) in sums.iter_mut().enumerate() {
            *sum = vfmaq_f32(
                *sum,
                vld1q_f32(a.as_ptr().add(i + n * 4)),
                vld1q_f32(b.as_ptr().add(i + n * 4)),
            );
        }
        i += 16;
    }
    let mut sum = vaddvq_f32(vaddq_f32(
        vaddq_f32(sums[0], sums[1]),
        vaddq_f32(sums[2], sums[3]),
    ));
    while i < a.len() {
        sum += a[i] * b[i];
        i += 1;
    }
    sum
}
#[inline]
fn add_weighted(out: &mut [f32], values: &[f32], weight: f32) {
    #[cfg(target_arch = "aarch64")]
    unsafe {
        add_weighted_neon(out, values, weight);
    }
    #[cfg(not(target_arch = "aarch64"))]
    for (o, v) in out.iter_mut().zip(values) {
        *o += weight * v;
    }
}
#[cfg(target_arch = "aarch64")]
#[target_feature(enable = "neon")]
unsafe fn add_weighted_neon(out: &mut [f32], values: &[f32], weight: f32) {
    use std::arch::aarch64::*;
    let weight = vdupq_n_f32(weight);
    let mut i = 0;
    while i + 4 <= out.len() {
        vst1q_f32(
            out.as_mut_ptr().add(i),
            vfmaq_f32(
                vld1q_f32(out.as_ptr().add(i)),
                vld1q_f32(values.as_ptr().add(i)),
                weight,
            ),
        );
        i += 4;
    }
    let weight = vgetq_lane_f32::<0>(weight);
    while i < out.len() {
        out[i] += values[i] * weight;
        i += 1;
    }
}

impl CustomOp3 for Attention<'_> {
    fn name(&self) -> &'static str {
        "semantic-tiled-attention"
    }
    fn cpu_fwd(
        &self,
        qs: &CpuStorage,
        ql: &Layout,
        ks: &CpuStorage,
        kl: &Layout,
        vs: &CpuStorage,
        vl: &Layout,
    ) -> Result<(CpuStorage, Shape)> {
        use rayon::prelude::*;
        let q = Rows::new(qs, ql)?;
        let k = Rows::new(ks, kl)?;
        let v = Rows::new(vs, vl)?;
        if ql.dims() != kl.dims() || ql.dims() != vl.dims() || self.lengths.len() != ql.dims()[0] {
            candle_core::bail!("attention shape mismatch");
        }
        let [batch, heads, seq, width]: [usize; 4] = ql.dims().try_into().unwrap();
        if width == 0 || width > 256 || seq == 0 || seq > 1024 {
            candle_core::bail!("empty attention dimension");
        }
        if self.lengths.iter().any(|n| *n > seq) {
            candle_core::bail!("attention length exceeds sequence");
        }
        #[cfg(target_os = "macos")]
        if self.blas_tiles {
            let output = blas_tiles(
                &q,
                &k,
                &v,
                [batch, heads, seq, width],
                self.lengths,
                self.radius,
                self.scale,
            )?;
            return Ok((
                CpuStorage::F32(output),
                Shape::from((batch, seq, heads * width)),
            ));
        }
        let _ = self.blas_tiles;
        let mut output = vec![0.; batch * heads * seq * width];
        let fill = |index: usize, out: &mut [f32]| {
            let pos = index / heads % seq;
            let head = index % heads;
            let b = index / (seq * heads);
            if pos >= self.lengths[b] {
                return;
            }
            let low = self.radius.map_or(0, |radius| pos.saturating_sub(radius));
            let high = self.radius.map_or(self.lengths[b], |radius| {
                (pos + radius + 1).min(self.lengths[b])
            });
            let mut scaled = [0f32; 256];
            for (out, value) in scaled[..width].iter_mut().zip(q.row(b, head, pos)) {
                *out = *value * self.scale;
            }
            let query = &scaled[..width];
            if self.vector_exp {
                // One bounded row of scores, rather than an S x S matrix.
                // Larger exponential vectors amortize the math-library call.
                let mut scores = [0f32; 1024];
                let scores = &mut scores[..high - low];
                let mut maximum = f32::NEG_INFINITY;
                for (j, score) in scores.iter_mut().enumerate() {
                    *score = dot(query, k.row(b, head, low + j));
                    maximum = maximum.max(*score);
                }
                for score in scores.iter_mut() {
                    *score -= maximum;
                }
                #[cfg(target_os = "macos")]
                unsafe {
                    unsafe extern "C" {
                        fn vvexpf(y: *mut f32, x: *const f32, n: *const i32);
                    }
                    let n = scores.len() as i32;
                    vvexpf(scores.as_mut_ptr(), scores.as_ptr(), &n);
                }
                #[cfg(not(target_os = "macos"))]
                for score in scores.iter_mut() {
                    *score = score.exp();
                }
                let denominator: f32 = scores.iter().sum();
                for (j, score) in scores.iter().enumerate() {
                    add_weighted(out, v.row(b, head, low + j), *score);
                }
                out.iter_mut().for_each(|x| *x /= denominator);
                return;
            }
            let mut maximum = f32::NEG_INFINITY;
            let mut denominator = 0.;
            for start in (low..high).step_by(32) {
                let end = (start + 32).min(high);
                let mut scores = [0.; 32];
                let mut next = maximum;
                for (j, score) in scores[..end - start].iter_mut().enumerate() {
                    *score = dot(query, k.row(b, head, start + j));
                    next = next.max(*score);
                }
                let correction = (maximum - next).exp();
                denominator *= correction;
                out.iter_mut().for_each(|x| *x *= correction);
                for (j, score) in scores[..end - start].iter().enumerate() {
                    let weight = (*score - next).exp();
                    denominator += weight;
                    add_weighted(out, v.row(b, head, start + j), weight);
                }
                maximum = next;
            }
            out.iter_mut().for_each(|x| *x /= denominator);
        };
        if batch * heads * seq < 256 {
            for (index, row) in output.chunks_mut(width).enumerate() {
                fill(index, row);
            }
        } else {
            output
                .par_chunks_mut(width * 8)
                .enumerate()
                .for_each(|(job, chunk)| {
                    for (r, out) in chunk.chunks_mut(width).enumerate() {
                        fill(job * 8 + r, out);
                    }
                });
        }
        Ok((
            CpuStorage::F32(output),
            Shape::from((batch, seq, heads * width)),
        ))
    }
}

pub(crate) fn cpu(
    q: &Tensor,
    k: &Tensor,
    v: &Tensor,
    lengths: &[usize],
    radius: Option<usize>,
    scale: f32,
) -> Result<Tensor> {
    q.apply_op3_no_bwd(
        k,
        v,
        &Attention {
            lengths,
            radius,
            scale,
            vector_exp: matches!(kind(), 2 | 3),
            blas_tiles: kind() == 3,
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use candle_core::{Device, D};
    #[test]
    fn tiled_attention_matches_masked_global_and_local_attention() {
        let device = Device::Cpu;
        for seq in [1, 7, 33, 129, 1024] {
            let count = 2 * 3 * seq * 64;
            let values: Vec<_> = (0..count)
                .map(|i| ((i * 17 % 113) as f32 - 56.) / 100.)
                .collect();
            let q = Tensor::from_vec(values, (2, 3, seq, 64), &device).unwrap();
            let k = (&q * 0.4).unwrap();
            let v = (&q * 0.7).unwrap();
            let lengths = [seq, seq.div_ceil(2)];
            for radius in [None, Some(8)] {
                let mask: Vec<_> = (0..2)
                    .flat_map(|b| {
                        (0..seq).flat_map(move |i| {
                            (0..seq).map(move |j| {
                                if j >= lengths[b] || radius.is_some_and(|r| i.abs_diff(j) > r) {
                                    f32::NEG_INFINITY
                                } else {
                                    0.
                                }
                            })
                        })
                    })
                    .collect();
                let mask = Tensor::from_vec(mask, (2, 1, seq, seq), &device).unwrap();
                let scores = q
                    .matmul(&k.transpose(D::Minus2, D::Minus1).unwrap())
                    .unwrap()
                    .broadcast_add(&mask)
                    .unwrap();
                let expected = candle_nn::ops::softmax_last_dim(&scores)
                    .unwrap()
                    .matmul(&v)
                    .unwrap()
                    .flatten_all()
                    .unwrap()
                    .to_vec1::<f32>()
                    .unwrap();
                for (vector_exp, blas_tiles) in [(false, false), (true, false), (true, true)] {
                    let actual = q
                        .apply_op3_no_bwd(
                            &k,
                            &v,
                            &Attention {
                                lengths: &lengths,
                                radius,
                                scale: 1.,
                                vector_exp,
                                blas_tiles,
                            },
                        )
                        .unwrap()
                        .flatten_all()
                        .unwrap()
                        .to_vec1::<f32>()
                        .unwrap();
                    for b in 0..2 {
                        for h in 0..3 {
                            for i in 0..lengths[b] {
                                for d in 0..64 {
                                    assert!(
                                        (actual[((b * seq + i) * 3 + h) * 64 + d]
                                            - expected[((b * 3 + h) * seq + i) * 64 + d])
                                            .abs()
                                            < 2e-6
                                    );
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
