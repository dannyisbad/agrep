//! F16 local/global attention with register tiles and no score/mask matrix.
use candle_core::{Device, Result, Tensor};
use std::sync::Arc;

pub(crate) struct Lengths {
    count: usize,
    #[cfg(target_os = "macos")]
    buffer: Option<Arc<candle_metal_kernels::metal::Buffer>>,
}
impl Lengths {
    pub(crate) fn new(device: &Device, values: &[usize]) -> Result<Self> {
        #[cfg(target_os = "macos")]
        if let Device::Metal(device) = device {
            let values: Vec<u32> = values.iter().map(|v| *v as u32).collect();
            return Ok(Self {
                count: values.len(),
                buffer: Some(device.new_buffer_with_data(&values)?),
            });
        }
        let _ = (device, values);
        Ok(Self {
            count: values.len(),
            #[cfg(target_os = "macos")]
            buffer: None,
        })
    }
}
pub(crate) struct Attention {
    #[cfg(target_os = "macos")]
    pipeline: candle_metal_kernels::metal::ComputePipeline,
}
impl Attention {
    pub(crate) fn new(device: &Device) -> Result<Option<Arc<Self>>> {
        #[cfg(target_os = "macos")]
        if let Device::Metal(device) = device {
            let library = device
                .device()
                .new_library_with_source(include_str!("semantic_attention.metal"), None)
                .map_err(|e| candle_core::Error::Msg(e.to_string()))?;
            let function = library
                .get_function("semantic_attention", None)
                .map_err(|e| candle_core::Error::Msg(e.to_string()))?;
            let pipeline = device
                .device()
                .new_compute_pipeline_state_with_function(&function)
                .map_err(|e| candle_core::Error::Msg(e.to_string()))?;
            return Ok(Some(Arc::new(Self { pipeline })));
        }
        let _ = device;
        Ok(None)
    }
    pub(crate) fn forward(
        &self,
        q: &Tensor,
        k: &Tensor,
        v: &Tensor,
        lengths: &Lengths,
        radius: Option<usize>,
        scale: f32,
    ) -> Result<Tensor> {
        q.apply_op3_no_bwd(
            k,
            v,
            &Op {
                attention: self,
                lengths,
                radius,
                scale,
            },
        )
    }
}
struct Op<'a> {
    attention: &'a Attention,
    lengths: &'a Lengths,
    radius: Option<usize>,
    scale: f32,
}
impl candle_core::CustomOp3 for Op<'_> {
    fn name(&self) -> &'static str {
        "semantic-metal-attention"
    }
    fn cpu_fwd(
        &self,
        _: &candle_core::CpuStorage,
        _: &candle_core::Layout,
        _: &candle_core::CpuStorage,
        _: &candle_core::Layout,
        _: &candle_core::CpuStorage,
        _: &candle_core::Layout,
    ) -> Result<(candle_core::CpuStorage, candle_core::Shape)> {
        let _ = (self.attention, self.lengths.count, self.radius, self.scale);
        candle_core::bail!("Metal attention requires Metal tensors")
    }
    #[cfg(target_os = "macos")]
    fn metal_fwd(
        &self,
        q: &candle_core::MetalStorage,
        ql: &candle_core::Layout,
        k: &candle_core::MetalStorage,
        kl: &candle_core::Layout,
        v: &candle_core::MetalStorage,
        vl: &candle_core::Layout,
    ) -> Result<(candle_core::MetalStorage, candle_core::Shape)> {
        use candle_core::{backend::BackendStorage, DType, MetalStorage, Shape};
        use objc2_metal::MTLSize;
        if q.dtype() != DType::F16
            || k.dtype() != DType::F16
            || v.dtype() != DType::F16
            || ql.dims() != kl.dims()
            || ql.dims() != vl.dims()
            || ql.dims().len() != 4
            || !matches!(ql.dims()[3], 32 | 64)
            || [ql, kl, vl].iter().any(|l| l.stride()[3] != 1)
        {
            candle_core::bail!("Metal attention requires matching F16 head-width-32/64 tensors");
        }
        let [b, h, s, d]: [usize; 4] = ql.dims().try_into().unwrap();
        let count = b * h * s * d;
        if self.lengths.count != b {
            candle_core::bail!("Metal attention length mismatch");
        }
        let device = q.device();
        let output = device.new_buffer(count, DType::F16, "semantic-attention")?;
        let lengths = self
            .lengths
            .buffer
            .as_ref()
            .ok_or_else(|| candle_core::Error::Msg("missing Metal lengths".into()))?;
        let encoder = device.command_encoder()?;
        encoder.set_compute_pipeline_state(&self.attention.pipeline);
        for (i, storage, layout) in [(0, q, ql), (1, k, kl), (2, v, vl)] {
            encoder.set_buffer(i, Some(storage.buffer()), layout.start_offset() * 2);
        }
        encoder.set_buffer(3, Some(lengths), 0);
        let qs: [usize; 4] = ql.stride().try_into().unwrap();
        let ks: [usize; 4] = kl.stride().try_into().unwrap();
        let vs: [usize; 4] = vl.stride().try_into().unwrap();
        encoder.set_bytes(4, &qs);
        encoder.set_bytes(5, &ks);
        encoder.set_bytes(6, &vs);
        encoder.set_bytes(7, &[b as u32, h as u32, s as u32, d as u32]);
        encoder.set_bytes(8, &self.scale);
        encoder.set_bytes(9, &self.radius.map_or(-1, |v| v as i32));
        encoder.set_buffer(10, Some(&output), 0);
        encoder.dispatch_threads(
            MTLSize {
                width: b * h * s * 32,
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
            MetalStorage::new(output, device.clone(), count, DType::F16),
            Shape::from((b, s, h * d)),
        ))
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use candle_core::{DType, IndexOp};
    #[test]
    fn simdgroup_attention_matches_masked_f16_attention_with_strided_values() {
        let device = Device::new_metal(0).unwrap();
        let attention = Attention::new(&device).unwrap().unwrap();
        for (seq, width) in [(7, 32), (33, 32), (200, 32), (1024, 32), (33, 64)] {
            let packed = Tensor::from_vec(
                (0..2 * seq * 3 * 3 * width)
                    .map(|i| ((i * 17 % 113) as f32 - 56.) / 70.)
                    .collect::<Vec<_>>(),
                (2, seq, 3, 3, width),
                &device,
            )
            .unwrap()
            .to_dtype(DType::F16)
            .unwrap()
            .permute((2, 0, 3, 1, 4))
            .unwrap();
            let q = packed.i(0).unwrap().contiguous().unwrap();
            let k = packed.i(1).unwrap().contiguous().unwrap();
            let v = packed.i(2).unwrap();
            let sizes = [seq, seq.div_ceil(2)];
            let lengths = Lengths::new(&device, &sizes).unwrap();
            for radius in [None, Some(8), Some(64)] {
                let mask = (0..2)
                    .flat_map(|b| {
                        (0..seq).flat_map(move |i| {
                            (0..seq).map(move |j| {
                                if j >= sizes[b] || radius.is_some_and(|r| i.abs_diff(j) > r) {
                                    -10000f32
                                } else {
                                    0.
                                }
                            })
                        })
                    })
                    .collect::<Vec<_>>();
                let mask = Tensor::from_vec(mask, (2, 1, seq, seq), &device)
                    .unwrap()
                    .to_dtype(DType::F16)
                    .unwrap();
                let expected = if seq > 8 {
                    let mask = mask.broadcast_as((2, 3, seq, seq)).unwrap();
                    // Candle 0.9.2's wrapper passes a view's element offset to
                    // the byte-offset Metal API. Materialize only the reference;
                    // the kernel under test still receives the strided V view.
                    candle_nn::ops::sdpa(
                        &q,
                        &k,
                        &v.contiguous().unwrap(),
                        Some(&mask),
                        false,
                        0.125,
                        1.,
                    )
                    .unwrap()
                } else {
                    let scores = (&q * 0.125)
                        .unwrap()
                        .matmul(&k.transpose(2, 3).unwrap())
                        .unwrap()
                        .broadcast_add(&mask)
                        .unwrap();
                    candle_nn::ops::softmax(&scores, candle_core::D::Minus1)
                        .unwrap()
                        .matmul(&v.contiguous().unwrap())
                        .unwrap()
                };
                let expected = expected
                    .transpose(1, 2)
                    .unwrap()
                    .flatten_all()
                    .unwrap()
                    .to_dtype(DType::F32)
                    .unwrap()
                    .to_vec1::<f32>()
                    .unwrap();
                let actual = attention
                    .forward(&q, &k, &v, &lengths, radius, 0.125)
                    .unwrap()
                    .flatten_all()
                    .unwrap()
                    .to_dtype(DType::F32)
                    .unwrap()
                    .to_vec1::<f32>()
                    .unwrap();
                for (b, &length) in sizes.iter().enumerate() {
                    for pos in 0..length {
                        for h in 0..3 {
                            for d in 0..width {
                                let i = ((b * seq + pos) * 3 + h) * width + d;
                                assert!(
                                    (actual[i] - expected[i]).abs() < 0.003,
                                    "{seq} {radius:?} {i}: {} != {}",
                                    actual[i],
                                    expected[i]
                                );
                            }
                        }
                    }
                }
            }
        }
    }
}
