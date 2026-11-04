//! A bounded, reusable Metal workspace. No tensor-buffer allocation in a layer.
use candle_core::{DType, Device, IndexOp, MetalDevice, MetalStorage, Result, Storage, Tensor};
use candle_metal_kernels::metal::{Buffer, ComputePipeline};
use objc2_metal::MTLSize;
use std::sync::Arc;

#[derive(Clone)]
pub(crate) struct View {
    buffer: Arc<Buffer>,
    offset: usize,
}
impl View {
    pub(crate) fn tensor(t: &Tensor) -> Result<Self> {
        let (storage, layout) = t.storage_and_layout();
        let Storage::Metal(storage) = &*storage else {
            candle_core::bail!("arena requires Metal weights")
        };
        Ok(Self {
            buffer: Arc::new(storage.buffer().clone()),
            offset: layout.start_offset() * t.dtype().size_in_bytes(),
        })
    }
    fn at(&self, bytes: usize) -> Self {
        Self {
            buffer: self.buffer.clone(),
            offset: self.offset + bytes,
        }
    }
}
pub(crate) struct Layer {
    pub qkv: View,
    pub projection: View,
    pub mlp_in: View,
    pub mlp_out: View,
    pub mlp_norm: View,
    pub next_norm: View,
    pub cosine: View,
    pub sine: View,
    pub radius: Option<usize>,
}
struct Workspace {
    capacity: usize,
    generation: usize,
    buffers: Vec<View>,
    lengths: Arc<Buffer>,
    global_mask: Arc<Buffer>,
    local_mask: Arc<Buffer>,
}
pub(crate) struct Arena {
    device: MetalDevice,
    kind: Device,
    embedding: View,
    norm: View,
    layers: Vec<Layer>,
    hidden: usize,
    heads: usize,
    intermediate: usize,
    eps: f32,
    embedding_kernel: ComputePipeline,
    rope: ComputePipeline,
    attention: ComputePipeline,
    norm_kernel: ComputePipeline,
    geglu: ComputePipeline,
    masks: ComputePipeline,
    workspace: Option<Workspace>,
}
impl Arena {
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new(
        kind: &Device,
        embedding: View,
        norm: View,
        layers: Vec<Layer>,
        hidden: usize,
        heads: usize,
        intermediate: usize,
        eps: f32,
    ) -> Result<Self> {
        let Device::Metal(device) = kind else {
            candle_core::bail!("arena requires Metal")
        };
        let source = [
            include_str!("semantic_arena.metal"),
            include_str!("semantic_attention.metal"),
            include_str!("semantic_norm.metal"),
            include_str!("semantic_geglu.metal"),
        ]
        .join("\n");
        let library = device
            .device()
            .new_library_with_source(&source, None)
            .map_err(|e| candle_core::Error::Msg(e.to_string()))?;
        let pipeline = |name| -> Result<_> {
            let function = library
                .get_function(name, None)
                .map_err(|e| candle_core::Error::Msg(e.to_string()))?;
            device
                .device()
                .new_compute_pipeline_state_with_function(&function)
                .map_err(|e| candle_core::Error::Msg(e.to_string()))
        };
        Ok(Self {
            device: device.clone(),
            kind: kind.clone(),
            embedding,
            norm,
            layers,
            hidden,
            heads,
            intermediate,
            eps,
            embedding_kernel: pipeline("embedding_norm")?,
            rope: pipeline("packed_rope")?,
            attention: pipeline("semantic_attention")?,
            norm_kernel: pipeline("residual_norm")?,
            geglu: pipeline("semantic_geglu")?,
            masks: pipeline("attention_masks")?,
            workspace: None,
        })
    }
    pub(crate) fn reserve(&mut self, capacity: usize) -> Result<()> {
        if self
            .workspace
            .as_ref()
            .is_some_and(|w| w.capacity >= capacity)
        {
            return Ok(());
        }
        // K and H are pairs (residual, normalized). C/D/E/F/G/I/J are transient.
        let widths = [
            2 * self.hidden,
            2 * self.hidden,
            3 * self.hidden,
            self.hidden,
            self.hidden,
            self.hidden,
            self.hidden,
            2 * self.intermediate,
            self.intermediate,
        ];
        let generation = self.workspace.as_ref().map_or(1, |w| w.generation + 1);
        let mut buffers = Vec::new();
        for width in widths {
            buffers.push(View {
                buffer: self.device.new_private_buffer(
                    capacity * width,
                    DType::F16,
                    "semantic-arena",
                )?,
                offset: 0,
            });
        }
        let lengths = self
            .device
            .new_buffer(capacity, DType::U32, "semantic-lengths")?;
        let global_mask = self
            .device
            .new_private_buffer(capacity, DType::F16, "global-mask")?;
        let local_mask =
            self.device
                .new_private_buffer(capacity * 1024, DType::F16, "local-mask")?;
        crate::semantic_profile::event(
            serde_json::json!({"arena_reserve":{"generation":generation,"tokens":capacity,"scratch_buffers":buffers.len()+1,"mask_buffers":2,"requested_bytes":capacity*(widths.iter().sum::<usize>()*2+4+2*(1024+1))}}),
        );
        self.workspace = Some(Workspace {
            capacity,
            generation,
            buffers,
            lengths,
            global_mask,
            local_mask,
        });
        Ok(())
    }
    fn gemm(
        &self,
        input: &View,
        weight: &View,
        output: &View,
        rows: usize,
        inner: usize,
        cols: usize,
    ) -> Result<()> {
        let encoder = self.device.command_encoder()?;
        candle_metal_kernels::call_mlx_gemm(
            self.device.device(),
            &encoder,
            self.device.kernels(),
            candle_metal_kernels::GemmDType::F16,
            (1, rows, cols, inner),
            &[inner, 1],
            input.offset,
            &input.buffer,
            &[1, inner],
            weight.offset,
            &weight.buffer,
            &output.buffer,
        )
        .map_err(|e| candle_core::Error::Msg(e.to_string()))
    }
    fn norm(
        &self,
        x: &View,
        residual: &View,
        weight: &View,
        output: &View,
        count: usize,
    ) -> Result<()> {
        let encoder = self.device.command_encoder()?;
        encoder.set_compute_pipeline_state(&self.norm_kernel);
        for (i, v) in [(0, x), (1, residual), (2, weight), (3, output)] {
            encoder.set_buffer(i, Some(&v.buffer), v.offset);
        }
        encoder.set_bytes(4, &self.hidden);
        encoder.set_bytes(5, &count);
        encoder.set_bytes(6, &self.eps);
        encoder.dispatch_thread_groups(
            MTLSize {
                width: count / self.hidden,
                height: 1,
                depth: 1,
            },
            MTLSize {
                width: 128,
                height: 1,
                depth: 1,
            },
        );
        Ok(())
    }
    pub(crate) fn embed(&mut self, ids: &Tensor, lengths: &[usize]) -> Result<Vec<Vec<f32>>> {
        use crate::semantic_profile::stage;
        let (batch, seq) = ids.dims2()?;
        let rows = batch * seq;
        let count = rows * self.hidden;
        let workspace = self
            .workspace
            .as_ref()
            .ok_or_else(|| candle_core::Error::Msg("arena was not reserved".into()))?;
        if rows > workspace.capacity {
            candle_core::bail!("arena capacity exceeded")
        }
        // Prior readback completes before this locked workspace is returned.
        unsafe {
            for (i, length) in lengths.iter().enumerate() {
                workspace
                    .lengths
                    .contents()
                    .cast::<u32>()
                    .add(i)
                    .write(*length as u32);
            }
        }
        let [pair, mid, qkv, q, k, context, projected, mlp, gate]: [&View; 9] =
            std::array::from_fn(|i| &workspace.buffers[i]);
        let normalized = pair.at(count * 2);
        let mlp_normalized = mid.at(count * 2);
        stage(&self.kind, "embedding_norm", || -> Result<()> {
            let input = View::tensor(ids)?;
            let encoder = self.device.command_encoder()?;
            encoder.set_compute_pipeline_state(&self.embedding_kernel);
            for (i, v) in [
                (0, &input),
                (1, &self.embedding),
                (2, &self.norm),
                (3, pair),
            ] {
                encoder.set_buffer(i, Some(&v.buffer), v.offset);
            }
            encoder.set_bytes(4, &self.hidden);
            encoder.set_bytes(5, &count);
            encoder.set_bytes(6, &self.eps);
            encoder.dispatch_thread_groups(
                MTLSize {
                    width: rows,
                    height: 1,
                    depth: 1,
                },
                MTLSize {
                    width: 128,
                    height: 1,
                    depth: 1,
                },
            );
            Ok(())
        })?;
        let shape = [
            batch as u32,
            self.heads as u32,
            seq as u32,
            (self.hidden / self.heads) as u32,
        ];
        if seq > 8 {
            stage(&self.kind, "attention_masks", || -> Result<()> {
                let encoder = self.device.command_encoder()?;
                encoder.set_compute_pipeline_state(&self.masks);
                encoder.set_buffer(0, Some(&workspace.lengths), 0);
                encoder.set_buffer(1, Some(&workspace.global_mask), 0);
                encoder.set_buffer(2, Some(&workspace.local_mask), 0);
                encoder.set_bytes(3, &(seq as u32));
                encoder.set_bytes(4, &(batch as u32));
                encoder.set_bytes(
                    5,
                    &(self.layers.iter().find_map(|l| l.radius).unwrap_or(0) as u32),
                );
                encoder.dispatch_threads(
                    MTLSize {
                        width: batch * seq * seq,
                        height: 1,
                        depth: 1,
                    },
                    MTLSize {
                        width: 256,
                        height: 1,
                        depth: 1,
                    },
                );
                Ok(())
            })?;
        }
        for layer in &self.layers {
            stage(&self.kind, "qkv", || {
                self.gemm(
                    &normalized,
                    &layer.qkv,
                    qkv,
                    rows,
                    self.hidden,
                    3 * self.hidden,
                )
            })?;
            stage(&self.kind, "rope", || -> Result<()> {
                let encoder = self.device.command_encoder()?;
                encoder.set_compute_pipeline_state(&self.rope);
                for (i, v) in [
                    (0, qkv),
                    (1, &layer.cosine),
                    (2, &layer.sine),
                    (3, q),
                    (4, k),
                ] {
                    encoder.set_buffer(i, Some(&v.buffer), v.offset);
                }
                encoder.set_bytes(5, &shape);
                encoder.dispatch_threads(
                    MTLSize {
                        width: count / 2,
                        height: 1,
                        depth: 1,
                    },
                    MTLSize {
                        width: 128,
                        height: 1,
                        depth: 1,
                    },
                );
                Ok(())
            })?;
            stage(&self.kind, "attention", || -> Result<()> {
                if seq > 8 {
                    let d = self.hidden / self.heads;
                    let dims = [batch, self.heads, seq, d];
                    let qs = [self.heads * seq * d, seq * d, d, 1];
                    let vs = [seq * 3 * self.hidden, d, 3 * self.hidden, 1];
                    let os = [seq * self.hidden, d, self.hidden, 1];
                    let ms = if layer.radius.is_some() {
                        [seq * seq, 0, seq, 1]
                    } else {
                        [seq, 0, 0, 1]
                    };
                    let mask: &Buffer = if layer.radius.is_some() {
                        &workspace.local_mask
                    } else {
                        &workspace.global_mask
                    };
                    let encoder = self.device.command_encoder()?;
                    return candle_metal_kernels::call_sdpa_full(
                        self.device.device(),
                        &encoder,
                        self.device.kernels(),
                        q.offset,
                        &dims,
                        &qs,
                        &q.buffer,
                        k.offset,
                        &dims,
                        &qs,
                        &k.buffer,
                        4 * self.hidden,
                        &qkv.buffer,
                        &vs,
                        Some(candle_metal_kernels::SdpaDType::F16),
                        Some(mask),
                        Some(&ms),
                        &context.buffer,
                        &os,
                        (d as f32).sqrt().recip(),
                        false,
                        candle_metal_kernels::SdpaDType::F16,
                    )
                    .map_err(|e| candle_core::Error::Msg(e.to_string()));
                }
                let encoder = self.device.command_encoder()?;
                encoder.set_compute_pipeline_state(&self.attention);
                let v = qkv.at(2 * self.hidden * 2);
                for (i, v) in [(0, q), (1, k), (2, &v)] {
                    encoder.set_buffer(i, Some(&v.buffer), v.offset);
                }
                encoder.set_buffer(3, Some(&workspace.lengths), 0);
                let d = self.hidden / self.heads;
                let qs = [self.heads * seq * d, seq * d, d, 1];
                let vs = [seq * 3 * self.hidden, d, 3 * self.hidden, 1];
                encoder.set_bytes(4, &qs);
                encoder.set_bytes(5, &qs);
                encoder.set_bytes(6, &vs);
                encoder.set_bytes(7, &shape);
                encoder.set_bytes(8, &(d as f32).sqrt().recip());
                encoder.set_bytes(9, &layer.radius.map_or(-1, |r| r as i32));
                encoder.set_buffer(10, Some(&context.buffer), context.offset);
                encoder.dispatch_threads(
                    MTLSize {
                        width: rows * self.heads * 32,
                        height: 1,
                        depth: 1,
                    },
                    MTLSize {
                        width: 128,
                        height: 1,
                        depth: 1,
                    },
                );
                Ok(())
            })?;
            stage(&self.kind, "attention_projection", || {
                self.gemm(
                    context,
                    &layer.projection,
                    projected,
                    rows,
                    self.hidden,
                    self.hidden,
                )
            })?;
            stage(&self.kind, "residual_norm", || {
                self.norm(projected, pair, &layer.mlp_norm, mid, count)
            })?;
            stage(&self.kind, "mlp_in", || {
                self.gemm(
                    &mlp_normalized,
                    &layer.mlp_in,
                    mlp,
                    rows,
                    self.hidden,
                    2 * self.intermediate,
                )
            })?;
            stage(&self.kind, "geglu", || -> Result<()> {
                let encoder = self.device.command_encoder()?;
                encoder.set_compute_pipeline_state(&self.geglu);
                encoder.set_bytes(0, &(rows * self.intermediate));
                encoder.set_bytes(1, &self.intermediate);
                encoder.set_buffer(2, Some(&mlp.buffer), mlp.offset);
                encoder.set_buffer(3, Some(&gate.buffer), gate.offset);
                encoder.dispatch_threads(
                    MTLSize {
                        width: rows * self.intermediate,
                        height: 1,
                        depth: 1,
                    },
                    MTLSize {
                        width: 256,
                        height: 1,
                        depth: 1,
                    },
                );
                Ok(())
            })?;
            stage(&self.kind, "mlp_out", || {
                self.gemm(
                    gate,
                    &layer.mlp_out,
                    projected,
                    rows,
                    self.intermediate,
                    self.hidden,
                )
            })?;
            stage(&self.kind, "residual_norm", || {
                self.norm(projected, mid, &layer.next_norm, pair, count)
            })?;
        }
        crate::semantic_profile::event(
            serde_json::json!({"arena_batch":{"rows":batch,"tokens":rows,"workspace_generation":workspace.generation,"reserved_tokens":workspace.capacity,"synchronized_profile":crate::semantic_profile::enabled()}}),
        );
        // Exactly one output conversion/readback after the complete batch.
        let tensor = Tensor::from_storage(
            Storage::Metal(MetalStorage::new(
                pair.buffer.clone(),
                self.device.clone(),
                2 * count,
                DType::F16,
            )),
            (2, batch, seq, self.hidden),
            candle_core::op::BackpropOp::none(),
            false,
        );
        tensor
            .i((1, .., 0, ..))?
            .to_dtype(DType::F32)?
            .to_vec2::<f32>()
    }
}

