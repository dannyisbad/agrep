# Embedder selection

This is agrep's permanent, fail-closed embedder bakeoff. The source survey was
refreshed on 2026-07-21 from publisher model cards and ONNX Runtime documentation.
The survey is an admission decision, not a benchmark result. A model wins only after
the frozen private-evaluation measurements and name-blind review below.

## Decision boundary

The production target is local inference on macOS Apple Silicon and ordinary
Windows systems. CPUExecutionProvider is the common baseline. Accelerator results
are reported separately: ONNX Runtime exposes a
[Core ML provider](https://onnxruntime.ai/docs/execution-providers/CoreML-ExecutionProvider.html)
on macOS, but provider support and speed are graph-dependent, while
[DirectML is in sustained engineering](https://onnxruntime.ai/docs/install/) and
WinML is recommended for new Windows integrations. No candidate gets an adoption
credit merely because an accelerator exists.

That rule is exactly why the incumbent's GPU lane is not a selection argument.
The winning profile is additionally served on Apple silicon by an MLX fp16
engine measured at 10.9x the int8 CPU lane, but it earned that by reproducing
the CPU lane's vectors to a 0.99933 worst-case cosine under a gate that refuses
to open otherwise, not by existing. It changes what a backfill costs, not which
model wins: every gate below is measured on the portable CPU baseline so that
candidates stay comparable on hardware every user has. `bench/EMBED_SPEED.md`
holds the lane measurement, its parity gate, and the store-identity rules.

The manifest deliberately caps every tested transcript at 1,024 tokens unless a
profile says otherwise. `native_max_seq` describes the publisher's model limit;
`runtime_profile.max_seq` is agrep's tested cap. This distinction prevents a long
context claim from being mistaken for the cost paid on every message.

License status is separate from runtime status:

- **Eligible** means a permissive model license, a pinned portable ONNX graph, and a
  source-resolved query/document contract. It admits a model to the bakeoff; actual
  adoption still requires physical macOS and Windows runtime gates.
- **Comparator only** means the model is measured but cannot be shipped as agrep's
  default under the permissive-license rule.
- **Blocked** means it is interesting, but no reproducible production profile exists.
  A blocked profile cannot win even when its source license is permissive.

## Admitted model families

All pooling, prefix, dimension, context, language, and license claims below come from
the linked publisher card. Artifact revisions point to the exact repositories used by
`embedder_profiles.json`.

| Family and profiles | Status | Source contract and reason for admission |
| --- | --- | --- |
| [Granite small English R2](https://huggingface.co/ibm-granite/granite-embedding-small-english-r2), `granite-small-r2-q8-384` | Eligible baseline, Apache-2.0 | English, 47M parameters, 384 dimensions, CLS pooling, no prefix, and 8,192 native tokens. The harness caps it at 1,024. IBM reports code and long-document retrieval results, so this is the quality and footprint control. The pinned q8 graph comes from the [ONNX conversion repository](https://huggingface.co/onnx-community/granite-embedding-small-english-r2-ONNX). |
| [Static Retrieval MRL English v1](https://huggingface.co/sentence-transformers/static-retrieval-mrl-en-v1), `static-retrieval-mrl-en-v1-256` | Eligible, Apache-2.0 | English, symmetric retrieval with no prefixes, native 1,024 dimensions, and a publisher-supported 256-dimension MRL slice. Its static token-embedding architecture directly targets corpus-build and incremental-update cost without another transformer attention stack. The pinned int8 graph exposes direct `sentence_embedding` output, which agrep normalizes; the harness conservatively caps both tested and native context at 1,024 tokens. Publisher-reported CPU throughput and NanoBEIR quality justify admission only: they are not agrep adoption evidence. |
| [EmbeddingGemma 300M](https://huggingface.co/google/embeddinggemma-300m), `embeddinggemma-300m-{768,256}` | **Comparator only**, Gemma license | Multilingual, 2,048 native tokens, 768 dimensions with published 512/256/128 MRL truncations. The [ONNX card](https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX) exposes normalized `sentence_embedding` output and specifies query prefix `task: search result \| query: ` and document prefix `title: none \| text: `. It is mandatory as a quality comparator but cannot be adopted because access is conditioned on the Gemma terms rather than a permissive OSS license. |
| [Nomic Embed Text v1.5](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5), `nomic-embed-text-v1.5-{768,384,256}` | Eligible, Apache-2.0 | English, native 768 dimensions, mean pooling, and `search_query: ` / `search_document: ` prefixes. MRL requires layer normalization, truncation, then L2 normalization. The card reports 768/512/256/128/64; 384 is an explicitly experimental intermediate truncation whose quality must be established here, not attributed to the publisher. The card describes dynamic-RoPE scaling to 8,192 tokens; agrep caps the pinned graph at 1,024. |
| [BGE base English v1.5](https://huggingface.co/BAAI/bge-base-en-v1.5), `bge-base-en-v1.5-768` | Eligible, MIT | English, 109M parameters, 768 dimensions, 512 tokens, CLS pooling. For short-query retrieval the card recommends query-only prefix `Represent this sentence for searching relevant passages: ` and no passage prefix. The pinned source graph is FP32, making this a deliberately expensive quality control rather than a footprint favorite. |
| [Snowflake Arctic Embed S](https://huggingface.co/Snowflake/snowflake-arctic-embed-s), `snowflake-arctic-embed-s-384` | Eligible, Apache-2.0 | English, 33M parameters, 384 dimensions, 512 tokens, CLS pooling, the same query-only retrieval prefix as BGE, and no document prefix. Snowflake recommends its newer v2 family for quality, but this small model remains useful as a lower-footprint control. |
| [Granite 97M Multilingual R2](https://huggingface.co/ibm-granite/granite-embedding-97m-multilingual-r2), `granite-embedding-97m-multilingual-r2-384` | Eligible, Apache-2.0 | 384 dimensions, CLS pooling, no prefixes, 32,768 native tokens, 200+ pretrained languages with enhanced retrieval training for 52 languages, and explicit training on nine programming languages. IBM positions it for latency-sensitive production. The harness uses a pinned weight-only Q4 conversion and caps it at 1,024 tokens. |
| [GTE ModernBERT Base](https://huggingface.co/Alibaba-NLP/gte-modernbert-base), `gte-modernbert-base-768` | Eligible, Apache-2.0 | English, 149M parameters, 768 dimensions, 8,192 native tokens, CLS pooling, and no retrieval prefix. The publisher reports code-retrieval and long-document evaluations and provides an ONNX artifact; agrep caps it at 1,024. |
| [Granite 311M Multilingual R2](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2), `granite-embedding-311m-multilingual-r2-384` | Eligible, Apache-2.0 | Native 768 dimensions with published 512/384/256/128 MRL truncations, CLS pooling, no prefixes, 32,768 native tokens, and the same multilingual/code scope as the 97M model. The harness tests its 384-dimension MRL slice at a 1,024-token cap. |
| [Snowflake Arctic Embed M v2](https://huggingface.co/Snowflake/snowflake-arctic-embed-m-v2.0), `snowflake-arctic-embed-m-v2-256` | Eligible, Apache-2.0 | Multilingual, 305M total / 113M non-embedding parameters, 768 native dimensions with a publisher-evaluated 256-dimension MRL slice, 8,192 native tokens, source-defined CLS pooling, query prefix `query: `, and no document prefix. The pinned export's direct `sentence_embedding` output was verified identical to normalized `token_embeddings[:, 0]`, including after 256-dimension truncation, so the harness selects it to avoid materializing the full token tensor. |
| [Qwen3 Embedding 0.6B](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B), `qwen3-embedding-0.6b-384` | Eligible, Apache-2.0 | Multilingual and code-aware, 0.6B parameters, 1,024 native dimensions with arbitrary MRL truncation, 32,768 native tokens, last-token pooling, and a query-only task instruction. The q4f16 [ONNX conversion](https://huggingface.co/onnx-community/Qwen3-Embedding-0.6B-ONNX) is much heavier than the other candidates, but its 384-dimensional slice meets the index-size gate and its published retrieval quality makes the CPU/footprint tradeoff worth measuring rather than guessing. |
| [F2LLM v2 80M](https://huggingface.co/codefuse-ai/F2LLM-v2-80M), `f2llm-v2-80m-320` | **Blocked**, Apache-2.0 | The current publisher card resolves the semantic contract: 320 dimensions, last non-padding/EOS-token pooling, query prompt `Instruct: Given a question, retrieve passages that can help answer the question.\nQuery: `, and unprefixed documents. It remains blocked because the source repository does not provide a pinned official ONNX graph and agrep has not validated a portable export. Its old “pooling conflict” blocker is stale; lack of a reproducible ONNX artifact is the remaining blocker. |

## Immutable artifact pins

The manifest is the executable source of truth for complete SHA-256 hashes, file
sizes, tokenizer pins, and output selectors. On 2026-07-21, all files staged for the
14 runnable profiles matched every declared byte size and SHA-256. The short hashes
below are only readable labels for the full immutable revisions.

| Artifact family | Pinned revision | Quantized/model bytes |
| --- | --- | ---: |
| Granite small English R2 ONNX | [`1dc7835`](https://huggingface.co/onnx-community/granite-embedding-small-english-r2-ONNX/tree/1dc7835ba0cb9c76a3618d0bf0c427c97671b3c8) | 52,484,470 |
| Static Retrieval MRL English v1 | [`f60985c`](https://huggingface.co/sentence-transformers/static-retrieval-mrl-en-v1/tree/f60985c706f192d45d218078e49e5a8b6f15283a) | 31,259,319 |
| EmbeddingGemma ONNX | [`5090578`](https://huggingface.co/onnx-community/embeddinggemma-300m-ONNX/tree/5090578d9565bb06545b4552f76e6bc2c93e4a66) | 309,458,498 |
| Nomic Embed Text v1.5 | [`e9b6763`](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/tree/e9b6763023c676ca8431644204f50c2b100d9aab) | 165,113,221 |
| BGE base English v1.5 | [`a5beb1e`](https://huggingface.co/BAAI/bge-base-en-v1.5/tree/a5beb1e3e68b9ab74eb54cfd186867f64f240e1a) | 435,811,539 |
| Snowflake Arctic Embed S | [`e596f50`](https://huggingface.co/Snowflake/snowflake-arctic-embed-s/tree/e596f507467533e48a2e17c007f0e1dacc837b33) | 34,015,111 |
| Granite 97M Multilingual R2 ONNX | [`536a9f2`](https://huggingface.co/onnx-community/granite-embedding-97m-multilingual-r2-ONNX/tree/536a9f241cb3f02a9c5995a1e708c784bd274859) | 294,460,363 |
| GTE ModernBERT Base | [`e7f32e3`](https://huggingface.co/Alibaba-NLP/gte-modernbert-base/tree/e7f32e3c00f91d699e8c43b53106206bcc72bb22) | 150,218,016 |
| Granite 311M Multilingual R2 | [`4439955`](https://huggingface.co/ibm-granite/granite-embedding-311m-multilingual-r2/tree/44399559930365213510b1ee2eb15ded83374f0e) | 313,421,909 |
| Snowflake Arctic Embed M v2 | [`95c2741`](https://huggingface.co/Snowflake/snowflake-arctic-embed-m-v2.0/tree/95c2741480856aa9666782eb4afe11959938017f) | 310,916,060 |
| Qwen3 Embedding 0.6B ONNX | [`c25a394`](https://huggingface.co/onnx-community/Qwen3-Embedding-0.6B-ONNX/tree/c25a394dd583836952667c12f008335071b3f43d) | 567,458,583 |
| F2LLM v2 80M source only | [`f4a16a1`](https://huggingface.co/codefuse-ai/F2LLM-v2-80M/tree/f4a16a11c9f5c8c7e22694653de6ce75430f4538) | no admitted ONNX artifact |

## Pin and contract audit

The source pass caught three executable-profile mismatches. They were corrected
before the bakeoff:

1. Granite 311M now records `native_dim: 768`, `dim: 384`, matching IBM's card and
   the pinned graph's `last_hidden_state[..., 768]` output.
2. Arctic M v2 now selects the pinned export's `sentence_embedding` output. A local
   conformance probe found cosine 1.0 against normalized `token_embeddings[:, 0]`
   and identical normalized 256-dimensional vectors and scores.
3. Granite small now records the publisher's 8,192-token native limit while keeping
   agrep's deliberate 1,024-token runtime cap.

Static Retrieval's publisher reports 397x CPU throughput relative to MPNet, 87.4%
of MPNet's NanoBEIR quality, and 0.4819 NDCG@10 at 256 dimensions. Those numbers
only motivate testing this distinct architecture. Adoption still requires agrep's
frozen private-evaluation quality, gibberish separation, batch coherence, scale, footprint,
and physical macOS and Windows gates.

Two source-status caveats remain:

1. Nomic's 384-dimension row is not one of the dimensions for which the publisher
   reports MTEB results. It is valid to measure as a truncation experiment, but it
   must not be described as a publisher-validated operating point.
2. F2LLM's official card now makes last-token pooling and the retrieval prompt clear.
   The candidate stays blocked for missing official ONNX, not unresolved pooling;
   any older manifest wording about a pooling conflict is stale.

Nomic's official dynamic-int8 graph was rejected during preflight. The same text
moved to cosine 0.9899-0.9923 when embedded beside other equal-token-length rows,
so an incremental update could disagree with a full rebuild. Its pinned weight-only
Q4 graph held at cosine 0.99999994-1.0 and ran about twice as many sampled rows per
second on the Apple-silicon test host. The smaller Q4F16 graph was also rejected:
ONNX Runtime CPU failed while initializing its fused LayerNorm graph. Only the Q4
artifact is admitted for Nomic.

Granite 97M's official AVX2 int8 graph was also rejected: mixed-versus-solo
cosine fell to 0.9831, including 0.9825 with equal-length rows. IBM's FP32 graph
was stable but reached about 1.27 GiB peak RSS in the focused smoke. The pinned
weight-only Q4 conversion held at cosine 0.9999998-1.0, used about 1.17 GiB peak
RSS in the same smoke, and is the profile admitted to the full campaign.

The compact Granite 311M and Arctic M v2 dynamic-int8 graphs remain quality
comparators, but their Mac CPU mixed-versus-solo cosines of 0.9965 and 0.9971
miss the 0.998 coherence gate. Their full-precision graphs are 1.25 GB and
1.23 GB, respectively. Neither can be adopted from this campaign without a
portable stable graph and physical evidence on both supported platforms.

Qwen's dynamic-int8 graph was rejected during preflight: corrected solo-versus-mixed
batch vectors moved to cosine 0.9856-0.9894 because activation scales depend on batch
neighbors. The pinned q4f16 graph is weight-only, was bit-stable across the same
mixed batches, and is the only Qwen artifact admitted to the campaign.

## Time-boxed production decision

The 2026-07-22 decision run stopped the exhaustive field campaign once hard gates
could no longer change the production choice. Missing quality evidence rejects a
candidate; it is never projected into a win. This made another five-hour serial
full-corpus run unnecessary.

| profile | throughput vs Granite | peak RSS | decision evidence |
| --- | ---: | ---: | --- |
| Granite small 384 | 1.0x | 1,118.6 MiB | Incumbent; the aggregate agent judge is 19/20 hybrid. |
| Static Retrieval 256 | 44.0x | 648.5 MiB | Fast enough for a future freshness overlay, but it added no agent-visible quality win. |
| EmbeddingGemma 768 | 0.27x | 2,479.8 MiB | Below the throughput gate and not permissively licensed. |
| EmbeddingGemma 256 | 0.23x | 2,228.5 MiB | Below the throughput gate and not permissively licensed. |

Static's exact run used the same private evaluation snapshot as Granite and reused
a substantial duplicate-text fraction. It produced a 45.93 MB frozen vector and
accelerator bundle from a 31.26 MB model. A direct 20-task oracle put five expected
rows in its top 40. Four were already Granite successes; the only Granite miss was
Static rank 27, outside the agent-visible page. The production CLI calibration
attempt was time-boxed after its raw batch stalled, so these numbers do not claim a
calibrated Static quality score.

Long-message coverage was tested separately with the pinned Static tokenizer.
Roughly one fifth of the private evaluation rows exceeded the 1,022-token payload.
Four overlapping windows raised vector count by about one third and token coverage
from about 46% to 73%, but the strict top-40 result stayed 5/20. Unbounded windows
lost one top-16 target as unrelated window maxima polluted the ranking. Naive
multi-window maximum scoring is therefore rejected.

The production verdict is **stay on Granite**. A permanent Static index, dual-model
ranker, and naive multi-window lane do not earn their extra storage, query work, or
surface complexity. Static remains a measured future option for a rolling backlog
overlay if initial semantic freshness becomes a demonstrated user problem.

## Surveyed but not admitted

- [Jina Embeddings v5 Text Nano](https://huggingface.co/jinaai/jina-embeddings-v5-text-nano)
  is a strong 2026 multilingual/long-context comparator, but its CC-BY-NC-4.0
  license makes it non-adoptable. Unlike EmbeddingGemma, it was not mandatory for
  the requested comparison set, so downloading and timing it would not affect the
  ship decision.
- Qwen3 Embedding 4B and 8B inherit the 0.6B model's strengths but are outside the
  local CPU and resident-memory envelope. The 0.6B model is measured instead.
- [Nomic Embed Text v2 MoE](https://huggingface.co/nomic-ai/nomic-embed-text-v2-moe)
  is Apache-2.0 and multilingual, but has 475M total / 305M active parameters, a
  custom MoE runtime path, only 512-token context, and no pinned official ONNX
  graph. It is a poor fit for an always-local resident worker.
- [Mixedbread mxbai-embed-large-v1](https://huggingface.co/mixedbread-ai/mxbai-embed-large-v1)
  is Apache-2.0 and does ship ONNX, but its 335M encoder and 1,024-dimensional
  native output provide no footprint advantage. A future MRL profile would first
  need an exact supported truncation and quantized-artifact pin; it is not rejected
  on quality.
- [E5 small v2](https://huggingface.co/intfloat/e5-small-v2),
  [GTE small](https://huggingface.co/thenlper/gte-small), and
  [MiniLM L6 v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2)
  remain permissive, portable historical controls. They were not admitted because
  the 47M Granite baseline already occupies the compact 384-dimension lane and IBM's
  published comparison reports stronger retrieval/code/long-document results than
  E5 small. They do not test a new production tradeoff.

## Reproducible bakeoff

`embedder_selection.py` freezes one real transcript generation, gives every model
its own data and model directories, and runs the production ONNX embedder plus the
real CLI. It does not download unless `prepare --allow-download` is present.
Copy `semantic_worth_tasks.example.json` to the ignored
`semantic_worth_tasks.json`, then replace its synthetic handles with private local
ground truth before starting a campaign. `validate` uses the example only to check
the committed schema; campaign stages default to the private task file.

```console
python bench/embedder_selection.py validate
python bench/embedder_selection.py prepare --artifact-cache /verified/pinned/cache
python bench/embedder_selection.py calibrate
python bench/embedder_selection.py quality
python bench/embedder_selection.py performance
python bench/embedder_selection.py blind --export /tmp/agrep-blind-review.json
python bench/embedder_selection.py blind --import-scores /tmp/agrep-blind-scores.json
python bench/embedder_platform.py run --artifact-cache /verified/pinned/cache \
  --output bench/.data/embedder-selection/platform-evidence.json
python bench/embedder_selection.py report \
  --platform-evidence bench/.data/embedder-selection/platform-evidence.json
```

Pass `--profile ID` repeatedly to run a subset. `report` must include the Granite
baseline. Every post-prepare stage recomputes the manifest, task, and frozen-corpus
digests; any drift refuses the run. `--expect-snapshot SHA256` lets a second machine
prove that it used the same corpus generation.

Campaign contract version 2 is intentionally incompatible with preliminary
campaign directories. A v1 state, prepare record, quality result, or performance
result is evidence-incomplete and must be regenerated in a new run directory; there
is no inferred migration. The version is a JSON integer; `2.0`, booleans, and strings
are rejected at every artifact boundary. Version 2 binds each full embed to the execution host,
CPU/core counts, Python/ONNX/numpy/tokenizers versions, provider activation, semantic
thread budget, process affinity when available, and the declared power policy.
Use `--power-policy LABEL` when the host is held in a known power mode. Federated
throughput comparisons require the complete benchmark environment to match exactly;
the separate physical macOS/Windows smoke remains portability evidence, not a
substitute for a comparable performance host.

Quality artifacts bind the exact task digest, hit cap, recall lane flags, output
budget, and self/query-echo exclusion policy. Performance artifacts bind at least two
ordered query samples, the cold/warm policy, exact top-up count, q8 scale sizes,
resource interval, and resolved absolute roots. Reusing a stage with different
options fails instead of silently overwriting evidence. Because absolute roots are
part of the record, moving a campaign directory invalidates it. This path binding is
intentional for the current contract. Before making any selection decision, `report`
requires every selected profile to have the exact same benchmark environment and
quality protocol, plus the same path-normalized performance protocol. Reports label
the measured top-up row count rather than assuming the 1,000-row default.

`performance` measures one 1,000-row delta per profile and runs one reusable,
multi-size q8 scale campaign per distinct vector dimension. Its 10M projections keep
source planning, delta inference, f32 publication, and derived q8/group/f16 work
separate. The independently stored `q8-scale/dim-N.json` schema-2 record binds the
code digest, benchmark environment, and Rust/Python/numpy runtime provenance.
Performance stores that record, its digest, absolute cache path, and file SHA.
Validation only reloads the existing cache; it never regenerates missing evidence.
It requires exact external equality before recomputing the current-layout projection
from the raw measured top-up and then the full projection. Internally consistent
performance-only edits are rejected. This detects accidental/stale local artifact
edits, not a malicious actor coordinating edits to both the canonical cache and every
dependent artifact. A platform bundle may be merged across hosts, but no candidate can win
without current `darwin-arm64` and `win32-x86_64` CPU evidence for both it and the
baseline.

The blind packet contains queries, evidence, and randomized labels. It contains no
profile names or similarity scores. Its random seed, packet digest, private map, and
imported/result scores all bind the full current quality protocol and its digest, so
quality regeneration invalidates prior judgments. They also bind SHA-256 for each
final per-profile `quality.json`; changing hit evidence under the same protocol
invalidates the old blind output. Score every option from 0 (not useful) through 2
(directly answers), preserving those protocol and artifact fields:

```json
{
  "schema": 1,
  "campaign_contract_version": 2,
  "snapshot_digest": "copied from the packet",
  "blind_digest": "copied from the packet",
  "quality_protocol": {"copied": "verbatim from the packet"},
  "quality_protocol_digest": "copied from the packet",
  "quality_artifact_digest": "copied from the packet",
  "judgments": [
    {
      "task": "ciphertext-pollution",
      "ratings": {"A": 2, "B": 0},
      "notes": "optional"
    }
  ]
}
```

Adoption is conjunctive and each gate is printed separately: semantic-only must beat
Granite, hybrid must score at least 19/20, the real-vs-gibberish gap must be at least
2x Granite, deduplicated effective throughput must retain at least half Granite's
rate, the 10M-row q8 matrix may be at most 2x Granite's, and the profile must be
license/runtime eligible. A candidate missing any measured artifact is rejected,
not projected into a win.
