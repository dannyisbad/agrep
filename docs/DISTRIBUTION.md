# Build, test and release

The product is one executable. Python and JavaScript only deliver and execute
it; development scripts under `bench/` are not installed runtime code.

```sh
export CARGO_BUILD_JOBS=6
nice -n 10 cargo fmt --check
nice -n 10 cargo clippy --workspace --all-targets -- -D warnings
nice -n 10 cargo test --workspace
nice -n 10 python -m unittest discover -s bench -p 'test_*.py' -v
nice -n 10 node --test npm/test_bin.js
python bench/check_skips.py
python bench/validate_repo_privacy.py --worktree --index --all-refs
nice -n 10 python bench/third_party_licenses.py --check
```

All fixture histories and data roots are temporary. No test needs a user's real
agent stores. `bench/test-skips.json` is the sole skip policy. It lists platform exclusions
with reasons. The only runtime exclusion is the exact registered guard that
acquires a capable Metal device at the start of four GPU-only tests. Hosted
macOS runners have no capable GPU: the Apple-silicon VM has no Metal device,
and the Intel runner lacks the complete kernel feature set. CPU fallback and
device-selection tests run on every platform. No other runtime skips are allowed. The launcher tests exercise POSIX signal behavior on
POSIX and common argv, stdio and exit behavior on every platform.

## Performance

The blocking performance gate is a matched comparison with agrep 0.4.9, the
release 0.5 replaces, on the same machine, corpus and load. CI installs
`agrep==0.4.9` from PyPI into an isolated virtual environment on all six hosted
runners, then runs:

```sh
nice -n 10 cargo build --release --bins --examples
target/release/examples/semantic_eval "$MODEL" provision
target/release/examples/semantic_eval "$MODEL" cpu
for workload in mixed semantic dense; do
  nice -n 10 target/release/examples/matched --reference "$AGREP_049/bin/agrep" \
    --candidate target/release/agrep --workload "$workload" --model-dir "$MODEL" \
    --runs 20 --build-runs 3 --check --output "matched-$workload.json"
done
```

(`--model-dir` matters only for the semantic workload; use `Scripts` and `.exe`
on Windows.) The workloads are the generated mixed 100,000-row and dense
2,000,000-row stores and a 1,000-row semantic store with a planted meaning
answer. Each version gets its own temporary data directory and both read the
same stores; 0.4.9 also gets a temporary model directory. 0.4.9 runs in its
default configuration, so its resident fork server, freshness daemon and
semantic worker start as they would for a user; every case begins with one
recorded warm-up invocation per version, and background processes of both
versions are ended between cases. Samples alternate 0.4.9 and 0.5, each a fresh
process measured to exit, kept and recorded with the load average: 20 per warm
case, 3 first builds and 3 cold first hybrid queries. 0.4.9's `index` returns
while its daemon is still building the search database, so its index samples
end when the build request it queued has been served; the process exit is
recorded too.

Every sample of both versions is verified: exit codes, non-empty answers where
matches exist, meaning rows for hybrid and `-s` cases, and on the dense corpus
0.5's exact top-40 handles and counts against an independent full-corpus
oracle. A case is a win only when 0.5's median and its p95 are both strictly
below 0.4.9's. A case 0.4.9 fails is reported as a reference failure, never a
win. The gate fails unless every case is a win; the report lists both
statistics, their ratios and every sample for each case.

`docs/perf-budgets.json` keeps the SPEC §9 numbers as documented targets with
their reasons. `examples/bench --check` (see CONTRIBUTING.md) and
`bench/semantic_owner.py` verify results and print measured-vs-target lines;
missing or wrong results fail them, a missed target does not. `semantic_eval
cpu` remains the blocking recall@k quality gate. Current engine limitations are
recorded in [ENGINE.md](ENGINE.md); gate failures remain failures.

## Packages

Cargo uses a portable CPU baseline; runtime SIMD dispatch can select supported instructions.
The release helper remaps build paths in binaries on Windows and Unix.

`bench/targets.json` lists the six target/asset/wheel mappings. A wheel is
`agrep-VERSION-py3-none-PLATFORM.whl`. Build it from a prebuilt matching binary:

```sh
AGREP_WHEEL_PLAT=macosx_11_0_arm64 \
AGREP_WHEEL_BINARY=target/release/agrep python -m build --wheel --no-isolation
python bench/validate_wheel.py dist/*.whl
```

The dependency-free PEP 517 backend checks the executable header and makes a
wheel with only a tiny launcher, the binary, package metadata and licenses. It
cannot build an sdist: **no sdist is published**. Source users build the Git
checkout with Cargo. There is no Python dependency graph or runtime download.

The npm package requires Node 22.15+ and uses first-run acquisition. Its committed
empty checksum map is a development input, never a publishable package.
`bench/prepare_release.py` requires all six validated wheels and matching
binaries, generates SHA-256 sidecars, pins the six hashes in a staged npm package,
and packs it. No uv, pipx, Python bootstrap or arbitrary binary override exists.
Downloads are verified once before entering the content-addressed cache; they
use GitHub HTTPS and an atomic cache replacement. A digest mismatch never executes. POSIX uses execve; Windows
inherits console/stdio and preserves the child exit code.

The cache is version-, platform- and hash-specific under
`$XDG_CACHE_HOME/agrep/bin` (or `~/.cache/agrep/bin`), and
`%LOCALAPPDATA%\agrep\bin` on Windows. It is separate from indexed data.

## Release workflow

Only a pushed `v*` tag starts `release.yml`; identity validation requires the tag
to exactly match Cargo, Python and npm versions. Never tag or publish without
the owner's authorization. Ordinary branch CI cannot publish.

Before every release, run this required local check on a capable Apple-silicon
Mac, in addition to hosted CI:

```sh
CARGO_BUILD_JOBS=6 nice -n 10 cargo test -p agrep-core --lib semantic_ -- --nocapture
```

Confirm that all four GPU tests actually execute, each printing `Metal GPU:`
with the local device name, and pass: `persistent_workspace_reuses_buffers_and_reads_new_inputs`,
`simdgroup_attention_matches_masked_f16_attention_with_strided_values`,
`fused_geglu_preserves_candle_f16_values`, and `residual_norm_matches_metal`.
A passing run without four GPU lines does not satisfy this release check.
The same run must pass the portable device-selection and CPU-fallback tests.
Local validation on 2026-11-02: Apple M2 Max; all four GPU tests printed that
device name and passed, and the semantic-filtered run passed all 33 tests with
zero failures or ignored tests.
The selector requires Apple7-or-newer features (SIMD reductions and matrix
multiply, nonuniform dispatch, 64-bit integer math), unified memory, sufficient
threadgroup limits, and compatible compiled pipelines, including 32-lane attention.
See [Apple's feature tables](https://developer.apple.com/metal/feature-sets/).

1. Run the full reusable CI gates and build all six binaries. Linux builds use
   pinned manylinux_2_28 images; auditwheel must prove the claimed ABI policy.
2. Smoke each binary, then each installed wheel, in isolated fixture homes:
   index, keyword hit/miss, around, status; wheels also download the model and
   exercise semantic retrieval. Validate binary privacy, wheel contents,
   executable architecture, RECORD hashes and release identity.
3. Require the complete six-target set. Compare each wheel binary byte-for-byte
   with its standalone asset; create hashes, licenses and the npm tarball.
4. Stage the GitHub assets as a prerelease, without marking it latest. Install
   the exact npm tarball on all six runners and smoke its real GitHub download,
   keyword and semantic retrieval. A failure leaves only a prerelease.
5. After every smoke passes, publish wheels to PyPI and the tarball to npm.
   Promote the GitHub release to non-prerelease/latest only after both publishes
   succeed. No retries or advisory performance steps.

`bench/third_party_licenses.py` generates notices for the non-dev dependencies
reachable on the six release targets. A crate without a license file fails
unless its Cargo license expression grants MIT (alone or as an alternative); its
notice is then the MIT text attributed to its Cargo authors. Dependency changes
require regenerating and reviewing the notice.

After combining dependency changes, regenerate `THIRD_PARTY_LICENSES.txt` with
`python bench/third_party_licenses.py --write`, review the diff, then run `--check`.
History privacy must be cleared by the integrator before any public push; the
working-tree fixes do not rewrite old commits.
