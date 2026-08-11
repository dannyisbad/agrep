#!/usr/bin/env node
"use strict";

const {
  mkdtempSync, cpSync, copyFileSync, readFileSync, writeFileSync, mkdirSync, rmSync,
  chmodSync,
} = require("fs");
const { tmpdir } = require("os");
const path = require("path");
const { spawnSync } = require("child_process");
const { npmInvocation } = require("./npm-command");

const root = path.resolve(__dirname, "..");
const source = path.join(root, "npm");
const args = process.argv.slice(2);
let outputDirectory;
if (args.length === 1 && args[0] === "--dry-run") {
  outputDirectory = null;
} else if (args.length === 2 && args[0] === "--out-dir" && args[1]) {
  outputDirectory = path.resolve(args[1]);
} else {
  console.error("usage: node npm/publish.js --dry-run | --out-dir DIRECTORY");
  process.exit(2);
}
const stage = mkdtempSync(path.join(tmpdir(), "agrep-npm-publish-"));
const packs = path.join(stage, "packs");

function run(args, options = {}) {
  const launch = npmInvocation(args);
  const result = spawnSync(launch.command, launch.args, {
    cwd: root,
    encoding: "utf8",
    shell: false,
    ...options,
  });
  return result;
}

function requireSuccess(result, label) {
  if (result.error || result.status !== 0) {
    if (result.stdout) process.stderr.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
    throw new Error(`${label} failed${result.error ? `: ${result.error.message}` : ""}`);
  }
}

function stagePackage(name) {
  const canonicalLicense = readFileSync(path.join(root, "LICENSE"));
  const sourceLicense = readFileSync(path.join(source, "LICENSE"));
  if (!canonicalLicense.equals(sourceLicense)) {
    throw new Error("npm/LICENSE differs from the canonical root LICENSE");
  }
  const target = path.join(stage, name);
  cpSync(source, target, { recursive: true });
  chmodSync(path.join(target, "bin.js"), 0o755);
  for (const file of ["LICENSE", "README.md", "package.json", "postinstall.js"]) {
    chmodSync(path.join(target, file), 0o644);
  }
  return target;
}

function prepareAlias() {
  const target = stagePackage("agrep-cli");
  const manifestPath = path.join(target, "package.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  manifest.name = "agrep-cli";
  manifest.description =
    "unscoped npm alias for @mundy/agrep, local search across AI coding-agent history";
  writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  cpSync(path.join(source, "alias", "README.md"), path.join(target, "README.md"));
  return target;
}

function pack(directory) {
  const canonicalLicense = readFileSync(path.join(root, "LICENSE"));
  const stagedLicense = readFileSync(path.join(directory, "LICENSE"));
  if (!canonicalLicense.equals(stagedLicense)) {
    throw new Error("npm package license differs from the canonical root LICENSE");
  }
  const result = run(["pack", directory, "--json", "--pack-destination", packs]);
  requireSuccess(result, `npm pack ${directory}`);
  let report;
  try {
    report = JSON.parse(result.stdout);
  } catch (error) {
    throw new Error(`npm pack returned invalid JSON: ${error.message}`);
  }
  if (!Array.isArray(report) || report.length !== 1
      || typeof report[0].integrity !== "string"
      || !report[0].integrity.startsWith("sha512-")
      || !Array.isArray(report[0].files)
      || typeof report[0].filename !== "string"
      || path.basename(report[0].filename) !== report[0].filename) {
    throw new Error("npm pack returned no unique integrity record");
  }
  const files = report[0].files.map((entry) => entry.path).sort();
  const expected = ["LICENSE", "README.md", "bin.js", "package.json", "postinstall.js"];
  if (JSON.stringify(files) !== JSON.stringify(expected)) {
    throw new Error(`npm pack file allowlist drift: ${JSON.stringify(files)}`);
  }
  return {
    name: report[0].name,
    version: report[0].version,
    integrity: report[0].integrity,
    filename: report[0].filename,
    tarball: path.join(packs, report[0].filename),
  };
}

function requireIdentity(pkg, name, version) {
  if (pkg.name !== name || pkg.version !== version) {
    throw new Error(
      `npm identity drift: expected ${name}@${version}, got ${pkg.name}@${pkg.version}`);
  }
  return pkg;
}

try {
  mkdirSync(packs);
  const version = JSON.parse(
    readFileSync(path.join(source, "package.json"), "utf8")).version;
  const packages = [
    requireIdentity(pack(stagePackage("mundy-agrep")), "@mundy/agrep", version),
    requireIdentity(pack(prepareAlias()), "agrep-cli", version),
  ];
  if (outputDirectory !== null) {
    mkdirSync(outputDirectory);
    for (const pkg of packages) {
      copyFileSync(pkg.tarball, path.join(outputDirectory, pkg.filename));
    }
  }
  for (const pkg of packages) {
    console.log(`packed ${pkg.name}@${pkg.version} ${pkg.integrity}`);
  }
} catch (error) {
  console.error(`agrep npm pack failed: ${error.message}`);
  process.exitCode = 1;
} finally {
  rmSync(stage, { recursive: true, force: true });
}
