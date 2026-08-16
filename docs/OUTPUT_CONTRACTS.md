# Output contracts: the two layers

agrep's surfaces live under exactly one of two contracts. The split exists
because the two consumers want opposite virtues: scripts and harnesses want
*predictability*; mid-task agents and humans want *the answer with the fewest
round trips*. Blurring the layers trades both away. This document is the
stable contract for the 0.2 line; the search JSON page shape is specified
below and pinned by producer, consumer, and release-canary tests.

## Deterministic machine output

`--flat` (including implicit piped TSV), `--lexical`, `-l`, `-c`,
`--count-by-tier`, `--json`. Grep semantics:
deterministic, stable columns/fields, explicit contract migrations before a
release boundary, and identical bytes for identical corpus + query.

Argument-parser failures are a separate front-door contract shared by every
command: exit 2, empty stdout, and one terminal-safe stderr line. They never
dump the full help page; explicit `--help` still does. A recognized command
word always keeps its grammar. When the remaining arguments also form a safely
copyable search invocation, the refusal appends the exact
`agrep search <word> ...` escape hatch instead of silently reinterpreting the
command. Versioned machine-mode validation errors keep their structured shape.

- **Never routed.** No lane substitution, semantic escalation, or interactive
  content-term recovery. A script gets the same shape every time.
  Caller-window exclusion is renderer-independent: when a numeric recap
  boundary proves the current window, that window is withheld from every
  surface; without that proof, automatic exclusion withholds nothing.
- **Never collapsed.** No inlined follow-ups; machine callers compose their
  own pipelines.
- **A miss must be proven.** Exit 1 means an exact empty result over a source
  generation verified current for that command. Stale, partial, or unchecked
  absence exits 2; `--no-auto` deliberately makes an empty result unverified.
- **Every surface states its own completeness.** A machine surface that
  printed part of the answer says so in the payload: search `--json` begins
  with one `agrep-meta` run envelope carrying `completeness` (`shown`, `total`,
  `total_basis` exact/floor, `unit`, `truncated`), and a cut page names
  a bounded live rerun (`more_command`, explicitly `broader-rerun`), the
  uncapped invocation (`full_command`), or why none exists
  (`no_exhaustive_form`, the meaning lane). When the local shell cannot quote
  an argument safely, the corresponding `more_argv` / `full_argv` is a JSON
  string array for direct process execution without a shell; unsafe copyable
  prose is never emitted. An already-uncapped partial run instead carries
  `action_unavailable_reason`. These are reruns, never frozen `--more`
  continuations. `--flat`, `-c` and
  `--count-by-tier` render the same judgement to stderr, so stdout keeps
  grep parity. A row count is never the signal; inferring a cap from it is
  how a parser reports 40 for 1,765. Emitter and checker share
  `surface_policy.completeness_disclosure`; pinned in
  `test_machine_completeness.py`.
- **Indexed JSON hits are directly inspectable.** Each search row carries a
  digest-bound `handle` accepted by `agrep around`; serialization never mints
  one from an elided snippet when an invalid legacy row lacks a content claim.
- **Search JSON separates run state from hit evidence.** A non-empty page is
  one leading `agrep-meta` envelope followed by row-only hit objects. Run-level
  completeness, freshness, filter, self-exclusion, engine, and semantic state
  appear once in the envelope; `self` and the digest-bound `handle` remain on
  each hit. An empty page remains one `agrep-meta` record with `hits: []`.
- **Chats JSON uses the same one-envelope page shape.** The leading
  `agrep-meta` record carries completeness, freshness, and filter state once;
  chat records contain only chat identity and navigation fields. An empty page
  remains one self-contained `agrep-meta` record with `hits: []`.
- **Every surface states why it is empty.** A zero from a filter selecting a
  dimension the index holds no value for is not the same answer as a zero
  from a search that looked and found nothing, and the two must not render
  alike: the leading `agrep-meta` record carries
  `filter_coverage` (`empty_dimensions`, each naming `flag`, `value`,
  `dimension`, the `indexed_values` count and the `known` values it does
  hold; plus `checked` and, when the domain could not be enumerated, the
  `reason` no gap is claimed). The interactive render, `-c` and `--count-by-tier`
  render the same record as one stderr line. A legal value the index has not
  seen stays a legal query, disclosed rather than refused. Emitter
  and checker share `surface_policy.empty_dimension_disclosure`; pinned in
  `test_zero_coverage.py`.
