# Semantic-worth gate

## Method

The product decision used 20 frozen developer-recall tasks written before inspecting
the result pages. Each task was run through the real `agrep recall` CLI in lexical,
semantic, and hybrid modes with three session heads, a 6,000-character budget,
`--no-auto`, and self/query-echo exclusion. A result counted as definite only when the
first page answered the question directly or pointed to one targeted `around` pull.

The task text, session identifiers, corpus inventory, and raw result pages are private
evaluation data and are not committed. `semantic_worth_tasks.example.json` is a fully
synthetic schema example for harness development; it is not the source of the product
quality claim. A local evaluation can be run explicitly:

```console
python bench/semantic_worth.py \
  --tasks bench/semantic_worth_tasks.local.json
```

The local task filename is ignored so real history cannot enter a release by accident.

## Aggregate result

| Lane | Definite answers | Ambiguous | Mean CLI turns per definite answer |
|---|---:|---:|---:|
| Keyword only | 9/20 | 2 | 1.56 |
| Semantic only | 14/20 | 1 | 1.71 |
| Initial hybrid | 14/20 | 1 | 1.50 |
| Final hybrid | 19/20 | 1 | 1.58 |

Semantic retrieval added useful paraphrase recall, especially when remembered wording
did not overlap the original conversation. Lexical retrieval remained stronger for exact
mechanisms and identifiers. The final merger therefore keeps the lanes independent:
strong phrase evidence leads, up to three semantic families may precede weak any-order
rows, and duplicate semantic families do not consume the visible quota.

The result supports semantic assistance by default for prose recall while preserving
explicit controls: `-s` forces meaning-only, `--lexical` opts out. Similarity scores are
not fused with lexical scores, weak nearest-neighbor tails stay silent under the
calibrated 0.82 floor and 0.84 strong band, and every visible semantic row stays
labeled.

These counts were measured on the ONNX int8 CPU lane. The Metal lane reproduces that
vector space to a 0.99933 worst-case cosine, which is close enough to share a corpus
and not close enough to guarantee an identical page: 3 of 45 fixture queries changed
at a threshold between lanes. Each store records the lane that built it and is only
served by that lane, so one index stays internally consistent even though the two
engines can rank a near-floor row differently. `bench/EMBED_SPEED.md` holds the lane
measurements and the parity gate.
