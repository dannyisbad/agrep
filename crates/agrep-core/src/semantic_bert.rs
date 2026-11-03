//! Candle 0.9.2 ModernBERT backbone (MIT/Apache-2.0), with dtype-correct masks.
//! Preserves the checkpoint architecture; fused operations are gated against the F32 spike oracle.
//! ModernBERT
//!
//! ModernBERT is a modernized bidirectional encoder-only Transformer model.
//! - [Arxiv](https://arxiv.org/abs/2412.13663) "Smarter, Better, Faster, Longer: A Modern Bidirectional Encoder for Fast, Memory Efficient, and Long Context Finetuning and Inference"
//! - Upstream [GitHub repo](https://github.com/AnswerDotAI/ModernBERT).
//! - See modernbert in [candle-examples](https://github.com/huggingface/candle/tree/main/candle-examples/) for runnable code
//!

use crate::semantic_profile::stage;
use candle_core::{DType, Device, IndexOp, Result, Tensor, D};
use candle_nn::{embedding, linear_no_bias, ops::softmax, Embedding, Linear, Module, VarBuilder};
use serde::Deserialize;

use core::f32;
use std::collections::HashMap;
use std::sync::Arc;
fn option(name: &str, default: bool) -> bool {
    match std::env::var(name).as_deref() {
        Ok("0") => false,
        Ok("1") => true,
        _ => default,
    }
}

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
    qkv: Linear,
    proj: Linear,
    num_attention_heads: usize,
    attention_head_size: usize,
    rotary_emb: Arc<RotaryEmbedding>,
    metal_attention: Option<Arc<crate::semantic_attention_metal::Attention>>,
}