- **Self-exclusion is counted, never advertised speculatively.** A proven
  current-window policy is silent unless matching rows were actually omitted.
  When the matching lane can prove the complete hidden scope, search JSON
  carries its exact pre-pagination count once in the leading `agrep-meta`
  envelope; flat/count surfaces put the same one-line count on stderr without
  changing stdout. An unprovable count stays explicitly unknown and produces
  no prose notice. `--self` disables the automatic window and explicit
  `--no-self` expands the scope to the caller's indexed family.
- **A window that selects no time is a usage error.** `--since`/`--until`
  name the half-open interval `[since, until)`; with the bounds the wrong way
  round it holds no instant, so every corpus answers zero. Both surfaces
  refuse before touching the index, naming both bounds
  (`surface_policy.window_bounds_error`, `--json` error code
  `empty-time-window`).
- **Every surface states what it served.** Positional `around SESSION TURN`
  requests may clamp and disclose the served turn. Result handles are exact
  references: an out-of-range, pruned, or content-mismatched handle is refused
  with one actionable error instead of being recentered. Successful differences
  travel in the stream the machine reads, never on stderr alone: `around --json`
  leads with an `agrep-meta` `served` record whose `divergences` name a clamped
  positional turn, a moved or unverifiable handle digest, or a prefix tie broken
  by digest. Each record carries the same rendered sentence stderr gets, so the
  two surfaces cannot disagree. Emitter and checker share
  `surface_policy.around_service_note`; pinned in
  `test_around_disclosure.py`.
- Correctness regime: contract tests, which are cheap, binary, and stable.
  Disclosure travels as fields rather than prose.
- **Compaction recovery is machine output.** `agrep postcompact` serves the
  pre-boundary tail of the calling session family after a provider compaction.
  It is continuation recovery, not retrieval: no query, no routing, and
  `semantic_search_performed` is always false. One record carries
  `schema_version`, `status`, and an `authority` block stating its own
  subordination — `role: supplement`, the visible user turns and the
  platform's own summary as `primary`, and `newer_visible_state_wins` — so a
  recovered tail can never read as an override of what the agent can already
  see. Scope is `root-only`: tool rows and delegated sessions are excluded and
  the output says so. Exit follows the proven-miss rule: 0 recovered, 1 proven
  empty against a generation verified current, 2 unavailable or invalid, and
  `--no-auto` marks the packet partial and exits 2 rather than presenting an
  unchecked absence as empty. Pages are bounded at 8,000 bytes of text or
  16,000 of JSON across at most eight blocks.
- This layer is the embedding surface: a harness builds on it precisely
  because it never surprises, so its stability is maintained deliberately
  across releases.

## Interactive output

Bare `agrep <need>`, `recall`, `around`, and their tty/compact renders.
Evidence-shaped, allowed to evolve, judged on the frozen fixture
set (hit-rate by mechanism class), not on contract stability.

Finite `recall` and `pack` `--budget` values cap UTF-8 bytes actually written,
including ANSI control bytes and the final newline; fitting never splits a
code point. A zero budget remains uncapped.

Compact output stays silent when an exact page is exhausted. Every
incomplete compact page emits one line from
`surface_policy.compact_completeness_line`: exact count, measured floor, or
unknown-total basis, followed by its copyable continuation when one exists.
It never prints `more=no` or promotes the page's row count to a corpus floor.
`--more` continues frozen rows; `--deeper` is labeled as a broader rerun that
may repeat rows rather than implying page two.

The routing rules for this layer:

1. **Disclose the route.** One line: which lane served, why, what would go
   deeper. A refused or degraded lane says so, because silence reads as "that
   capability doesn't exist." If automatic current-chat exclusion cannot
   resolve one caller, compact output gives one identifier-free warning that
   current-chat rows may appear.
