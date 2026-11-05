The expected rows are the installed 0.4.9 CLI's real `--json` output on this
synthetic pi fixture, captured on 2026-11-05. Each case's `args` was followed by
`--json`; `AGREP_HOME` named this fixture's home, and `AGREP_DATA_DIR`, `HOME`,
`XDG_CONFIG_HOME` and `XDG_CACHE_HOME` named fresh temporary directories.
`AGREP_NO_DAEMON=1`, `AGREP_NO_RESIDENT=1` and the 0.4.9 bundled Rust helper were
set. A first keyword search built the isolated reference index.

Only speaker and snippet are retained: together they identify every prose and
tool row in this fixture. The test compares multisets, including duplicates.
It covers one-, two- and three-or-more-character queries, `-w`, decomposed
accents, Python case equivalences and compatibility spellings in both
directions, plus the lowercase shortcut that expands dotted İ for keyword
matching but not `-w`. Compatibility normalization must not invent a literal
match.

`lowercase_combinations.json` is a second 0.4.9 CLI fixture. Its eight input
messages distinguish literal combining dots, dots contributed by lowercasing
İ, and nonmatching dotless-ı spellings. The expected rows were captured with
the stored pattern, `-n 0 --json`, both without and with `-w`, using fresh
isolated roots. It pins candidate completeness when one term contains several
possible expansions.

`fallback.json` captures the same installed CLI in the `compact` profile, with
`-n 40` and no machine-output flag. Its natural-language queries have no phrase
or all-terms hits, so they exercise the last content fallback. Speaker and
snippet are parsed from its compact rows; a lowercase match is literal and
must not add NFKC or Python `re.I` equivalences in this lane.

`priority.json` contains twelve synthetic texts and six installed-CLI results,
including `-c` output, for prose and tools. Long padding separates a raw case
alias from a later lowercase occurrence. It pins lowercase-first windows, the
first raw fallback occurrence, and plural variants whose raw fallback must
still use the original token. Sigma cases also pin Python's contextual lowercase.
