//! Candle 0.9.2 ModernBERT backbone (MIT/Apache-2.0), with dtype-correct masks.
//! Preserves the checkpoint architecture; fused operations are gated against the F32 spike oracle.
//! ModernBERT
//!
//! ModernBERT is a modernized bidirectional encoder-only Transformer model.
//! - [Arxiv](https://arxiv.org/abs/2412.13663) "Smarter, Better, Faster, Longer: A Modern Bidirectional Encoder for Fast, Memory Efficient, and Long Context Finetuning and Inference"
//! - Upstream [GitHub repo](https://github.com/AnswerDotAI/ModernBERT).
//! - See modernbert in [candle-examples](https://github.com/huggingface/candle/tree/main/candle-examples/) for runnable code
//!

mod cpu;
mod mapped;
use crate::semantic_profile::stage;
use candle_core::{DType, Device, Result, Tensor, D};
use candle_nn::{embedding, ops::softmax, Module, VarBuilder};
use cpu::{cpu_attention, projection, CpuGeglu};
pub(crate) use mapped::{checkpoint_name, MappedWeights};
use mapped::{Embeddings, Projection};
use serde::Deserialize;

use core::f32;
use std::collections::HashMap;
use std::sync::Arc;

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct Config {
    pub vocab_size: usize,
    pub hidden_size: usize,
    pub num_hidden_layers: usize,
    pub num_attention_heads: usize,
    pub intermediate_size: usize,
    pub max_position_embeddings: usize,
    pub layer_norm_eps: f64,
    pub pad_token_id: u32,
    pub global_attn_every_n_layers: usize,
    pub global_rope_theta: f64,
    pub local_attention: usize,
    pub local_rope_theta: f64,
    #[serde(default)]
    #[serde(flatten)]
    pub classifier_config: Option<ClassifierConfig>,
}

#[derive(Debug, Clone, Deserialize, PartialEq, Copy, Default)]
#[serde(rename_all = "lowercase")]
pub enum ClassifierPooling {
    #[default]
    Cls,
    Mean,
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct ClassifierConfig {
    pub id2label: HashMap<String, String>,
    pub label2id: HashMap<String, String>,
    pub classifier_pooling: ClassifierPooling,
}

// Candle's no-bias wrapper uses separate reductions/copies. A zero bias lets
// the existing fused CPU/Metal normalization kernel execute the same formula.
#[derive(Clone)]
struct FastNorm {
    weight: Tensor,
    zero: Tensor,
    eps: f32,
}
fn layer_norm_no_bias(size: usize, eps: f64, vb: VarBuilder) -> Result<FastNorm> {
    let weight = vb.get(size, "weight")?;
    let zero = weight.zeros_like()?;
    Ok(FastNorm {
        weight,
        zero,
        eps: eps as f32,
    })
}
impl Module for FastNorm {
    fn forward(&self, x: &Tensor) -> Result<Tensor> {
        candle_nn::ops::layer_norm(&x.contiguous()?, &self.weight, &self.zero, self.eps)
    }
}

#[derive(Debug, Clone)]
struct RotaryEmbedding {
    sin: Tensor,
    cos: Tensor,
}

impl RotaryEmbedding {
    fn new(dtype: DType, config: &Config, rope_theta: f64, dev: &Device) -> Result<Self> {
        let dim = config.hidden_size / config.num_attention_heads;
        let inv_freq: Vec<_> = (0..dim)
            .step_by(2)
            .map(|i| 1f32 / rope_theta.powf(i as f64 / dim as f64) as f32)
            .collect();
        let inv_freq_len = inv_freq.len();
        let inv_freq = Tensor::from_vec(inv_freq, (1, inv_freq_len), dev)?.to_dtype(dtype)?;
        // The embedding contract caps inputs at 1024, independent of the
        // checkpoint's 8192-position capacity. Unused trig tables cost every
        // fresh query process; this leaves the input window unchanged.
        let max_seq_len = config.max_position_embeddings.min(1024);
        let t = Tensor::arange(0u32, max_seq_len as u32, dev)?
            .to_dtype(dtype)?
            .reshape((max_seq_len, 1))?;
        let freqs = t.matmul(&inv_freq)?;
        Ok(Self {
            sin: freqs.sin()?,
            cos: freqs.cos()?,
        })
    }

