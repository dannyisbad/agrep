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
structurally ahead of all-terms hits. On interactive or compact output, a
content-terms fallback runs when both token lanes are empty and the multi-word
query has stop words, one-character terms or punctuation to drop. It is a third,
lower tier based on the remaining informative terms, not a relaxation of
identifier-shaped grep. Plumbing surfaces (`--flat`, piped TSV, `--json`, `-l`,
`-c`, and `--lexical`) never run it.

`-w` requests literal whole-word matching and `-E` requests the supplied regular
expression. Those explicit modes bypass boundary ranking. `--sort time` uses
recency instead of the default score order.

## Hit snippets

A hit's snippet is its match with 80 characters on each side, whitespace runs
collapsed to one space and `…` marking each cut (0.4.9's window). The match is
the phrase's first occurrence; a lone word's first occurrence aligned at both
ends, else its first of the best boundary quality; an explicit pattern's first
match. An all-terms row places a window at each term's occurrence chosen the
same way (with its singular/plural spellings), merges windows that touch, and
joins the rest with a space. A content-fallback row places one window at the
earliest occurrence of a found term. Recall, pack and tool handles name the
same match.

## Default score order

For row output, lane order is lexicographic: phrase, all-terms, then content
fallback. Within a lane, the relevance score is:

```text
S = round(T * R * W * B, 4)
```

- `T` is match tightness and repetition **inside the hit's snippet**. The phrase
  (tokens joined by `[\W_]*`) is counted there and nowhere else: its `n`
  non-overlapping matches give `T = tight * (1 - 0.5^n)`, with
  `tight = min(1, query_characters / shortest_match_characters)` and characters
  counted as the snippet shows them. Repetitions elsewhere in a long reply, or a
  tighter match outside the snippet, do not count; a match spread over a
  whitespace run counts as its collapsed width. Matches are found on the same
  original text as eligibility, with Python's `re.IGNORECASE` character classes
  and its `[\W_]*` separator rule. The single-token lowercase shortcut also
  preserves 0.4.9's expanding dotted İ. Accents and compatibility spellings
  retain their original characters. An all-terms row takes the larger
  of that phrase strength over its stitched snippet and its proximity: the share
  of terms the boundary scorer finds in the snippet times
  `0.5 + 0.5 * min(1, query_characters / spread) * scale`, where `spread` covers
  the selected occurrences plus 80 for each `…` between them and `scale` is the
  least of `max(0, 2q - 1)` over their qualities. An interior fragment therefore
  cannot earn a near-phrase score merely because it sits beside another term.
  `-w` counts its whole-word pattern in the snippet; `-E` counts its matches with
  no tightness, `T = 1 - 0.5^n`.
- `R = 0.5^(age_days / 14)`. Human prompts retain a floor above the fresh
  recap/tool score ceilings. Explicit `-w` and `-E` searches also retain a
  recency floor.
- `W` is the speaker/source prior. User prose leads; agent and subagent prose
  follow; tool output, recaps, control, synthetic, and harness rows are
  progressively downweighted. Rows about tooling rather than history (harness
  and synthetic rows, and a subagent's first turn, which is its spawned task
  prompt) take a further 0.45 in every tier and in a meaning row's score; the
  explicit `--coverage` lane does not apply it. Such rows carry `~meta`: grouped
  classic pages and compact pages show them below lived rows, and recall and pack
  rank each query's lived family heads before them. A row proven to follow a
  history command keeps its score; `--no-meta` drops it.
- `B` is the boundary factor below. It is a penalty only: `0 <= B <= 1`. It is
  graded on the snippet with every `…` replaced by a neighbouring character, so
  a cut never looks like a word edge: a phrase row takes the best factor over
  its snippet's matches (the shorter match on equal factors), an all-terms row
  grades its stitched snippet.

The product is rounded to four decimal places, ties to even on its exact binary
value (Python's `round`). Rows compare by that rounded score, then by
timestamp (newer first), session, turn and speaker, so two rows whose scores
differ only past the fourth place rank by time. Because `B` can never raise a
score, an unseen candidate can safely be bounded with `B = 1`; a broad
candidate walk skips a block or row only when its rounded ceiling is below the
page's last score, or equal to it and the time/position order cannot favour it.

Session-head views (`-l` and content-ranked `chats`) choose the best row per
session by a lane-folded score instead of row output's lane-first key, then by
lane, time and position:

```text
S_view = S * L, where L = 1.0 (phrase), 0.7 (all terms), 0.5 (content fallback)
```

This lets strong human prose represent a chat ahead of a weak phrase found in
tool output without changing ordinary row-search ordering.

The content fallback matches each content word literally (Python lowercase, no spelling
variants) and weighs it by `ln(1 + N / (df + 1))`, where `N` counts the rows in
scope (prose only unless tool rows are searched) and `df` the rows holding the
word; a word under three characters counts as in every row. Only rows holding one
of the three rarest words of three or more characters qualify. A row's `T` is the
share of the total weight its words carry, rounded to four places, and `B = 1`;
its snippet, rounding and order follow the rules above.

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
sequences stay attached to their base. Boundary grading uses NFKC case-folded
clusters and maps spans back to the original text; literal eligibility does not
normalize those clusters.

For token `t`, ingest commits short-token statistics:

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
and changes only with committed source updates. If a token has no observation, the
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
prose-shaped query may run the optional semantic lane alongside it, and prose
recall can combine both lanes. Semantic rows remain labeled evidence; they do not
overwrite keyword scores.

`--lexical` disables automatic meaning evidence. `-s` requests semantic-only
search. If the model is missing, explicit semantic search exits 2 with the setup
instruction; automatic hybrid search uses keywords and status names the missing
model. Query embedding runs in process with the pinned Candle model on the CPU;
background embedding uses Metal on supported Apple hardware. There is one
embedding space. [SEMANTIC.md](SEMANTIC.md) describes the lane.

Semantic ranking is conversation-family aware by default so one root chat and
its side chats do not consume every slot; `--all-side-chats` ranks each session
on its own. A vector belongs only to its exact snapshot text. The lane embeds
queued text inline before scanning, so changed rows join the next meaning answer.
A larger build quietly leaves automatic searches on keywords; explicit `-s` exits 2
with build progress. A completed semantic miss exits 1. See [OUTPUT_CONTRACTS.md](OUTPUT_CONTRACTS.md) for the interface
and [PORTING.md](PORTING.md) for deliberate differences from 0.4.9.

In the automatic hybrid merge, meaning rows below cosine 0.82 never appear, and
rows below 0.84 appear only beside strong lexical evidence (a phrase row that is
neither a tool row nor an interior match); at most three meaning rows join a
page. A meaning row is corroborated when its conversation family also holds a
strong lexical row; bag-of-words scatter cousins corroborate only when the
lexical lane has no strong row. Weak lexical evidence never vetoes strong
meaning evidence: only a strong row of the same family on the page suppresses
the semantic lead as already covered, and a weak lexical copy of the exact same
row yields its slot to the semantic-labeled twin (shown once). With strong
lexical rows and no corroborated meaning row, the leading strong rows stay first
and one meaning row follows them.

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

## Coverage lane

`--coverage` adds an explicit coverage lane after a default keyword page; there
is no automatic retry. The query splits into words at whitespace, hyphens and
underscores (at least two are needed), and its words of three characters or
more are OR-ed and ranked by SQLite's bm25: prose through its trigram index,
tool output through the indexed words holding each query word, both lanes
merged by score. Of the 400 best rows, a row quoting the whole query is left
out, each chat gives its best remaining row, each chat family its best chat,
and at most 20 rows remain, whatever `--sort` says. Rows the page already
shows can return.

The rows print on stderr after the page (after a compact page's summary line,
before a classic page's `showing N of M` line), led by
`coverage lane (forced): top row matched M/T terms - dropped: …` (or
`coverage lane: no candidate rows`). They honour every scope filter of the
query except `--no-meta`, including `--exclude-project`, `--no-side` and the
caller's self-exclusion, and never enter counts or the exit status.
`--coverage` is refused with machine output, counts, chat lists, `-s`,
`--lexical`, `-w` and `-E`.

## Caller window

Automatic self-exclusion resolves the exported or process-published caller before
calculating its window. The caller's newest recap sets the inclusive start; a
session never compacted starts at turn zero. Side sessions descending from the
caller that started inside that window are excluded too. Older family members
remain ordinary evidence, and older turns of the caller's own session are
labeled `~self`. A caller that cannot be resolved hides nothing automatically.
`--self` includes the current window; explicit `--no-self` excludes the complete
indexed family. Keyword retrieval, counts, session lists and the meaning lane
share this scope. [ENGINE.md](ENGINE.md) describes how the caller is identified.

`recall --probe` reports a confident match only when a phrase or meaning hit is
present; otherwise it prints a qualified miss.

## Compact agent profile

Known agent shells use compact output automatically - including when piped,
which supersedes the human default of flat TSV on a pipe (`--flat` restores
it). `AGREP_PROFILE=compact` forces compact, while `--classic` or
`AGREP_PROFILE=classic` selects the human renderer and turns off automatic
hybrid retrieval; `-s` still runs the meaning lane explicitly. `--json`, `-c`,
chat lists, and tier counts retain their dedicated output contracts.

In classic keyword output, `-n 0` requests all hits; explicit semantic output
caps that request at 200 chats. Without `-n` a search fetches the ranked top 40.

A compact page:

- orders rows as 0.4.9 did: `~meta` rows last, and within each quality lane
  (phrase, meaning, all terms) one row per conversation family before the rest;
  meaning rows follow the first three phrase rows that are not interior matches;
- shows at most 16 lines (fewer when `-n` asks for fewer);
- after four rows, stops at a 3,584-byte budget; if at least four valid matches
  exist, the best four still render, with lines shortened to share the budget;
- shows one row per session turn (one per handle for tool rows) and, without
  `-n`, at most three rows per conversation family and two tool rows (four when
  every row is a tool row);
- without `-n`, after the first four rows, stops at a row of the leader's lane
  scoring below 35% of the leader, and a phrase-led page stops at its first
  all-terms row unless an earlier row was deferred;
- without `-n`, keeps room on the first page for the two best all-terms rows,
  dropping its last phrase rows if the page would overflow;
- marks lower-quality rows with `~substr` or `~all`, and meaning rows with
  `~semantic (meaning match)`.

`agrep --more <handle>` reads the next page from a frozen page kept for five
minutes; it does not rerun the query. The frozen page keeps the query, the total
and whether it is exact, and the full command, so an exact total survives every
continuation. Result handles also paste directly into `agrep around` and
`agrep recall`.

## Counts and summaries

`-c` keeps grep semantics: it exhaustively counts every matching row once,
before display caps, across phrase boundary classes and the all-terms lane.
`--count-by-tier` reports
`phrase_aligned`, `phrase_partial`, `phrase_interior`, `all_terms`, and `total`.

An exhausted compact page with a known exact total stays silent, as does a page
merged with meaning rows when neither lane held rows it did not return. An
incomplete page emits exactly one completeness line: `N matches` for an exact
total, or `N+ matches (floor)` (`N+ matches (floor; -c exact)` when an exhaustive
count is available; never on a meaning page, since `-c` counts keyword rows only)
for a measured lower bound. ` · more: agrep --more HANDLE` is appended
when a continuation exists, or ` · full results (live rerun): COMMAND` when only
a rerun can show the rest; compact output never presents the number of rendered
rows as a corpus floor. Machine and classic surfaces retain their separate output
contracts.
