<!-- PreCompact hook payload. Appended to the summarizer's prompt as custom
     instructions. Written as numbered sections continuing the bundled nine,
     because the summarizer obeys the numbered schema and ignores prose asides.
     Section order is deliberate: recovery comes FIRST among the appended
     sections because placement-first is what won the adoption benchmark -
     do not renumber it behind the others.
     Installed by `agrep setup` to ~/.claude/hooks/compact-contract.md
     (`agrep setup --no-hook` skips installing it). Remove with
     `agrep remove`, or by deleting this file and the PreCompact block in
     ~/.claude/settings.json. Never auto-repaired. -->

10. Post-compact recovery: this summary is lossy, but the conversation it replaced is still indexed and partly recoverable. State under the heading "Post-compact recovery", as an instruction rather than an option: whenever the work needs any exact value, decision, error text, or result from before this boundary, the resumed agent's FIRST move is `agrep postcompact`, bounded rather than a dump, before acting or reconstructing from memory. Say explicitly that `agrep recall` is the wrong tool for this: recall hides the current context window's echoes, demotes this session's turns to ~self side-evidence (`--self` overrides), and ranks the actual tail against lookalikes from all history - while postcompact serves verbatim excerpts of the newest pre-boundary turns, marking what it omits, with no query to get right. If postcompact cannot identify the calling session, name it explicitly with `agrep postcompact --session <id>`. Acting on a guess where this summary is silent is the failure this section exists to prevent.

11. Retrieval anchors: for each pivot in the conversation (frame change, key discovery, user correction, commissioned result that arrived), quote one short distinctive VERBATIM phrase of 4 to 10 words, copied exactly and never paraphrased, chosen so that grepping the transcript for that string lands on that moment and nowhere else. Output them as a bulleted list under the heading "Retrieval anchors". A paraphrased anchor is worse than none, because it grep-misses silently. If a pivot has no quotable phrase, name the pivot and write "no anchor" rather than inventing one.

12. Killed hypotheses and open contradictions: every hypothesis tested and disproved, each with the observation that killed it; every anomaly still unexplained, flagged open. A resumed agent reviving a dead hypothesis is the costliest compaction failure. Also list anything commissioned but not yet consumed (pending reviews, subagent verdicts, background jobs whose results have not been read) under the heading "Commissioned, unread".
