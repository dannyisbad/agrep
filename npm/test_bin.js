'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const {ensureBinary, download} = require('./bin');

function fixture(t) {
  const cache = fs.mkdtempSync(path.join(os.tmpdir(), 'agrep-launcher-'));
  t.after(() => fs.rmSync(cache, {recursive: true, force: true}));
  const bytes = Buffer.from('a verified binary fixture');
  const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
  const manifest = {'linux-x64': {asset: 'agrep-linux-x64', sha256}};
  return {cache, bytes, manifest, platform: 'linux', arch: 'x64'};
}
test('downloads and verifies once; warm cache requires no binary read', async t => {
  const f = fixture(t);
  let calls = 0;
  const fetch = async url => {
    calls++;
    assert.equal(url, `https://github.com/dannyisbad/agrep/releases/download/v${require('./package.json').version}/agrep-linux-x64`);
    return f.bytes;
  };
  const binary = await ensureBinary({...f, fetch});
  assert.deepEqual(fs.readFileSync(binary), f.bytes);
  await ensureBinary({...f, fetch});
  assert.equal(calls, 1);
  const original = fs.readFileSync;
  fs.readFileSync = () => assert.fail('warm invocation must not read the executable');
  try { assert.equal(await ensureBinary({...f, fetch}), binary); }
  finally { fs.readFileSync = original; }
  assert.equal(calls, 1);
});
test('wrong SHA never creates executable or leaves a temporary file', async t => {
  const f = fixture(t);
  await assert.rejects(ensureBinary({...f, fetch: async () => Buffer.from('tampered')}), /SHA-256 mismatch/);
  assert.deepEqual(fs.readdirSync(f.cache), []);
});
test('unknown platforms and missing hashes fail before fetching', async t => {
  const f = fixture(t);
  await assert.rejects(ensureBinary({...f, arch: 'riscv64', fetch: () => assert.fail()}), /no pinned binary/);
  f.manifest['linux-x64'].sha256 = 'incomplete';
  await assert.rejects(ensureBinary({...f, fetch: () => assert.fail()}), /no pinned binary/);
});
test('rejects unsafe download origins without connecting', async () => {
  for (const url of ['http://github.com/a', 'https://evil.example/a', 'https://token@github.com/a', 'https://github.com:8443/a']) {
    await assert.rejects(download(url), /GitHub HTTPS/);
  }
});
test('concurrent first uses produce the same verified cache', async t => {
  const f = fixture(t);
  const binaries = await Promise.all(Array.from({length: 8}, () => ensureBinary({...f, fetch: async () => f.bytes})));
  assert.equal(new Set(binaries).size, 1);
  assert.deepEqual(fs.readFileSync(binaries[0]), f.bytes);
  assert.equal(fs.readdirSync(path.dirname(binaries[0])).length, 1);
});

test('follows only allowed HTTPS redirects and rejects HTTP failures without retry', async t => {
  const https = require('node:https');
  const {EventEmitter} = require('node:events');
  const {PassThrough} = require('node:stream');
  const original = https.get;
  t.after(() => { https.get = original; });
  let calls = 0;
  let responseCode = 302;
  let location = 'https://release-assets.githubusercontent.com/example/asset';
  https.get = (url, options, callback) => {
    calls++;
    const request = new EventEmitter();
    request.setTimeout = () => request;
    process.nextTick(() => {
      const response = new PassThrough();
      response.statusCode = responseCode;
      response.headers = {location};
      responseCode = 200;
      callback(response);
      response.end('downloaded bytes');
    });
    return request;
  };
  assert.equal((await download('https://github.com/example/asset')).toString(), 'downloaded bytes');
  assert.equal(calls, 2);
  responseCode = 302;
  location = 'http://evil.example/asset';
  await assert.rejects(download('https://github.com/example/asset'), /GitHub HTTPS/);
  responseCode = 503;
  calls = 0;
  await assert.rejects(download('https://github.com/example/asset'), /HTTP 503/);
  assert.equal(calls, 1);
});
