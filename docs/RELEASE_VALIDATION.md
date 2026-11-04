# Release validation

Release requires formatting, strict all-target clippy, workspace tests, package
contract tests, the exact platform-test registry, working-tree/index and history
privacy gates, generated dependency notices, and the matched full-process
performance comparison with agrep 0.4.9 described in `DISTRIBUTION.md`. Failing
measurements remain failures; `docs/perf-budgets.json` records the performance
targets.

Every executable smoke (`bench/smoke.py`) uses a temporary HOME, agent home,
data root and model location. The semantic smoke contains unrelated distractors
and must rank the planted answer first. CI tests all six supported targets;
local macOS checks do not establish Linux or Windows runtime behavior.

The release workflow stages only a prerelease until all six installed npm
smokes and both package publishes succeed. See `DISTRIBUTION.md` for commands
and ordering.
