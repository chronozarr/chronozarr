import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DecodePool, MainThreadDecoder } from '../chronozarr/pool.js';

/** In-process stand-in for a Worker: replies to init and decode messages like decode-worker.js does. */
function fakeWorkerFactory({ readyDelayMs = 0, decodeDelayMs = 0, failInit = false, log = [] } = {}) {
  const spawn = () => {
    const worker = {
      onmessage: null,
      onerror: null,
      terminated: false,
      postMessage(message) {
        setTimeout(() => {
          if (worker.terminated) return;
          if (message.type === 'init') {
            worker.onmessage({ data: failInit ? { type: 'error', message: 'no such codec' } : { type: 'ready' } });
          } else if (message.bytes[0] === 0xff) {
            worker.onmessage({ data: { type: 'error', id: message.id, message: 'corrupt chunk' } });
          } else {
            log.push(message.bytes[0]);
            const data = new Uint16Array(message.bytes.length);
            data.fill(message.bytes[0]);
            worker.onmessage({ data: { type: 'decoded', id: message.id, data } });
          }
        }, message.type === 'init' ? readyDelayMs : decodeDelayMs);
      },
      terminate() {
        worker.terminated = true;
      },
    };
    return worker;
  };
  return { spawn, log };
}

test('pool decodes and returns arrays; jobs run lowest priority number first', async () => {
  const { spawn, log } = fakeWorkerFactory({ readyDelayMs: 5, decodeDelayMs: 2 });
  const pool = new DecodePool({ size: 1, spawn, init: { type: 'init' } });
  const results = await Promise.all([
    pool.decode(Uint8Array.of(1, 1), 1),
    pool.decode(Uint8Array.of(2, 2), 1),
    pool.decode(Uint8Array.of(3, 3), 0),
    pool.decode(Uint8Array.of(4, 4), 0),
  ]);
  assert.deepEqual(log, [3, 4, 1, 2], 'demand (0) before prefetch (1), first come first served within a priority');
  assert.deepEqual(results.map((r) => r[0]), [1, 2, 3, 4]);
  pool.close();
});

test('pool spreads work over its workers', async () => {
  const seen = new Set();
  const { spawn } = fakeWorkerFactory({ decodeDelayMs: 5 });
  const pool = new DecodePool({
    size: 3,
    spawn: () => {
      const worker = spawn();
      const post = worker.postMessage;
      worker.postMessage = (message) => {
        if (message.type === 'decode') seen.add(worker);
        post(message);
      };
      return worker;
    },
    init: { type: 'init' },
  });
  await Promise.all(Array.from({ length: 6 }, (_, i) => pool.decode(Uint8Array.of(i + 1, 0))));
  assert.equal(seen.size, 3);
  pool.close();
});

test('an aborted job that is still queued is dropped without running', async () => {
  const { spawn, log } = fakeWorkerFactory({ decodeDelayMs: 10 });
  const pool = new DecodePool({ size: 1, spawn, init: { type: 'init' } });
  const abort = new AbortController();
  const running = pool.decode(Uint8Array.of(1, 1));
  const dropped = pool.decode(Uint8Array.of(2, 2), 0, abort.signal);
  abort.abort();
  await assert.rejects(dropped, { name: 'AbortError' });
  await running;
  assert.deepEqual(log, [1]);
  pool.close();
});

test('worker errors reject only their job', async () => {
  const { spawn } = fakeWorkerFactory();
  const pool = new DecodePool({ size: 1, spawn, init: { type: 'init' } });
  await assert.rejects(pool.decode(Uint8Array.of(0xff, 0)), /corrupt chunk/);
  assert.equal((await pool.decode(Uint8Array.of(7, 7)))[0], 7);
  pool.close();
});

test('a worker that fails to start fails every job with the reason', async () => {
  const { spawn } = fakeWorkerFactory({ failInit: true });
  const pool = new DecodePool({ size: 2, spawn, init: { type: 'init' } });
  await assert.rejects(pool.decode(Uint8Array.of(1, 1)), /decode worker failed to start: no such codec/);
  await assert.rejects(pool.decode(Uint8Array.of(1, 1)), /failed to start/);
});

test('close() rejects pending jobs as aborted and terminates the workers', async () => {
  const { spawn } = fakeWorkerFactory({ readyDelayMs: 20 });
  const workers = [];
  const pool = new DecodePool({ size: 2, spawn: () => workers.push(spawn()) && workers.at(-1), init: { type: 'init' } });
  const pending = pool.decode(Uint8Array.of(1, 1));
  pool.close();
  await assert.rejects(pending, { name: 'AbortError' });
  assert.ok(workers.every((w) => w.terminated));
  await assert.rejects(pool.decode(Uint8Array.of(1, 1)), { name: 'AbortError' });
});

