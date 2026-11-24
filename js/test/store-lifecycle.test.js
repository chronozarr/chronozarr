import { test } from 'node:test';
import assert from 'node:assert/strict';
import { getEventListeners } from 'node:events';
import { openStore } from '../chronozarr/decoder.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { filesFetch } from '../support/files-fetch.js';

const spec = { nTime: 12, nBand: 1, height: 32, width: 32, chunk: 32,  sharded: true, mask: true, coverage: true };

/** Hold the first index request at fetch(), including the HEAD used before a bounded suffix read. */
async function heldIndex(t, { shardBytes, suffixRequests }) {
  const { files } = buildSyntheticStore({ ...spec, nTime: 2, consolidated: true, shardBytes });
  const fetchFile = filesFetch(files);
  let markStarted;
  const started = new Promise(resolve => { markStarted = resolve; });
  let request;
  let release;
  const store = await openStore('https://example.test/store/', {
    workers: 0,
    suffixRequests,
    fetch: incoming => {
      if (request || !new URL(incoming.url).pathname.includes('/data/c/')) return fetchFile(incoming);
      request = incoming;
      return new Promise((resolve, reject) => {
        let finished = false;
        const onAbort = () => {
          finished = true;
          incoming.signal.removeEventListener('abort', onAbort);
          reject(new DOMException('Aborted', 'AbortError'));
        };
        release = () => {
          if (finished) return;
          finished = true;
          incoming.signal.removeEventListener('abort', onAbort);
          resolve(fetchFile(incoming));
        };
        incoming.signal.addEventListener('abort', onAbort, { once: true });
        markStarted();
      });
    },
  });
  t.after(() => { store.close(); release?.(); });
  return { store, started, release: () => release(), get request() { return request; } };
}

for (const [name, options, method] of [
  ['bounded index GET', { shardBytes: true, suffixRequests: false }, 'GET'],
  ['suffix index HEAD', { shardBytes: false, suffixRequests: false }, 'HEAD'],
  ['suffix index GET', { shardBytes: false, suffixRequests: true }, 'GET'],
]) {
  test(`close aborts a pending shard-index ${name} without waiting for the host`, async (t) => {
    const fixture = await heldIndex(t, options);
    const { store } = fixture;
    const outcome = store.getRaw(0, 0, 0, 0).catch(error => error.name);
    await fixture.started;
    assert.equal(fixture.request.method, method);
    store.close();
    assert.equal(fixture.request.signal.aborted, true, 'closing the store cancels its shared index request');
    assert.equal(await outcome, 'AbortError');
    await new Promise(setImmediate);
    assert.equal(store.stats.network.inflight, 0, 'no host response is needed to release the request slot');
  });
}

test('cancelling one reader leaves its shared shard index available to another timestep', async (t) => {
  const fixture = await heldIndex(t, { shardBytes: true, suffixRequests: false });
  const { store } = fixture;
  const controller = new AbortController();
  const cancelled = store.getRaw(0, 0, 0, 0, { signal: controller.signal }).catch(error => error.name);
  await fixture.started;
  const survivor = store.getRaw(0, 0, 0, 1);
  controller.abort();
  assert.equal(await cancelled, 'AbortError');
  assert.equal(fixture.request.signal.aborted, false, 'a caller does not own the shared index request');
  fixture.release();
  assert.equal((await survivor)[0], 1040, 'the other timestep still decodes from that index');
});

test('close releases data, compressed bytes and auxiliary arrays even when the store remains referenced', async () => {
  const readable = buildSyntheticStore(spec);
  const store = await openStore('memory://closed-cache', { store: readable, workers: 0 });
  await Promise.all([store.getRaw(0, 0, 0, 0), store.getMask(0, 0, 0, 0), store.getCoverage(0, 0, 0, 0)]);
  assert.ok(store.cacheInfo().bytes > 0 && store.cacheInfo().compressedBytes > 0);
  store.close(); store.close();
  assert.equal(store.cacheInfo().bytes, 0);
  assert.equal(store.cacheInfo().compressedBytes, 0);
  assert.equal(store.peekMask(0, 0, 0, 0), undefined);
  assert.equal(store.peekCoverage(0, 0, 0, 0), undefined);
  const calls = readable.log.length;
  await assert.rejects(store.getRaw(0, 0, 0, 0), /closed/);
  await assert.rejects(store.getMask(0, 0, 0, 1), /closed/);
  await assert.rejects(store.prefetch({ lod: 0, cells: [[0, 0]], t: 0 }), /closed/);
  assert.equal(readable.log.length, calls);
});

test('completed prefetches detach every owner listener without requiring an abort', async (t) => {
  const observed = new Set();
  const add = AbortSignal.prototype.addEventListener;
  t.mock.method(AbortSignal.prototype, 'addEventListener', function(event, ...args) {
    if (event === 'abort') observed.add(this);
    return add.call(this, event, ...args);
  });
  const store = await openStore('memory://owner-listeners', { store: buildSyntheticStore(spec), workers: 0 });
  const owner = new AbortController();
  await store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, playing: true, masks: true, signal: owner.signal });
  for (const signal of observed) assert.equal(getEventListeners(signal, 'abort').length, 0);
  store.close();
});

test('close prevents a late decode from repopulating caches', async () => {
  let decode;
  let started;
  const decoding = new Promise(resolve => { started = resolve; });
  const worker = {
    postMessage(message) {
      if (message.type === 'init') queueMicrotask(() => worker.onmessage({ data: { type: 'ready' } }));
      if (message.type === 'decode') { decode = message; started(); }
    },
    terminate() {},
  };
  const store = await openStore('memory://late-decode', { store: buildSyntheticStore(spec), workers: 1, spawnWorker: () => worker });
  const pending = store.getRaw(0, 0, 0, 0);
  const result = pending.catch(error => error.name);
  await decoding;
  const onmessage = worker.onmessage;
  store.close();
  // A worker message already in transit may arrive after terminate().
  onmessage({ data: { type: 'decoded', id: decode.id, data: new Uint16Array(1024) } });
  assert.equal(await result, 'AbortError');
  assert.equal(store.cacheInfo().bytes, 0);
});

test('a store that fails to construct after the decode workers of its own pool (spawnWorker, not the shared lease) started terminates them', async () => {
  const readable = buildSyntheticStore(spec);
  const root = JSON.parse(new TextDecoder().decode(readable.files.get('/zarr.json')));
  root.attributes.chronozarr.times.pop();
  readable.files.set('/zarr.json', new TextEncoder().encode(JSON.stringify(root)));
  const workers = [];
  const spawnWorker = () => {
    const worker = {
      terminated: false,
      postMessage(message) {
        if (message.type === 'init') queueMicrotask(() => worker.onmessage?.({ data: { type: 'ready' } }));
      },
      terminate() {
        worker.terminated = true;
      },
    };
    workers.push(worker);
    return worker;
  };
  await assert.rejects(openStore('memory://bad-times', { store: readable, workers: 2, spawnWorker }), /has 12 timesteps, times attr has 11/);
  assert.equal(workers.length, 2, 'the workers were started before the store was built');
  assert.ok(workers.every((w) => w.terminated), 'and are terminated when building it fails');
});
