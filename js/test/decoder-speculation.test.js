import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStore } from '../chronozarr/decoder.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

// What speculative fetching spends: the idle allowance follows the measured link, a job that is not the user does not
// count as activity, a request is never sent twice, a new plan takes over the requests of the one it replaces, and the
// number of requests in flight follows the link. Chunks are 4 bands of 128 x 128 uint16 (128 KB decoded), 2 x 2 cells.

const LARGE = { nTime: 60, nBand: 4, height: 256, width: 256, chunk: 128, sharded: false };
const LARGE_CHUNK = 4 * 128 * 128 * 2;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/** The data chunk objects requested so far, in order (the store is unsharded: one object per chunk). */
const dataKeys = (readable) => readable.log.filter((c) => /\/data\/c\//.test(c.key)).map((c) => c.key);

/** A readable whose transfers take bytes / rate of *simulated* time, on a clock the test owns. */
function simulatedLink(readable, rate) {
  const link = { now: 0, clock: () => link.now };
  const pass = (read) => async (...args) => {
    const bytes = await read(...args);
    if (bytes) link.now += (bytes.length / rate) * 1000;
    return bytes;
  };
  link.store = { get: pass((key, o) => readable.get(key, o)), getRange: pass((key, r, o) => readable.getRange(key, r, o)) };
  return link;
}

/** A store on a simulated link of `rate` bytes/s whose bandwidth estimate has been measured with a few demand reads; idle prefetch limited only by the defaults. */
async function openMeasured(rate, options = {}) {
  const readable = buildSyntheticStore(LARGE);
  const link = simulatedLink(readable, rate);
  const store = await openStore('memory://spec', { store: link.store, workers: 0, clock: link.clock, speculativeBytesInitial: 1e12, ...options });
  // Enough demand reads that the estimate has over a millisecond of (simulated) transfer time, whatever the rate.
  for (let t = 0; t < 30 && !(store.bandwidthEstimate() > 0); t++) await store.getRaw(0, 0, 0, t);
  assert.ok(store.bandwidthEstimate() > 0, 'the link is measured');
  return { store, readable, link };
}

test('idle prefetch spends what the measured link delivers in idleSeconds, not the fixed byte cap, when that is less', async () => {
  const slow = await openMeasured(400_000);
  const fast = await openMeasured(400_000_000);
  const slowRun = await slow.store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, concurrency: 1 });
  const fastRun = await fast.store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, concurrency: 1 });
  const chunkWire = slow.store.chunkWireBytes(0);
  assert.equal(slowRun.budgetReached, true);
  assert.ok(slowRun.fetched >= 1 && slowRun.fetched <= Math.ceil(400_000 / chunkWire) + 1, `a 0.4 MB/s link speculates about one second of itself: ${slowRun.fetched} chunks of ${Math.round(chunkWire)} B`);
  assert.equal(fastRun.budgetReached, false, 'the fast link reaches the whole horizon within the 32 MiB cap');
  assert.equal(fastRun.fetched, fastRun.planned);
});

test('idleSeconds is an option: a longer allowance on the same link reaches further', async () => {
  const short = await openMeasured(400_000, { idleSeconds: 1 });
  const long = await openMeasured(400_000, { idleSeconds: 4 });
  const a = await short.store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, concurrency: 1 });
  const b = await long.store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, concurrency: 1 });
  assert.ok(b.fetched >= 3 * a.fetched, `4 s of link started ${b.fetched} chunks, 1 s started ${a.fetched}`);
});

test('an ambient job (the coarse loop that stays behind a frame) does not open an active period for the horizon prefetch', async () => {
  // A fast link, so the simulated clock hardly moves and the 3 s of an active period do not run out; the allowance is three chunks.
  const allowance = 3 * 0.7 * LARGE_CHUNK;
  const { store } = await openMeasured(400_000_000, { idleBytes: allowance });
  const coarse = await store.prefetch({ lod: 0, cells: [[1, 0]], t: 30, loop: true, playing: true, ambient: true, maxSteps: 3, concurrency: 1 });
  assert.equal(coarse.planned, 3, 'the coarse loop plans what it was asked for, nearest first');
  const horizon = await store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, concurrency: 1 });
  assert.equal(horizon.budgetReached, true, 'the horizon prefetch is still held to the idle allowance');
  const asPlayback = await openMeasured(400_000_000, { idleBytes: allowance });
  await asPlayback.store.prefetch({ lod: 0, cells: [[1, 0]], t: 30, loop: true, playing: true, maxSteps: 3, concurrency: 1 });
  const uncapped = await asPlayback.store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, concurrency: 1 });
  assert.equal(uncapped.budgetReached, false, 'the same job without ambient is playback: the viewer is not idle for the next idleMs');
});