    fn apply_rotary_emb_qkv(&self, q: &Tensor, k: &Tensor) -> Result<(Tensor, Tensor)> {
        let q_embed = candle_nn::rotary_emb::rope(&q.contiguous()?, &self.cos, &self.sin)?;
        let k_embed = candle_nn::rotary_emb::rope(&k.contiguous()?, &self.cos, &self.sin)?;
        Ok((q_embed, k_embed))
    }
}

#[derive(Clone)]
struct ModernBertAttention {
    qkv: Projection,
    proj: Projection,
    num_attention_heads: usize,
    attention_head_size: usize,
    rotary_emb: Arc<RotaryEmbedding>,
}

impl ModernBertAttention {
    fn load(
        vb: VarBuilder,
        config: &Config,
        rotary_emb: Arc<RotaryEmbedding>,
        mapped: Option<&MappedWeights>,
    ) -> Result<Self> {
        let num_attention_heads = config.num_attention_heads;
        let attention_head_size = config.hidden_size / config.num_attention_heads;

        let qkv = Projection::load(
            config.hidden_size,
            config.hidden_size * 3,
            vb.pp("Wqkv"),
            mapped,
        )?;
        let proj = Projection::load(config.hidden_size, config.hidden_size, vb.pp("Wo"), mapped)?;

        Ok(Self {
            qkv,
            proj,
            num_attention_heads,
            attention_head_size,
            rotary_emb,
        })
    }

    fn forward(
        &self,
        hidden_states: &Tensor,
        attention_mask: Option<&Tensor>,
        lengths: &[usize],
        radius: Option<usize>,
        tiled: bool,
    ) -> Result<Tensor> {
        let xs = hidden_states.clone();
        let (b, seq_len, d) = xs.dims3()?;
        let qkv = stage(xs.device(), "qkv", || projection(&xs, &self.qkv))?
            .reshape((
                b,
                seq_len,
                3,
                self.num_attention_heads,
                self.attention_head_size,
            ))?
            .permute((2, 0, 3, 1, 4))?;

        let q = qkv.get(0)?;
        let k = qkv.get(1)?;
        let v = qkv.get(2)?;

        let (q, k) = stage(xs.device(), "rope", || {
            self.rotary_emb.apply_rotary_emb_qkv(&q, &k)
        })?;

        let scale = (self.attention_head_size as f64).powf(-0.5);
        // Candle 0.9.2's full Metal SDPA kernel accepts the exact local/padding
        // mask, avoids materializing qk/softmax, and reduces command dispatch.
        // Its <=8-token vector kernel ignores explicit masks, so those inputs
        // retain the original attention path.
        let xs = stage(xs.device(), "attention", || {
            if q.device().is_cpu() && tiled {
                return crate::semantic_attention::cpu(&q, &k, &v, lengths, radius, scale as f32);
            }
            let attention_mask = attention_mask
                .ok_or_else(|| candle_core::Error::Msg("missing attention mask".into()))?;
            if q.device().is_metal() && seq_len > 8 {
                // All-masked tiles must stay finite during online softmax rescaling.
                let finite_mask = attention_mask.clamp(-65504.0, 0.0)?;
                let mask =
                    finite_mask.broadcast_as((b, self.num_attention_heads, seq_len, seq_len))?;
                candle_nn::ops::sdpa(&q, &k, &v, Some(&mask), false, scale as f32, 1.0)
            } else {
                let q = (q * scale)?;
                if q.device().is_cpu() && b > 1 {
                    cpu_attention(&q, &k, &v, attention_mask)
                } else {
                    let att = q.matmul(&k.transpose(D::Minus2, D::Minus1)?)?;
                    let scores = att.broadcast_add(attention_mask)?;
                    let att = if q.device().is_cpu() {
                        candle_nn::ops::softmax_last_dim(&scores.contiguous()?)?
                    } else {
                        softmax(&scores, D::Minus1)?
                    };
                    att.matmul(&v)
                }
            }
        })?;

        let xs = if xs.device().is_cpu() && tiled {
            xs
        } else {
            xs.transpose(1, 2)?.reshape((b, seq_len, d))?
        };
        let xs = stage(xs.device(), "attention_projection", || {
            projection(&xs, &self.proj)
        })?;
        let xs = xs.reshape((b, seq_len, d))?;

        Ok(xs)
    }
}

#[derive(Clone)]
pub struct ModernBertMLP {
    wi: Projection,
    wo: Projection,
}

impl ModernBertMLP {
    fn load(vb: VarBuilder, config: &Config, mapped: Option<&MappedWeights>) -> Result<Self> {
        let wi = Projection::load(
            config.hidden_size,
            config.intermediate_size * 2,
            vb.pp("Wi"),
            mapped,
        )?;
        let wo = Projection::load(
            config.intermediate_size,
            config.hidden_size,
            vb.pp("Wo"),
            mapped,
        )?;
        Ok(Self { wi, wo })
    }
}

impl Module for ModernBertMLP {
    fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        let xs = stage(xs.device(), "mlp_in", || projection(xs, &self.wi))?;
        let xs = stage(xs.device(), "geglu", || {
            if xs.device().is_cpu() && xs.dtype() == DType::F32 {
                xs.contiguous()?.apply_op1_no_bwd(&CpuGeglu {
                    vector: cfg!(target_os = "macos"),
                })
            } else {
                let halves = xs.chunk(2, D::Minus1)?;
                &halves[0].gelu_erf()? * &halves[1]
            }
        })?;
        stage(xs.device(), "mlp_out", || projection(&xs, &self.wo))
    }
}

