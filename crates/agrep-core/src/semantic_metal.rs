//! One-pass F16 GeGLU; erf is Candle 0.9.2's A&S 7.1.26 expression.
use candle_core::{CustomOp1, Device, Result};
use std::sync::Arc;

pub(crate) struct Geglu {
    #[cfg(target_os = "macos")]
    pipeline: candle_metal_kernels::metal::ComputePipeline,
}

impl Geglu {
    pub(crate) fn new(device: &Device) -> Result<Option<Arc<Self>>> {
        #[cfg(target_os = "macos")]
        if let Device::Metal(device) = device {
            let library = device
                .device()
                .new_library_with_source(include_str!("semantic_geglu.metal"), None)
                .map_err(|e| candle_core::Error::Msg(e.to_string()))?;
            let function = library
                .get_function("semantic_geglu", None)
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
}

impl CustomOp1 for Geglu {
    fn name(&self) -> &'static str {
        "semantic-metal-geglu"
    }
    fn cpu_fwd(
        &self,
        _: &candle_core::CpuStorage,
        _: &candle_core::Layout,
    ) -> Result<(candle_core::CpuStorage, candle_core::Shape)> {
        candle_core::bail!("Metal GeGLU requires a Metal tensor")
    }
    #[cfg(target_os = "macos")]
    fn metal_fwd(
        &self,
        storage: &candle_core::MetalStorage,
        layout: &candle_core::Layout,
    ) -> Result<(candle_core::MetalStorage, candle_core::Shape)> {
        use candle_core::{backend::BackendStorage, DType, MetalStorage, Shape};
        use objc2_metal::MTLSize;
        if storage.dtype() != DType::F16 {
            candle_core::bail!("Metal GeGLU requires F16");
        }
        let (start, end) = layout
            .contiguous_offsets()
            .ok_or_else(|| candle_core::Error::Msg("GeGLU requires contiguous input".into()))?;
        let mut dims = layout.dims().to_vec();
        let last = dims
            .last_mut()
            .ok_or_else(|| candle_core::Error::Msg("GeGLU requires a row".into()))?;
        if *last == 0 || !last.is_multiple_of(2) {
            candle_core::bail!("invalid GeGLU width");
        }
        *last /= 2;
        let width = *last;
        let count = (end - start) / 2;
        let device = storage.device();
        let output = device.new_buffer(count, DType::F16, "semantic-geglu")?;
        let encoder = device.command_encoder()?;
        encoder.set_compute_pipeline_state(&self.pipeline);
        encoder.set_bytes(0, &count);
        encoder.set_bytes(1, &width);
        encoder.set_buffer(2, Some(storage.buffer()), start * 2);
        encoder.set_buffer(3, Some(&output), 0);
        encoder.dispatch_threads(
            MTLSize {
                width: count,
                height: 1,
                depth: 1,
            },
            MTLSize {
                width: 256.min(self.pipeline.max_total_threads_per_threadgroup()),
                height: 1,
                depth: 1,
            },
        );
        Ok((
            MetalStorage::new(output, device.clone(), count, DType::F16),
            Shape::from(dims),
        ))
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    use candle_core::{DType, Tensor, D};
    #[test]
    fn fused_geglu_preserves_candle_f16_values() {
        let device = Device::new_metal(0).unwrap();
        let op = Geglu::new(&device).unwrap().unwrap();
        let input = Tensor::from_vec(
            (0..8192)
                .map(|i| (i % 161) as f32 / 16. - 5.)
                .collect::<Vec<_>>(),
            (16, 512),
            &device,
        )
        .unwrap()
        .to_dtype(DType::F16)
        .unwrap();
        let halves = input.chunk(2, D::Minus1).unwrap();
        let expected = (&halves[0].gelu_erf().unwrap() * &halves[1])
            .unwrap()
            .to_dtype(DType::F32)
            .unwrap()
            .flatten_all()
            .unwrap()
            .to_vec1::<f32>()
            .unwrap();
        let actual = input
            .apply_op1_no_bwd(op.as_ref())
            .unwrap()
            .to_dtype(DType::F32)
            .unwrap()
            .flatten_all()
            .unwrap()
            .to_vec1::<f32>()
            .unwrap();
        for (a, b) in actual.iter().zip(expected) {
            assert!(a.is_finite() && (a - b).abs() <= 0.004, "{a} vs {b}");
        }
    }
}
