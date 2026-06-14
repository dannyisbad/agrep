#!/usr/bin/env node
// agrep is a python package with a bundled rust binary; this npm shim just finds a
// runner for it. uv is preferred because uvx manages python itself — it works on a
// box with no python at all. pipx is the fallback. If neither exists we print the
// one-liner that fixes it instead of half-installing anything ourselves.
//
// AGREP_PYPI_SPEC overrides what gets run (e.g. a local wheel path, or agrep==0.1.0).

"use strict";
const { spawnSync } = require("child_process");
const { version } = require("./package.json");

const SPEC = process.env.AGREP_PYPI_SPEC || `agrep==${version}`;
const args = process.argv.slice(2);

function has(cmd) {
  const probe = spawnSync(cmd, ["--version"], { stdio: "ignore", shell: false });
  return probe.status === 0;
}

function run(cmd, cmdArgs) {
  const r = spawnSync(cmd, cmdArgs, { stdio: "inherit", shell: false });
  if (r.error) {
    console.error(`agrep: failed to launch ${cmd}: ${r.error.message}`);
    process.exit(1);
  }
  process.exit(r.status === null ? 1 : r.status);
}

if (has("uv")) {
  // `uvx agrep` == `uv tool run agrep`; --from lets AGREP_PYPI_SPEC point anywhere
  run("uv", [
    "tool", "run",
    "--exclude-newer-package", "agrep=false",
    "--from", SPEC,
    "agrep",
    ...args,
  ]);
} else if (has("pipx")) {
  run("pipx", ["run", "--spec", SPEC, "agrep", ...args]);
} else {
  console.error(
    "agrep needs uv (or pipx) to run — it's a python package with a bundled rust binary.\n" +
    "install uv (one line, manages python itself):\n\n" +
    (process.platform === "win32"
      ? '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
      : "  curl -LsSf https://astral.sh/uv/install.sh | sh") +
    `\n\nthen \`agrep\` works. This npm package runs PyPI agrep==${version}; ` +
    `you can also install that directly with \`uv tool install agrep==${version}\`.`
  );
  process.exit(1);
}