test('maxSteps plans that many timesteps, nearest first, in a loop too', async () => {
  const { store } = await openMeasured(400_000_000);
  const result = await store.prefetch({ lod: 0, cells: [[0, 1]], t: 58, loop: true, playing: true, behindFactor: 8, maxSteps: 4, concurrency: 1 });
  assert.equal(result.planned, 4);
  for (const t of [58, 59, 0, 1]) assert.ok(store.peekRaw(0, 0, 1, t), `t=${t} is the nearest four around the wrap`);
  assert.equal(store.peekRaw(0, 0, 1, 2), undefined);
});

test('a demand read that starts while a prefetch worker waits for its allowance is not fetched a second time', async () => {
  // Three slow reads measure the link at about 2 MB/s on the real clock and earn about one chunk of allowance (half the link,
  // with no initial allowance); the next chunk takes a worker about 40 ms to earn.
  const readable = buildSyntheticStore({ ...LARGE, delayMs: 40 });
  const store = await openStore('memory://race', { store: readable, workers: 0, speculativeBytesInitial: 0, idleBytes: 1e12 });
  for (let t = 0; t < 3; t++) await store.getRaw(0, 0, 0, t);
  assert.ok(store.bandwidthEstimate() > 0);
  readable.delayMs = 300;
  const abort = new AbortController();
  const prefetch = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, concurrency: 2, signal: abort.signal });
  await sleep(10);
  // The first worker took t=30 (the warm-up earned enough for that one); the second took t=31 and waits for the allowance. A demand
  // read of t=31 starts meanwhile and is still in flight when it wakes.
  const demand = store.getRaw(0, 0, 0, 31);
  await sleep(150);
  const requests = dataKeys(readable).filter((key) => /\/c\/31\//.test(key));
  abort.abort();
  await Promise.all([demand, prefetch]);
  assert.equal(requests.length, 1, `t=31 was requested once, by the demand read (${requests.length} requests)`);
});

test('a new prefetch job takes over the requests of the one it replaces: the old one can be aborted and nothing is fetched twice', async () => {
  const readable = buildSyntheticStore({ ...LARGE, delayMs: 60 });
  const store = await openStore('memory://takeover', { store: readable, workers: 0, speculativeBytesInitial: 1e12, idleBytes: 1e12 });
  const first = new AbortController();
  const a = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, concurrency: 4, signal: first.signal });
  await sleep(20);
  assert.ok(dataKeys(readable).length >= 3, 'the first job has requests in flight');
  const second = new AbortController();
  const landed = [];
  const b = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, concurrency: 4, signal: second.signal, onChunk: (lod, row, col, t) => landed.push(t) });
  first.abort();
  const [, result] = await Promise.all([a, b]);
  for (let t = 18; t <= 42; t++) assert.ok(store.peekRaw(0, 0, 0, t), `t=${t} loaded`);
  const keys = dataKeys(readable);
  assert.equal(new Set(keys).size, keys.length, 'every chunk was requested once');
  assert.equal(result.errors.length, 0);
  assert.deepEqual([...landed].sort((x, y) => x - y), Array.from({ length: 25 }, (_, i) => 18 + i), 'the new job heard of every chunk, the ones it took over included, once');
});

