#!/usr/bin/env node
// Global installs finish enrolled, not merely downloaded: after warming uv's
// ephemeral tool cache, run the pinned `agrep setup --yes` so the box is
// indexed and the detected agents are taught the moment `npm install -g`
// returns. Everything setup writes is disclosed in its output, owned by
// agrep, and undone by `agrep remove`; --no-semantic defers the ~52MiB model
// fetch to the first semantic use, and --no-archive leaves source-store
// retention off until someone asks for it (`agrep setup --archive`).
//
// AGREP_NO_AUTO_SETUP=1 restores the old warm-only behavior;
// AGREP_SKIP_POSTINSTALL=1 skips this script entirely. The npm shim remains
// the entrypoint and resolves the pinned PyPI package on every run.

"use strict";

const { spawnSync } = require("child_process");
const { version } = require("./package.json");

const spec = process.env.AGREP_PYPI_SPEC || `agrep==${version}`;
const npmGlobal = String(process.env.npm_config_global || "").toLowerCase() === "true";
const forced = Boolean(process.env.AGREP_POSTINSTALL_FORCE);
const strict = Boolean(process.env.AGREP_POSTINSTALL_STRICT);
const autoSetup = !process.env.AGREP_NO_AUTO_SETUP;
const SETUP_TIMEOUT_MS = 15 * 60 * 1000;

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

function tryRun(cmd, args, options) {
  const result = spawnSync(cmd, args, { shell: false, ...options });
  return !result.error && result.status === 0;
}

function agrepArgs(...rest) {
  return [
    "tool", "run",
    "--exclude-newer-package", "agrep=false",
    "--from", spec,
    "agrep", ...rest,
  ];
}

let done = false;
const uvAvailable = has("uv");
if (uvAvailable && autoSetup) {
  // Inherited stdio: npm surfaces this with --foreground-scripts (and on
  // failure), so every file setup touches is disclosed, never silent.
  console.log(
    "agrep: running one-time setup (agrep setup --yes) - writes agent instruction blocks"
    + " and an uninstall-cleanup sentinel, all undone by `agrep remove`;"
    + " AGREP_NO_AUTO_SETUP=1 skips this.");
  done = tryRun(
    "uv", agrepArgs("setup", "--yes", "--no-semantic", "--no-archive"),
    { stdio: "inherit", timeout: SETUP_TIMEOUT_MS });
  if (!done) {
    console.warn("agrep: automatic setup did not finish; `agrep setup` completes it any time.");
  }
} else if (uvAvailable) {
  // Warm-only: keep npm output clean; the shim provisions on first run.
  done = tryRun("uv", agrepArgs("--version"), { stdio: "ignore" });
  if (!done) {
    console.warn("agrep: uv cache warm failed; the first agrep run will retry");
  }
} else {
  console.warn("agrep: install uv (https://docs.astral.sh/uv) - agrep needs it on first run.");
}
if (!done && strict) {
  console.error("agrep: postinstall failed to provision the pinned PyPI tool");
  process.exit(1);
}
