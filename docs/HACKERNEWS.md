# Hacker News launch draft

## Suggested title

**Show HN: agrep – grep/search everything your AI agents have done**

## Draft

I built `agrep`, a local search engine for the work AI coding agents leave on
disk.

It indexes the session formats used by Claude Code, Codex, opencode, Gemini,
Cursor, pi, and other agent tools into one normalized, searchable index. The
useful part is the loop after search: every result can open the surrounding
source context, carry a digest-checked handle, or resume the original chat.

```console
uv tool install agrep
agrep setup
agrep "retry backoff"
agrep around @handle
agrep resume @handle
```

The core path is keyword search; meaning search is an optional local ONNX tier.
Sources are opened read-only, transcript content is not uploaded, and derived
data stays in the local data directory. It also has bounded JSON output for
agent workflows, live activity views, coverage checks, and atomic publication
so a failed refresh does not replace the last good index.

The release gate covers selective queries, large hit sweeps, cold ingest, and
first-hit streaming. The full benchmark contracts are in `bench/` rather than
being a single hand-picked demo number.

I would especially like feedback on which agent store should be supported next,
what evidence people need before trusting a local cross-agent index, and where
the search workflow breaks down.

**Release:** https://github.com/dannyisbad/agrep/releases/tag/v0.4.6
**Install:** https://pypi.org/project/agrep/0.4.6/
**Source:** https://github.com/dannyisbad/agrep

## Launch checklist

- [ ] `v0.4.6` tag and GitHub release are published and non-draft.
- [ ] PyPI `agrep==0.4.6` is installable from a clean environment.
- [ ] npm `@mundy/agrep@0.4.6` is published through trusted publishing.
- [ ] `python bench/release_identity.py --tag v0.4.6` passes.
- [ ] `python bench/validate_repo_privacy.py --index --all-refs` passes.
- [ ] `python bench/perf.py --check` passes, including first-hit streaming.
- [ ] Semantic scale is either green or its platform limitation is stated.
- [ ] README install and three-command demo work from a clean install.
- [ ] Post with the `Show HN:` title; do not cross-post a launch announcement
      before the release assets are live.
- [ ] Watch the first comments for unsupported stores, privacy questions, and
      install failures; answer with reproducible commands and known limits.