test('a request nobody wants any more is still cancelled: aborting a job that nothing took over stops its fetches', async () => {
  const readable = buildSyntheticStore({ ...LARGE, delayMs: 60 });
  const store = await openStore('memory://cancel', { store: readable, workers: 0, speculativeBytesInitial: 1e12 });
  const only = new AbortController();
  const a = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, concurrency: 3, signal: only.signal });
  await sleep(20);
  const started = dataKeys(readable).length;
  only.abort();
  await a;
  await sleep(120);
  assert.equal(dataKeys(readable).length, started, 'nothing new started after the abort');
  assert.equal(store.peekRaw(0, 0, 0, 30), undefined, 'and the cancelled requests did not land');
});

for (const firstPart of ['data', 'mask']) {
  test(`masked takeover awaits both owned requests when ${firstPart} arrives first`, async () => {
    const readable = buildSyntheticStore({ nTime: 2, nBand: 1, height: 8, width: 8, chunk: 8, sharded: false, mask: true });
    const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
    const gates = { data: deferred(), mask: deferred() };
    const started = { data: deferred(), mask: deferred() };
    const fetching = {
      async get(key, options) {
        const bytes = await readable.get(key, options);
        const kind = /\/0\/(data|mask)\/c\//.exec(key)?.[1];
        if (kind) {
          started[kind].resolve();
          await gates[kind].promise;
          options.signal.throwIfAborted();
        }
        return bytes;
      },
    };
    const store = await openStore('memory://masked-takeover', { store: fetching, workers: 0, speculativeBytesInitial: 1e12, idleBytes: 1e12 });
    const old = new AbortController(), next = new AbortController();
    const seen = [];
    const a = store.prefetch({ lod: 0, cells: [[0, 0]], t: 1, maxSteps: 1, masks: true, signal: old.signal, onChunk: () => assert.fail('aborted plan must not notify') });
    await Promise.all([started.data.promise, started.mask.promise]);
    let completed = false;
    const b = store.prefetch({ lod: 0, cells: [[0, 0]], t: 1, maxSteps: 1, masks: true, signal: next.signal, onChunk: () => seen.push(!!store.peekMask(0, 0, 0, 1)) }).then((result) => { completed = true; return result; });
    old.abort();
    const calls = readable.log.filter(({ key }) => key.includes('/c/'));
    assert.equal(calls.length, 2);
    assert.ok(calls.every(({ signal }) => !signal.aborted), 'successor owns data and mask before prior owner aborts');
    gates[firstPart].resolve();
    await new Promise(setImmediate);
    assert.equal(completed, false, 'one part is not logical completion');
    assert.deepEqual(seen, []);
    gates[firstPart === 'data' ? 'mask' : 'data'].resolve();
    const [, result] = await Promise.all([a, b]);
    assert.equal(result.errors.length, 0);
    assert.ok(store.peekRaw(0, 0, 0, 1));
    assert.ok(store.peekMask(0, 0, 0, 1));
    assert.deepEqual(seen, [true]);
    assert.equal(readable.log.filter(({ key }) => key.includes('/c/')).length, 2, 'neither part fetched twice');
    store.close();
  });
}

test('fresh data prefetch waits for an already-running demand mask before completion or onChunk', async () => {
  const readable = buildSyntheticStore({ nTime: 2, nBand: 1, height: 8, width: 8, chunk: 8, sharded: false, mask: true });
  let maskStarted, releaseMask;
  const started = new Promise((resolve) => { maskStarted = resolve; });
  const gate = new Promise((resolve) => { releaseMask = resolve; });
  const fetching = { async get(key, options) {
    const bytes = await readable.get(key, options);
    if (key.includes('/mask/c/')) { maskStarted(); await gate; }
    return bytes;
  } };
  const store = await openStore('memory://demand-mask', { store: fetching, workers: 0 });
  const demand = store.getMask(0, 0, 0, 1);
  await started;
  const seen = [];
  let completed = false;
  const job = store.prefetch({ lod: 0, cells: [[0, 0]], t: 1, maxSteps: 1, masks: true, seek: true, onChunk: () => seen.push(!!store.peekMask(0, 0, 0, 1)) }).then((result) => { completed = true; return result; });
  await new Promise(setImmediate);
  assert.ok(store.peekRaw(0, 0, 0, 1), 'fresh data arrived');
  assert.equal(completed, false);
  assert.deepEqual(seen, []);
  releaseMask();
  const [, result] = await Promise.all([demand, job]);
  assert.equal(result.errors.length, 0);
  assert.deepEqual(seen, [true]);
  assert.equal(readable.log.filter(({ key }) => key.includes('/mask/c/')).length, 1);
  store.close();
});

