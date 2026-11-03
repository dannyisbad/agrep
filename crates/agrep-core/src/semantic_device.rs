//! Device selection for the complete semantic kernel set.
use candle_core::Device;

#[cfg(any(test, target_os = "macos"))]
#[derive(Clone, Copy)]
struct Capabilities {
    apple7: bool,
    unified_memory: bool,
    max_threads: usize,
    threadgroup_memory: usize,
}

#[cfg(any(test, target_os = "macos"))]
impl Capabilities {
    fn supported(self) -> bool {
        // Apple7 supplies SIMD reductions/matrix multiply (attention and
        // Candle GEMM), nonuniform dispatch (Apple4), and 64-bit integer math
        // (Apple3). Mac2 alone does not supply this full set. Shared buffers and
        // the fixed dispatch sizes also need unified memory and these limits.
        // https://developer.apple.com/metal/feature-sets/
        self.apple7
            && self.unified_memory
            && self.max_threads >= 1024
            && self.threadgroup_memory >= 32 * 1024
    }
}

#[cfg(any(test, target_os = "macos"))]
fn select_device(
    capabilities: Option<Capabilities>,
    create: impl FnOnce() -> Option<Device>,
) -> Device {
    if capabilities.is_some_and(Capabilities::supported) {
        create().unwrap_or(Device::Cpu)
    } else {
        Device::Cpu
    }
}

/// Only return Metal when every kernel used by the semantic lane is supported.
/// Missing or unsuitable GPUs are normal on macOS VMs and Intel Macs.
pub fn metal_device() -> Option<Device> {
    #[cfg(target_os = "macos")]
    {
        use candle_metal_kernels::metal;
        use objc2_foundation::NSString;
        use objc2_metal::{MTLComputePipelineState, MTLDevice, MTLGPUFamily, MTLLibrary};

        // Candle 0.9.2 indexes its default-device vector without checking it.
        // Inspect the optional native device before invoking that constructor.
        let candidate = metal::Device::system_default()?;
        let raw = candidate.as_ref();
        let capabilities = Capabilities {
            apple7: raw.supportsFamily(MTLGPUFamily::Apple7),
            unified_memory: raw.hasUnifiedMemory(),
            max_threads: raw.maxThreadsPerThreadgroup().width,
            threadgroup_memory: raw.maxThreadgroupMemoryLength(),
        };
        let selected = select_device(Some(capabilities), || {
            // Use fallible native compilation here: Candle's wrapper unwraps
            // compilation errors. Check the actual pipeline limits, including
            // the 32-lane SIMD width assumed by semantic_attention.
            let source = [
                include_str!("semantic_arena.metal"),
                include_str!("semantic_attention.metal"),
                include_str!("semantic_norm.metal"),
                include_str!("semantic_geglu.metal"),
            ]
            .join("\n");
            let library = raw
                .newLibraryWithSource_options_error(&NSString::from_str(&source), None)
                .ok()?;
            for (name, threads) in [
                ("embedding_norm", 128),
                ("packed_rope", 256),
                ("semantic_attention", 128),
                ("residual_norm", 128),
                ("semantic_geglu", 256),
                ("attention_masks", 256),
            ] {
                let function = library.newFunctionWithName(&NSString::from_str(name))?;
                let pipeline = raw
                    .newComputePipelineStateWithFunction_error(&function)
                    .ok()?;
                if pipeline.maxTotalThreadsPerThreadgroup() < threads
                    || (name == "semantic_attention" && pipeline.threadExecutionWidth() != 32)
                {
                    return None;
                }
            }
            // Eligible Apple GPUs are integrated, so the checked default device
            // cannot disappear through an eGPU unplug between these calls.
            let device = Device::new_metal(0).ok()?;
            (device.as_metal_device().ok()?.registry_id() == candidate.registry_id())
                .then_some(device)
        });
        selected.is_metal().then_some(selected)
    }
    #[cfg(not(target_os = "macos"))]
    {
        None
    }
}

