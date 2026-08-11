"""ModernBERT encoder in MLX, depending on nothing but `mlx`.

WHY VENDOR THIS. The obvious way to reach Metal was `mlx-embeddings`, which
works - but it requires `transformers`, which in turn drags starlette, uvicorn
and typer into a CLI that searches chat logs, and pins `tokenizers` below the
version agrep's base install resolves (0.23.1 -> 0.22.2). Paying a web-server
stack and a downgrade of a core dependency to run twelve transformer layers is
the wrong trade for an optional accelerator. The encoder itself is small and
completely specified by the checkpoint, so agrep owns it.

WHAT IT MUST MATCH. Not "a ModernBERT" - the exact arithmetic of the ONNX
graph already serving rows into the index, because both lanes write vectors to
one space. The details that are easy to get subtly wrong, all taken from the
checkpoint config rather than memory:

  - alternating attention: every `global_attn_every_n_layers`-th layer sees the
    whole sequence, the rest see a +/- (local_attention // 2) window
  - two RoPE bases: global layers use `global_rope_theta`, local ones
    `local_rope_theta` - one base for both silently degrades long rows
  - layer 0 has NO attention norm (identity), unlike every other layer
  - GeGLU: Wi emits 2x intermediate, split into (input, gate), and the
    activation lands on `input`, with `gate` multiplied in unmodified
  - LayerNorm carries weight but no bias throughout

Correctness is not asserted by reading this file: embedder.py refuses to use
the lane unless it reproduces the ONNX vectors numerically on probe text.
"""
from __future__ import annotations

import json
from pathlib import Path

import mlx.core as mx
import mlx.nn as nn


class ModernBertMLP(nn.Module):
    """GeGLU block: one projection, split in half, gated."""

    def __init__(self, hidden: int, intermediate: int, bias: bool):
        super().__init__()
        self.Wi = nn.Linear(hidden, intermediate * 2, bias=bias)
        self.Wo = nn.Linear(intermediate, hidden, bias=bias)
        self.act = nn.GELU(approx="precise")

    def __call__(self, x):
        proj = self.Wi(x)
        half = proj.shape[-1] // 2
        gated, gate = proj[..., :half], proj[..., half:]
        return self.Wo(self.act(gated) * gate)


class ModernBertAttention(nn.Module):
    """Multi-head attention; local or global depending on the layer index."""

    def __init__(self, cfg: dict, layer_id: int):
        super().__init__()
        hidden = cfg["hidden_size"]
        self.heads = cfg["num_attention_heads"]
        self.head_dim = hidden // self.heads
        self.scale = self.head_dim ** -0.5
        bias = cfg.get("attention_bias", False)
        self.Wqkv = nn.Linear(hidden, 3 * hidden, bias=bias)
        self.Wo = nn.Linear(hidden, hidden, bias=bias)
        # A layer is global on multiples of the stride, local otherwise; the
        # two use DIFFERENT rope bases, which is the part worth being explicit
        # about since a single base still produces plausible-looking vectors.
        self.is_global = layer_id % cfg.get("global_attn_every_n_layers", 3) == 0
        theta = (cfg.get("global_rope_theta", 160000.0) if self.is_global
                 else cfg.get("local_rope_theta", 10000.0))
        self.rope = nn.RoPE(dims=self.head_dim, base=theta)

    def __call__(self, x, global_mask, local_mask):
        batch, seq, _ = x.shape
        qkv = self.Wqkv(x).reshape(batch, seq, 3, self.heads, self.head_dim)
        qkv = qkv.transpose(0, 3, 2, 1, 4)          # b, heads, 3, seq, dim
        q, k, v = [qkv[:, :, i] for i in range(3)]
        q, k = self.rope(q), self.rope(k)
        mask = global_mask if self.is_global else local_mask
        out = mx.fast.scaled_dot_product_attention(
            q, k, v, scale=self.scale, mask=mask)
        out = out.transpose(0, 2, 1, 3).reshape(batch, seq, -1)
        return self.Wo(out)


