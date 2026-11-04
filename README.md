# agrep

**Your agents already solved this. Find it.**

Search, read and resume local conversations across Claude Code, Codex, Gemini,
Cursor, OpenCode, Cline, Kimi, Crush, Antigravity, pi and oh-my-pi. One Rust
binary, one local index. Your conversations stay on your machine.

```sh
uv tool install agrep              # or: pipx install agrep
npm install -g @mundy/agrep        # alternative; Node 22.15+
agrep setup
```

[Standalone binaries](https://github.com/dannyisbad/agrep/releases) need neither
Python nor Node. Wheels bundle the binary; npm downloads its version's binary
from GitHub and verifies the SHA-256 pinned in the package. Linux glibc 2.28+,
macOS 11+, and Windows: x64 and arm64. Linux and Windows arm64 need ARMv8.2-A
with FP16 (Raspberry Pi 5, Graviton2 and later; not Raspberry Pi 3/4 or Graviton1).

Five commands to remember:

```sh
agrep 'connection pool exhausted' --here  # search this project's history
agrep chats 'release workflow'           # find a conversation
agrep around @session:214.e7a9 -C 3       # read a returned handle in context
agrep recall 'how we fixed the timeout'  # recover useful context in one call
agrep resume @session                    # continue in its native agent
```

`agrep` alone shows status. Add `--json` for structured output, `--lexical` for
keywords only, or `-s` for meaning search. `agrep --help` lists the rest.

`agrep setup` adds instructions and compaction hooks for supported agents,
with consent. Agents learn to find previous solutions and open the source before
citing it. `agrep remove` removes agrep's integrations, preserving your other
instructions. `agrep setup` downloads and verifies the meaning-search model once; keyword search works
without it.

The index and configuration live in:

| Platform | Data directory |
|---|---|
| macOS | `~/Library/Application Support/agrep/0.5` |
| Linux | `$XDG_DATA_HOME/agrep/0.5`, or `~/.local/share/agrep/0.5` |
| Windows | `%LOCALAPPDATA%\agrep\0.5` |

`AGREP_DATA_DIR` overrides that directory. `AGREP_HOME` changes the home used to
discover agent histories. Source conversations stay in their native stores.
Search reads the committed index and checks for changed sources. Updates are processed
automatically; `--no-auto` reads without checking. The first index is built inline.

Compared with 0.4.9, median wall time for a complete command on a hosted
Ubuntu x64 runner, 100,000-row history:

| Command | 0.4.9 | 0.5 |
|---|---:|---:|
| first `agrep index` | 19.9 s | 7.7 s |
| check with nothing changed | 383 ms | 12 ms |
| refresh after one new turn | 3.0 s | 28 ms |
| keyword search | 927 ms | 33 ms |
| `recall` | 569 ms | 49 ms |
| first meaning query after setup | 1.07 s | 62 ms |
| meaning-only search (CPU) | 135 ms | 46 ms |

On a 1.2-million-row personal history (Apple M2 Max) the first index takes
84 s against 0.4.9's 319 s. CI repeats the comparison on Linux, macOS and
Windows, x64 and arm64, and every case must beat 0.4.9 at median and p95.

**Before upgrading from 0.4, run `agrep remove` with 0.4** to remove its
instruction blocks, hooks and uninstall sentinel. Then install 0.5, build a
fresh index and run `agrep setup`. 0.5 does not read or migrate old data.
After saving any archives you want, you can delete the old 0.4 data under
`~/Library/Application Support/agrep` (macOS), `~/.local/share/agrep` (Linux),
or `%LOCALAPPDATA%\agrep` (Windows); retain the `0.5` subdirectory.

[Output contracts](docs/OUTPUT_CONTRACTS.md) · [Development and release](docs/DISTRIBUTION.md)