#[cfg(all(test, target_os = "macos"))]
mod tests {
    use super::*;
    #[test]
    fn persistent_workspace_reuses_buffers_and_reads_new_inputs() {
        let Some(device) = crate::semantic_device::metal_device() else {
            return;
        };
        use objc2_metal::MTLDevice;
        eprintln!(
            "Metal GPU: {}",
            device.as_metal_device().unwrap().device().as_ref().name()
        );
        let d = 32;
        let intermediate = 64;
        let tensor = |dims: (usize, usize)| Tensor::zeros(dims, DType::F16, &device).unwrap();
        let norm = Tensor::ones(d, DType::F16, &device).unwrap();
        let embedding = Tensor::from_vec(
            (0..4 * d)
                .map(|i| (i % 13) as f32 / 13. - 0.5)
                .collect::<Vec<_>>(),
            (4, d),
            &device,
        )
        .unwrap()
        .to_dtype(DType::F16)
        .unwrap();
        let cosine = Tensor::ones((1024, d / 2), DType::F16, &device).unwrap();
        let sine = cosine.zeros_like().unwrap();
        let view = |t: &Tensor| View::tensor(t).unwrap();
        let layer = Layer {
            qkv: view(&tensor((3 * d, d))),
            projection: view(&tensor((d, d))),
            mlp_in: view(&tensor((2 * intermediate, d))),
            mlp_out: view(&tensor((d, intermediate))),
            mlp_norm: view(&norm),
            next_norm: view(&norm),
            cosine: view(&cosine),
            sine: view(&sine),
            radius: Some(64),
        };
        let mut arena = Arena::new(
            &device,
            view(&embedding),
            view(&norm),
            vec![layer],
            d,
            1,
            intermediate,
            1e-5,
        )
        .unwrap();
        arena.reserve(8).unwrap();
        let pointers = arena
            .workspace
            .as_ref()
            .unwrap()
            .buffers
            .iter()
            .map(|v| Arc::as_ptr(&v.buffer))
            .collect::<Vec<_>>();
        let ids = Tensor::new(&[[0u32, 1], [2, 3]], &device).unwrap();
        let first = arena.embed(&ids, &[2, 1]).unwrap();
        arena.reserve(4).unwrap();
        assert_eq!(arena.workspace.as_ref().unwrap().generation, 1);
        assert_eq!(
            pointers,
            arena
                .workspace
                .as_ref()
                .unwrap()
                .buffers
                .iter()
                .map(|v| Arc::as_ptr(&v.buffer))
                .collect::<Vec<_>>()
        );
        assert_eq!(first, arena.embed(&ids, &[2, 1]).unwrap());
        let ids = Tensor::new(&[[3u32, 1], [0, 2]], &device).unwrap();
        let changed = arena.embed(&ids, &[2, 2]).unwrap();
        assert_ne!(first, changed);
        let input = embedding
            .index_select(&Tensor::new(&[3u32, 0], &device).unwrap(), 0)
            .unwrap();
        let zeros = norm.zeros_like().unwrap();
        let expected = candle_nn::ops::layer_norm(&input, &norm, &zeros, 1e-5).unwrap();
        let expected = candle_nn::ops::layer_norm(&expected, &norm, &zeros, 1e-5)
            .unwrap()
            .to_dtype(DType::F32)
            .unwrap()
            .to_vec2::<f32>()
            .unwrap();
        for (a, b) in changed.iter().flatten().zip(expected.iter().flatten()) {
            assert!((a - b).abs() < 0.004, "{a} != {b}");
        }
    }
}
