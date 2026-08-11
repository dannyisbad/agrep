## Context

This session just resumed from a compaction. The summary above is lossy: it
records what was decided but routinely drops the exact values, error texts,
paths, and results those decisions rest on. The replaced conversation is
still indexed and partly recoverable.

## Instructions

- If this request needs anything the summary refers to but does not fully
  state, run `agrep postcompact` FIRST - before acting or reconstructing
  from memory. It returns this session's newest pre-boundary turns as
  verbatim excerpts, bounded, marking what it omits, no query needed.
- Do not use `agrep recall` for facts from this session: recall hides the
  current context window, demotes this session's turns to ~self
  side-evidence (`--self` overrides), and ranks your own tail against
  lookalikes from all history. postcompact quotes the tail and needs no
  query.
- If postcompact reports it cannot identify the calling session, rerun as
  `agrep postcompact --session <id>`.

## Example

    # summary: "the tests were green" - but you need the exact counts
    $ agrep postcompact
    # -> verbatim excerpts of the newest pre-boundary turns
