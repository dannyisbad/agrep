# Post-compaction adoption

A compaction summary is lossy by construction. It records what was decided and
routinely drops the exact values, names, and paths those decisions rest on. The
conversation behind it is still on disk, and `agrep postcompact` returns that
pre-boundary tail verbatim, with no query to get right.

This benchmark asks the only question that matters for a tool like that: does an
agent resuming from a summary actually reach for it, unprompted, and does the
fact the summary dropped end up in the answer.

Everything below was measured against live agent CLIs. No transcript is mocked
and no reach is inferred from prose: a call counts only when it appears as an
actual invocation in the seat's own transcript, parsed from argv. The harness
and its fixture corpora are not part of this repository, so these are reported
results rather than a command you can run here.

## What is being measured

Each task plants a real session whose turns hold one specific fact, then resumes
that session from a compaction summary that omits it, and gives the resumed
agent work that needs it. Two things are then true at once, which is the entire
point: the durable transcript still holds every pre-boundary turn, while the
live context holds only the summary. That divergence is the state a history tool
exists to serve.

Six signals are scored separately, because reach without recovery and recovery
without reach are both real outcomes:

| signal | meaning |
|---|---|
| reached | any agrep call at all, the crudest adoption signal |
| postcompact | `agrep postcompact` specifically, not just any search |
| postcompact first | it was the first agrep call, not a later fallback |
| recovered | the omitted fact reached the answer |
| wrong | a forbidden guess reached the answer |
| grind | four or more agrep calls in one transcript |

Four of the tasks are recovery tasks, each omitting a different kind of fact: a
value with a stated ceiling, a name plus a location, a rejected approach, and a
value pair behind a summary that reads self-sufficient. In every one the
intuitive guess is also the mistake on record, so guessing is distinguishable
from knowing. Planting is verified rather than asserted: the answer really is in
the named session, and the phrases a task calls absent appear nowhere.

A fifth task is an over-triggering control where the summary is sufficient and
reach is the anti-goal. It is reported separately throughout. Pooling it into a
recovery rate would hide the one cost that guidance can impose.

## The result

Three arms, on the four recovery tasks, across two Claude tiers:

| arm | n | reached | used postcompact | recovered | grind | mean calls |
|---|---:|---:|---:|---:|---:|---:|
| plain summary, nothing installed | 30 | 0 | 0 | 1 | 0 | 0.00 |
| plain summary, standing instruction block installed | 29 | 28 | 0 | 23 | 17 | 4.62 |
| summary carrying a recovery tail | 151 | 151 | 151 | 151 | 2 | 1.21 |

The first row is the finding the rest of the work exists to answer. Counting the
over-triggering control as well, a resumed agent holding a plain summary reached
for the tool in **0 of 35** runs. Not rarely: never. The single recovered
control answer contained the fact with no agrep call at all, which the scoring
counts as recovered because the metric is the outcome rather than the mechanism.

Documenting the tool in the agent's standing instruction file moves reach almost
to ceiling and recovery to 23/29, but it is not a good trade: none of those 28
reaching runs used `agrep postcompact`, they searched instead, and 17 of 29
crossed into grind at a mean of 4.62 calls.

A summary that ends with a recovery tail behaves differently in kind. All 151
runs reached, all 151 used `agrep postcompact`, in all 151 it was the first
agrep call, and all 151 recovered the omitted fact, at a mean of 1.21 calls and
two grinds. Two runs also emitted a forbidden guess alongside the recovered
fact.

| comparison | Fisher exact, two-sided |
|---|---:|
| recovery-tail reach vs plain control reach | 6.3e-35 |
| recovery-tail recovery vs instruction-block recovery | 1.1e-05 |
| recovery-tail grind vs instruction-block grind | 2.7e-14 |

Rates from small cells look more precise than they are, so every headline rate
carries a Wilson interval: 151/151 is [0.975, 1.000] and 0/35 is [0.000, 0.099].
Both are exact and computed without approximation at the edges, where the normal
interval collapses to zero width and lies.

## The price, measured rather than assumed

Guidance that says "always pull" scores perfectly on every task that can only
punish under-reach. The over-triggering control is where that costs something:
the summary already states everything the pending work needs, so any pull is a
command and a context window spent on a question answered in front of the agent.

| arm on the over-triggering control | n | reached | mean calls |
|---|---:|---:|---:|
| plain summary, nothing installed | 5 | 0 | 0.00 |
| plain summary, standing instruction block installed | 5 | 5 | 3.20 |
| summary carrying a recovery tail | 25 | 23 | 1.04 |

