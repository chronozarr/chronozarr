import { test } from 'node:test';
import assert from 'node:assert/strict';
import { resolveObjectURL } from 'node:buffer';
import { openStore } from '../chronozarr/decoder.js';
import { packUint16 } from '../chronozarr/compact.js';
import { DecodePool } from '../chronozarr/pool.js';
import { buildSyntheticStore, defaultValues } from '../support/synthetic-store.js';

const SPEC = { nTime: 4, nBand: 1, height: 16, width: 16, chunk: 32,  sharded: true };

/**
 * Install a stand-in for the Worker constructor for the duration of `body`. `onConstruct(call)` may throw; the
 * workers it hands out never answer, so every demand read is decoded on the main thread by the pool's fallback.
 */
async function withWorkerConstructor(onConstruct, body) {
  const calls = [];
  class FakeWorker {
    constructor(url, options) {
      calls.push({ url: String(url), options });
      onConstruct(calls.length);
    }
    postMessage() {}
    terminate() {}
  }
  const had = Object.hasOwn(globalThis, 'Worker');
  const before = globalThis.Worker;
  globalThis.Worker = FakeWorker;
  try {
    await body(calls);
  } finally {
    if (had) globalThis.Worker = before;
    else delete globalThis.Worker;
  }
}

test('a SecurityError from the Worker constructor (package loaded from a CDN) is retried through a same-origin blob', async () => {
  const securityError = () => new DOMException('Failed to construct Worker: Script at a cross-origin URL cannot be loaded', 'SecurityError');
  await withWorkerConstructor(
    (n) => {
      if (n === 1) throw securityError();
    },
    async (calls) => {
      const store = await openStore('memory://cross-origin', { store: buildSyntheticStore(SPEC), workers: 3 });
      assert.equal(calls.length, 4, 'one direct attempt that threw, then three workers through the blob');
      assert.match(calls[0].url, /\/chronozarr\/decode-worker\.js$/);
      assert.ok(calls.slice(1).every((c) => c.url.startsWith('blob:') && c.options.type === 'module'), 'module workers started from a blob URL');
      assert.equal(new Set(calls.slice(1).map((c) => c.url)).size, 1, 'one bootstrap blob for all of them');
      const script = await resolveObjectURL(calls[1].url).text();
      assert.equal(script, `import ${JSON.stringify(calls[0].url)};`, 'the blob imports the real worker script');
      const cell = await store.getCell(0, 0, 0, 1);
      assert.equal(cell.data[5], defaultValues('uint16')(1, 0, 0, 5, 0), 'decodes (on the main thread: the stand-in workers never answer)');
      store.close();
    },
  );
});

test('a Worker constructor that fails for another reason is logged once and openStore falls back to the main thread', async () => {
  const warnings = [];
  const warn = console.warn;
  console.warn = (...args) => warnings.push(args.join(' '));
  try {
    await withWorkerConstructor(
      (n) => {
        if (n === 2) throw new TypeError('worker-src blocks this');
      },
      async (calls) => {
        const store = await openStore('memory://no-workers', { store: buildSyntheticStore(SPEC), workers: 4 });
        assert.equal(calls.length, 2, 'the pool gave up at the second worker');
        assert.equal(warnings.length, 1, 'one warning');
        assert.match(warnings[0], /cannot start decode workers \(TypeError: worker-src blocks this\); decoding on the main thread/);
        const raw = await store.getRaw(0, 0, 0, 0);
        assert.ok(raw instanceof Uint16Array);
        assert.equal(raw[5], defaultValues('uint16')(0, 0, 0, 5, 0));
        store.close();
      },
    );
  } finally {
    console.warn = warn;
  }
});

const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

/** The worker starts only when ready() is called; afterwards it decodes this fixture's little-endian uint16 bytes. */
function heldWorker() {
  const worker = {
    onmessage: null,
    onerror: null,
    terminated: false,
    decoded: [],
    postMessage(message) {
      if (message.type !== 'decode') return;
      const view = new DataView(message.bytes.buffer, message.bytes.byteOffset, message.bytes.byteLength);
      const data = Uint16Array.from({ length: message.bytes.length / 2 }, (_, i) => view.getUint16(i * 2, true));
      worker.decoded.push({ first: data[0], spec: message.spec, compact: message.compact });
      const packed = message.compact ? packUint16(data) : null;
      // A real worker transfers the backing arrays without the unpack() method; the pool restores it.
      const result = packed ? { words: packed.words, blocks: packed.blocks, length: packed.length } : data;
      queueMicrotask(() => {
        if (!worker.terminated) worker.onmessage({ data: { type: 'decoded', id: message.id, data: result } });
      });
    },
    ready() { worker.onmessage({ data: { type: 'ready' } }); },
    terminate() { worker.terminated = true; },
  };
  return worker;
}