#[derive(Clone)]
pub struct ModernBertLayer {
    attn: ModernBertAttention,
    mlp: ModernBertMLP,
    attn_norm: Option<FastNorm>,
    mlp_norm: FastNorm,
    uses_local_attention: bool,
    local_radius: usize,
}

impl ModernBertLayer {
    fn load(
        vb: VarBuilder,
        config: &Config,
        rotary_emb: Arc<RotaryEmbedding>,
        uses_local_attention: bool,
        mapped: Option<&MappedWeights>,
    ) -> Result<Self> {
        let attn = ModernBertAttention::load(vb.pp("attn"), config, rotary_emb, mapped)?;
        let mlp = ModernBertMLP::load(vb.pp("mlp"), config, mapped)?;
        let attn_norm = layer_norm_no_bias(
            config.hidden_size,
            config.layer_norm_eps,
            vb.pp("attn_norm"),
        )
        .ok();
        let mlp_norm =
            layer_norm_no_bias(config.hidden_size, config.layer_norm_eps, vb.pp("mlp_norm"))?;
        Ok(Self {
            attn,
            mlp,
            attn_norm,
            mlp_norm,
            uses_local_attention,
            local_radius: config.local_attention / 2,
        })
    }

    /// The layer's additive mask: padding, plus the sliding window of a local layer.
    fn attention_mask(
        &self,
        global: Option<&Tensor>,
        local: Option<&Tensor>,
    ) -> Result<Option<Tensor>> {
        Ok(match (global, local) {
            (Some(global), Some(local)) if self.uses_local_attention => {
                Some(global.broadcast_add(local)?)
            }
            (Some(global), _) => Some(global.clone()),
            _ => None,
        })
    }

    fn forward(
        &self,
        xs: &Tensor,
        global_attention_mask: Option<&Tensor>,
        local_attention_mask: Option<&Tensor>,
        lengths: &[usize],
        tiled: bool,
    ) -> Result<Tensor> {
        let residual = xs.clone();
        let mut xs = xs.clone();
        if let Some(norm) = &self.attn_norm {
            xs = xs.apply(norm)?;
        }

        let attention_mask = self.attention_mask(global_attention_mask, local_attention_mask)?;
        let radius = self.uses_local_attention.then_some(self.local_radius);
        let xs = self
            .attn
            .forward(&xs, attention_mask.as_ref(), lengths, radius, tiled)?;
        let xs = (xs + residual)?;
        let mlp_out = xs.apply(&self.mlp_norm)?.apply(&self.mlp)?;
        let xs = (xs + mlp_out)?;
        Ok(xs)
    }
}