/** The most requests the store had in flight at once while `body` ran. */
async function peakInflight(rate, body) {
  const readable = buildSyntheticStore({ ...LARGE, delayMs: 20 });
  const link = simulatedLink(readable, rate);
  let inflight = 0;
  let peak = 0;
  const counted = (read) => async (...args) => {
    inflight++;
    peak = Math.max(peak, inflight);
    try {
      return await read(...args);
    } finally {
      inflight--;
    }
  };
  const counting = { get: counted(link.store.get), getRange: counted(link.store.getRange) };
  const store = await openStore('memory://flight', { store: counting, workers: 0, clock: link.clock, speculativeBytesInitial: 1e12, idleBytes: 1e12 });
  for (let t = 0; t < 3; t++) await store.getRaw(0, 0, 0, t);
  inflight = 0;
  peak = 0;
  await body(store);
  return peak;
}

test('speculative requests in flight carry about a second of the measured link: one on a slow link, the default six on a fast one', async () => {
  const run = (store) => store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, playing: true });
  const slow = await peakInflight(200_000, run);
  const fast = await peakInflight(400_000_000, run);
  assert.equal(slow, 1, 'a 0.2 MB/s link moves about one 90 KB chunk in the time it takes one chunk');
  assert.equal(fast, 6);
});

test('the limit counts the speculative requests of every job together, not each job on its own', async () => {
  // A movie's prefetch and the coarse loop each sized for the whole link would take twice the link.
  const both = (store) =>
    Promise.all([
      store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, playing: true }),
      store.prefetch({ lod: 0, cells: [[1, 0]], t: 30, loop: true, playing: true, ambient: true }),
    ]);
  const slow = await peakInflight(200_000, both);
  const fast = await peakInflight(400_000_000, both);
  assert.equal(slow, 1, 'two jobs on a 0.2 MB/s link still have one request in flight between them');
  assert.equal(fast, 6, 'and on a fast link six, not twelve');
});

test('the limit follows the estimate as it settles: a job that started before the link was measured is held to it afterwards', async () => {
  const readable = buildSyntheticStore({ ...LARGE, delayMs: 20 });
  const link = simulatedLink(readable, 200_000);
  let inflight = 0;
  const log = [];
  const counted = (read) => async (...args) => {
    inflight++;
    log.push({ at: link.now, inflight });
    try {
      return await read(...args);
    } finally {
      inflight--;
    }
  };
  const store = await openStore('memory://settle', { store: { get: counted(link.store.get), getRange: counted(link.store.getRange) }, workers: 0, clock: link.clock, speculativeBytesInitial: 1e12, idleBytes: 1e12 });
  // Nothing measured yet: the job gets all six slots for its first requests.
  const mark = log.length;
  const job = store.prefetch({ lod: 0, cells: [[0, 1]], t: 30, playing: true });
  await sleep(5);
  const burst = inflight;
  await job;
  const later = log.slice(mark + 6).map((entry) => entry.inflight);
  assert.equal(burst, 6, 'six requests start before there is an estimate');
  assert.ok(later.length > 10, 'the job went on after the burst');
  assert.equal(Math.max(...later), 1, 'once the link is measured (0.2 MB/s, one slot) a request starts only when none is in flight');
});

test('chunkWireBytes is what a chunk of the level takes on the wire, estimated from what was fetched', async () => {
  const readable = buildSyntheticStore({ ...LARGE, nLevels: 2 });
  const store = await openStore('memory://wire', { store: readable, workers: 0 });
  const before = store.chunkWireBytes(0);
  assert.ok(Math.abs(before - 0.7 * LARGE_CHUNK) < 1, 'before any chunk: 0.7 of the decoded size');
  await store.getRaw(0, 0, 0, 0);
  const after = store.chunkWireBytes(0);
  assert.ok(after > before, 'the synthetic chunks are stored uncompressed, so the estimate rises toward their size');
  assert.ok(after <= LARGE_CHUNK * 1.1);
  assert.ok(store.chunkWireBytes(1) > 0, 'a level with no chunks yet uses the average of the others');
  assert.throws(() => store.chunkWireBytes(5), /out of range/);
});