Reported plainly: the recovery tail does over-trigger here, at roughly one call.
The conditional framing every shipping candidate uses ("recover what the work
needs, when this text does not state it") did not prevent the pull on a summary
that was in fact sufficient. What it bounded was the cost, which is one command
against the instruction block's 3.20.

## What won the wording comparison

Five wordings were measured, each a versioned fixture so every result records
which text produced it. The first places the recovery command inside a
retrieval-anchors paragraph, alongside a search idiom the reader has to compose.
The second changes nothing about the claims and only moves recovery into its own
section, placed first among the appended sections, and states what the command
returns so the reader can judge the cost before running it. Later versions add a
checkable trigger, cut the prose to the shortest text carrying every claim, and
one drops the condition entirely as a backfire probe.

On the recovery tasks the wordings do not separate on the outcome: every version
recovered on every run. The separation is in cost and in error.

| wording | n | recovered | wrong | grind | mean calls |
|---|---:|---:|---:|---:|---:|
| recovery inside the anchors paragraph | 53 | 53 | 1 | 1 | 1.32 |
| recovery first, in its own section | 20 | 20 | 0 | 0 | 1.05 |
| plus a checkable trigger | 20 | 20 | 0 | 0 | 1.20 |
| shortest text carrying every claim | 38 | 38 | 1 | 1 | 1.24 |
| unconditional, backfire probe | 20 | 20 | 0 | 0 | 1.05 |

Placement-first is the cheapest shipping candidate and the only one with a clean
sheet on both error columns at its sample size. That is why the shipped contract
puts recovery first among the sections it appends and says so in the file, so a
later edit does not renumber it behind the others as a tidiness change.

Two honest limits on that table. The first two rows come from different runs, so
part of the gap between 1.32 and 1.05 is between-run variation rather than
wording; where all five were measured head to head in a single run they were
indistinguishable on recovery and call count, every version recovering on every
run at exactly 1.00 calls, and the only forbidden guess in that run came from
the anchors-paragraph placement. And 20 runs
cannot separate 1.05 from 1.20. The claim placement-first earns is that it costs
no more than any alternative and never scored worse, not that it is measurably
better than the checkable-trigger version.

The unconditional probe is not a shipping candidate and was never treated as
one. It matched the conditional wordings rather than beating them, which is the
useful result: dropping the condition bought no additional adoption, so the
conditional framing is not leaving anything on the table.

## The effect is not a property of Claude

The same planted summaries, byte for byte, were run through two non-Anthropic
models to separate the guidance from the model. Three tasks, three runs per
cell, with caller identity supplied so the command itself can resolve the
session. The identity condition those harnesses actually ship with is the
subject of the next paragraph, and is worse.

| model | arm | reached | recovered | mean calls |
|---|---|---:|---:|---:|
| deepseek-v4-flash | recovery tail | 3/3 | 3/3 | 2.0 |
| deepseek-v4-flash | plain summary | 0/3 | 2/3 | 0.0 |
| grok-4.5 | recovery tail | 3/3 | 3/3 | 3.7 |

The zero-to-recovery flip reproduces. The plain control reaches 0/3, mirroring
the Claude fleet's 0/35, and its two recovered answers came from facts the
summary already carried rather than from any retrieval. Both models recover
under guidance, so the win belongs to the summary contract rather than to one
vendor's instruction following.

That comparison also surfaced the most consequential product finding of the
whole exercise, and it is a gap rather than a win. Caller identity resolves
exactly two exported session variables, one for Claude Code and one for Codex.
An agent harness that exports neither cannot be identified, so bare
`agrep postcompact` fails closed there with a nonzero exit rather than serving
the wrong session. Under the identity conditions those two models really ship
with, the command failed every time and recovery still mostly landed, because
the anchors in the summary are strong enough that a model told to run
`agrep postcompact` falls back to searching for a verbatim phrase when the
command refuses. It landed at a cost the working cell does not pay: 24 calls
against 17 for the same six runs, and in one of those six it did not land at
all.

The gap is now partly closed. `agrep postcompact --session <id>` recovers a
named session's pre-boundary tail for callers that cannot be auto-identified,
the identity-unavailable error names that lever, and the shipped guidance
teaches the fallback. What remains is that bare `postcompact` still fails for
those harnesses until the agent reads the error. Quantifying how much grind the
fallback removes needs a re-run that has not been done.

## Limits

- The truncated context is constructed rather than produced by a real
  compaction. What is real is the state being measured, an agent holding a
  summary while the index holds the turns behind it. What is simulated is how it
  got there.
- The non-Anthropic seats receive the summary as their prompt, because that
  harness starts a fresh session and cannot be pointed at an existing
  transcript. Same information in the same order, different mechanism, and every
  record names which one it was.
- Five tasks and two Claude tiers. The reach result is wide enough that its
  interval does not reach the other arm; the wording differences are not, and
  are reported as costs rather than rankings.
- Recovery rates and grind rates are not comparable across arms measured on
  different task sets. Every table above holds the task set fixed within itself.
