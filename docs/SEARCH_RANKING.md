# Search matching, ranking, and compact output

This is the reference contract for agrep's default keyword search and its
agent-oriented renderer. The CLI help remains authoritative for flags; this page
explains the behavior that is easy to miss from a one-line option description.

## Keyword match lanes

Default keyword search is case-insensitive and keeps grep's substring behavior:
`akd` remains eligible inside `peakDetect`. Query text is split on whitespace,
hyphens, and underscores.

For a multi-token query, two lanes always run independently:

1. **Phrase:** every token appears in query order, joined by zero or more
   non-word/underscore characters. For example, `cyber filter` matches
   `cyber_filter`.
2. **All terms:** every raw token appears as a substring in any order.

The all-terms lane never depends on how many phrase hits exist. A row found by
both lanes is emitted once as a phrase hit; phrase hits sort structurally ahead
of all-terms hits. On porcelain output, a natural-language-only content fallback
may run when both raw token lanes are empty. It is a third, lower tier based on
informative query terms, not a relaxation of identifier-shaped grep. Plumbing
surfaces (`--flat`, piped TSV, `--json`, `-l`, `-c`, and `--lexical`) never run it.

`-w` requests literal whole-word matching and `-E` requests the supplied regular
expression. Those explicit modes bypass boundary ranking. `--sort time` uses
recency instead of the default score order.

## Default score order

Lane order is lexicographic: phrase, all-terms, then content fallback. Within a
lane, the relevance score is:

```text
S = T * R * W * B
```

- `T` is match tightness and repetition. For the tightest matching span,
  `tight = min(1, query_characters / span_characters)`, and `n` occurrences
  contribute `T = tight * (1 - 0.5^n)`. All-terms rows also use matched-term
  coverage and spread.
- `R = 0.5^(age_days / 14)`. Human prompts retain a floor above the fresh
  recap/tool score ceilings. Explicit `-w` and `-E` searches also retain a
  recency floor.
- `W` is the speaker/source prior. User prose leads; agent and subagent prose
  follow; tool output, recaps, control, synthetic, and harness rows are
  progressively downweighted.
- `B` is the boundary factor below. It is a penalty only: `0 <= B <= 1`.

Final ties are deterministic by timestamp, session, turn, and speaker. Because
`B` can never raise a score, an unseen candidate can safely be bounded with
`B = 1`; broad candidate walks may therefore stop once their score ceiling
cannot enter the requested page.

## Code-aware boundary factor

Boundary ranking does not filter substring matches. It promotes recognizable
tokens while leaving interior fragments eligible. Each token occurrence gets
quality `q`:

- `1` when both ends align;
- `0.5` when one end aligns;
- `0` when both ends are interior.

Aligned positions include text start/end, punctuation and path/operator
separators, `lowerCase` transitions, `HTTPServer` acronym-to-word transitions,
letter/digit transitions, and Unicode script changes. Apostrophes between
letters are joiners, so the `t` in `don't` is not promoted. Combining marks,
variation selectors, skin-tone modifiers, Hangul clusters, and emoji ZWJ
sequences stay attached to their base. NFKC case-folded matching maps spans back
to the original text before grading boundaries.

For token `t`, ingest publishes generation-bound short-token statistics:

- `n_t`: root conversation families containing `t` anywhere;
- `s_t`: those families containing `t` as an aligned identifier subtoken.

Occurrences and agent echoes within one family count once. The ambiguity value is
the smoothed observed contamination:

```text
A(t) = clamp((n_t - s_t + 32 * prior(t)) / (n_t + 32), 0, 1)
p_t  = max(0.12, 1 - A(t) * (1 - q_t))
B    = geometric_mean(p_t for each query token)
```

The sidecar covers normalized identifier subtokens up to four grapheme clusters
and changes only with an ingest generation. If a token has no observation, the
cold prior is:

| Token class | 1-2 clusters | 3 | 4 | 5+ |
|---|---:|---:|---:|---:|
| Cased script | 0.90 | 0.75 | 0.40 | 0.05 |
| Segmented uncased script | 0.60 | 0.30 | 0.30 | 0.05 |
| Unsegmented script or no alphanumeric content | 0 | 0 | 0 | 0 |

This makes short accidental fragments pay the strongest penalty without a
stopword list that would break identifiers such as `id`, `db`, or `fn`.

## Semantic evidence

Keyword search begins with the lexical lane. In an agent's compact profile, a
prose-shaped query may run the optional semantic lane alongside it. Interactive
classic output may try semantic search after a prose-shaped query returns zero
exact hits, and prose recall can combine both lanes. Semantic rows remain
labeled evidence; they do not overwrite keyword scores.

`--lexical` disables automatic meaning evidence. `-s` forces semantic-only
search and fails explicitly when the optional runtime or a coherent vector
generation is unavailable. Automatic hybrid paths keep the keyword result when
semantic search is unavailable, stale, times out, or has no confident hit.

Semantic ranking is conversation-family aware by default so one root chat and
its side chats do not consume every slot. `--all-side-chats` expands them for an
explicit semantic search. Partial embedding coverage is disclosed as
indexed/total; old or generation-mismatched vectors are never queried.

Which engine wrote a vector is part of that identity, not a runtime detail. Two
lanes can produce vectors: ONNX int8 on CPU, and MLX fp16 on Metal, which is
the default on Apple silicon, where the base install carries mlx - and a query is
always embedded by the lane recorded in the store it is querying. A lane the
running machine cannot open degrades exactly like a stale generation: keyword
results with one disclosure, never a query answered across two vector spaces.
See `py/README.md` for the lane contract.

