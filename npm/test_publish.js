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
