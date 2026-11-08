//! CPU paths: row-parallel projections, per-head attention and the fused GeGLU.
use super::mapped::Projection;
use candle_core::{DType, IndexOp, Result, Tensor, D};

// BLAS is deliberately single-threaded in background workers. Share large
// projections across that same bounded Rayon pool, preserving each dot product
// and the input order. Small query matrices avoid scheduling/copy overhead.
pub(super) fn projection(xs: &Tensor, linear: &Projection) -> Result<Tensor> {
    use rayon::prelude::*;
    if !xs.device().is_cpu() || xs.dtype() != DType::F32 || !xs.is_contiguous() {
        return linear.forward(xs);
    }
    let width = xs.dim(D::Minus1)?;
    let rows = xs.elem_count() / width;
    let workers = rayon::current_num_threads();
    if rows < 256 || workers == 1 {
        return linear.forward(xs);
    }
    let flat = xs.reshape((rows, width))?;
    let chunk = rows.div_ceil(workers).max(128);
    let parts = (0..rows.div_ceil(chunk))
        .into_par_iter()
        .map(|i| linear.forward(&flat.narrow(0, i * chunk, chunk.min(rows - i * chunk))?))
        .collect::<Result<Vec<_>>>()?;
    let mut shape = xs.dims().to_vec();
    *shape.last_mut().unwrap() = linear.outputs()?;
    Tensor::cat(&parts, 0)?.reshape(shape)
}

// Accelerate's batched-matmul wrapper loops serially over small head matrices.
// Run independent heads in parallel; each head keeps exactly the same operations.
pub(super) fn cpu_attention(q: &Tensor, k: &Tensor, v: &Tensor, mask: &Tensor) -> Result<Tensor> {
    use rayon::prelude::*;
    let (batch, heads, seq, width) = q.dims4()?;
    let values: Vec<Tensor> = (0..batch * heads)
        .into_par_iter()
        .map(|i| {
            let b = i / heads;
            let h = i % heads;
            let q = q.i((b, h))?;
            let k = k.i((b, h))?;
            let v = v.i((b, h))?.contiguous()?;
            let mask = mask.i((b, 0))?;
            let scores = q.matmul(&k.t()?)?.broadcast_add(&mask)?;
            candle_nn::ops::softmax_last_dim(&scores.contiguous()?)?.matmul(&v)
        })
        .collect::<Result<_>>()?;
    Tensor::stack(&values, 0)?.reshape((batch, heads, seq, width))
}

// Fusing the two elementwise passes also exposes independent rows to Rayon.
// The scalar expression deliberately matches Candle's GeluErf operation exactly.
#[inline]
pub(super) fn cpu_erf(x: f32) -> f32 {
    #[cfg(target_os = "macos")]
    {
        // Darwin's system math library supplies its optimized IEEE erf. No
        // additional library or compiler is needed by the delivered binary.
        unsafe extern "C" {
            fn erff(x: f32) -> f32;
        }
        unsafe { erff(x) }
    }
    #[cfg(not(target_os = "macos"))]
    candle_core::cpu::erf::erf_f32(x)
}
#[cfg(target_os = "macos")]
pub(super) fn vector_geglu(dest: &mut [f32], src: &[f32]) {
    // A&S 7.1.26 with vectorized exp. The unchanged full-model oracle decides
    // whether the polynomial's roundoff is acceptable; it is never assumed.
    unsafe extern "C" {
        fn vvexpf(y: *mut f32, x: *const f32, n: *const i32);
    }
    for (d, x) in dest.iter_mut().zip(src) {
        let x = *x * std::f32::consts::FRAC_1_SQRT_2;
        *d = -x * x;
    }
    let n = dest.len() as i32;
    unsafe {
        vvexpf(dest.as_mut_ptr(), dest.as_ptr(), &n);
    }
    let width = dest.len();
    for (i, e) in dest.iter_mut().enumerate() {
        let x = src[i] * std::f32::consts::FRAC_1_SQRT_2;
        // Avoid cancellation in 1 + erf for the small negative tail. The
        // pointwise gate stays unchanged; common activations use vector exp.
        if src[i] < -2. {
            *e = (cpu_erf(x) + 1.) * 0.5 * src[i] * src[width + i];
            continue;
        }
        let t = 1. / (1. + 0.3275911 * x.abs());
        let p = (((((1.0614054 * t - 1.4531521) * t) + 1.4214138) * t - 0.28449672) * t
            + 0.2548296)
            * t;
        let erf = (1. - p * *e).copysign(x);
        *e = (erf + 1.) * 0.5 * src[i] * src[width + i];
    }
}
pub(super) struct CpuGeglu {
    pub vector: bool,
}
impl candle_core::CustomOp1 for CpuGeglu {
    fn name(&self) -> &'static str {
        "semantic-geglu"
    }
    fn cpu_fwd(
        &self,
        storage: &candle_core::CpuStorage,
        layout: &candle_core::Layout,
    ) -> Result<(candle_core::CpuStorage, candle_core::Shape)> {
        use rayon::prelude::*;
        let candle_core::CpuStorage::F32(values) = storage else {
            candle_core::bail!("GeGLU requires F32");
        };
        let mut dims = layout.dims().to_vec();
        let last = dims
            .last_mut()
            .ok_or_else(|| candle_core::Error::Msg("GeGLU requires a row dimension".into()))?;
        if *last == 0 || !last.is_multiple_of(2) {
            candle_core::bail!("invalid GeGLU row width");
        }
        *last /= 2;
        let width = *last;
        let (start, end) = layout
            .contiguous_offsets()
            .ok_or_else(|| candle_core::Error::Msg("GeGLU requires contiguous input".into()))?;
        let input = &values[start..end];
        let mut output = vec![0f32; input.len() / 2];
        let _ = self.vector;
        let row = |dest: &mut [f32], src: &[f32]| {
            #[cfg(target_os = "macos")]
            if self.vector {
                vector_geglu(dest, src);
                return;
            }
            for (i, out) in dest.iter_mut().enumerate() {
                let x = src[i];
                let gelu = (cpu_erf(x * std::f32::consts::FRAC_1_SQRT_2) + 1.) * 0.5 * x;
                *out = gelu * src[width + i];
            }
        };
        if output.len() / width >= 64 {
            output
                .par_chunks_mut(width)
                .zip(input.par_chunks(width * 2))
                .for_each(|(d, s)| row(d, s));
        } else {
            output
                .chunks_mut(width)
                .zip(input.chunks(width * 2))
                .for_each(|(d, s)| row(d, s));
        }
        Ok((
            candle_core::CpuStorage::F32(output),
            candle_core::Shape::from(dims),
        ))
    }
}
