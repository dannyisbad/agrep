//! Candle 0.9.2 ModernBERT backbone (MIT/Apache-2.0), with dtype-correct masks.
//! Preserves the checkpoint architecture; fused operations are gated against the F32 spike oracle.
//! ModernBERT
//!
//! ModernBERT is a modernized bidirectional encoder-only Transformer model.
//! - [Arxiv](https://arxiv.org/abs/2412.13663) "Smarter, Better, Faster, Longer: A Modern Bidirectional Encoder for Fast, Memory Efficient, and Long Context Finetuning and Inference"
//! - Upstream [GitHub repo](https://github.com/AnswerDotAI/ModernBERT).
//! - See modernbert in [candle-examples](https://github.com/huggingface/candle/tree/main/candle-examples/) for runnable code
//!

use candle_core::{DType, Device, IndexOp, Result, Tensor, D};
use candle_nn::{embedding, linear_no_bias, ops::softmax, Embedding, Linear, Module, VarBuilder};
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
        let max_seq_len = config.max_position_embeddings;
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
    qkv: Linear,
    proj: Linear,
    num_attention_heads: usize,
    attention_head_size: usize,
    rotary_emb: Arc<RotaryEmbedding>,
}

impl ModernBertAttention {
    fn load(vb: VarBuilder, config: &Config, rotary_emb: Arc<RotaryEmbedding>) -> Result<Self> {
        let num_attention_heads = config.num_attention_heads;
        let attention_head_size = config.hidden_size / config.num_attention_heads;

        let qkv = linear_no_bias(config.hidden_size, config.hidden_size * 3, vb.pp("Wqkv"))?;
        let proj = linear_no_bias(config.hidden_size, config.hidden_size, vb.pp("Wo"))?;

        Ok(Self {
            qkv,
            proj,
            num_attention_heads,
            attention_head_size,
            rotary_emb,
        })
    }

    fn forward(&self, hidden_states: &Tensor, attention_mask: &Tensor) -> Result<Tensor> {
        let xs = hidden_states.clone();
        let (b, seq_len, d) = xs.dims3()?;
        let qkv = xs
            .apply(&self.qkv)?
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

        let (q, k) = self.rotary_emb.apply_rotary_emb_qkv(&q, &k)?;

        let scale = (self.attention_head_size as f64).powf(-0.5);
        // Candle 0.9.2's full Metal SDPA kernel accepts the exact local/padding
        // mask, avoids materializing qk/softmax, and reduces command dispatch.
        // Its <=8-token vector kernel ignores explicit masks, so those inputs
        // retain the original attention path.
        let xs = if q.device().is_metal() && seq_len > 8 {
            // All-masked tiles must stay finite during online softmax rescaling.
            let finite_mask = attention_mask.clamp(-65504.0, 0.0)?;
            let mask = finite_mask.broadcast_as((b, self.num_attention_heads, seq_len, seq_len))?;
            candle_nn::ops::sdpa(&q, &k, &v, Some(&mask), false, scale as f32, 1.0)?
        } else {
            let q = (q * scale)?;
            if q.device().is_cpu() && b > 1 {
                cpu_attention(&q, &k, &v, attention_mask)?
            } else {
                let att = q.matmul(&k.transpose(D::Minus2, D::Minus1)?)?;
                let att = softmax(&att.broadcast_add(attention_mask)?, D::Minus1)?;
                att.matmul(&v)?
            }
        };

        let xs = xs.transpose(1, 2)?.reshape((b, seq_len, d))?;
        let xs = xs.apply(&self.proj)?;
        let xs = xs.reshape((b, seq_len, d))?;

        Ok(xs)
    }
}

// Accelerate's batched-matmul wrapper loops serially over small head matrices.
// Run independent heads in parallel; each head keeps exactly the same operations.
fn cpu_attention(q: &Tensor, k: &Tensor, v: &Tensor, mask: &Tensor) -> Result<Tensor> {
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
            softmax(&scores, D::Minus1)?.matmul(&v)
        })
        .collect::<Result<_>>()?;
    Tensor::stack(&values, 0)?.reshape((batch, heads, seq, width))
}