In the automatic hybrid merge, weak lexical evidence never vetoes strong
meaning evidence: only a strong visible row of the same conversation family
may suppress the semantic lead as already-covered, and a weak lexical copy of
the exact same row yields its slot to the semantic-labeled twin (shown once).

## Recall lane hierarchy

`agrep recall` merges up to three lanes into one pack in a fixed order:
prose keyword evidence, then labeled semantic evidence, then tool output.
That hierarchy holds between hits of comparable strength. Weak bag-of-words
scatter (`all-terms`/`content-terms` fallback rows) sorts below strong
evidence from every lane, so a tool session holding the queried phrase
verbatim always outranks prose that merely contains the words somewhere.

The larger tool corpus is queried only when prose cannot fill the requested
pack with strong evidence. The gate is evidence strength, not row count: a
full page of weak scatter does not count as fill and never skips the tool
lane. `--who tool` forces the tool lane directly.

## Over-specification recovery

A wordy natural-language keyword query whose page holds no strong independent
row - every hit is a weak-tier match, a semantic assist, a `~self` family row,
or a verbatim quote of the query itself (an echo, judged on row text, never
the rendered snippet) - retries once with a coverage lane: the query's terms
are OR-ed and rows ranked by FTS5 `bm25()`, so the corpus's own document
frequencies decide which terms are informative. Narration the corpus holds
everywhere weighs approximately nothing; rare evidence terms dominate; length
normalization keeps giant blobs from outranking focused rows. There is
deliberately no curated stopword list in this lane.

The recovered rows render as a labeled block after the page, capped at five
family-diverse sessions not already shown, and the disclosure names the
reformulation that was actually measured ("top row matched 13/17 terms -
dropped: …"). Echo rows never enter the block. Code-shaped queries keep the
pure-grep carve-out: identifier-shaped input and bare keyword bags are never
retried, and `--lexical`, `-w`, `-E`, `-s`, machine modes, and non-score sorts
never run the retry.

## Caller-window identity and echo demotion

Automatic self-exclusion requires two independent facts: a directly exported
caller session identity and a numeric recap boundary indexed for that session.
It hides only rows in that caller transcript at or after the boundary. A recap
does not prove where a child or sibling transcript's active window begins, so
family members remain ordinary evidence; their names are never used as a scope
heuristic. Missing, malformed, or unavailable boundaries fail open and hide
nothing. `--self` includes the proven current window, while explicit
`--no-self` excludes the caller and every structurally indexed family member.
The rule holds identically in the SQL pre-top-k filter, JSONL fallback scan,
semantic filter, and post-top-k defensive check.

On the compact display lane, a row whose text verbatim-quotes a wordy
natural-language query (the same echo judgment the over-specification retry
uses: row text, never the rendered snippet) restates the question instead of
answering it, so echo rows sink below every non-echo row before page assembly.
Code-shaped queries, forced lanes, machine modes, and non-score sorts are
untouched, and an all-echo page keeps its order - the retry block above owns
that case.

## Compact agent profile

Known agent shells use compact output automatically - including when piped,
which supersedes the human default of flat TSV on a pipe (`--flat` restores
it). `AGREP_PROFILE=compact` forces compact, while `--classic` or
`AGREP_PROFILE=classic` selects the human renderer and disables the compact
profile's hybrid retrieval. It does not disable semantics outright: an
interactive prose query that returns no keyword hits may still run the meaning
lane as a zero-hit fallback. `--lexical` is what turns that off; `-s` keeps
semantic explicit.
`--json`, `-c`, chat lists, and tier counts retain their dedicated output
contracts.

In classic keyword output, `-n 0` requests all hits; explicit semantic output
caps that request at 200 chats. A positive `-n` requests up to the frozen top
40, while compact's adaptive byte and row limits still apply.

A compact page:

- freezes at most the ranked top 40;
- targets 4-16 self-contained lines under a 3,584-visible-byte default budget;
- emits at most three rows per conversation family and one per session/turn;
- reserves up to two slots for all-terms evidence;
- may stop at the byte budget, a phrase-to-all-terms drop, or a same-lane score
  below 35% of the page leader;
- marks lower-quality rows with `~substr` or `~all`.

If at least four valid matches exist, the best four still render even when lines
must be shortened to fit. `agrep --more <handle>` reads the next page from a
short-lived frozen snapshot; it does not rerun the query. The versioned snapshot
also pins the query, any exact total, and a structured deeper-search command, so
an exact total survives every continuation when available, and the final page
can give an opaque `agrep --deeper <handle>` action beyond the frozen top 40.
Query text never passes through shell rendering. Result handles also paste
directly into `agrep around` and `agrep recall`.

## Counts and summaries

`-c` keeps grep semantics: it exhaustively counts every matching row once,
before display caps, across phrase boundary classes and the all-terms lane.
`--count-by-tier` reports
`phrase_aligned`, `phrase_partial`, `phrase_interior`, `all_terms`, and `total`.

An exhausted compact page with a known exact total stays silent. An incomplete
page emits exactly one completeness line: `N matches` for an exact total,
`N+ matches (floor)` (or `N+ matches (floor; -c exact)` when exhaustive count is
available) for a measured lower bound, or `N shown · total unknown` when no
corpus bound exists. ` · more: COMMAND` is appended only when a continuation
exists; compact output never prints `more=no` and never presents the number of
rendered rows as a corpus floor. Machine and classic surfaces retain their
separate output contracts.
