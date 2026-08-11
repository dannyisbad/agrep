# Handle identity: the content-bound citation

`@session:turn` handles are the citation primitive. The installed instructions
tell agents to open a returned handle at its source before citing it. A
citation that can silently serve different content is worse than no citation,
because poisoned recall propagates into new decisions with no detection path.
This document records the product contract and its staged implementation.

## The failure model

A handle names an *address*, session prefix plus positional turn, and both
coordinates are unstable:

1. **Prefix rebinding.** Codex and several other providers mint UUID-like
   session ids, where the 8-hex prefix is the top 32 bits of a 48-bit
   millisecond timestamp: any two sessions created within the same ~65-second
   window share the entire default prefix. Other adapters keep provider-native
   identifiers instead (Cline, for one, uses its task directory name), so the
   prefix collision window depends on the store. Mint-time widening handles
   ambiguity that exists at mint; it cannot see sessions pruned later. When the
   true owner's transcript is pruned (Claude's `cleanupPeriodDays`), the lone
   same-prefix survivor becomes the unique resolution and the handle silently
   rebinds.
2. **Turn renumbering.** Turns are positional over the merged per-session
   timeline (per-adapter counters; `repair_turn_collisions` re-enumerates by
   (ts, turn, text) when duplicate files merge). Any membership change — a
   second copy of the session in another project dir, one line that stops
   parsing after an upstream format change, an interleaved-timestamp row —
   shifts every later turn. `@handle:5` returns turn 6's content, in range,
   invisible to the existing range check.
3. The range check (`around.py`) already refuses out-of-range turns for
   @handles with an honest error. Only the in-range wrong-content case is
   open, which is the dangerous one.

## The design: the handle carries a content claim

```
@1a2b3c4d:214.e7a9
          └──┘ └──┘
          turn digest
```

The digest is 4 lowercase hex chars: the low 16 bits of FNV-1a-64 over the
row's indexed text (UTF-8 bytes). The same stable-FNV idiom the tree already
treats as a persistence contract (`hash_event_identity`): deliberately not a
process-seeded hash, cross-implementable in Rust and Python, frozen forever.

Hashing the **text**, not the address, is the load-bearing choice: a handle
stops being a pointer and becomes a pointer *plus a claim about what it
points at*. If an address change lands on identical text, serving it is
correct, because the content is what was cited. Every silent failure mode becomes
either self-healing or a loud refusal:

| state at resolve time | behavior |
|---|---|
| address resolves, digest matches | serve, silent (the ~100% path) |
| address resolves, digest mismatch | **rescue**: digest-scan a bounded window around the named turn (radius at least 4, widened to the displayed window); unique match → serve that turn with a one-line disclosure (`handle content moved: turn 214 → 219 (session was renumbered)`) |
| rescue finds nothing in that window | refuse, exit 2: `this handle's content is no longer in session <s8> - it was pruned or the handle was minted against a different session` + the stale-handle recovery hint. Content that moved beyond the window is not rescued and reports as absent. |
| rescue finds several rows | refuse, exit 2, ambiguous; never guess between candidate rows |
| legacy digestless handle | resolve address-only with an explicit unverified warning; old notes still work without claiming content verification |

Prefix rebinding is covered by the same table with no special case: the
impostor session's turn 214 fails the digest, rescue finds nothing in the
impostor, the agent gets a refusal instead of wrong content served as truth.

The `@~token:turn` opaque form gains the same `.digest` suffix identically.

Tool-result handles may append `~<event-id>:<start>-<end>`, for example
`@1a2b3c4d:214.e7a9~0123456789abcdef01234567:93-109`. The bounded 96-bit
event id fingerprints the session, event timestamp, and canonical indexed tool
text, so turn renumbering does not change it. The resolver requires both it and
the content digest, preventing a 16-bit digest collision from selecting a
different event in the same turn.
Only after that verification does `around` use the bounded character span to
center its concise output preview. Older handles retain their existing
address-plus-digest behavior.

### Separator: `.`

`#` is a glob operator under zsh extended_glob and pastes unsafely unquoted;
`=` triggers zsh =cmd expansion at word start and reads as assignment
elsewhere. `.` is shell-inert in every POSIX-adjacent shell, visually
subordinate, and already legal inside the session-id grammar the parser
accepts.

### Sizing honesty

The four-hex suffix is a non-cryptographic check for accidental rebinding. It
is a *verification* budget rather than an identity budget, and it can collide:

- False-accept (wrong content passing verify): 2^-16 per resolve. That detects
  drift that happens by accident; it is not integrity against an adversary who
  can choose the colliding text.
- Rescue ambiguity: expected rows sharing a given digest in a 10k-turn
  mega-session ≈ 0.15. When it happens the resolver refuses rather than
  tie-breaking. Widening to 6+ chars buys little and costs every rendered row,
  so 4 is the deliberate choice.

## Implementation map (staged)

**Stage 1, product layer (complete).** The compact codec carries the digest,
all agent-copyable search and recall pointers require it, and both `around`
and direct-handle `recall` verify before serving. Matching content serves,
uniquely moved content is disclosed and rescued, and missing or ambiguous
content refuses. Tool pointers also carry their strong event selector and
bounded display span. The two-value parser remains compatible; callers that
need the full claim use `parse_result_handle_claim`.

**Stage 2, ingest turn stability (agrep-core, separate goal).** Kill
renumbering at the root: persist assigned turns per (agent, session, row
identity) across generations: row identity is the source uuid where the
adapter has one (claude), else FNV-1a over (ts, text). New rows extend;
existing rows keep their turn even when earlier rows appear or vanish.
`repair_turn_collisions` becomes a stable allocator against the persisted
map instead of a re-enumeration. Stage 1's rescue already makes this failure
survivable, so stage 2 is correctness-hardening rather than a blocker. It is
also what would make `@handle:turn` a stable identity instead of a lucky one.

Not in scope: prefix-widening policy (mint-time behavior stays), handle TTLs
(a citation should not expire), embedding digests in `--json` rows beyond
the handle string itself (the handle IS the field).

## The sigil is part of the printed identity

A printed identity is a valid input everywhere identities are accepted. `@` is
the sigil agrep prints in front of both forms, so `compact.normalize_session_arg`
strips it at the parse boundary of `around`, `--chat` and `resume`. A bare
`@session` (what session listings print) is a mutable chat identity: `around`
opens that chat's latest indexed turn. An `@session:turn.digest` handle cites
one digest-verified row and refuses a second turn.
`compact.is_result_handle` is the predicate that separates exact citations
from session identities; only `around` retains the printed sigil long enough
to distinguish its explicit latest-chat shorthand from an unprefixed session
that still requires a turn.

## Interaction with the NUDGE

Stage 1 has landed, so every minted handle now carries the `.digest` suffix.
The taught block text lives in `py/nudge_default.md` and `py/nudge_codex.md`.
Both route agents through the placeholder `agrep around <handle>` rather than
restating the handle grammar, so the digest needed no version bump of its own;
the Codex block's worked transcript is the one place a literal handle appears.
Digestless input stays accepted forever, so nothing taught earlier goes stale.

This document is pinned by `py/test_architecture_doc.py` (file references
must resolve).
