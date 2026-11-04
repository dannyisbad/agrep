//! Residual addition and normalization share one pass; both outputs stay live.
use candle_core::{CpuStorage, CustomOp3, Layout, Result, Shape, Tensor};

struct Op {
    eps: f32,
}
fn contiguous<'a>(s: &'a CpuStorage, l: &Layout) -> Result<&'a [f32]> {
    let CpuStorage::F32(s) = s else {
        candle_core::bail!("residual norm requires F32 CPU input")
    };
    let (a, b) = l
        .contiguous_offsets()
        .ok_or_else(|| candle_core::Error::Msg("residual norm requires contiguous input".into()))?;
    Ok(&s[a..b])
}
fn shape(l: &Layout) -> Shape {
    let mut dims = vec![2];
    dims.extend(l.dims());
    Shape::from(dims)
}
impl CustomOp3 for Op {
    fn name(&self) -> &'static str {
        "semantic-residual-norm"
    }
    fn cpu_fwd(
        &self,
        x: &CpuStorage,
        xl: &Layout,
        r: &CpuStorage,
        rl: &Layout,
        w: &CpuStorage,
        wl: &Layout,
    ) -> Result<(CpuStorage, Shape)> {
        use rayon::prelude::*;
        let x = contiguous(x, xl)?;
        let r = contiguous(r, rl)?;
        let w = contiguous(w, wl)?;
        if xl.dims() != rl.dims() || xl.dims().last() != Some(&w.len()) {
            candle_core::bail!("residual norm shape mismatch")
        }
        let width = w.len();
        let mut out = vec![0.; x.len() * 2];
        let (residual, normalized) = out.split_at_mut(x.len());
        let row = |(sum, norm): (&mut [f32], &mut [f32]), (a, b): (&[f32], &[f32])| {
            let mut total = 0.;
            let mut squares = 0.;
            for ((s, a), b) in sum.iter_mut().zip(a).zip(b) {
                *s = *a + *b;
                total += *s;
                squares += *s * *s;
            }
            let mean = total / width as f32;
            let inv = (squares / width as f32 - mean * mean + self.eps)
                .sqrt()
                .recip();
            for ((n, s), w) in norm.iter_mut().zip(sum).zip(w) {
                *n = (*s - mean) * inv * *w;
            }
        };
        if x.len() / width >= 64 {
            residual
                .par_chunks_mut(width)
                .zip(normalized.par_chunks_mut(width))
                .zip(x.par_chunks(width).zip(r.par_chunks(width)))
                .for_each(|(out, input)| row(out, input));
        } else {
            residual
                .chunks_mut(width)
                .zip(normalized.chunks_mut(width))
                .zip(x.chunks(width).zip(r.chunks(width)))
                .for_each(|(out, input)| row(out, input));
        }
        Ok((CpuStorage::F32(out), shape(xl)))
    }
}
pub(crate) fn forward(
    x: &Tensor,
    residual: &Tensor,
    weight: &Tensor,
    eps: f32,
) -> Result<(Tensor, Tensor)> {
    let pair = x.apply_op3_no_bwd(residual, weight, &Op { eps })?;
    Ok((pair.get(0)?, pair.get(1)?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use candle_core::Device;
    fn compare(
        device: Device,
        tolerance: f32,
        run: impl Fn(&Tensor, &Tensor, &Tensor) -> Result<(Tensor, Tensor)>,
    ) {
        let dtype = if device.is_cpu() {
            candle_core::DType::F32
        } else {
            candle_core::DType::F16
        };
        for rows in [3, 128] {
            let x = Tensor::from_vec(
                (0..rows * 384)
                    .map(|i| (i % 97) as f32 / 90. - 0.5)
                    .collect::<Vec<_>>(),
                (rows, 384),
                &device,
            )
            .unwrap()
            .to_dtype(dtype)
            .unwrap();
            let r = (&x * 0.3).unwrap();
            let w = Tensor::ones(384, dtype, &device).unwrap();
            let zero = w.zeros_like().unwrap();
            let expected = (&x + &r).unwrap();
            let normalized = candle_nn::ops::layer_norm(&expected, &w, &zero, 1e-5).unwrap();
            let (actual, norm) = run(&x, &r, &w).unwrap();
            for (a, b) in [(actual, expected), (norm, normalized)] {
                let values = |x: Tensor| {
                    x.flatten_all()
                        .unwrap()
                        .to_dtype(candle_core::DType::F32)
                        .unwrap()
                        .to_vec1::<f32>()
                        .unwrap()
                };
                for (a, b) in values(a).into_iter().zip(values(b)) {
                    assert!((a - b).abs() <= tolerance, "{a} != {b}")
                }
            }
        }
    }
    #[test]
    fn residual_norm_matches_cpu() {
        compare(Device::Cpu, 1e-6, |x, r, w| forward(x, r, w, 1e-5))
    }
    /// The arena's `residual_norm` shader, dispatched on Candle tensors.
    #[cfg(target_os = "macos")]
    struct Metal<'a> {
        pipeline: &'a candle_metal_kernels::metal::ComputePipeline,
        eps: f32,
    }
    #[cfg(target_os = "macos")]
    impl CustomOp3 for Metal<'_> {
        fn name(&self) -> &'static str {
            "semantic-residual-norm-metal"
        }
        fn cpu_fwd(
            &self,
            _: &CpuStorage,
            _: &Layout,
            _: &CpuStorage,
            _: &Layout,
            _: &CpuStorage,
            _: &Layout,
        ) -> Result<(CpuStorage, Shape)> {
            candle_core::bail!("the residual norm shader runs on Metal")
        }
        fn metal_fwd(
            &self,
            x: &candle_core::MetalStorage,
            xl: &Layout,
            r: &candle_core::MetalStorage,
            rl: &Layout,
            w: &candle_core::MetalStorage,
            wl: &Layout,
        ) -> Result<(candle_core::MetalStorage, Shape)> {
            use candle_core::{backend::BackendStorage, DType, MetalStorage};
            use objc2_metal::MTLSize;
            if [x, r, w].iter().any(|s| s.dtype() != DType::F16)
                || xl.dims() != rl.dims()
                || xl.dims().last() != wl.dims().last()
            {
                candle_core::bail!("residual norm requires matching F16 tensors")
            }
            let count = xl.shape().elem_count();
            let width = *xl.dims().last().unwrap();
            let device = x.device();
            let out = device.new_buffer(count * 2, DType::F16, "residual-norm")?;
            let encoder = device.command_encoder()?;
            encoder.set_compute_pipeline_state(self.pipeline);
            for (i, s, l) in [(0, x, xl), (1, r, rl), (2, w, wl)] {
                let (offset, _) = l.contiguous_offsets().ok_or_else(|| {
                    candle_core::Error::Msg("residual norm requires contiguous input".into())
                })?;
                encoder.set_buffer(i, Some(s.buffer()), offset * 2);
            }
            encoder.set_buffer(3, Some(&out), 0);
            encoder.set_bytes(4, &width);
            encoder.set_bytes(5, &count);
            encoder.set_bytes(6, &self.eps);
            encoder.dispatch_thread_groups(
                MTLSize {
                    width: count / width,
                    height: 1,
                    depth: 1,
                },
                MTLSize {
                    width: 128,
                    height: 1,
                    depth: 1,
                },
            );
            Ok((
                MetalStorage::new(out, device.clone(), count * 2, DType::F16),
                shape(xl),
            ))
        }
    }
    #[test]
    #[cfg(target_os = "macos")]
    fn residual_norm_matches_metal() {
        let Some(device) = crate::semantic_device::metal_device() else {
            return;
        };
        use objc2_metal::MTLDevice;
        let metal = device.as_metal_device().unwrap();
        eprintln!("Metal GPU: {}", metal.device().as_ref().name());
        let library = metal
            .device()
            .new_library_with_source(include_str!("semantic_norm.metal"), None)
            .unwrap();
        let function = library.get_function("residual_norm", None).unwrap();
        let pipeline = metal
            .device()
            .new_compute_pipeline_state_with_function(&function)
            .unwrap();
        compare(device, 0.004, |x, r, w| {
            let op = Metal {
                pipeline: &pipeline,
                eps: 1e-5,
            };
            let pair = x.apply_op3_no_bwd(r, w, &op)?;
            Ok((pair.get(0)?, pair.get(1)?))
        })
    }
}
