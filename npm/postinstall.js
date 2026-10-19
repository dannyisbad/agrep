#!/usr/bin/env node
// Global installs finish enrolled, not merely downloaded: after warming the
// runner's ephemeral tool cache, run the pinned `agrep setup --yes` so the box
// is indexed and the detected agents are taught the moment `npm install -g`
// returns. Everything setup writes is disclosed in its output, owned by
// agrep, and undone by `agrep remove`; --no-semantic defers the ~52MiB model
// fetch to the first semantic use, and --no-archive leaves source-store
// retention off until someone asks for it (`agrep setup --archive`).
//
// AGREP_NO_AUTO_SETUP=1 restores the old warm-only behavior;
// AGREP_SKIP_POSTINSTALL=1 skips this script entirely. The npm shim remains
// the entrypoint and resolves the pinned PyPI package on every run.

"use strict";

const path = require("path");
const { spawnSync } = require("child_process");
const { version } = require("./package.json");

const spec = process.env.AGREP_PYPI_SPEC || `agrep==${version}`;
const npmGlobal = String(process.env.npm_config_global || "").toLowerCase() === "true";
const forced = Boolean(process.env.AGREP_POSTINSTALL_FORCE);
const strict = Boolean(process.env.AGREP_POSTINSTALL_STRICT);
const autoSetup = !process.env.AGREP_NO_AUTO_SETUP;
const SETUP_TIMEOUT_MS = 15 * 60 * 1000;
// `agrep setup` exit 3 (docs/OUTPUT_CONTRACTS.md): declined without changing
// anything; the tool is provisioned, so this is not a failed install
const SETUP_DECLINED = 3;
// the sentinel must track this package, not the runner's prunable cache copy
const ANCHOR_ENV = "AGREP_INSTALL_ANCHOR";
const anchor = path.join(__dirname, "bin.js");

if (process.env.AGREP_SKIP_POSTINSTALL) {
  process.exit(0);
}

if (!npmGlobal && !forced) {
  process.exit(0);
}

function probe(cmd) {
  const r = spawnSync(cmd, ["--version"], { encoding: "utf8", shell: false });
  return !r.error && r.status === 0 ? String(r.stdout || "") : null;
}

function uvVersion(text) {
  const m = /\buv\s+(\d+)\.(\d+)\.(\d+)/.exec(text || "");
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

// same rule as bin.js: the `=false` opt-out parses only on uv >= 0.9.25
function excludeNewerOptOut(v) {
  return v !== null && (v[0] > 0 || v[1] > 9 || (v[1] === 9 && v[2] >= 25));
}

function tryRun(cmd, args, options) {
  const result = spawnSync(cmd, args, {
    shell: false, ...options, env: { ...process.env, [ANCHOR_ENV]: anchor },
  });
  return result.error ? null : result.status;
}

// Resolve and cache the pinned agrep without running it for real; a failure here
// is the runner's (network, registry, an unsupported python), not setup's.
function provision(runner) {
  const result = spawnSync(runner.cmd, runner.args("--version"), {
    shell: false, encoding: "utf8", stdio: ["ignore", "ignore", "pipe"],
    env: { ...process.env, [ANCHOR_ENV]: anchor },
  });
  if (result.error) return { ok: false, detail: result.error.message };
  const lines = String(result.stderr || "").trim().split("\n").filter(Boolean);
  return { ok: result.status === 0, detail: lines.length ? lines[lines.length - 1] : `exit ${result.status}` };
}

// bin.js's runner choice, in the same order: uv, else pipx
function findRunner() {
  const uv = probe("uv");
  if (uv !== null) {
    const flag = excludeNewerOptOut(uvVersion(uv))
      ? ["--exclude-newer-package", "agrep=false"] : [];
    return {
      cmd: "uv",
      args: (...rest) => ["tool", "run", ...flag, "--from", spec, "agrep", ...rest],
    };
  }
  if (probe("pipx") !== null) {
    return { cmd: "pipx", args: (...rest) => ["run", "--spec", spec, "agrep", ...rest] };
  }
  return null;
}

let done = false;
const runner = findRunner();
if (runner && autoSetup) {
  const fetched = provision(runner);
  if (!fetched.ok) {
    console.warn(
      `agrep: ${runner.cmd} could not fetch ${spec} (${fetched.detail}); nothing was set up.`
      + " once the registry is reachable, `agrep setup` runs the one-time setup.");
  } else {
    // Inherited stdio: npm surfaces this with --foreground-scripts (and on
    // failure), so every file setup touches is disclosed, never silent.
    console.log(
      "agrep: running one-time setup (agrep setup --yes) - writes agent instruction blocks"
      + " and an uninstall-cleanup sentinel, all undone by `agrep remove`;"
      + " AGREP_NO_AUTO_SETUP=1 skips this.");
    const status = tryRun(
      runner.cmd, runner.args("setup", "--yes", "--no-semantic", "--no-archive"),
      { stdio: "inherit", timeout: SETUP_TIMEOUT_MS });
    done = status === 0 || status === SETUP_DECLINED;
    if (status === null) {
      console.warn(`agrep: could not launch ${runner.cmd} for setup; \`agrep setup\` runs it any time.`);
    } else if (status === SETUP_DECLINED) {
      console.warn(
        "agrep: setup declined and changed nothing; its output above names the cause"
        + " and the fix. `agrep setup` declines the same way until that is resolved.");
    } else if (!done) {
      console.warn(
        `agrep: setup exited ${status}; its output above names each step that did not complete`
        + " and the fix. the rest of setup still ran; `agrep setup` retries any time.");
    }
  }
} else if (runner) {
  // Warm-only: keep npm output clean; the shim provisions on first run.
  done = tryRun(runner.cmd, runner.args("--version"), { stdio: "ignore" }) === 0;
  if (!done) {
    console.warn(`agrep: ${runner.cmd} cache warm failed; the first agrep run will retry`);
  }
} else {
  console.warn("agrep: install uv (https://docs.astral.sh/uv) or pipx - agrep needs one of them on first run.");
}
if (!done && strict) {
  console.error("agrep: postinstall failed to provision the pinned PyPI tool");
  process.exit(1);
}
