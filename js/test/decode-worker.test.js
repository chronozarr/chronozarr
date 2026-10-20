// decode-worker.js run in-process, with a stand-in for the worker global and a codec registry whose loads can be seen and slowed.
// What is tested is when the worker says 'ready': a pool gives demand jobs to a ready worker, so a worker that says it before
// its codecs have loaded holds the first chunk of the page while a warm fallback decoder could have decoded it.

import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const posted = [];
let registryUrl;
let directory;

before(async () => {
  globalThis.self = { postMessage: (message) => posted.push(message) };
  directory = await mkdtemp(path.join(tmpdir(), 'chronozarr-worker-test-'));
  // A registry like zarrita's: codec name -> a loader. The log is on globalThis so the test sees the loads.
  const file = path.join(directory, 'registry.mjs');
  await writeFile(
    file,
    `globalThis.registryLog = [];
const codec = { fromConfig: () => ({ decode: async (bytes) => bytes, encode: async (bytes) => bytes }) };
export const registry = new Map([
  ['zstd', async () => { globalThis.registryLog.push('zstd:start'); await new Promise((r) => setTimeout(r, 40)); globalThis.registryLog.push('zstd:done'); return codec; }],
  ['lz4', async () => { throw new Error('this codec cannot load'); }],
]);
`,
  );
  registryUrl = pathToFileURL(file).href;
  await import('../chronozarr/decode-worker.js');
});
after(async () => {
  delete globalThis.self;
  await rm(directory, { recursive: true, force: true });
});

const spec = (key, compressor) => ({ key, dtype: 'uint16', shape: [2, 2], codecs: [{ name: 'bytes', configuration: { endian: 'little' } }, { name: compressor }] });
const send = (message) => globalThis.self.onmessage({ data: message });

test('a worker told which codecs to warm says ready only once they are loaded', async () => {
  posted.length = 0;
  const init = send({ type: 'init', zarritaUrl: registryUrl, warm: [spec('warm-first', 'zstd')] });
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.deepEqual(posted, [], 'still loading the codec: not ready');
  assert.deepEqual(globalThis.registryLog, ['zstd:start']);
  await init;
  assert.deepEqual(posted, [{ type: 'ready' }]);
  assert.deepEqual(globalThis.registryLog, ['zstd:start', 'zstd:done'], 'ready came after the load');
});

test('a worker told to warm nothing says ready at once', async () => {
  posted.length = 0;
  await send({ type: 'init', zarritaUrl: registryUrl });
  assert.deepEqual(posted, [{ type: 'ready' }]);
  posted.length = 0;
  await send({ type: 'init', zarritaUrl: registryUrl, warm: [] });
  assert.deepEqual(posted, [{ type: 'ready' }]);
});

test('a codec that fails to warm does not keep the worker from saying ready: the first decode of it reports the error', async () => {
  posted.length = 0;
  await send({ type: 'init', zarritaUrl: registryUrl, warm: [spec('warm-broken', 'lz4')] });
  assert.deepEqual(posted, [{ type: 'ready' }]);
  posted.length = 0;
  await send({ type: 'decode', id: 7, spec: spec('warm-broken', 'lz4'), bytes: new Uint8Array(8) });
  assert.equal(posted.length, 1);
  assert.deepEqual([posted[0].type, posted[0].id], ['error', 7]);
  assert.match(posted[0].message, /cannot load/);
});
