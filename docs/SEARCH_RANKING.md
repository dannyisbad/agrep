# Search matching, ranking, and compact output

This is the reference contract for agrep's default keyword search and its
agent-oriented renderer. The CLI help remains authoritative for flags; this page
explains the behavior that is easy to miss from a one-line option description.

## Keyword match lanes

Default keyword search is case-insensitive and keeps grep's substring behavior:
`akd` remains eligible inside `peakDetect`. Query text is split on whitespace,
hyphens, and underscores.

For a multi-token query, two lanes always run independently:

1. **Phrase:** every original token appears in query order, joined by zero or
   more non-word/underscore characters. For example, `cyber filter` matches
   `cyber_filter`. Inflection folding never changes this lane.
2. **All terms:** every token appears as a substring in any order. ASCII word
   tokens also accept a minimal singular/plural spelling (`s`, `es`, or `ies`);
   the original spelling is always retained, and short or non-alphabetic tokens
   stay exact. Thus `don calls` can recover prose containing `don` and `call`.

The all-terms lane never depends on how many phrase hits exist. A row found by
both lanes is emitted once as a phrase hit. Row search keeps phrase hits
structurally ahead of all-terms hits. On porcelain output, a
natural-language-only content fallback may run when both token lanes are empty.
It is a third, lower tier based on informative query terms, not a relaxation of
identifier-shaped grep. Plumbing surfaces (`--flat`, piped TSV, `--json`, `-l`,
`-c`, and `--lexical`) never run it.

`-w` requests literal whole-word matching and `-E` requests the supplied regular
expression. Those explicit modes bypass boundary ranking. `--sort time` uses
recency instead of the default score order.

## Default score order

For row output, lane order is lexicographic: phrase, all-terms, then content
fallback. Within a lane, the relevance score is:

```text
S = T * R * W * B
```

- `T` is match tightness and repetition. For the tightest matching span,
  `tight = min(1, query_characters / span_characters)`, and `n` occurrences
  contribute `T = tight * (1 - 0.5^n)`. All-terms rows measure coverage and
  spread from the same best-aligned occurrence selected for `B`. If any selected
  occurrence has only one or no aligned edge, its quality removes the adjacency
  bonus down to the `0.5` proximity floor; an interior fragment cannot earn a
  near-phrase score merely because it sits beside another query term.
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

Session-head views (`-l` and content-ranked `chats`) choose the best row per
session by a lane-folded score instead of row output's lane-first key:

```text
S_view = S * L, where L = 1.0 (phrase), 0.7 (all terms), 0.5 (content fallback)
```

This lets strong human prose represent a chat ahead of a weak phrase found in
tool output without changing ordinary row-search ordering.

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

A meaning row is trusted at or above the strong band (cosine 0.84) and shown at
all from the floor (0.82); between them it is labeled a weak meaning match.
`bench/semantic_calibration.py` places those bands against a committed text
baseline: unrelated short rows reach 0.79, rows sharing one content noun reach
0.85, paraphrases start at 0.85 and centre near 0.93. Scores carry about ±0.02
of int8 batch-padding noise on the CPU lane (a row embedded alone and the same
row embedded in a padded batch agree to ~0.99 cosine); a query always embeds
alone, so a row within that distance of a band may land on either side.

Each meaning row is also anchored on the query's content words (three or more
characters, not a stopword): a row whose full indexed text carries fewer than
half of them is a weak meaning match whatever its cosine, because one shared
noun buys a strong-band score on its own. Weak meaning rows sort after
confident ones and never lead a page.

In the automatic hybrid merge, a meaning row is corroborated when its
conversation family also holds a strong lexical row; bag-of-words scatter
cousins corroborate only when the lexical lane is itself all scatter. Weak
lexical evidence never vetoes strong meaning evidence: only a strong visible
row of the same family may suppress the semantic lead as already-covered, and
a weak lexical copy of the exact same row yields its slot to the
semantic-labeled twin (shown once). With strong lexical rows and no
corroborated meaning row, exact matches lead and one meaning row follows them.

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

A multi-term keyword query (a query with a space that splits on whitespace,
`-` and `_` into five or more distinct terms) whose page holds no strong
independent row - every hit is a weak-tier match, a meaning row, a `~self`
family row, or a verbatim quote of the query itself (an echo, judged on row
text, never the rendered snippet) - retries once with a coverage lane: the
query's terms are OR-ed and rows ranked by FTS5 `bm25()`, so the corpus's own
document frequencies decide which terms are informative. Narration the corpus
holds everywhere weighs approximately nothing; rare evidence terms dominate;
length normalization keeps giant blobs from outranking focused rows. There is
deliberately no curated stopword list in this lane.

An empty or echo-only page retries whatever the query's shape, because nothing
else is left to show. A page that already carries weak scatter is partial
evidence, so it retries only when the query has narration to shed: a stop
word, or a term the corpus holds in a quarter or more of its sessions. A
single joined identifier is one grep pattern and never retries. Meaning rows
never suppress the retry: a page filled only by the automatic semantic lane is
a masked page, and `agrep recall` runs the same lane after a pack whose rows
are all meaning rows, weak scatter or echoes, and on a zero page.

The recovered rows render as a labeled block on stderr after the page, capped
at five family-diverse sessions not already shown, and the disclosure names
the reformulation that was actually measured ("top row matched 5/7 terms -
dropped: …"). Every row is a weak `content-terms` match: the block never
enters the pack, `--json`, counts, or exit status, and it honours every scope
filter of the query, including `--exclude-project`, `--no-side` and the
caller's self-exclusion. Echo rows never enter the block. Explicit lanes stay
pure: `--lexical`, `-w`, `-E`, `-s`, `--probe`, machine modes, and non-score
sorts never run the retry; `--coverage` forces the lane on a porcelain page.

Concise recall skips `~self` tool rows when selecting the recovered row; the full
coverage view retains them. It keeps the selected evidence, measured term coverage,
and scoped command rather than replacing retrieved evidence with another command.

## Caller-window identity and echo demotion

Automatic self-exclusion resolves the exported or process-published caller before
calculating its window. A numeric recap sets the inclusive start; a session proved
never compacted starts at turn zero. Descendants with proven membership in that
active window are excluded too. Older family members remain ordinary evidence.
Missing, malformed, or unavailable window proof hides nothing automatically.
`--self` includes the current window; explicit `--no-self` excludes the complete
structurally indexed family. SQL, JSONL, semantic, and post-top-k checks share
this scope.

When a pi/OMP root header changes ID, its stamped source filename still identifies
the sidechat container. Ingest derives that filename alias from the existing parse
cache, publishes it on the canonical session row, and reattaches child parents.
The version-3 family digest includes aliases. Both spellings resolve to the header
ID before caller/window lookup; verified census fallback also preserves aliases
when the query database is unavailable. Source transcripts are not rewritten.

Lexical probe candidates cannot establish confidence from `~self` tool inputs
alone, including relay messages and search arguments. The check binds to the exact
event and selected match in the canonical tool record, using the active matcher.
Matching output remains eligible even when the selected occurrence is in the input.
Prose that repeats a multi-term query also cannot supply `~self` probe confidence.
Classification precedes tool/meaning fallback decisions and pointer selection.
Ordinary recall rows and explicit handle retrieval remain available.

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
