import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DecodePool, MainThreadDecoder, leaseDecodePool, sharedPoolCount } from '../chronozarr/pool.js';

const SPEC = { key: 'spec-a', dtype: 'uint16', shape: [1, 1, 1, 2], codecs: [] };
const OTHER_SPEC = { key: 'spec-b', dtype: 'uint8', shape: [1, 1, 1, 2], codecs: [] };
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** In-process Worker stand-in that records every message it receives. */
function recordingWorkers({ failInit = false } = {}) {
  const made = [];
  const spawn = () => {
    const worker = {
      onmessage: null,
      onerror: null,
      terminated: false,
      received: [],
      postMessage(message) {
        worker.received.push(message);
        setTimeout(() => {
          if (worker.terminated) return;
          if (message.type === 'init') worker.onmessage({ data: failInit ? { type: 'error', message: 'no codecs' } : { type: 'ready' } });
          else if (message.type === 'decode') worker.onmessage({ data: { type: 'decoded', id: message.id, data: new Uint16Array(message.bytes.length).fill(message.bytes[0]) } });
        }, 1);
      },
      terminate() {
        worker.terminated = true;
      },
    };
    made.push(worker);
    return worker;
  };
  return { spawn, made };
}

test('every decode message names its chunk spec, so one worker serves several arrays', async () => {
  const { spawn, made } = recordingWorkers();
  const pool = new DecodePool({ size: 1, spawn, init: { type: 'init' } });
  await pool.decode(Uint8Array.of(1, 1), 1, undefined, SPEC);
  await pool.decode(Uint8Array.of(2, 2), 1, undefined, OTHER_SPEC);
  const decodes = made[0].received.filter((m) => m.type === 'decode');
  assert.deepEqual(decodes.map((m) => m.spec.key), ['spec-a', 'spec-b']);
  pool.close();
});

test('warm() reaches workers that are ready now and workers that become ready later, once per spec', async () => {
  const { spawn, made } = recordingWorkers();
  const pool = new DecodePool({ size: 2, spawn, init: { type: 'init' } });
  pool.warm(SPEC);
  pool.warm(SPEC);
  await sleep(10);
  for (const worker of made) assert.deepEqual(worker.received.filter((m) => m.type === 'warm').map((m) => m.spec.key), ['spec-a'], 'warmed when it became ready');
  pool.warm(OTHER_SPEC);
  for (const worker of made) assert.deepEqual(worker.received.filter((m) => m.type === 'warm').map((m) => m.spec.key), ['spec-a', 'spec-b']);
  pool.close();
});

test('a demand job whose priority handle is lowered jumps the queue', async () => {
  const { spawn, made } = recordingWorkers();
  const pool = new DecodePool({ size: 1, spawn, init: { type: 'init' } });
  const order = [];
  const track = (name, promise) => promise.then(() => order.push(name));
  await sleep(5);
  const handle = { value: 1 };
  const all = [
    track('first', pool.decode(Uint8Array.of(1, 1), 1, undefined, SPEC)),
    track('second', pool.decode(Uint8Array.of(2, 2), 1, undefined, SPEC)),
    track('raised', pool.decode(Uint8Array.of(3, 3), handle, undefined, SPEC)),
  ];
  handle.value = 0;
  pool.reprioritize();
  await Promise.all(all);
  assert.deepEqual(made[0].received.filter((m) => m.type === 'decode').map((m) => m.bytes[0]), [1, 3, 2], 'the running job finishes, then the raised one, then the rest');
  assert.deepEqual(order, ['first', 'raised', 'second']);
  pool.close();
});

test('leases of one pool share its workers; they are spawned once', async () => {
  const { spawn, made } = recordingWorkers();
  const key = 'shared-once';
  const a = leaseDecodePool({ key, size: 3, spawn, init: { type: 'init' }, idleMs: 20 });
  const b = leaseDecodePool({ key, size: 3, spawn, init: { type: 'init' }, idleMs: 20 });
  assert.equal(made.length, 3);
  assert.equal(a.size, 3);
  assert.equal((await b.decode(Uint8Array.of(5, 5), 1, undefined, SPEC))[0], 5);
  a.close();
  a.close();
  await sleep(40);
  assert.equal(made.some((w) => w.terminated), false, 'the other lease is still using the pool');
  assert.equal((await b.decode(Uint8Array.of(6, 6), 1, undefined, SPEC))[0], 6);
  b.close();
  await sleep(60);
  assert.ok(made.every((w) => w.terminated), 'workers are terminated once the last lease has been idle');
  assert.equal(sharedPoolCount(), 0);
});

test('a store opened right after another closed finds the workers still running', async () => {
  const { spawn, made } = recordingWorkers();
  const key = 'shared-reopen';
  const first = leaseDecodePool({ key, size: 2, spawn, init: { type: 'init' }, idleMs: 100 });
  await first.decode(Uint8Array.of(1, 1), 1, undefined, SPEC);
  first.close();
  await sleep(20);
  const second = leaseDecodePool({ key, size: 2, spawn, init: { type: 'init' }, idleMs: 100 });
  assert.equal(made.length, 2, 'no new workers: the idle pool was reused');
  assert.equal((await second.decode(Uint8Array.of(7, 7), 1, undefined, OTHER_SPEC))[0], 7);
  assert.equal(made.filter((w) => w.received.some((m) => m.type === 'init')).length, 2);
  second.close();
  await sleep(150);
  assert.ok(made.every((w) => w.terminated));
});

test('a pool that failed is replaced by the next lease', async () => {
  const broken = recordingWorkers({ failInit: true });
  const key = 'shared-failed';
  const first = leaseDecodePool({ key, size: 1, spawn: broken.spawn, init: { type: 'init' }, idleMs: 20 });
  await assert.rejects(first.decode(Uint8Array.of(1, 1), 1, undefined, SPEC), /failed to start: no codecs/);
  const healthy = recordingWorkers();
  const second = leaseDecodePool({ key, size: 1, spawn: healthy.spawn, init: { type: 'init' }, idleMs: 20 });
  assert.equal(healthy.made.length, 1, 'a fresh pool was built');
  assert.equal((await second.decode(Uint8Array.of(4, 4), 1, undefined, SPEC))[0], 4);
  first.close();
  second.close();
  await sleep(60);
});

test('a lease with a fallback decodes demand jobs on the calling thread until a worker is ready', async () => {
  const { spawn } = recordingWorkers();
  const key = 'shared-fallback';
  const fallback = new MainThreadDecoder({ registry: new Map([['gzip', () => ({ fromConfig: () => ({ decode: async (b) => b }) })]]) });
  const spec = { key: 'fb', dtype: 'uint8', shape: [1, 1, 1, 2], codecs: [{ name: 'bytes' }, { name: 'gzip', configuration: {} }] };
  const lease = leaseDecodePool({ key, size: 1, spawn, init: { type: 'init' }, fallback, idleMs: 20 });
  const out = await lease.decode(Uint8Array.of(8, 9), 0, undefined, spec);
  assert.deepEqual([...out], [8, 9]);
  lease.close();
  await sleep(50);
});