impl ModernBertAttention {
    fn load(
        vb: VarBuilder,
        config: &Config,
        rotary_emb: Arc<RotaryEmbedding>,
        metal_attention: Option<Arc<crate::semantic_attention_metal::Attention>>,
    ) -> Result<Self> {
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
            metal_attention,
        })
    }

    fn forward(
        &self,
        hidden_states: &Tensor,
        attention_mask: Option<&Tensor>,
        lengths: &[usize],
        radius: Option<usize>,
        tiled: bool,
        metal_lengths: Option<&crate::semantic_attention_metal::Lengths>,
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
            if q.device().is_metal() && tiled {
                return self
                    .metal_attention
                    .as_ref()
                    .ok_or_else(|| {
                        candle_core::Error::Msg("missing Metal attention pipeline".into())
                    })?
                    .forward(
                        &q,
                        &k,
                        &v,
                        metal_lengths.ok_or_else(|| {
                            candle_core::Error::Msg("missing Metal lengths".into())
                        })?,
                        radius,
                        scale as f32,
                    );
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

        let xs = if (xs.device().is_cpu() || xs.device().is_metal()) && tiled {
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

// BLAS is deliberately single-threaded in background workers. Share large
// projections across that same bounded Rayon pool, preserving each dot product
// and the input order. Small query matrices avoid scheduling/copy overhead.
fn projection(xs: &Tensor, linear: &Linear) -> Result<Tensor> {
    use rayon::prelude::*;
    if !xs.device().is_cpu() || xs.dtype() != DType::F32 || !xs.is_contiguous() {
        return xs.apply(linear);
    }
    let width = xs.dim(D::Minus1)?;
    let rows = xs.elem_count() / width;
    let workers = rayon::current_num_threads();
    if rows < 256 || workers == 1 {
        return xs.apply(linear);
    }
    let flat = xs.reshape((rows, width))?;
    let chunk = rows.div_ceil(workers).max(128);
    let parts = (0..rows.div_ceil(chunk))
        .into_par_iter()
        .map(|i| {
            flat.narrow(0, i * chunk, chunk.min(rows - i * chunk))?
                .apply(linear)
        })
        .collect::<Result<Vec<_>>>()?;
    let mut shape = xs.dims().to_vec();
    *shape.last_mut().unwrap() = linear.weight().dim(0)?;
    Tensor::cat(&parts, 0)?.reshape(shape)
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
            candle_nn::ops::softmax_last_dim(&scores.contiguous()?)?.matmul(&v)
        })
        .collect::<Result<_>>()?;
    Tensor::stack(&values, 0)?.reshape((batch, heads, seq, width))
}

// Fusing the two elementwise passes also exposes independent rows to Rayon.
// The scalar expression deliberately matches Candle's GeluErf operation exactly.
#[inline]
fn cpu_erf(x: f32) -> f32 {
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
fn vector_geglu(dest: &mut [f32], src: &[f32]) {
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
struct CpuGeglu {
    vector: bool,
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

#[derive(Clone)]
pub struct ModernBertMLP {
    wi: Linear,
    wo: Linear,
    metal_geglu: Option<Arc<crate::semantic_metal::Geglu>>,
    vector_erf: bool,
}

impl ModernBertMLP {
    fn load(
        vb: VarBuilder,
        config: &Config,
        metal_geglu: Option<Arc<crate::semantic_metal::Geglu>>,
    ) -> Result<Self> {
        let wi = linear_no_bias(
            config.hidden_size,
            config.intermediate_size * 2,
            vb.pp("Wi"),
        )?;
        let wo = linear_no_bias(config.intermediate_size, config.hidden_size, vb.pp("Wo"))?;
        Ok(Self {
            wi,
            wo,
            metal_geglu,
            vector_erf: cfg!(target_os = "macos") && option("AGREP_SEM_VECTOR_ERF", true),
        })
    }
}

impl Module for ModernBertMLP {
    fn forward(&self, xs: &Tensor) -> Result<Tensor> {
        let xs = stage(xs.device(), "mlp_in", || projection(xs, &self.wi))?;
        let xs = stage(xs.device(), "geglu", || {
            if xs.device().is_cpu() && xs.dtype() == DType::F32 {
                xs.contiguous()?.apply_op1_no_bwd(&CpuGeglu {
                    vector: self.vector_erf,
                })
            } else if let Some(op) = &self.metal_geglu {
                xs.contiguous()?.apply_op1_no_bwd(op.as_ref())
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
        metal_geglu: Option<Arc<crate::semantic_metal::Geglu>>,
        metal_attention: Option<Arc<crate::semantic_attention_metal::Attention>>,
    ) -> Result<Self> {
        let attn = ModernBertAttention::load(vb.pp("attn"), config, rotary_emb, metal_attention)?;
        let mlp = ModernBertMLP::load(vb.pp("mlp"), config, metal_geglu)?;
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

    fn forward(
        &self,
        xs: &Tensor,
        global_attention_mask: Option<&Tensor>,
        local_attention_mask: Option<&Tensor>,
        lengths: &[usize],
        tiled: bool,
        metal_lengths: Option<&crate::semantic_attention_metal::Lengths>,
    ) -> Result<Tensor> {
        let residual = xs.clone();
        let mut xs = xs.clone();
        if let Some(norm) = &self.attn_norm {
            xs = xs.apply(norm)?;
        }

        let attention_mask = match (global_attention_mask, local_attention_mask) {
            (Some(global), Some(local)) if self.uses_local_attention => {
                Some(global.broadcast_add(local)?)
            }
            (Some(global), _) => Some(global.clone()),
            _ => None,
        };
        let radius = self.uses_local_attention.then_some(self.local_radius);
        let xs = self.attn.forward(
            &xs,
            attention_mask.as_ref(),
            lengths,
            radius,
            tiled,
            metal_lengths,
        )?;
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
    tiled_attention: bool,
    fused_residual: bool,
    residual_kernel: Option<Arc<crate::semantic_norm::Kernel>>,
    #[cfg(target_os = "macos")]
    arena: Option<Arc<std::sync::Mutex<crate::semantic_arena::Arena>>>,
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

        let arena_selected = cfg!(target_os = "macos")
            && vb.device().is_metal()
            && matches!(
                std::env::var("AGREP_SEM_ARENA")
                    .unwrap_or_else(|_| "2".into())
                    .as_str(),
                "1" | "2"
            );
        let mut layers = Vec::with_capacity(config.num_hidden_layers);
        // Embedder routes arena models before tensor-forward. Compiling the
        // inactive tensor shaders adds three avoidable compiler-service calls.
        let metal_geglu = if arena_selected {
            None
        } else {
            crate::semantic_metal::Geglu::new(vb.device())?
        };
        let tiled_attention = crate::semantic_attention::kind() != 0;
        let fused_residual = option("AGREP_SEM_FUSED_RESIDUAL", cfg!(target_os = "macos"));
        let residual_kernel = if fused_residual && !arena_selected {
            crate::semantic_norm::Kernel::new(vb.device())?
        } else {
            None
        };
        let metal_attention = if tiled_attention && !arena_selected {
            crate::semantic_attention_metal::Attention::new(vb.device())?
        } else {
            None
        };
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
                metal_geglu.clone(),
                metal_attention.clone(),
            )?);
        }

        let final_norm = layer_norm_no_bias(
            config.hidden_size,
            config.layer_norm_eps,
            vb.pp("model.final_norm"),
        )?;

        #[cfg(target_os = "macos")]
        let arena = if arena_selected {
            use crate::semantic_arena::{Arena, Layer, View};
            let parts = layers
                .iter()
                .enumerate()
                .map(|(i, l)| -> Result<Layer> {
                    let next = layers
                        .get(i + 1)
                        .and_then(|l| l.attn_norm.as_ref())
                        .unwrap_or(&final_norm);
                    Ok(Layer {
                        qkv: View::tensor(l.attn.qkv.weight())?,
                        projection: View::tensor(l.attn.proj.weight())?,
                        mlp_in: View::tensor(l.mlp.wi.weight())?,
                        mlp_out: View::tensor(l.mlp.wo.weight())?,
                        mlp_norm: View::tensor(&l.mlp_norm.weight)?,
                        next_norm: View::tensor(&next.weight)?,
                        cosine: View::tensor(&l.attn.rotary_emb.cos)?,
                        sine: View::tensor(&l.attn.rotary_emb.sin)?,
                        radius: l.uses_local_attention.then_some(l.local_radius),
                    })
                })
                .collect::<Result<Vec<_>>>()?;
            Some(Arc::new(std::sync::Mutex::new(Arena::new(
                vb.device(),
                View::tensor(word_embeddings.embeddings())?,
                View::tensor(&norm.weight)?,
                parts,
                config.hidden_size,
                config.num_attention_heads,
                config.intermediate_size,
                config.layer_norm_eps as f32,
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
            tiled_attention,
            fused_residual,
            residual_kernel,
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
    pub(crate) fn uses_arena(&self) -> bool {
        #[cfg(target_os = "macos")]
        {
            self.arena.is_some()
        }
        #[cfg(not(target_os = "macos"))]
        {
            false
        }
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
        let metal_lengths = if self.tiled_attention && xs.device().is_metal() {
            Some(crate::semantic_attention_metal::Lengths::new(
                xs.device(),
                lengths,
            )?)
        } else {
            None
        };
        let (global_attention_mask, local_attention_mask) = if self.tiled_attention
            && (xs.device().is_cpu() || xs.device().is_metal())
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
        let mut xs = xs.apply(&self.word_embeddings)?.apply(&self.norm)?;
        if self.fused_residual {
            // Carry each residual and its pre-normalized view into the next layer.
            // The packed pair consists of two contiguous views of one allocation.
            let mut normalized = xs.clone();
            for (index, layer) in self.layers.iter().enumerate() {
                let (residual, next_normalized) = stage(xs.device(), "layer", || {
                    let mask = match (&global_attention_mask, &local_attention_mask) {
                        (Some(global), Some(local)) if layer.uses_local_attention => {
                            Some(global.broadcast_add(local)?)
                        }
                        (Some(global), _) => Some(global.clone()),
                        _ => None,
                    };
                    let attention = layer.attn.forward(
                        &normalized,
                        mask.as_ref(),
                        lengths,
                        layer.uses_local_attention.then_some(layer.local_radius),
                        self.tiled_attention,
                        metal_lengths.as_ref(),
                    )?;
                    let (residual, mlp_input) = stage(xs.device(), "residual_norm", || {
                        crate::semantic_norm::forward(
                            &attention,
                            &xs,
                            &layer.mlp_norm.weight,
                            layer.mlp_norm.eps,
                            self.residual_kernel.as_deref(),
                        )
                    })?;
                    let mlp = layer.mlp.forward(&mlp_input)?;
                    let next_norm = self
                        .layers
                        .get(index + 1)
                        .and_then(|next| next.attn_norm.as_ref())
                        .unwrap_or(&self.final_norm);
                    stage(xs.device(), "residual_norm", || {
                        crate::semantic_norm::forward(
                            &mlp,
                            &residual,
                            &next_norm.weight,
                            next_norm.eps,
                            self.residual_kernel.as_deref(),
                        )
                    })
                })?;
                xs = residual;
                normalized = next_normalized;
            }
            return Ok(normalized);
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
                    metal_lengths.as_ref(),
                )
            })?;
        }
        let xs = xs.apply(&self.final_norm)?;
        Ok(xs)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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
}