// Global attention mask calculated from padded token inputs
fn prepare_4d_attention_mask(
    mask: &Tensor,
    dtype: DType,
    tgt_len: Option<usize>,
) -> Result<Tensor> {
    let bsz = mask.dim(0)?;
    let src_len = mask.dim(1)?;
    let tgt_len = tgt_len.unwrap_or(src_len);

    let expanded_mask = mask
        .unsqueeze(1)?
        .unsqueeze(2)?
        .expand((bsz, 1, tgt_len, src_len))?
        .to_dtype(dtype)?;

    let inverted_mask = (1.0 - expanded_mask)?;

    let floor = if dtype == DType::F16 {
        -65504.0
    } else {
        f32::MIN as f64
    };
    (inverted_mask * floor)?.to_dtype(dtype)
}

// Attention mask caused by the sliding window
fn get_local_attention_mask(
    seq_len: usize,
    max_distance: usize,
    device: &Device,
) -> Result<Tensor> {
    let mask: Vec<_> = (0..seq_len)
        .flat_map(|i| {
            (0..seq_len).map(move |j| {
                if (j as i32 - i as i32).abs() > max_distance as i32 {
                    f32::NEG_INFINITY
                } else {
                    0.
                }
            })
        })
        .collect();
    Tensor::from_slice(&mask, (seq_len, seq_len), device)
}

// ModernBERT backbone
#[derive(Clone)]
pub struct ModernBert {
    word_embeddings: Embeddings,
    norm: FastNorm,
    layers: Vec<ModernBertLayer>,
    final_norm: FastNorm,
    local_attention_size: usize,
    dtype: DType,
    tiled_attention: bool,
    fused_residual: bool,
    #[cfg(target_os = "macos")]
    arena: Option<Arc<std::sync::Mutex<crate::semantic_arena::Arena>>>,
}

impl ModernBert {
    /// `mapped` keeps the F32 projection and embedding weights in the mapped
    /// file (CPU only); otherwise every weight is copied into a tensor.
    pub(crate) fn load(
        vb: VarBuilder,
        config: &Config,
        mapped: Option<&MappedWeights>,
    ) -> Result<Self> {
        if mapped.is_some() && (!vb.device().is_cpu() || vb.dtype() != DType::F32) {
            candle_core::bail!("mapped weights are F32 CPU weights");
        }
        let embeddings = vb.pp("model.embeddings.tok_embeddings");
        let word_embeddings = match mapped {
            Some(weights) => Embeddings::Mapped(
                weights.get(&embeddings, &[config.vocab_size, config.hidden_size])?,
            ),
            None => Embeddings::Owned(embedding(
                config.vocab_size,
                config.hidden_size,
                embeddings,
            )?),
        };
        let norm = layer_norm_no_bias(
            config.hidden_size,
            config.layer_norm_eps,
            vb.pp("model.embeddings.norm"),
        )?;
        let layers = load_layers(&vb, config, mapped)?;
        let final_norm = layer_norm_no_bias(
            config.hidden_size,
            config.layer_norm_eps,
            vb.pp("model.final_norm"),
        )?;

        // Metal always embeds through the persistent arena; the tensor forward serves the CPU.
        #[cfg(target_os = "macos")]
        let arena = if vb.device().is_metal() {
            Some(Arc::new(std::sync::Mutex::new(arena(
                vb.device(),
                config,
                &word_embeddings,
                &norm,
                &layers,
                &final_norm,
            )?)))
        } else {
            None
        };
        Ok(Self {
            word_embeddings,
            norm,
            layers,
            final_norm,
            local_attention_size: config.local_attention,
            dtype: vb.dtype(),
            tiled_attention: cfg!(target_os = "macos"),
            fused_residual: cfg!(target_os = "macos"),
            #[cfg(target_os = "macos")]
            arena,
        })
    }

