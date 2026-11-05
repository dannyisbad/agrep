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
runners. After the tests, the `test` job runs:

```sh
nice -n 10 cargo build --release --workspace --bin agrep \
  --example matched --example semantic_eval --example semantic_scale
target/release/examples/semantic_eval "$MODEL" provision
target/release/examples/semantic_eval "$MODEL" cpu
for workload in mixed semantic; do
  nice -n 10 target/release/examples/matched --reference "$AGREP_049/bin/agrep" \
    --candidate target/release/agrep --workload "$workload" --model-dir "$MODEL" \
    --runs 20 --build-runs 3 --check --output "matched-$workload.json"
done
```

and `bench/semantic_owner.py`. A separate `dense` job on each runner builds
`agrep` and `matched` and runs the same command with `--workload dense`.
(`--model-dir` matters only for the semantic workload; use `Scripts` and `.exe`
on Windows.) The workloads are the generated mixed 100,000-row and dense
2,000,000-row stores and a 1,000-row semantic store with a planted meaning
answer. Each version gets its own temporary data directory and both read the
same stores; 0.4.9 also gets a temporary model directory. 0.4.9 runs in its
default configuration, so its resident fork server, freshness daemon and
semantic worker start as they would for a user; every case begins with one
recorded warm-up invocation per version, and background processes of both
versions are ended between cases. Samples alternate 0.4.9 and 0.5, each a fresh
process measured to exit, kept and recorded with the load average. A case plans
20 samples per version, 3 for first builds and cold first hybrid queries.
0.4.9's `index` returns while its daemon is still building the search database,
so its index samples end when the build request it queued has been served; the
process exit is recorded too.

Every sample of both versions is verified against the work its case asks for:
exit codes; non-empty answers where matches exist; the message and session totals
of the generated corpus in every index report; after each measured refresh and
each changed-source drain, the unique text appended before it found exactly once
by an untimed `--no-auto` count; exact `-c` counts from a full-corpus oracle;
meaning rows for hybrid and `-s` cases, with the planted answer first on the
semantic corpus; and on the dense corpus 0.5's exact top-40 handles. A command
that returns without doing that work fails its case. A case is a win only when
0.5's median and its p95 over the samples
taken are both strictly below 0.4.9's. When 0.4.9 crashes, exits with the wrong
code or leaves broken evidence, the case is a reference failure and never a
win. When 0.4.9 reaches its expected exit on every sample but cannot prove a
correct answer (for example an incomplete hybrid result, or an index that has
not settled), it spent at least its measured time without one, so its samples
are a lower bound: the case is a win over a degraded answer only if every 0.5
sample is verified and 0.5's median and p95 are strictly below that bound. The
gate fails unless every case is a win; the report lists both statistics, their
ratios, how each case's sampling ended and every sample.

### Early stops

A case stops early only when its first five pairs (two pairs for a three-sample
case) are completely separated:

- Every 0.5 sample slower than every 0.4.9 sample: the case ends, a loss.
- Every 0.5 sample faster: 0.4.9 stops, and 0.5 takes its remaining planned
  samples alone, so its p95 still rests on all of them. The case ends when every
  one is still faster than every 0.4.9 sample. A single one that is not sends
  0.4.9 back: the versions alternate again until 0.4.9 has all its planned
  samples, and every sample of both versions counts.

Otherwise both versions take all their planned samples. What this guarantees
(`crates/agrep/tests/matched_gate.rs` checks each point):

- Separation after more pairs implies separation of the first five, so one
  check after five pairs is the check after every pair. There are no repeated
  looks to correct for.
- With both versions equally fast, every ordering of the samples is equally
  likely. An early win then needs 0.4.9's five samples to be the slowest five of
  25, probability 1/C(25,5) = 1/53,130. An early loss has probability
  1/C(10,5) = 1/252. For comparison, 20 full pairs of such a tie pass both
  statistics about 30% of the time (simulated).
- An early win's p95 comparison is the one 20 pairs would make, whatever
  0.4.9's skipped samples would have measured: 0.4.9's 20-sample p95 is its
  second-slowest sample, at least the second-slowest of the five it took. Its
  median comparison could differ only if at least 11 of the 15 skipped samples
  had been faster than 0.5's median, which is faster than every 0.4.9 sample
  taken. If 0.4.9's samples are exchangeable, that rank pattern has probability
  at most C(9,5)/C(20,5) = 126/15,504 (0.81%).
- Three-sample cases are decided exactly: after two separated pairs, no value
  of the skipped samples changes either comparison.
- 0.5's samples taken alone can end an early win but never create one, so every
  early win also passes the plain rule "the first five pairs are separated".
  The tail requirement is what keeps a 0.5 version with a fast median and a
  slow p95 from passing on five samples.
- An early loss never makes a case pass.

A 0.5 sample taken alone in a refresh case also appends the turn 0.4.9's sample
would have appended. 0.4.9 catches up on appended turns, untimed, before it
resumes and before the next case.

`docs/perf-budgets.json` keeps the performance targets with their reasons.
`examples/bench --check` (see CONTRIBUTING.md) and `bench/semantic_owner.py`
verify results and print measured-vs-target lines; missing or wrong results
fail them, a missed target does not. `semantic_eval cpu` remains the blocking
recall@k quality gate (see [SEMANTIC.md](SEMANTIC.md)). Engine limits are listed
in [ENGINE.md](ENGINE.md); gate failures remain failures.

## Packages

Cargo uses a portable CPU baseline (ARMv8.0-A on Linux and Windows arm64);
runtime SIMD dispatch can select supported instructions. CPU inference uses F32.
`vendor/gemm-common` patches four FP16 assembly helpers in 0.19.0 with
per-function `neon,fp16` annotations, retaining the existing runtime feature
checks without enabling FP16 across the binary. See its
[patch notes](../vendor/gemm-common/PATCH.md).
The Linux arm64 CI test job and release binary smoke run version, fixture
indexing, keyword search, recall and CPU meaning search under Cortex-A72 QEMU
emulation with the job's provisioned model; any non-zero exit fails the job.
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
CI's `privacy-history` job scans every ref's history with
`python bench/validate_repo_privacy.py --index --all-refs`.
