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
/// One batch's dimensions: `rows` tokens of `count` hidden values in all.
struct Batch {
    batch: usize,
    seq: usize,
    rows: usize,
    count: usize,
    /// (batch, heads, sequence, head width), as the rope and attention kernels take it.
    shape: [u32; 4],
}
/// The workspace's scratch views. `pair` and `mid` each hold a residual followed by
/// its normalized copy, at `normalized` and `mlp_normalized`.
struct Scratch<'a> {
    pair: &'a View,
    mid: &'a View,
    normalized: View,
    mlp_normalized: View,
    qkv: &'a View,
    q: &'a View,
    k: &'a View,
    context: &'a View,
    projected: &'a View,
    mlp: &'a View,
    gate: &'a View,
}
/// A one-dimensional Metal grid or threadgroup size.
fn line(width: usize) -> MTLSize {
    MTLSize {
        width,
        height: 1,
        depth: 1,
    }
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
        encoder.dispatch_thread_groups(line(count / self.hidden), line(128));
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
        let scratch = Scratch {
            pair,
            mid,
            normalized: pair.at(count * 2),
            mlp_normalized: mid.at(count * 2),
            qkv,
            q,
            k,
            context,
            projected,
            mlp,
            gate,
        };
        let b = Batch {
            batch,
            seq,
            rows,
            count,
            shape: [
                batch as u32,
                self.heads as u32,
                seq as u32,
                (self.hidden / self.heads) as u32,
            ],
        };
        stage(&self.kind, "embedding_norm", || {
            self.encode_embedding_norm(ids, pair, &b)
        })?;
        if seq > 8 {
            stage(&self.kind, "attention_masks", || {
                self.encode_masks(workspace, &b)
            })?;
        }
        for layer in &self.layers {
            self.layer(layer, workspace, &scratch, &b)?;
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
    fn encode_embedding_norm(&self, ids: &Tensor, pair: &View, b: &Batch) -> Result<()> {
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
        encoder.set_bytes(5, &b.count);
        encoder.set_bytes(6, &self.eps);
        encoder.dispatch_thread_groups(line(b.rows), line(128));
        Ok(())
    }
    fn encode_masks(&self, workspace: &Workspace, b: &Batch) -> Result<()> {
        let encoder = self.device.command_encoder()?;
        encoder.set_compute_pipeline_state(&self.masks);
        encoder.set_buffer(0, Some(&workspace.lengths), 0);
        encoder.set_buffer(1, Some(&workspace.global_mask), 0);
        encoder.set_buffer(2, Some(&workspace.local_mask), 0);
        encoder.set_bytes(3, &(b.seq as u32));
        encoder.set_bytes(4, &(b.batch as u32));
        encoder.set_bytes(
            5,
            &(self.layers.iter().find_map(|l| l.radius).unwrap_or(0) as u32),
        );
        encoder.dispatch_threads(line(b.batch * b.seq * b.seq), line(256));
        Ok(())
    }
    fn layer(&self, layer: &Layer, workspace: &Workspace, s: &Scratch, b: &Batch) -> Result<()> {
        use crate::semantic_profile::stage;
        let (rows, hidden) = (b.rows, self.hidden);
        stage(&self.kind, "qkv", || {
            self.gemm(&s.normalized, &layer.qkv, s.qkv, rows, hidden, 3 * hidden)
        })?;
        stage(&self.kind, "rope", || self.encode_rope(layer, s, b))?;
        stage(&self.kind, "attention", || {
            self.encode_attention(layer, workspace, s, b)
        })?;
        stage(&self.kind, "attention_projection", || {
            self.gemm(
                s.context,
                &layer.projection,
                s.projected,
                rows,
                hidden,
                hidden,
            )
        })?;
        stage(&self.kind, "residual_norm", || {
            self.norm(s.projected, s.pair, &layer.mlp_norm, s.mid, b.count)
        })?;
        stage(&self.kind, "mlp_in", || {
            self.gemm(
                &s.mlp_normalized,
                &layer.mlp_in,
                s.mlp,
                rows,
                hidden,
                2 * self.intermediate,
            )
        })?;
        stage(&self.kind, "geglu", || self.encode_geglu(s, b))?;
        stage(&self.kind, "mlp_out", || {
            self.gemm(
                s.gate,
                &layer.mlp_out,
                s.projected,
                rows,
                self.intermediate,
                hidden,
            )
        })?;
        stage(&self.kind, "residual_norm", || {
            self.norm(s.projected, s.mid, &layer.next_norm, s.pair, b.count)
        })
    }
    fn encode_rope(&self, layer: &Layer, s: &Scratch, b: &Batch) -> Result<()> {
        let encoder = self.device.command_encoder()?;
        encoder.set_compute_pipeline_state(&self.rope);
        for (i, v) in [
            (0, s.qkv),
            (1, &layer.cosine),
            (2, &layer.sine),
            (3, s.q),
            (4, s.k),
        ] {
            encoder.set_buffer(i, Some(&v.buffer), v.offset);
        }
        encoder.set_bytes(5, &b.shape);
        encoder.dispatch_threads(line(b.count / 2), line(128));
        Ok(())
    }
    /// Longer sequences use the full SDPA kernel with the precomputed masks; short ones
    /// the arena's own kernel, which masks by length and radius.
    fn encode_attention(
        &self,
        layer: &Layer,
        workspace: &Workspace,
        s: &Scratch,
        b: &Batch,
    ) -> Result<()> {
        let (batch, seq) = (b.batch, b.seq);
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
                s.q.offset,
                &dims,
                &qs,
                &s.q.buffer,
                s.k.offset,
                &dims,
                &qs,
                &s.k.buffer,
                4 * self.hidden,
                &s.qkv.buffer,
                &vs,
                Some(candle_metal_kernels::SdpaDType::F16),
                Some(mask),
                Some(&ms),
                &s.context.buffer,
                &os,
                (d as f32).sqrt().recip(),
                false,
                candle_metal_kernels::SdpaDType::F16,
            )
            .map_err(|e| candle_core::Error::Msg(e.to_string()));
        }
        let encoder = self.device.command_encoder()?;
        encoder.set_compute_pipeline_state(&self.attention);
        let v = s.qkv.at(2 * self.hidden * 2);
        for (i, v) in [(0, s.q), (1, s.k), (2, &v)] {
            encoder.set_buffer(i, Some(&v.buffer), v.offset);
        }
        encoder.set_buffer(3, Some(&workspace.lengths), 0);
        let d = self.hidden / self.heads;
        let qs = [self.heads * seq * d, seq * d, d, 1];
        let vs = [seq * 3 * self.hidden, d, 3 * self.hidden, 1];
        encoder.set_bytes(4, &qs);
        encoder.set_bytes(5, &qs);
        encoder.set_bytes(6, &vs);
        encoder.set_bytes(7, &b.shape);
        encoder.set_bytes(8, &(d as f32).sqrt().recip());
        encoder.set_bytes(9, &layer.radius.map_or(-1, |r| r as i32));
        encoder.set_buffer(10, Some(&s.context.buffer), s.context.offset);
        encoder.dispatch_threads(line(b.rows * self.heads * 32), line(128));
        Ok(())
    }
    fn encode_geglu(&self, s: &Scratch, b: &Batch) -> Result<()> {
        let encoder = self.device.command_encoder()?;
        encoder.set_compute_pipeline_state(&self.geglu);
        encoder.set_bytes(0, &(b.rows * self.intermediate));
        encoder.set_bytes(1, &self.intermediate);
        encoder.set_buffer(2, Some(&s.mlp.buffer), s.mlp.offset);
        encoder.set_buffer(3, Some(&s.gate.buffer), s.gate.offset);
        encoder.dispatch_threads(line(b.rows * self.intermediate), line(256));
        Ok(())
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