    pub(crate) fn prepare_arena(&self, capacity: usize) -> Result<()> {
        #[cfg(target_os = "macos")]
        if let Some(arena) = &self.arena {
            arena
                .lock()
                .map_err(|e| candle_core::Error::Msg(e.to_string()))?
                .reserve(capacity)?;
        }
        let _ = capacity;
        Ok(())
    }
    pub(crate) fn arena_embeddings(
        &self,
        ids: &Tensor,
        lengths: &[usize],
    ) -> Result<Option<Vec<Vec<f32>>>> {
        #[cfg(target_os = "macos")]
        if let Some(arena) = &self.arena {
            return Ok(Some(
                arena
                    .lock()
                    .map_err(|e| candle_core::Error::Msg(e.to_string()))?
                    .embed(ids, lengths)?,
            ));
        }
        let _ = (ids, lengths);
        Ok(None)
    }

    pub fn forward(&self, xs: &Tensor, mask: &Tensor, lengths: &[usize]) -> Result<Tensor> {
        let seq_len = xs.shape().dims()[1];
        let (global_attention_mask, local_attention_mask) = if self.tiled_attention
            && xs.device().is_cpu()
        {
            (None, None)
        } else {
            (
                Some(prepare_4d_attention_mask(mask, self.dtype, None)?.to_device(xs.device())?),
                Some(
                    get_local_attention_mask(seq_len, self.local_attention_size / 2, xs.device())?
                        .to_dtype(self.dtype)?,
                ),
            )
        };
        let mut xs = self.word_embeddings.forward(xs)?.apply(&self.norm)?;
        if self.fused_residual {
            return self.fused_layers(
                xs,
                global_attention_mask.as_ref(),
                local_attention_mask.as_ref(),
                lengths,
            );
        }
        for (index, layer) in self.layers.iter().enumerate() {
            if crate::semantic_profile::enabled() {
                eprintln!("{}", serde_json::json!({"layer":index,"shape":xs.dims()}));
            }
            xs = stage(xs.device(), "layer", || {
                layer.forward(
                    &xs,
                    global_attention_mask.as_ref(),
                    local_attention_mask.as_ref(),
                    lengths,
                    self.tiled_attention,
                )
            })?;
        }
        let xs = xs.apply(&self.final_norm)?;
        Ok(xs)
    }

    /// Carries each residual and its pre-normalized view into the next layer: the next
    /// layer's (or the final) norm is fused into this layer's second residual add.
    /// The packed pair consists of two contiguous views of one allocation.
    fn fused_layers(
        &self,
        mut xs: Tensor,
        global_attention_mask: Option<&Tensor>,
        local_attention_mask: Option<&Tensor>,
        lengths: &[usize],
    ) -> Result<Tensor> {
        let mut normalized = xs.clone();
        for (index, layer) in self.layers.iter().enumerate() {
            let (residual, next_normalized) = stage(xs.device(), "layer", || {
                let mask = layer.attention_mask(global_attention_mask, local_attention_mask)?;
                let attention = layer.attn.forward(
                    &normalized,
                    mask.as_ref(),
                    lengths,
                    layer.uses_local_attention.then_some(layer.local_radius),
                    self.tiled_attention,
                )?;
                let (residual, mlp_input) = stage(xs.device(), "residual_norm", || {
                    crate::semantic_norm::forward(
                        &attention,
                        &xs,
                        &layer.mlp_norm.weight,
                        layer.mlp_norm.eps,
                    )
                })?;
                let mlp = layer.mlp.forward(&mlp_input)?;
                let next_norm = self
                    .layers
                    .get(index + 1)
                    .and_then(|next| next.attn_norm.as_ref())
                    .unwrap_or(&self.final_norm);
                stage(xs.device(), "residual_norm", || {
                    crate::semantic_norm::forward(&mlp, &residual, &next_norm.weight, next_norm.eps)
                })
            })?;
            xs = residual;
            normalized = next_normalized;
        }
        Ok(normalized)
    }
}

/// The encoder layers; every `global_attn_every_n_layers`-th is global, the rest local.
fn load_layers(
    vb: &VarBuilder,
    config: &Config,
    mapped: Option<&MappedWeights>,
) -> Result<Vec<ModernBertLayer>> {
    let global_rotary_emb = Arc::new(RotaryEmbedding::new(
        vb.dtype(),
        config,
        config.global_rope_theta,
        vb.device(),
    )?);
    let local_rotary_emb = Arc::new(RotaryEmbedding::new(
        vb.dtype(),
        config,
        config.local_rope_theta,
        vb.device(),
    )?);
    let mut layers = Vec::with_capacity(config.num_hidden_layers);
    for layer_id in 0..config.num_hidden_layers {
        let layer_uses_local_attention = layer_id % config.global_attn_every_n_layers != 0;
        layers.push(ModernBertLayer::load(
            vb.pp(format!("model.layers.{layer_id}")),
            config,
            if layer_uses_local_attention {
                local_rotary_emb.clone()
            } else {
                global_rotary_emb.clone()
            },
            layer_uses_local_attention,
            mapped,
        )?);
    }
    Ok(layers)
}

