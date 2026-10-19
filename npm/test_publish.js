"use strict";

const assert = require("node:assert/strict");
const {
  mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync,
} = require("node:fs");
const { tmpdir } = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const root = path.resolve(__dirname, "..");
const script = path.join(__dirname, "publish.js");
const { version } = require("./package.json");
const { npmInvocation } = require("./npm-command");

test("Windows pack invokes npm's JavaScript entrypoint without a shell", () => {
  const temporary = mkdtempSync(path.join(tmpdir(), "agrep-npm-command-test-"));
  try {
    const node = path.join(temporary, "node.exe");
    const cli = path.join(temporary, "node_modules", "npm", "bin", "npm-cli.js");
    mkdirSync(path.dirname(cli), { recursive: true });
    writeFileSync(cli, "");
    const args = ["pack", "C:\\path with spaces & metacharacters", "--json"];
    assert.deepEqual(
      npmInvocation(args, {
        platform: "win32", execPath: node, npmExecPath: "",
      }),
      { command: node, args: [cli, ...args] });
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("Windows pack fails closed when npm's JavaScript entrypoint is absent", () => {
  assert.throws(
    () => npmInvocation(["pack"], {
      platform: "win32", execPath: "C:\\missing\\node.exe",
      npmExecPath: "", exists: () => false,
    }),
    /npm-cli\.js was not found/);
});

test("pack command seals exactly the primary and alias tarballs", () => {
  const temporary = mkdtempSync(path.join(tmpdir(), "agrep-npm-pack-test-"));
  try {
    const output = path.join(temporary, "sealed");
    const result = spawnSync(process.execPath, [script, "--out-dir", output], {
      cwd: root,
      encoding: "utf8",
      env: { ...process.env, npm_config_cache: path.join(temporary, "cache") },
    });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(readdirSync(output).sort(), [
      `agrep-cli-${version}.tgz`,
      `mundy-agrep-${version}.tgz`,
    ]);
    assert.ok(result.stdout.includes(`packed @mundy/agrep@${version} sha512-`));
    assert.ok(result.stdout.includes(`packed agrep-cli@${version} sha512-`));
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});

test("pack helper has no registry lookup or live publish path", () => {
  const source = readFileSync(script, "utf8");
  assert.doesNotMatch(source, /run\(\["view"/);
  assert.doesNotMatch(source, /run\(\["publish"/);
  const result = spawnSync(process.execPath, [script, "--live"], {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(result.status, 2);
  assert.match(result.stderr, /usage:/);
});

// --- shim behaviour: fake runners on PATH record what bin.js/postinstall.js run

const shim = path.join(__dirname, "bin.js");
const postinstall = path.join(__dirname, "postinstall.js");
const posix = process.platform !== "win32";

// `exits.setup` is what `agrep setup` returns; `exits.agrep` is what every other
// agrep invocation (the provisioning probe `agrep --version`) returns, with
// `fetchError` on stderr: a runner that cannot resolve the package.
function fakeRunner(dir, name, versionLine, exits = {}, fetchError = "") {
  const log = path.join(dir, `${name}.argv`);
  const script = path.join(dir, name);
  writeFileSync(script, [
    "#!/bin/sh",
    `if [ "$1" = "--version" ]; then echo '${versionLine}'; exit 0; fi`,
    `printf '%s\\n' "$@" > '${log}'`,
    `printf '%s' "$AGREP_INSTALL_ANCHOR" > '${log}.anchor'`,
    `case " $* " in *" setup "*) exit ${exits.setup || 0};; esac`,
    fetchError ? `echo '${fetchError}' >&2` : ":",
    `exit ${exits.agrep || 0}`,
    "",
  ].join("\n"), { mode: 0o755 });
  return log;
}

function runShim(script, dir, env) {
  return spawnSync(process.execPath, [script, "--version"], {
    encoding: "utf8",
    env: { PATH: dir, HOME: dir, ...env },
  });
}

for (const [uvVersion, flagged] of [
  ["uv 0.9.24 (0fda1525e 2026-01-09)", false],
  ["uv 0.8.0 (0b2357294 2025-07-17)", false],
  ["uv 0.9.25 (38fcac0f3 2026-01-13)", true],
  ["uv 0.12.22 (70fe1196a 2026-10-01 aarch64-apple-darwin)", true],
]) {
  test(`bin.js passes --exclude-newer-package only where ${uvVersion.split(" ")[1]} parses it`,
    { skip: !posix }, () => {
      const dir = mkdtempSync(path.join(tmpdir(), "agrep-shim-"));
      try {
        const log = fakeRunner(dir, "uv", uvVersion);
        const result = runShim(shim, dir, {});
        assert.equal(result.status, 0, result.stderr);
        const argv = readFileSync(log, "utf8").trim().split("\n");
        assert.equal(argv.includes("--exclude-newer-package"), flagged, argv.join(" "));
        assert.deepEqual(argv.slice(-4), ["--from", `agrep==${version}`, "agrep", "--version"]);
        assert.equal(readFileSync(`${log}.anchor`, "utf8"), shim);
      } finally {
        rmSync(dir, { recursive: true, force: true });
      }
    });
}

test("postinstall runs setup through pipx when uv is absent", { skip: !posix }, () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agrep-postinstall-"));
  try {
    const log = fakeRunner(dir, "pipx", "1.7.1");
    const result = runShim(postinstall, dir, { npm_config_global: "true" });
    assert.equal(result.status, 0, result.stderr);
    assert.doesNotMatch(result.stderr, /install uv/);
    assert.deepEqual(readFileSync(log, "utf8").trim().split("\n"), [
      "run", "--spec", `agrep==${version}`, "agrep",
      "setup", "--yes", "--no-semantic", "--no-archive",
    ]);
    assert.equal(readFileSync(`${log}.anchor`, "utf8"), path.join(__dirname, "bin.js"));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("postinstall reports a failed setup by its exit code, not as unfinished",
  { skip: !posix }, () => {
    const dir = mkdtempSync(path.join(tmpdir(), "agrep-postinstall-"));
    try {
      fakeRunner(dir, "uv", "uv 0.9.24 (0fda1525e 2026-01-09)", { setup: 1 });
      const result = runShim(postinstall, dir, { npm_config_global: "true" });
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stderr, /setup exited 1; its output above names each step/);
      assert.doesNotMatch(result.stderr, /did not finish|could not fetch/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

test("postinstall reports a declined setup as unchanged, not as unfinished",
  { skip: !posix }, () => {
    // exit 3 (docs/OUTPUT_CONTRACTS.md): setup changed nothing and a retry
    // declines the same way; the tool itself is provisioned
    const dir = mkdtempSync(path.join(tmpdir(), "agrep-postinstall-"));
    try {
      fakeRunner(dir, "uv", "uv 0.9.24 (0fda1525e 2026-01-09)", { setup: 3 });
      const result = runShim(postinstall, dir, { npm_config_global: "true", AGREP_POSTINSTALL_STRICT: "1" });
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stderr, /setup declined and changed nothing; its output above names the cause/);
      assert.doesNotMatch(result.stderr, /the rest of setup still ran|retries any time|setup exited|failed to provision/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

test("postinstall says the runner could not fetch agrep when setup never ran",
  { skip: !posix }, () => {
    const dir = mkdtempSync(path.join(tmpdir(), "agrep-postinstall-"));
    try {
      const log = fakeRunner(
        dir, "uv", "uv 0.9.25 (38fcac0f3 2026-01-13)", { agrep: 1 },
        "error: No solution found when resolving tool dependencies");
      const result = runShim(postinstall, dir, { npm_config_global: "true" });
      assert.equal(result.status, 0, result.stderr);
      assert.match(result.stderr, /uv could not fetch agrep==.*No solution found.*nothing was set up/);
      assert.doesNotMatch(result.stderr, /setup exited|the rest of setup still ran/);
      // setup was never attempted: the last invocation is the provisioning probe
      assert.deepEqual(readFileSync(log, "utf8").trim().split("\n").slice(-2), ["agrep", "--version"]);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

test("neither runner: both scripts say so", { skip: !posix }, () => {
  const dir = mkdtempSync(path.join(tmpdir(), "agrep-norunner-"));
  try {
    const shimResult = runShim(shim, dir, {});
    assert.equal(shimResult.status, 1);
    assert.match(shimResult.stderr, /agrep needs uv \(or pipx\)/);
    const postResult = runShim(postinstall, dir, { npm_config_global: "true" });
    assert.equal(postResult.status, 0);
    assert.match(postResult.stderr, /install uv .* or pipx/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
