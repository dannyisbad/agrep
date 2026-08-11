"use strict";

const { existsSync } = require("node:fs");
const path = require("node:path");

function npmInvocation(args, options = {}) {
  const platform = options.platform || process.platform;
  if (platform !== "win32") {
    return { command: "npm", args };
  }
  const execPath = options.execPath || process.execPath;
  const npmExecPath = options.npmExecPath === undefined
    ? process.env.npm_execpath
    : options.npmExecPath;
  const exists = options.exists || existsSync;
  const candidates = [
    npmExecPath,
    path.join(path.dirname(execPath), "node_modules", "npm", "bin", "npm-cli.js"),
  ];
  const cli = candidates.find((candidate) =>
    typeof candidate === "string"
      && /(?:^|[\\/])npm-cli\.c?js$/i.test(candidate)
      && exists(candidate));
  if (!cli) {
    throw new Error("npm-cli.js was not found beside the Windows Node runtime");
  }
  return { command: execPath, args: [cli, ...args] };
}

module.exports = { npmInvocation };