// Fusing the two elementwise passes also exposes independent rows to Rayon.
// The scalar expression deliberately matches Candle's GeluErf operation exactly.
struct CpuGeglu;
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
        let row = |dest: &mut [f32], src: &[f32]| {
            for (i, out) in dest.iter_mut().enumerate() {
                let x = src[i];
                let gelu = (candle_core::cpu::erf::erf_f32(x * std::f32::consts::FRAC_1_SQRT_2)
                    + 1.)
                    * 0.5
                    * x;
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

#[derive(Clone)]
pub struct ModernBertMLP {
    wi: Linear,
    wo: Linear,
}

impl ModernBertMLP {
    fn load(vb: VarBuilder, config: &Config) -> Result<Self> {
        let wi = linear_no_bias(
            config.hidden_size,
            config.intermediate_size * 2,
            vb.pp("Wi"),
        )?;
        let wo = linear_no_bias(config.intermediate_size, config.hidden_size, vb.pp("Wo"))?;
        Ok(Self { wi, wo })
    }
}

impl Module for ModernBertMLP {
    fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        let xs = xs.apply(&self.wi)?;
        let xs = if xs.device().is_cpu() && xs.dtype() == DType::F32 {
            xs.contiguous()?.apply_op1_no_bwd(&CpuGeglu)?
        } else {
            let halves = xs.chunk(2, D::Minus1)?;
            (&halves[0].gelu_erf()? * &halves[1])?
        };
        xs.apply(&self.wo)
    }
}

#[derive(Clone)]
pub struct ModernBertLayer {
    attn: ModernBertAttention,
    mlp: ModernBertMLP,
    attn_norm: Option<FastNorm>,
    mlp_norm: FastNorm,
    uses_local_attention: bool,
}

impl ModernBertLayer {
    fn load(
        vb: VarBuilder,
        config: &Config,
        rotary_emb: Arc<RotaryEmbedding>,
        uses_local_attention: bool,
    ) -> Result<Self> {
        let attn = ModernBertAttention::load(vb.pp("attn"), config, rotary_emb)?;
        let mlp = ModernBertMLP::load(vb.pp("mlp"), config)?;
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
        })
    }

    fn forward(
        &self,
        xs: &Tensor,
        global_attention_mask: &Tensor,
        local_attention_mask: &Tensor,
    ) -> Result<Tensor> {
        let residual = xs.clone();
        let mut xs = xs.clone();
        if let Some(norm) = &self.attn_norm {
            xs = xs.apply(norm)?;
        }

        let attention_mask = if self.uses_local_attention {
            &global_attention_mask.broadcast_add(local_attention_mask)?
        } else {
            global_attention_mask
        };
        let xs = self.attn.forward(&xs, attention_mask)?;
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
    word_embeddings: Embedding,
    norm: FastNorm,
    layers: Vec<ModernBertLayer>,
    final_norm: FastNorm,
    local_attention_size: usize,
    dtype: DType,
}

impl ModernBert {
    pub fn load(vb: VarBuilder, config: &Config) -> Result<Self> {
        let word_embeddings = embedding(
            config.vocab_size,
            config.hidden_size,
            vb.pp("model.embeddings.tok_embeddings"),
        )?;
        let norm = layer_norm_no_bias(
            config.hidden_size,
            config.layer_norm_eps,
            vb.pp("model.embeddings.norm"),
        )?;
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
            )?);
        }

        let final_norm = layer_norm_no_bias(
            config.hidden_size,
            config.layer_norm_eps,
            vb.pp("model.final_norm"),
        )?;

        Ok(Self {
            word_embeddings,
            norm,
            layers,
            final_norm,
            local_attention_size: config.local_attention,
            dtype: vb.dtype(),
        })
    }

    pub fn forward(&self, xs: &Tensor, mask: &Tensor) -> Result<Tensor> {
        let seq_len = xs.shape().dims()[1];
        let global_attention_mask =
            prepare_4d_attention_mask(mask, self.dtype, None)?.to_device(xs.device())?;
        let local_attention_mask =
            get_local_attention_mask(seq_len, self.local_attention_size / 2, xs.device())?
                .to_dtype(self.dtype)?;
        let mut xs = xs.apply(&self.word_embeddings)?.apply(&self.norm)?;
        for layer in self.layers.iter() {
            xs = layer.forward(&xs, &global_attention_mask, &local_attention_mask)?;
        }
        let xs = xs.apply(&self.final_norm)?;
        Ok(xs)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn fused_cpu_geglu_is_bit_identical_in_serial_and_parallel_batches() {
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
                .apply_op1_no_bwd(&CpuGeglu)
                .unwrap()
                .flatten_all()
                .unwrap()
                .to_vec1::<f32>()
                .unwrap();
            assert_eq!(actual, expected);
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
}