/// The Metal arena over the loaded weights; each layer's second norm is the next
/// layer's attention norm, or the final norm.
#[cfg(target_os = "macos")]
fn arena(
    device: &Device,
    config: &Config,
    word_embeddings: &Embeddings,
    norm: &FastNorm,
    layers: &[ModernBertLayer],
    final_norm: &FastNorm,
) -> Result<crate::semantic_arena::Arena> {
    use crate::semantic_arena::{Arena, Layer, View};
    let parts = layers
        .iter()
        .enumerate()
        .map(|(i, l)| -> Result<Layer> {
            let next = layers
                .get(i + 1)
                .and_then(|l| l.attn_norm.as_ref())
                .unwrap_or(final_norm);
            Ok(Layer {
                qkv: View::tensor(l.attn.qkv.weight()?)?,
                projection: View::tensor(l.attn.proj.weight()?)?,
                mlp_in: View::tensor(l.mlp.wi.weight()?)?,
                mlp_out: View::tensor(l.mlp.wo.weight()?)?,
                mlp_norm: View::tensor(&l.mlp_norm.weight)?,
                next_norm: View::tensor(&next.weight)?,
                cosine: View::tensor(&l.attn.rotary_emb.cos)?,
                sine: View::tensor(&l.attn.rotary_emb.sin)?,
                radius: l.uses_local_attention.then_some(l.local_radius),
            })
        })
        .collect::<Result<Vec<_>>>()?;
    Arena::new(
        device,
        View::tensor(word_embeddings.table()?)?,
        View::tensor(&norm.weight)?,
        parts,
        config.hidden_size,
        config.num_attention_heads,
        config.intermediate_size,
        config.layer_norm_eps as f32,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use candle_nn::Linear;
    #[cfg(target_os = "macos")]
    use cpu::{cpu_erf, vector_geglu};
    #[test]
    #[cfg(target_os = "macos")]
    fn vector_exponential_geglu_preserves_erf_values() {
        let src = (0..4096)
            .map(|i| (i % 257) as f32 / 16. - 8.)
            .collect::<Vec<_>>();
        let mut output = vec![0.; src.len() / 2];
        vector_geglu(&mut output, &src);
        for (i, actual) in output.iter().enumerate() {
            let x = src[i];
            let expected = (cpu_erf(x * std::f32::consts::FRAC_1_SQRT_2) + 1.)
                * 0.5
                * x
                * src[output.len() + i];
            assert!((actual - expected).abs() <= 8. * f32::EPSILON * expected.abs().max(1.));
        }
    }
    #[test]
    fn fused_cpu_geglu_matches_candle_in_serial_and_parallel_batches() {
        for rows in [3, 2048] {
            let values: Vec<f32> = (0..rows * 20).map(|i| (i % 127) as f32 / 8. - 7.).collect();
            let tensor = Tensor::from_vec(values, (rows, 20), &Device::Cpu).unwrap();
            let halves = tensor.chunk(2, D::Minus1).unwrap();
            let expected = (&halves[0].gelu_erf().unwrap() * &halves[1])
                .unwrap()
                .flatten_all()
                .unwrap()
                .to_vec1::<f32>()
                .unwrap();
            let actual = tensor
                .apply_op1_no_bwd(&CpuGeglu { vector: false })
                .unwrap()
                .flatten_all()
                .unwrap()
                .to_vec1::<f32>()
                .unwrap();
            #[cfg(not(target_os = "macos"))]
            assert_eq!(actual, expected);
            // Darwin and libm erf may round one ULP apart. The independent
            // full-model cosine/ordered-neighbor gate remains unchanged.
            #[cfg(target_os = "macos")]
            for (a, b) in actual.iter().zip(expected) {
                assert!((a - b).abs() <= 4.0 * f32::EPSILON * b.abs().max(1.0));
            }
        }
    }
    #[test]
    fn bounded_parallel_projection_preserves_values_and_row_order() {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(3)
            .build()
            .unwrap();
        for rows in [7, 513] {
            let input = Tensor::from_vec(
                (0..rows * 32)
                    .map(|i| (i % 101) as f32 / 101. - 0.5)
                    .collect::<Vec<_>>(),
                (rows, 32),
                &Device::Cpu,
            )
            .unwrap();
            let weight = Tensor::from_vec(
                (0..17 * 32)
                    .map(|i| (i % 47) as f32 / 47. - 0.5)
                    .collect::<Vec<_>>(),
                (17, 32),
                &Device::Cpu,
            )
            .unwrap();
            let linear = Linear::new(weight, None);
            let expected = input.apply(&linear).unwrap().to_vec2::<f32>().unwrap();
            let linear = Projection::Owned(linear);
            let actual = pool
                .install(|| projection(&input, &linear))
                .unwrap()
                .to_vec2::<f32>()
                .unwrap();
            for (a, b) in actual.iter().flatten().zip(expected.iter().flatten()) {
                assert!((a - b).abs() < 1e-6);
            }
        }
    }

    #[test]
    fn parallel_cpu_attention_matches_batched_attention() {
        let q = Tensor::from_vec(
            (0..2 * 3 * 5 * 4)
                .map(|x| x as f32 / 100.)
                .collect::<Vec<_>>(),
            (2, 3, 5, 4),
            &Device::Cpu,
        )
        .unwrap();
        let k = (&q * 0.4).unwrap();
        let v = (&q * 0.7).unwrap();
        let mask = Tensor::zeros((2, 1, 5, 5), DType::F32, &Device::Cpu).unwrap();
        let scores = q
            .matmul(&k.transpose(D::Minus2, D::Minus1).unwrap())
            .unwrap()
            .broadcast_add(&mask)
            .unwrap();
        let expected = softmax(&scores, D::Minus1)
            .unwrap()
            .matmul(&v)
            .unwrap()
            .flatten_all()
            .unwrap()
            .to_vec1::<f32>()
            .unwrap();
        let actual = cpu_attention(&q, &k, &v, &mask)
            .unwrap()
            .flatten_all()
            .unwrap()
            .to_vec1::<f32>()
            .unwrap();
        for (a, b) in actual.iter().zip(expected) {
            assert!((a - b).abs() < 1e-6);
        }
    }

    fn fixture_config() -> Config {
        Config {
            vocab_size: 11,
            hidden_size: 32,
            num_hidden_layers: 3,
            num_attention_heads: 2,
            intermediate_size: 48,
            max_position_embeddings: 512,
            layer_norm_eps: 1e-5,
            pad_token_id: 0,
            global_attn_every_n_layers: 3,
            global_rope_theta: 160_000.,
            local_attention: 8,
            local_rope_theta: 10_000.,
            classifier_config: None,
        }
    }

    /// A converted-weights file in the checkpoint's naming, with layer 0 lacking
    /// its attention norm as ModernBERT checkpoints do.
    fn write_weights(path: &std::path::Path, config: &Config, dtype: DType) {
        let mut state = 0x2545_f491_4f6c_dd1du64;
        let mut next = move || {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            (state >> 40) as f32 / (1u64 << 24) as f32 - 0.5
        };
        let (h, i) = (config.hidden_size, config.intermediate_size);
        let mut shapes = vec![
            (
                "embeddings.tok_embeddings.weight".to_string(),
                vec![config.vocab_size, h],
            ),
            ("embeddings.norm.weight".into(), vec![h]),
            ("final_norm.weight".into(), vec![h]),
        ];
        for layer in 0..config.num_hidden_layers {
            for (name, shape) in [
                ("attn.Wqkv", vec![3 * h, h]),
                ("attn.Wo", vec![h, h]),
                ("mlp.Wi", vec![2 * i, h]),
                ("mlp.Wo", vec![h, i]),
                ("mlp_norm", vec![h]),
            ] {
                shapes.push((format!("layers.{layer}.{name}.weight"), shape));
            }
            if layer > 0 {
                shapes.push((format!("layers.{layer}.attn_norm.weight"), vec![h]));
            }
        }
        let tensors: HashMap<String, Tensor> = shapes
            .into_iter()
            .map(|(name, shape)| {
                let values: Vec<f32> = (0..shape.iter().product::<usize>())
                    .map(|_| {
                        if name.contains("norm") {
                            1. + next() * 0.1
                        } else {
                            next() * 0.2
                        }
                    })
                    .collect();
                let tensor = Tensor::from_vec(values, shape, &Device::Cpu)
                    .unwrap()
                    .to_dtype(dtype)
                    .unwrap();
                (name, tensor)
            })
            .collect();
        candle_core::safetensors::save(&tensors, path).unwrap();
    }

    #[test]
    fn mapped_query_weights_embed_bit_for_bit_like_copied_weights() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("weights-f32.safetensors");
        let config = fixture_config();
        write_weights(&path, &config, DType::F32);
        let vb = || {
            unsafe { VarBuilder::from_mmaped_safetensors(&[&path], DType::F32, &Device::Cpu) }
                .unwrap()
                .rename_f(|name| checkpoint_name(name).to_owned())
        };
        let copied = ModernBert::load(vb(), &config, None).unwrap();
        let weights = MappedWeights::open(&path).unwrap();
        let mapped = ModernBert::load(vb(), &config, Some(&weights)).unwrap();
        drop(weights);
        // One short query, one long enough for the split parallel projection,
        // and a padded batch.
        for lengths in [vec![1], vec![7], vec![300], vec![9, 4]] {
            let width = *lengths.iter().max().unwrap();
            let mut ids = vec![0u32; lengths.len() * width];
            let mut mask = vec![0u32; ids.len()];
            for (row, &length) in lengths.iter().enumerate() {
                for token in 0..length {
                    ids[row * width + token] = ((row * 7 + token * 5 + 1) % 11) as u32;
                    mask[row * width + token] = 1;
                }
            }
            let ids = Tensor::from_vec(ids, (lengths.len(), width), &Device::Cpu).unwrap();
            let mask = Tensor::from_vec(mask, (lengths.len(), width), &Device::Cpu).unwrap();
            let run = |model: &ModernBert| {
                model
                    .forward(&ids, &mask, &lengths)
                    .unwrap()
                    .flatten_all()
                    .unwrap()
                    .to_vec1::<f32>()
                    .unwrap()
            };
            let (expected, actual) = (run(&copied), run(&mapped));
            assert!(expected.iter().all(|x| x.is_finite()));
            assert!(expected.iter().any(|x| x.abs() > 0.01));
            let bits = |v: &[f32]| v.iter().map(|x| x.to_bits()).collect::<Vec<_>>();
            assert_eq!(bits(&actual), bits(&expected), "lengths {lengths:?}");
        }
    }

    #[test]
    fn mapped_weights_accept_only_aligned_f32_tensors() {
        let dir = tempfile::tempdir().unwrap();
        let config = fixture_config();
        let f16 = dir.path().join("weights-f16.safetensors");
        write_weights(&f16, &config, DType::F16);
        assert!(MappedWeights::open(&f16).is_err());
        let f32 = dir.path().join("weights-f32.safetensors");
        write_weights(&f32, &config, DType::F32);
        let bytes = std::fs::read(&f32).unwrap();
        let truncated = dir.path().join("truncated.safetensors");
        std::fs::write(&truncated, &bytes[..bytes.len() - 4]).unwrap();
        assert!(MappedWeights::open(&truncated).is_err());
        let weights = MappedWeights::open(&f32).unwrap();
        let vb = unsafe { VarBuilder::from_mmaped_safetensors(&[&f32], DType::F32, &Device::Cpu) }
            .unwrap()
            .rename_f(|name| checkpoint_name(name).to_owned());
        // A checkpoint whose shapes disagree with the configuration is refused.
        let wider = Config {
            intermediate_size: 64,
            ..config
        };
        assert!(ModernBert::load(vb, &wider, Some(&weights)).is_err());
    }
}