/** Let every promise continuation that can run without a timer run. */
const settled = async () => {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve));
};

/**
 * A store whose data chunks are answered only when the test lets them through (the gate is armed after the link has been
 * measured). `started` lists the timesteps requested since, in order; `release(t, row, col)` answers one request and
 * `openAll()` all of them, now and from then on; an aborted request is rejected. With a `rate` the link is measured at that
 * many bytes/s on the simulated clock, which fixes how many speculative requests may be in flight (0.2 MB/s leaves one).
 */
async function openGated({ rate = 400_000_000, ...options } = {}) {
  const readable = buildSyntheticStore(LARGE);
  const link = simulatedLink(readable, rate);
  const waiting = new Map();
  const started = [];
  let armed = false;
  const gate = (read) => async (key, o, ...rest) => {
    const chunk = /\/data\/c\/(\d+)\/0\/(\d+\/\d+)$/.exec(key);
    if (chunk && armed) started.push(Number(chunk[1]));
    const bytes = await read(key, o, ...rest);
    if (!chunk || !armed) return bytes;
    await new Promise((resolve, reject) => {
      waiting.set(`${chunk[1]}/${chunk[2]}`, resolve);
      o?.signal?.addEventListener('abort', () => reject(o.signal.reason), { once: true });
    });
    return bytes;
  };
  const store = await openStore('memory://gated', { store: { get: gate(link.store.get), getRange: gate(link.store.getRange) }, workers: 0, clock: link.clock, speculativeBytesInitial: 1e12, idleBytes: 1e12, ...options });
  for (let t = 0; t < 30 && !(store.bandwidthEstimate() > 0); t++) await store.getRaw(0, 0, 0, t);
  armed = true;
  const release = (t, row = 0, col = 0) => {
    waiting.get(`${t}/${row}/${col}`)();
    waiting.delete(`${t}/${row}/${col}`);
  };
  const openAll = () => {
    armed = false;
    for (const resolve of waiting.values()) resolve();
    waiting.clear();
  };
  return { store, readable, started, release, openAll };
}

test('a demand read that joins a speculative request holds speculative starts back until that request settles', async () => {
  const { store, started, release } = await openGated();
  const abort = new AbortController();
  const job = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, signal: abort.signal });
  await settled();
  assert.deepEqual(started, [30, 31, 29, 32, 33, 28], 'six speculative requests in flight, nearest first');
  const read = store.getRaw(0, 0, 0, 32);
  for (const t of [30, 31, 29, 33, 28]) release(t);
  await settled();
  assert.equal(started.length, 6, 'nothing replaced the five that settled while the read waits for the sixth');
  release(32);
  await read;
  await settled();
  assert.ok(started.length > 6, 'prefetch goes on once the joined request has settled');
  abort.abort();
  await job;
});

test('workers that wait for a request slot do not start once a demand read has begun', async () => {
  // A long idleSeconds, so that the allowance of an idle job (a second of this slow link) does not end the job after two chunks.
  const { store, started, release } = await openGated({ rate: 200_000, idleSeconds: 1000 });
  const abort = new AbortController();
  const job = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, signal: abort.signal });
  await settled();
  assert.deepEqual(started, [30], 'the slow link leaves one speculative slot; the other workers wait for it');
  const read = store.getRaw(0, 0, 0, 50);
  await settled();
  release(30);
  await settled();
  assert.deepEqual(started, [30, 50], 'the slot that freed up stays unused while the read waits');
  release(50);
  await read;
  await settled();
  assert.ok(started.length > 2, 'prefetch goes on once the read has settled');
  abort.abort();
  await job;
});

