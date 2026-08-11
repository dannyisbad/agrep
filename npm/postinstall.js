#!/usr/bin/env node
// Warm uv's ephemeral tool cache on global installs. The npm shim remains the
// entrypoint and resolves the pinned PyPI package on every run.

"use strict";

const { spawnSync } = require("child_process");
const { version } = require("./package.json");

const spec = process.env.AGREP_PYPI_SPEC || `agrep==${version}`;
const npmGlobal = String(process.env.npm_config_global || "").toLowerCase() === "true";
const forced = Boolean(process.env.AGREP_POSTINSTALL_FORCE);
const strict = Boolean(process.env.AGREP_POSTINSTALL_STRICT);

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
  // Keep npm output clean; the shim can provision the tool on first run.
  const result = spawnSync(cmd, args, { stdio: "ignore", shell: false });
  return !result.error && result.status === 0;
}

let warmed = false;
const uvAvailable = has("uv");
if (uvAvailable) {
  warmed = tryRun("uv", [
    "tool", "run",
    "--exclude-newer-package", "agrep=false",
    "--from", spec,
    "agrep", "--version",
  ]);
} else {
  console.warn("agrep: install uv (https://docs.astral.sh/uv) - agrep needs it on first run.");
}
if (!warmed && strict) {
  console.error("agrep: failed to warm the pinned PyPI tool");
  process.exit(1);
}
if (uvAvailable && !warmed) {
  console.warn("agrep: uv cache warm failed; the first agrep run will retry");
}
