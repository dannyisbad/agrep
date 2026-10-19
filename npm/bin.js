#!/usr/bin/env node
// agrep is a python package with a bundled rust binary; this npm shim just finds a
// runner for it. uv is preferred because uvx manages python itself - it works on a
// box with no python at all. pipx is the fallback. If neither exists we print the
// one-liner that fixes it instead of half-installing anything ourselves.
//
// AGREP_PYPI_SPEC overrides what gets run (e.g. a local wheel path, or agrep==X.Y.Z).

"use strict";
const { spawnSync } = require("child_process");
const { version } = require("./package.json");

const SPEC = process.env.AGREP_PYPI_SPEC || `agrep==${version}`;
const args = process.argv.slice(2);

// uv/pipx run agrep out of a cache that a prune wipes while this package
// stays installed; the sentinel tracks this file instead (see teach.install_anchor).
const ANCHOR_ENV = "AGREP_INSTALL_ANCHOR";

function probe(cmd) {
  const r = spawnSync(cmd, ["--version"], { encoding: "utf8", shell: false });
  return !r.error && r.status === 0 ? String(r.stdout || "") : null;
}

function uvVersion(text) {
  const m = /\buv\s+(\d+)\.(\d+)\.(\d+)/.exec(text || "");
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

// `--exclude-newer-package agrep=false` opts agrep out of a user's global
// exclude-newer pin; only uv >= 0.9.25 parses the `=false` form, older uv
// rejects the whole invocation, so the flag rides along only where it parses.
function excludeNewerOptOut(v) {
  return v !== null && (v[0] > 0 || v[1] > 9 || (v[1] === 9 && v[2] >= 25));
}

function uvArgs(uvVersionText, rest) {
  const flag = excludeNewerOptOut(uvVersion(uvVersionText))
    ? ["--exclude-newer-package", "agrep=false"] : [];
  return ["tool", "run", ...flag, "--from", SPEC, "agrep", ...rest];
}

function run(cmd, cmdArgs) {
  const r = spawnSync(cmd, cmdArgs, {
    stdio: "inherit", shell: false,
    env: { ...process.env, [ANCHOR_ENV]: __filename },
  });
  if (r.error) {
    console.error(`agrep: failed to launch ${cmd}: ${r.error.message}`);
    process.exit(1);
  }
  process.exit(r.status === null ? 1 : r.status);
}

const uv = probe("uv");
if (uv !== null) {
  // `uvx agrep` == `uv tool run agrep`; --from lets AGREP_PYPI_SPEC point anywhere
  run("uv", uvArgs(uv, args));
} else if (probe("pipx") !== null) {
  run("pipx", ["run", "--spec", SPEC, "agrep", ...args]);
} else {
  console.error(
    "agrep needs uv (or pipx) to run - it's a python package with a bundled rust binary.\n" +
    "install uv (one line, manages python itself):\n\n" +
    (process.platform === "win32"
      ? '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
      : "  curl -LsSf https://astral.sh/uv/install.sh | sh") +
    `\n\nthen \`agrep\` works. This npm package runs PyPI agrep==${version}; ` +
    `you can also install that directly with \`uv tool install agrep==${version}\`.`
  );
  process.exit(1);
}