/** Observe the actual enqueue, after decode() returns, without replacing the reader's ordinary fetch or decode path. */
async function withDecodeObserver(observe, body) {
  const decode = DecodePool.prototype.decode;
  DecodePool.prototype.decode = function (bytes, priority, signal, spec, options) {
    const result = decode.call(this, bytes, priority, signal, spec, options);
    observe({ bytes, priority, signal, spec, options });
    return result;
  };
  try {
    await body();
  } finally {
    DecodePool.prototype.decode = decode;
  }
}

async function beforeReady(promise) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error('visible read still waits for worker readiness')), 1500);
      }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}

for (const compact of [false, true]) {
  test(`a visible read promoted from ${compact ? 'compact' : 'ordinary'} prefetch decodes before worker readiness`, { timeout: 5000 }, async () => {
    const spec = { nTime: 4, nBand: 2, height: 8, width: 8, chunk: 8, sharded: false };
    const readable = buildSyntheticStore(spec);
    const worker = heldWorker();
    const value = defaultValues('uint16');
    const expected = (t) => Uint16Array.from({ length: spec.nBand * spec.chunk ** 2 }, (_, i) => {
      const plane = spec.chunk ** 2;
      return value(t, Math.floor(i / plane), Math.floor((i % plane) / spec.chunk), i % spec.chunk, 0);
    });
    const queued = new Map([1, 2].map((t) => [value(t, 0, 0, 0, 0), deferred()]));
    const pending = [];
    let store;
    await withDecodeObserver(({ bytes, priority, signal, options }) => {
      if (priority.value !== 1) return;
      const first = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getUint16(0, true);
      queued.get(first)?.resolve({ signal, compact: options.compact });
    }, async () => {
      try {
        store = await openStore('memory://startup-promotion', {
          store: readable, workers: 1, spawnWorker: () => worker,
          speculativeBytesInitial: 1e9, idleBytes: 1e9,
        });
        if (compact) assert.equal(store.enableCompactCache(), true);
        const original = new AbortController();
        const promoted = store.prefetch({
          lod: 0, cells: [[0, 0]], t: 1, maxSteps: 1, signal: original.signal,
          onChunk: () => assert.fail('an aborted prefetch owner must not be notified'),
        });
        const background = store.prefetch({ lod: 0, cells: [[0, 0]], t: 2, maxSteps: 1 });
        pending.push(promoted, background);
        const jobs = await Promise.all([...queued.values()].map(({ promise }) => promise));
        assert.ok(jobs.every((job) => job.compact === compact), 'prefetch passes its compact mode to the decode queue');
        assert.equal(worker.decoded.length, 0, 'both chunks wait in the decode queue while readiness is held');

        const visible = store.getRaw(0, 0, 0, 1);
        pending.push(visible);
        original.abort();
        assert.equal(jobs[0].signal.aborted, false, 'the visible reader retains the queued request when its original owner aborts');
        assert.deepEqual(await beforeReady(visible), expected(1), 'demand gets every true value before the worker starts');
        assert.deepEqual((await promoted).errors, [], 'the demand read retains the request after its prefetch owner aborts');
        assert.equal(worker.decoded.length, 0, 'the promoted chunk used the startup fallback');
        assert.equal(store.hasCachedRaw(0, 0, 0, 2), false, 'unrelated background work still waits for readiness');
        if (compact) assert.ok(store.peekCompact(0, 0, 0, 1), 'promotion retains the compact decode option');

        worker.ready();
        assert.deepEqual((await background).errors, []);
        assert.deepEqual(store.peekRaw(0, 0, 0, 2), expected(2));
        assert.deepEqual(await store.getRaw(0, 0, 0, 3), expected(3), 'normal later reads use the ready worker');
        assert.deepEqual(worker.decoded.map(({ first }) => first), [value(2, 0, 0, 0, 0), value(3, 0, 0, 0, 0)], 'readiness never dispatches the completed promoted chunk again');
        assert.ok(worker.decoded.every((job) => job.compact === compact && job.spec.dtype === 'uint16'));
        const dataReads = readable.log.filter(({ key }) => /\/data\/c\//.test(key));
        assert.equal(dataReads.length, 3, 'promotion and readiness do not fetch any chunk twice');
        assert.equal(new Set(dataReads.map(({ key }) => key)).size, 3);
      } finally {
        store?.close();
        await Promise.allSettled(pending);
      }
    });
    assert.equal(worker.terminated, true, 'closing the store terminates its worker');
  });
}
