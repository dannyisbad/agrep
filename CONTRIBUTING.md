# Contributing

The workspace has `crates/agrep-core` (adapters, storage, matching, rendering) and `crates/agrep` (the command-line executable and black-box tests). [docs/README.md](docs/README.md) lists the design documents and contracts.

Run `cargo fmt --check`, `cargo clippy --workspace --all-targets -- -D warnings`, and `cargo test --workspace`. No integration test skips when the executable is missing. Fixture tests set both `AGREP_HOME` and `AGREP_DATA_DIR` to temporary directories.

Build benchmarks with `cargo build --release --bins --examples`. Run `nice -n 10 target/release/examples/bench --rows 100000 --runs 5`, or `--dense --rows 2000000 --runs 20 --check` (which verifies results and prints measured values against the targets in `docs/perf-budgets.json`). The performance gate is `target/release/examples/matched --reference <agrep 0.4.9> --workload mixed|dense|semantic --check`; `--workload real` compares both versions on your own history, read-only, in temporary data directories. `--layout` measures physical FTS alternatives. `--real --home PATH --data SCRATCH` reads real agent inputs into an isolated derived store. Never point a benchmark at an existing user's agrep data directory. `docs/ENGINE.md` describes the engine and its known limits.

All derived mutations belong in a single SQLite transaction. Preserve source-native formats and their read-only guards. Do not introduce compatibility with old agrep data or old process protocols. Record every deliberate difference from 0.4.9's output in `docs/PORTING.md` with its reason.