pub fn background_device() -> Device {
    metal_device().unwrap_or(Device::Cpu)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn capable() -> Capabilities {
        Capabilities {
            apple7: true,
            unified_memory: true,
            max_threads: 1024,
            threadgroup_memory: 32768,
        }
    }

    #[test]
    fn selection_requires_the_entire_kernel_capability_set() {
        assert!(capable().supported());
        for unavailable in [
            None,
            Some(Capabilities {
                apple7: false,
                ..capable()
            }),
            Some(Capabilities {
                unified_memory: false,
                ..capable()
            }),
            Some(Capabilities {
                max_threads: 512,
                ..capable()
            }),
            Some(Capabilities {
                threadgroup_memory: 16384,
                ..capable()
            }),
        ] {
            assert!(select_device(unavailable, || panic!("ineligible GPU acquired")).is_cpu());
        }
        let mut called = false;
        assert!(select_device(Some(capable()), || {
            called = true;
            None
        })
        .is_cpu());
        assert!(called);
    }

    #[test]
    fn unavailable_metal_embeds_identically_to_explicit_cpu() {
        use crate::semantic_bert::{Config, ModernBert};
        use candle_core::{DType, IndexOp, Shape, Tensor};
        use candle_nn::{var_builder::SimpleBackend, Init, VarBuilder};

        struct Weights;
        impl SimpleBackend for Weights {
            fn get(
                &self,
                shape: Shape,
                name: &str,
                _: Init,
                dtype: DType,
                device: &Device,
            ) -> candle_core::Result<Tensor> {
                let values = (0..shape.elem_count())
                    .map(|i| {
                        if name.contains("norm") {
                            1.0
                        } else {
                            ((i * 17 % 101) as f32 - 50.) / 500.
                        }
                    })
                    .collect::<Vec<_>>();
                Tensor::from_vec(values, shape, device)?.to_dtype(dtype)
            }
            fn get_unchecked(&self, _: &str, _: DType, _: &Device) -> candle_core::Result<Tensor> {
                candle_core::bail!("fixture weights require a shape")
            }
            fn contains_tensor(&self, _: &str) -> bool {
                true
            }
        }
        let config = Config {
            vocab_size: 8,
            hidden_size: 32,
            num_hidden_layers: 2,
            num_attention_heads: 1,
            intermediate_size: 64,
            max_position_embeddings: 16,
            layer_norm_eps: 1e-5,
            pad_token_id: 0,
            global_attn_every_n_layers: 2,
            global_rope_theta: 160_000.,
            local_attention: 4,
            local_rope_theta: 10_000.,
            classifier_config: None,
        };
        let embed = |device: Device| {
            let model = ModernBert::load(
                VarBuilder::from_backend(Box::new(Weights), DType::F32, device.clone()),
                &config,
            )
            .unwrap();
            assert!(!model.uses_arena());
            let ids = Tensor::new(&[[1u32, 2, 3, 4], [1, 5, 0, 0]], &device).unwrap();
            let mask = Tensor::new(&[[1f32, 1., 1., 1.], [1., 1., 0., 0.]], &device).unwrap();
            let cls = model
                .forward(&ids, &mask, &[4, 2])
                .unwrap()
                .i((.., 0, ..))
                .unwrap();
            let norm = cls.sqr().unwrap().sum_keepdim(1).unwrap().sqrt().unwrap();
            cls.broadcast_div(&norm).unwrap().to_vec2::<f32>().unwrap()
        };
        let expected = embed(Device::Cpu);
        assert!(expected.iter().flatten().all(|x| x.is_finite()));
        assert!(expected.iter().flatten().any(|x| x.abs() > 0.01));
        for capabilities in [
            None,
            Some(Capabilities {
                apple7: false,
                ..capable()
            }),
            Some(capable()),
        ] {
            let fallback = select_device(capabilities, || None);
            assert!(fallback.is_cpu());
            assert_eq!(embed(fallback), expected);
        }
    }
}