class ModernBertLayer(nn.Module):
    def __init__(self, cfg: dict, layer_id: int):
        super().__init__()
        hidden = cfg["hidden_size"]
        eps = cfg.get("norm_eps", 1e-5)
        norm_bias = cfg.get("norm_bias", False)
        # Layer 0 is pre-normalized by the embedding norm, so it carries an
        # identity here; giving it a real LayerNorm would double-normalize.
        self.attn_norm = (nn.Identity() if layer_id == 0
                          else nn.LayerNorm(hidden, eps=eps, bias=norm_bias))
        self.attn = ModernBertAttention(cfg, layer_id)
        self.mlp_norm = nn.LayerNorm(hidden, eps=eps, bias=norm_bias)
        self.mlp = ModernBertMLP(hidden, cfg["intermediate_size"],
                                 cfg.get("mlp_bias", False))

    def __call__(self, x, global_mask, local_mask):
        x = x + self.attn(self.attn_norm(x), global_mask, local_mask)
        return x + self.mlp(self.mlp_norm(x))


class ModernBertEmbeddings(nn.Module):
    def __init__(self, cfg: dict):
        super().__init__()
        self.tok_embeddings = nn.Embedding(cfg["vocab_size"], cfg["hidden_size"])
        self.norm = nn.LayerNorm(cfg["hidden_size"],
                                 eps=cfg.get("norm_eps", 1e-5),
                                 bias=cfg.get("norm_bias", False))

    def __call__(self, ids):
        return self.norm(self.tok_embeddings(ids))


class ModernBertEncoder(nn.Module):
    """The encoder, ending at CLS - agrep's pooling contract, not a choice.

    Deliberately has no pooling switch. The library this replaces defaulted to
    mean pooling and would silently produce a different vector space than the
    index it was writing into; here CLS is the only thing the class can do.
    """

    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.embeddings = ModernBertEmbeddings(cfg)
        self.layers = [ModernBertLayer(cfg, i)
                       for i in range(cfg["num_hidden_layers"])]
        self.final_norm = nn.LayerNorm(cfg["hidden_size"],
                                       eps=cfg.get("norm_eps", 1e-5),
                                       bias=cfg.get("norm_bias", False))
        self.window = cfg.get("local_attention", 128) // 2

    def _masks(self, attention_mask, dtype):
        """Additive masks: padding everywhere, plus a band for local layers.

        Built in the activation dtype because fast SDPA refuses a mask that
        does not promote to the output type, and -1e9 in float16 saturates to
        -inf, which is the intended "never attend here" either way.
        """
        finfo_min = -1e4 if dtype == mx.float16 else -1e9
        pad = mx.where(attention_mask == 1, 0.0, finfo_min)[:, None, None, :]
        seq = attention_mask.shape[1]
        rows = mx.arange(seq)
        distance = mx.abs(rows[None, :] - rows[:, None])
        band = mx.where(distance <= self.window, 0.0, finfo_min)[None, None]
        return pad.astype(dtype), (pad + band).astype(dtype)

    def __call__(self, input_ids, attention_mask):
        x = self.embeddings(input_ids)
        global_mask, local_mask = self._masks(attention_mask, x.dtype)
        for layer in self.layers:
            x = layer(x, global_mask, local_mask)
        # CLS row, after the final norm. Unnormalized on purpose: the caller
        # owns layernorm/truncation/normalization so both lanes share one tail.
        return self.final_norm(x)[:, 0, :]


def load_encoder(weights_path: Path, config_path: Path,
                 dtype=mx.float16) -> ModernBertEncoder:
    """Build the encoder from a safetensors checkpoint and its config.

    Refuses unknown architectures rather than loading whatever is on disk:
    a checkpoint that is not ModernBERT would otherwise fill these modules
    with mismatched tensors and fail far away from the cause.
    """
    cfg = json.loads(Path(config_path).read_text())
    if cfg.get("model_type") != "modernbert":
        raise ValueError(
            f"expected a modernbert checkpoint, got {cfg.get('model_type')!r}")
    model = ModernBertEncoder(cfg)
    weights = mx.load(str(weights_path))
    # The checkpoint stores no MLM head for this model, but tolerate one:
    # anything the encoder does not declare is not ours to load.
    known = dict(_flat_keys(model))
    missing = [k for k in known if k not in weights]
    if missing:
        raise ValueError(f"checkpoint is missing {len(missing)} tensors, "
                         f"first: {missing[:3]}")
    model.load_weights([(k, weights[k]) for k in known], strict=False)
    model.set_dtype(dtype)
    mx.eval(model.parameters())
    return model


def _flat_keys(module) -> list[tuple[str, tuple]]:
    """Parameter names in checkpoint order, so a mismatch names itself."""
    from mlx.utils import tree_flatten
    return [(k, v.shape) for k, v in tree_flatten(module.parameters())]