2. **Every route is forcible.** Any decision the router makes, a flag pins:
   `--lexical` forces keyword, `-s` forces semantic, and `--classic` forces
   classic rendering without automatic semantic escalation. The router is a
   default, never a cage.
3. **Machine modes never route.** Crossing rule: an interactive-layer feature
   may not leak into machine output. Enforced by the machine contract tests.
4. **Collapse the near-certain round trip.** When an agent reading the output
   would fire a specific follow-up call with near-certainty, run it and include
   the result as a labeled, budget-capped block instead of printing the
   command. Auto-semantic escalation is the only implemented instance, and its
   threshold has not been generalized: per-shape thresholds calibrated from the
   fixture set are target-state (see Status). A follow-up needed in
   substantially all miss-to-hit paths collapses; anything genuinely optional
   stays a pointer. Costs are capped (bounded rows/bytes per collapsed block)
   and every collapsed block is labeled with what was run.
5. **Degrade legibly.** The interactive layer falls back predictably: semantic
   down → keyword-only, disclosed, still correct. When automatic meaning
   actually starts and then fails or times out, keyword hits remain available
   beside the canonical `meaning unavailable; keyword-only` notice. A runtime
   that was never present and a completed meaning miss are not lane failures.
   A meaning lane that ran against partial coverage can still serve hits. An
   empty semantic result on partial coverage exits 2 instead of presenting a
   proven zero.
   The deterministic layer is the availability floor that makes the
   interactive layer safe to depend on.
6. **Compression has a floor: the judgment call.** A compact row exists so
   the reader can decide relevance without expanding it. Compression that
   destroys the discriminating detail (which error, which decision, which
   session) has negative value at any token count: the reader expands
   everything or, worse, judges wrong. Under budget pressure, evict whole
   rows before degrading rows; a shorter page of judgeable lines beats a
   full page of stubs. One-liners are a means; the judgment call is the
   contract. On compact page one, useful prose may rescue at most two
   non-redundant tool rows; tool-only results show four. Explicit `-n` and
   `--who tool` keep their requested behavior. Rows withheld by that display
   policy stay in the frozen continuation, including proven exact echoes.
   One prose row plus two rescued tool rows is intentionally a three-row page:
   the mixed-tool cap outranks the generic four-row floor. `around` never
   collapses a selected tool event or a sole distinct failure, and centers
   selected input or output previews on the verified match. Human recall context
   hides incidental tool events behind a counted `around --full` pointer; when
   the hit itself is tool-origin, its one match-centered preview remains judgeable.

7. **A remedy the product can run is never delegated to the reader.**
   "Run X to fix" is legal only when the remedy needs human judgment
   (destructive, consent), privileges the product lacks (chmod, disk
   access), or is too heavy to run unasked, and then the message states
   the cost. Everything else self-heals, backgrounded where possible,
   guarded against loops (an escalation is spent before it launches), while
   the surface serves last-good state and says what is running. The inverse
   lie is the same violation: "retries automatically" when the retry cannot
   succeed. Diagnosis is part of the remedy: "run doctor to see why" is
   delegation when the surface could carry the reason itself.

One meaning lane, two participations (rules 1, 2, and 4 together): the agent
profile (compact) fuses keyword and meaning evidence into one labeled page,
because its reader consumes exactly one page programmatically. The human
profile (classic tty) keeps the hit list exact-match-first with
single-meaning keyword scores; the meaning lane still participates as
disclosed sidelines - a zero-exact-hit query escalates to labeled semantic
matches, and a weak page (tool-output-only, or a related-terms fallback)
appends the `chats about this semantically` block, which counts the
neighbors held below the similarity floor and points at `agrep recall` as
the lane-fusing surface. Cross-lane scores are never merged into one human
ranking, and `--lexical` / `-s` pin either lane on both surfaces.

## Status

Every rule above is classified here, and every rule listed as standing names
the mechanism that enforces it. `py/test_output_contracts_doc.py` fails when a
rule is added without classification, or claims standing without an enforcer.
That test exists because rule 6 was once added without being classified here,
and went unenforced until an audit found it.

Standing today, in full:

- Rule 1 (disclose the route): lane labels, degradation notices, and uncertain
  caller disclosure pinned in `test_auto_semantic_hybrid.py` and
  `test_agent_context_contracts.py`.
