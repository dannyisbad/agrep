# Release validation

Release requires formatting, strict all-target clippy, workspace tests, package
contract tests, the exact platform-test registry, working-tree/index and history
privacy gates, generated dependency notices, and the full-process performance
gates in `docs/perf-budgets.json`. Failing measurements remain failures.

Every executable smoke uses a temporary HOME, agent home, data root and model
location. Semantic smoke contains unrelated distractors and must rank the
planted answer first. CI tests all six supported targets; local macOS checks do
not establish Linux or Windows runtime behavior.

The release workflow stages only a prerelease until all six installed npm
smokes and both package publishes succeed. No release has been authorized by
this validation work. Before any public push, the integrator must clear the
history privacy gate separately; working-tree cleanup does not erase history.

See `DISTRIBUTION.md` for commands and ordering. Current fixer validation results
are reported with the commits, rather than retained as raw logs in this tree.