test('bytes that are a view into a larger buffer are copied before transfer', async () => {
  const seen = [];
  const { spawn } = fakeWorkerFactory();
  const pool = new DecodePool({
    size: 1,
    spawn: () => {
      const worker = spawn();
      const post = worker.postMessage;
      worker.postMessage = (message, transfer) => {
        if (message.type === 'decode') seen.push([message.bytes.byteOffset, message.bytes.buffer.byteLength, transfer[0] === message.bytes.buffer]);
        post(message, transfer);
      };
      return worker;
    },
    init: { type: 'init' },
  });
  const big = new Uint8Array(100).fill(9);
  const view = big.subarray(10, 20);
  await pool.decode(view);
  assert.deepEqual(seen, [[0, 10, true]]);
  assert.equal(view.buffer.byteLength, 100, 'the caller keeps its buffer');
  pool.close();
});

test('main-thread decoder honours an aborted signal', async () => {
  const decoder = new MainThreadDecoder({ registry: new Map([['zstd', () => ({ fromConfig: () => ({ decode: async (b) => b }) })]]) });
  const spec = { key: 'k', dtype: 'uint16', shape: [1, 1, 1, 2], codecs: [{ name: 'bytes', configuration: { endian: 'little' } }, { name: 'zstd', configuration: {} }] };
  const out = await decoder.decode(new Uint8Array(4), 0, undefined, spec);
  assert.equal(out.length, 2);
  const abort = new AbortController();
  abort.abort();
  await assert.rejects(decoder.decode(new Uint8Array(4), 0, abort.signal, spec), { name: 'AbortError' });
});

test('with a fallback, demand jobs do not wait for workers to start; background jobs do', async () => {
  const { spawn, log } = fakeWorkerFactory({ readyDelayMs: 20 });
  const fallbackLog = [];
  const fallback = { decode: async (bytes) => { fallbackLog.push(bytes[0]); return new Uint16Array(bytes.length).fill(bytes[0]); } };
  const pool = new DecodePool({ size: 1, spawn, init: { type: 'init' }, fallback });
  const demand = await pool.decode(Uint8Array.of(1, 1), 0);
  assert.equal(demand[0], 1);
  assert.deepEqual(fallbackLog, [1], 'decoded on the calling thread while the worker was still loading');
  const background = pool.decode(Uint8Array.of(2, 2), 1);
  assert.deepEqual(fallbackLog, [1], 'background work waits for a worker');
  assert.equal((await background)[0], 2);
  assert.deepEqual(log, [2]);
  const later = await pool.decode(Uint8Array.of(3, 3), 0);
  assert.equal(later[0], 3);
  assert.deepEqual(fallbackLog, [1], 'once a worker is ready, demand jobs use it too');
  pool.close();
});

test('with a fallback, workers that cannot start are replaced by it, with one warning', async (t) => {
  const warn = t.mock.method(console, 'warn', () => {});
  const { spawn } = fakeWorkerFactory({ failInit: true, readyDelayMs: 5 });
  const fallbackLog = [];
  const fallback = { decode: async (bytes) => { fallbackLog.push(bytes[0]); return new Uint16Array(bytes.length).fill(bytes[0]); } };
  const pool = new DecodePool({ size: 2, spawn, init: { type: 'init' }, fallback });
  const queued = pool.decode(Uint8Array.of(5, 5), 1);
  assert.equal((await queued)[0], 5);
  assert.equal((await pool.decode(Uint8Array.of(6, 6), 0))[0], 6);
  assert.equal((await pool.decode(Uint8Array.of(7, 7), 1))[0], 7);
  assert.deepEqual(fallbackLog, [5, 6, 7]);
  assert.equal(warn.mock.callCount(), 1);
  assert.match(warn.mock.calls[0].arguments[0], /decode worker failed to start: no such codec.*decoding on the main thread/);
});

test('cancelling a queued decode releases its job before a worker becomes ready', async (t) => {
  const { spawn, log } = fakeWorkerFactory({ readyDelayMs: 500 });
  const pool = new DecodePool({ size: 1, spawn, init: { type: 'init' } });
  t.after(() => pool.close());
  const abort = new AbortController();
  const pending = pool.decode(Uint8Array.of(9, 9), 1, abort.signal).catch(error => error.name);
  abort.abort();
  assert.equal(await Promise.race([pending, new Promise(resolve => setTimeout(() => resolve('still queued'), 50))]), 'AbortError');
  assert.deepEqual(log, []);
});

test('closed main-thread decoders cannot resurrect their codec cache', async () => {
  const decoder = new MainThreadDecoder({ registry: new Map() });
  const spec = { key: 'bytes', dtype: 'uint16', shape: [1, 1, 1, 2], codecs: [{ name: 'bytes', configuration: { endian: 'little' } }] };
  assert.equal((await decoder.decode(new Uint8Array(4), 0, undefined, spec)).length, 2);
  decoder.close(); decoder.close();
  await assert.rejects(decoder.decode(new Uint8Array(4), 0, undefined, spec), { name: 'AbortError' });
});
