# tilt - Python semantic sidecar

This directory is the Python side of tilt: it turns the developer-chat messages
that `agrep index` produces into (a) semantic embeddings for similarity search and
(b) a graded affect read per message. The Rust core (`crates/agrep-core`) reads
the files written here.

The CLI creates and uses a smart-tier venv in agrep's per-user state dir. If you want
a repo-local dev venv for running these scripts by hand, set `AGREP_VENV_DIR=py/.venv`,
create it from the repo root, and use its interpreter:

```
# Windows
py/.venv/Scripts/python embed.py
# macOS / Linux
py/.venv/bin/python embed.py
```

Requirements: `transformers>=4.51.0` (needed for Qwen3), `sentence-transformers`,
`torch`, `numpy`, `scikit-learn`. A CUDA GPU speeds embeddings/affect up; everything
also runs on CPU (slower), and the code auto-detects which. None of this is needed for
the core explorer - see the repo-root README's tier table.

---

## The shared data contract

`data/` below means the active agrep data dir, which defaults to the OS user data
dir and can be overridden with `AGREP_DATA_DIR`.

| File | Format |
|---|---|
| `data/messages.jsonl` | one JSON per line: `{id, agent, project, session, ts, turn, who, text, model?, model_source}`. `id == "agent:session:turn"`. Produced by `agrep index`. `who=user` rows are real prompts; control/synthetic/recap/harness rows are searchable but excluded from model-attribution denominators. Non-user rows keep visible placeholder model buckets when no real model is available; `who` is the authoritative tag. |
| `data/replies.jsonl` | one JSON per line: `{id, reply}`. Agent replies are sidecar rows joined by `id`, so embedding/affect reads stay user-side and search can still emit `who=agent`. |
| `data/embeddings.f32` | raw little-endian `f32`, row-major, `N` rows × `D` cols. **Each row is L2-normalized.** `D = 256` (Matryoshka truncation of the model's native dim, then renormalized). |
| `data/embeddings.ids` | UTF-8, one message id per line. Row `r` of `embeddings.f32` ↔ line `r` here. **The ids file is the authority for row order** - it need not match `messages.jsonl` order. |
| `data/query.f32` | a single `D=256`-dim L2-normalized `f32` vector (the current search query). |
| `data/emotions.jsonl` | one JSON per line: `{id, rage_raw, hype_raw, top:[...], routed_to_judge}`. |

Because every stored row is L2-normalized, **cosine similarity == dot product** -
exactly what the Rust AVX2 brute-force kernel computes.

---

## How to run

### 1. Embed all messages

```
.venv/Scripts/python.exe embed.py
```

Reads `data/messages.jsonl`, embeds each message as a **passage**, truncates to
256-d, L2-normalizes, and writes `data/embeddings.f32` + `data/embeddings.ids`.
Prints model used, device, count, dim, elapsed, and approx VRAM (to stderr).

Quick check without embedding everything:

```
.venv/Scripts/python.exe embed.py --smoke 8
```

### 2. Embed a search query

```
.venv/Scripts/python.exe embed_query.py "why is the build still broken"
```

Applies the correct **query-side** prefix for whichever model is configured,
truncates to 256-d, normalizes, and writes `data/query.f32`. The Rust side then
dots `query.f32` against `embeddings.f32` to rank messages.

### 3. Affect gate

```
.venv/Scripts/python.exe emotion.py
.venv/Scripts/python.exe emotion.py --smoke 16
.venv/Scripts/python.exe emotion.py --profanity-ids data/profanity.ids
```

Reads `data/messages.jsonl`, runs a GoEmotions classifier, and writes
`data/emotions.jsonl` with per-message `rage_raw`, `hype_raw`, top-3 labels, and
a `routed_to_judge` flag.

Typical query flow end to end:

```
agrep index                                # (Rust) writes data/messages.jsonl
.venv/Scripts/python.exe embed.py          # writes embeddings.f32 + .ids
.venv/Scripts/python.exe embed_query.py "…" # writes query.f32
tilt search …                              # (Rust) AVX2 dot-product over the matrix
```

`embed.py` and `emotion.py` are independent - run them in either order.

---

## resolve-or-fallback model loading

Each script tries a **primary** model and, on any load failure (404, gated repo,
out-of-memory, load error), falls through to the next candidate, logging which
id was used. So a missing or gated Qwen3 repo degrades gracefully to a smaller
permissive model rather than crashing.

| Script | Primary | Fallbacks |
|---|---|---|
| `embed.py` / `embed_query.py` | `Qwen/Qwen3-Embedding-0.6B` (1024-d) | `BAAI/bge-base-en-v1.5` (768-d), `sentence-transformers/all-MiniLM-L6-v2` (384-d) |
| `emotion.py` | `cirimus/modernbert-base-go-emotions` | `SamLowe/roberta-base-go_emotions` |

**Model-specific embedding handling** (branched on which candidate loaded):

- **Qwen3-Embedding** - last-token pooling, **left padding**, and an asymmetric
  instruction: **passages get NO prefix**; **queries** get
  `Instruct: Retrieve developer-chat messages relevant to the query\nQuery: {q}`.
- **BGE** - mean pooling; passages no prefix; queries get
  `Represent this sentence for searching relevant passages: {q}`.
- **MiniLM** - mean pooling; no prefix on either side.

`embed_query.py` re-runs the same resolve list so the query is embedded by the
same model that produced the matrix, and applies the matching query prefix.

> Whichever model `embed.py` uses, `embed_query.py` must use the same one -
> cosine is only meaningful within a single embedding space. If you pin or
> change the candidate list, change it in `embed.py` (the single source of
> truth `embed_query.py` imports from).

---

## Affect gate details (`emotion.py`)

Multi-label GoEmotions (sigmoid per label, **graded** - not argmax). Per message:

- `rage_raw = anger + annoyance + disapproval + disgust + disappointment`
- `hype_raw = excitement + admiration + joy + approval + amusement`
- `top` = the 3 highest-scoring emotion labels
- `routed_to_judge = profanity-present AND ambiguous`, where *ambiguous* means
  rage and hype are within a small margin of each other, or both are weak.
  Profanity comes from `--profanity-ids` (an upstream set) when provided, else
  is recomputed from a small built-in lexicon.

**The LLM judge is a separate, later stage.** Messages flagged
`routed_to_judge=true` are intended to be handed to an LLM (Qwen3.5-4B) for a
final affect verdict. That judge is **not** implemented here - `emotion.py` only
scores and routes.

---

## File map

| File | Role |
|---|---|
| `common.py` | message loading; embeddings/ids/query writers (little-endian f32, L2-normalized rows); Matryoshka truncation; resolve-or-fallback model loader; device/VRAM helpers. |
| `embed.py` | embed every message (passages) → `embeddings.f32` + `.ids`. `--smoke N`. |
| `embed_query.py` | embed one query string (query prefix) → `query.f32`. |
| `emotion.py` | affect gate → `emotions.jsonl`. `--smoke N`, `--profanity-ids`. |