- Rule 2 (every route forcible): the flag set, with the coverage lane's
  forced mode pinned in `test_search_correctness.py`.
- Rule 3 (machine modes never route): pinned in
  `test_search_query_contract.py` and `test_output_contracts_doc.py`.
- Rule 4 (collapse the near-certain round trip): auto-semantic escalation is
  the only implemented instance, pinned in `test_auto_semantic_hybrid.py`. The
  generalized threshold is target-state below.
- Rule 5 (degrade legibly): one degradation story on every surface: search,
  recall, and the probe render the same keyword-only notice when the meaning
  lane is down; the `-s` failure line, doctor's state rows, and the tty retry
  hint all render from `surface_policy.SEMANTIC_LANE_POLICY`; recall `--json`
  carries a `semantic_status` that separates "searched, empty" from "never
  ran"; parity-pinned in `test_surface_policy.py`.
- Rule 6 (compression preserves the judgment call): blocks are evicted whole
  before any block degrades to stubs, with the eviction disclosed; compact
  completeness is fixture-locked to the documented exact/floor/unknown forms;
  pinned in `test_compact.py` and `test_output_contracts_doc.py`.
- Rule 7 (remedies self-run), partially: remedy text lives in
  `surface_policy.REMEDIES`, typed by kind (auto / consent / privilege /
  human-prereq): auto remedies name their owning mechanism and their text
  reports it (the freshness footer names the pending or spent automatic
  rebuild; stale handles say "rerun the search"; audit's gap line states
  the self-heal; the daemon spends `--full` escalations before launching
  them). Registry structure pinned in `test_surface_policy.py`; renderers
  consume the registry, so a new hand-written remedy string is a review smell
  rather than a grep target. Doctor's remedies, the semantic remedies, and the
  foreground search path's recovery from a wedged parse cache have not migrated
  into `REMEDIES` yet; until they do, rule 7 holds only where the registry
  reaches.

Target-state, not yet standing: the remaining rule-7 sites named above; rule 4
generalized (probe→pull collapse, top-hit context inline, prose-miss→tool-lane
inline, mega-session auto-narrow); the front-door router (bare agrep choosing
rows vs windows from evidence shape, verbs remaining as expert lanes); and the
per-shape collapse thresholds calibrated from the frozen fixture set. Target
items move up only with a named mechanism and a fixture-set result beside
them.

## Live observation

`agrep tail` is machine JSONL. Its `tail_ready` marker means the watcher's
initial store census is complete and the subscription is armed. `--snapshot`
also waits for that census; its object includes `booting`, `last_err`, and
`degraded_sources`, so access failure cannot be interpreted as an empty store.
Every emitted event precedes the byte-offset commit that acknowledges its
journal line. A failed line handler is retried rather than skipping later
events in the same batch.

Tail snapshots apply agent and session filters before computing their counts
and diagnostics. A scoped snapshot omits global `last_err`; it retains only
`degraded_sources` attributable to the selected agent. SIGINT returns 130 after
any event already emitted as a complete JSON line.

`agrep board` is the other live surface: a bounded window over what is running
on this box right now, where `chats` is indexed history. Its `--json` page
carries the same completeness vocabulary the indexed surfaces use, so the two
cannot be read differently: `matched` with a `matched_basis` of exact or floor,
`shown`, `sessions_complete`, `page_complete`, and a `booting` flag, plus
per-source error detail rather than a silent omission. A partial page names its
bounded rerun as `retry`, and when the local shell cannot safely quote every
filter it supplies `retry_argv` for direct process execution and says why the
copyable form is missing. As with `tail`, a still-scanning or degraded snapshot
is disclosed rather than rendered as an idle box: absence of rows is not
evidence that nothing is running.

Live discovery is deliberately bounded. A new watcher seeds journal-backed
sessions whose stores moved within the last 90 seconds; a session already quiet
inside a long tool call appears on its next write. This is an observer-start
horizon, not a claim that the agent stopped. A tracked working session is kept
alive by exact process evidence when available. Writer disappearance and
working-row expiry produce terminal `done` events instead of silent removal.
