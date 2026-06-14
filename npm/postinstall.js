#!/usr/bin/env node
// Warm the matching PyPI tool when installed globally through npm. npm's own
// `bin` shim is still the portable PATH entrypoint; this just makes the Python
// side ready too when uv is available.

"use strict";

const { spawnSync } = require("child_process");
const { version } = require("./package.json");

const spec = process.env.AGREP_PYPI_SPEC || `agrep==${version}`;
const npmGlobal = String(process.env.npm_config_global || "").toLowerCase() === "true";
const forced = Boolean(process.env.AGREP_POSTINSTALL_FORCE);

if (process.env.AGREP_SKIP_POSTINSTALL) {
  process.exit(0);
}

if (!npmGlobal && !forced) {
  process.exit(0);
}

function has(cmd) {
  const probe = spawnSync(cmd, ["--version"], { stdio: "ignore", shell: false });
  return probe.status === 0;
}

function tryRun(cmd, args) {
  const result = spawnSync(cmd, args, { stdio: "inherit", shell: false });
  return !result.error && result.status === 0;
}

if (has("uv")) {
  console.log(`agrep: installing matching PyPI tool with uv (${spec})`);
  const ok = tryRun("uv", [
    "tool",
    "install",
    "--force",
    "--exclude-newer-package",
    "agrep=false",
    spec,
  ]);
  if (!ok) {
    console.warn("agrep: uv tool install failed; npm's agrep shim will still run via uv at command time.");
  }
  process.exit(0);
}

console.warn(
  "agrep: uv was not found, so the PyPI tool was not preinstalled. " +
  "npm's agrep shim is still installed; install uv for first-run execution."
);