test('a movie is not held back by a demand read that joins its requests or begins while its workers wait', async () => {
  const joined = await openGated();
  const abortJoined = new AbortController();
  const movie = joined.store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, playing: true, signal: abortJoined.signal });
  await settled();
  const read = joined.store.getRaw(0, 0, 0, 32);
  for (const t of [30, 31, 29, 33, 28]) joined.release(t);
  await settled();
  assert.ok(joined.started.length > 6, 'the pump joining a chunk of the movie does not stop the next ones');
  joined.release(32);
  await read;
  abortJoined.abort();
  await movie;

  const parked = await openGated({ rate: 200_000 });
  const abortParked = new AbortController();
  const other = parked.store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, playing: true, signal: abortParked.signal });
  await settled();
  assert.deepEqual(parked.started, [30]);
  const pump = parked.store.getRaw(0, 0, 0, 50);
  await settled();
  parked.release(30);
  await settled();
  assert.equal(parked.started.length, 3, 'a worker that waited for the slot starts as soon as it frees up');
  parked.release(50);
  await pump;
  abortParked.abort();
  await other;
});

test('a seek does not cancel the requests of an ambient job: its plan is complete once both jobs have finished', async () => {
  const { store, readable, started, openAll } = await openGated();
  const mark = readable.log.length;
  const loopAbort = new AbortController();
  const landed = [];
  const loop = store.prefetch({ lod: 0, cells: [[0, 0]], t: 10, loop: true, playing: true, ambient: true, behindFactor: 8, maxSteps: 30, signal: loopAbort.signal, onChunk: (l, r, c, t) => landed.push(t) });
  await settled();
  assert.equal(started.length, 6, 'the coarse loop has six requests in flight');
  const jump = store.prefetch({ lod: 0, cells: [[1, 0]], t: 10, seek: true });
  await settled();
  assert.ok(readable.log.slice(mark, mark + 6).every(({ signal }) => !signal.aborted), 'the seek left them running');
  openAll();
  const [result] = await Promise.all([loop, jump]);
  assert.equal(result.fetched, 30, 'the coarse loop fetched every chunk it planned');
  assert.equal(landed.length, 30);
  for (const t of landed) assert.ok(store.hasCachedRaw(0, 0, 0, t), `t=${t} is cached`);
});

test('a seek takes over the speculative requests its plan wants and cancels the others', async () => {
  const { store, readable, started, openAll } = await openGated();
  const mark = readable.log.length;
  const horizon = new AbortController();
  const old = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, signal: horizon.signal });
  await settled();
  assert.deepEqual(started, [30, 31, 29, 32, 33, 28]);
  const jump = new AbortController();
  const next = store.prefetch({ lod: 0, cells: [[0, 0]], t: 31, seek: true, maxSteps: 2, signal: jump.signal });
  horizon.abort();
  await settled();
  const aborted = (t) => readable.log.slice(mark).find(({ key }) => key.includes(`/c/${t}/0/0/0`)).signal.aborted;
  for (const t of [31, 32]) assert.equal(aborted(t), false, `t=${t} is in the seek's plan: it keeps running`);
  for (const t of [30, 29, 33, 28]) assert.equal(aborted(t), true, `t=${t} is not: it is cancelled`);
  openAll();
  const [, result] = await Promise.all([old, next]);
  assert.equal(result.errors.length, 0);
  for (const t of [31, 32]) assert.ok(store.peekRaw(0, 0, 0, t), `t=${t} loaded`);
  assert.equal(started.filter((t) => t === 31 || t === 32).length, 2, 'each was requested once');
});

test('a seek promotes the request for its own timestep that was running as speculation: the rest of its window waits for it', async () => {
  const { store, started, release, openAll } = await openGated();
  const horizon = new AbortController();
  const old = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, signal: horizon.signal });
  await settled();
  assert.deepEqual(started, [30, 31, 29, 32, 33, 28]);
  const jump = new AbortController();
  const next = store.prefetch({ lod: 0, cells: [[0, 0]], t: 31, seek: true, maxSteps: 8, signal: jump.signal });
  horizon.abort();
  await settled();
  assert.equal(started.length, 6, 'the six requests of the window are all taken over, and nothing else starts while t=31 is outstanding');
  release(31);
  await settled();
  assert.ok(started.length > 6, 'the window goes on once it has landed');
  openAll();
  await Promise.all([old, next]);
});
