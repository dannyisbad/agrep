# Build, test and release

The product is one executable. Python and JavaScript only deliver and execute
it; development scripts under `bench/` are not installed runtime code.

```sh
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
agent stores. `bench/test-skips.json` is the sole skip policy; it lists compile-time platform exclusions with reasons and permits no runtime
skips. The launcher tests exercise POSIX signal behavior on
POSIX and common argv, stdio and exit behavior on every platform.

## Performance

`docs/perf-budgets.json` is the only budget file. Each limit states its SPEC §9
basis, and each runner class states its policy. There are no environmental slack
multipliers. Changes to a limit require a written reason in the same commit.

```sh
nice -n 10 cargo build --release --bins --examples
nice -n 10 target/release/examples/bench --binary target/release/agrep \
  --rows 100000 --runs 20 --build-runs 3 --check --output mixed.json
nice -n 10 target/release/examples/bench --binary target/release/agrep \
  --semantic --rows 1000 --runs 20 --build-runs 3 --check --output semantic.json
nice -n 10 target/release/examples/bench --binary target/release/agrep \
  --dense --rows 2000000 --runs 20 --build-runs 3 --check --output dense.json
```

Use `.exe` on Windows. Samples time a fresh process through full output and exit;
none are discarded. Reports retain all samples and exit codes. p95 needs at
least 20 samples. First-index medians use three complete builds. Dense tests
require exactly 2M requested rows and budgets reserving 2x headroom. Hybrid
checks require a meaning row (`sem_score` or `lane: semantic`), not just a fast
keyword answer. They download the model into a temporary data root, drain the
fixture embedding backlog, then measure first and repeated query processes.
Installation/download time is outside the query deadline. The full suite is
blocking on six hosted runners. Current engine performance limitations are
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
