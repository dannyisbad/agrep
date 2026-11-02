#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const https = require('node:https');
const crypto = require('node:crypto');
const {spawn} = require('node:child_process');
const {version} = require('./package.json');
const hashes = require('./checksums.json');

function download(url, redirects = 0) {
  const parsed = new URL(url);
  const allowed = ['github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'];
  if (parsed.protocol !== 'https:' || !allowed.includes(parsed.hostname) || parsed.username || parsed.password || parsed.port) {
    return Promise.reject(new Error('release download must use GitHub HTTPS'));
  }
  if (redirects > 5) return Promise.reject(new Error('too many release redirects'));
  return new Promise((resolve, reject) => {
    const request = https.get(parsed, {headers: {'User-Agent': '@mundy/agrep'}}, response => {
      if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
        response.resume();
        if (!response.headers.location) return reject(new Error('missing release redirect'));
        download(new URL(response.headers.location, parsed), redirects + 1).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`release download returned HTTP ${response.statusCode}`));
        return;
      }
      const parts = [];
      let size = 0;
      response.on('data', part => {
        size += part.length;
        if (size > 256 * 1024 * 1024) response.destroy(new Error('release asset too large'));
        else parts.push(part);
      });
      response.on('end', () => resolve(Buffer.concat(parts)));
      response.on('error', reject);
    });
    request.setTimeout(60000, () => request.destroy(new Error('release download timed out')));
    request.on('error', reject);
  });
}

async function ensureBinary({platform = process.platform, arch = process.arch,
  manifest = hashes, cache, fetch = download} = {}) {
  const key = `${platform}-${arch}`;
  const entry = manifest[key];
  if (!entry || !/^[a-f0-9]{64}$/.test(entry.sha256) || !/^agrep-[a-z0-9-]+(?:\.exe)?$/.test(entry.asset)) {
    throw new Error(`no pinned binary for ${key} in agrep ${version}`);
  }
  const base = cache || (platform === 'win32'
    ? path.join(process.env.LOCALAPPDATA || os.homedir(), 'agrep', 'bin')
    : path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), '.cache'), 'agrep', 'bin'));
  const directory = path.join(base, version, key, entry.sha256);
  const binary = path.join(directory, platform === 'win32' ? 'agrep.exe' : 'agrep');
  const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
  try {
    if (fs.lstatSync(binary).isFile()) return binary;
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const bytes = await fetch(`https://github.com/dannyisbad/agrep/releases/download/v${version}/${entry.asset}`);
  if (digest(bytes) !== entry.sha256) throw new Error('release SHA-256 mismatch');
  fs.mkdirSync(directory, {recursive: true, mode: 0o700});
  const temporary = `${binary}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
  try {
    fs.writeFileSync(temporary, bytes, {flag: 'wx', mode: 0o755});
    // Concurrent first invocations may have completed the same verified download.
    try { fs.renameSync(temporary, binary); }
    catch (error) {
      if (!['EEXIST', 'EPERM'].includes(error.code) || digest(fs.readFileSync(binary)) !== entry.sha256) throw error;
    }
  } finally {
    fs.rmSync(temporary, {force: true});
  }
  return binary;
}

async function main() {
  const binary = await ensureBinary();
  const args = process.argv.slice(2);
  if (process.platform !== 'win32' && process.execve) {
    process.execve(binary, [binary, ...args], process.env);
    return;
  }
  const child = spawn(binary, args, {stdio: 'inherit'});
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK']) {
    process.on(signal, () => { if (process.platform !== 'win32') child.kill(signal); });
  }
  child.on('error', error => { console.error(`agrep: ${error.message}`); process.exitCode = 2; });
  child.on('exit', (code, signal) => {
    if (signal && process.platform !== 'win32') {
      process.removeAllListeners(signal);
      process.kill(process.pid, signal);
    } else process.exitCode = code === null ? 2 : code;
  });
}
if (require.main === module) main().catch(error => {console.error(`agrep: ${error.message}`); process.exitCode = 2;});
module.exports = {ensureBinary, download};
