import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStore } from '../chronozarr/decoder.js';
import { buildSyntheticStore, defaultValues } from '../support/synthetic-store.js';

const SMALL = { nTime: 12, nBand: 1, height: 16, width: 16, chunk: 32, anchorInterval: 4, sharded: true };
const SMALL_CHUNK = 32 * 32 * 2;
/** Chunks of 128 KB decoded: big enough for the bandwidth estimator to take notice. */
const LARGE = { nTime: 12, nBand: 4, height: 128, width: 128, chunk: 128, anchorInterval: 4, sharded: true };
const LARGE_CHUNK = 4 * 128 * 128 * 2;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const chunkReads = (readable, from = 0) => readable.log.slice(from).filter((c) => c.range && 'offset' in c.range);

// ---- compressed tier ----

test('a chunk demoted from the decoded tier is decoded again from its compressed bytes, with no request', async () => {
  const readable = buildSyntheticStore(SMALL);
  const store = await openStore('memory://tiers', { store: readable, workers: 0, maxCacheBytes: SMALL_CHUNK * 2, compressedBytes: SMALL_CHUNK * 50 });
  store.evictionScore = (entry) => entry.t;
  for (const t of [0, 1, 2, 3]) await store.getRaw(0, 0, 0, t);
  assert.ok(store.peekRaw(0, 0, 0, 0) && store.peekRaw(0, 0, 0, 3), 'the nearest chunks stay decoded');
  assert.equal(store.peekRaw(0, 0, 0, 1), undefined);
  assert.equal(store.peekRaw(0, 0, 0, 2), undefined);
  const info = store.cacheInfo();
  assert.equal(info.entries, 2);
  assert.equal(info.compressedEntries, 4, 'every fetched chunk is also held compressed');
  assert.equal(store.stats.cache.decodedBytes, info.bytes);
  assert.equal(store.stats.cache.compressedBytes, info.compressedBytes);
  assert.equal(store.stats.cache.evictions, 2);

  const before = readable.log.length;
  const hits = store.stats.cache.compressedHits;
  const cell = await store.getCell(0, 0, 0, 1);
  assert.equal(readable.log.length, before, 'no request: the compressed copy was decoded');
  assert.equal(store.stats.cache.compressedHits, hits + 1);
  assert.equal(cell.data[5], defaultValues('uint16')(1, 0, 0, 5, 0), 'and it decodes to the right values');
  assert.ok(store.peekRaw(0, 0, 0, 1), 'decoded again, so resident');
});

test('with the compressed tier off, a demoted chunk is fetched again', async () => {
  const readable = buildSyntheticStore(SMALL);
  const store = await openStore('memory://no-compressed', { store: readable, workers: 0, maxCacheBytes: SMALL_CHUNK * 2, compressedBytes: 0 });
  store.evictionScore = (entry) => entry.t;
  for (const t of [0, 1, 2, 3]) await store.getRaw(0, 0, 0, t);
  assert.equal(store.cacheInfo().compressedEntries, 0);
  assert.equal(store.peekRaw(0, 0, 0, 1), undefined);
  const before = chunkReads(readable).length;
  await store.getRaw(0, 0, 0, 1);
  assert.equal(chunkReads(readable).length, before + 1);
});

test('prefetch keeps far timesteps as compressed bytes only and reaches further along time than the decoded tier could', async () => {
  const readable = buildSyntheticStore({ ...SMALL, nTime: 24 });
  const store = await openStore('memory://far', { store: readable, workers: 0, maxCacheBytes: SMALL_CHUNK * 4, compressedBytes: SMALL_CHUNK * 100, speculativeBytesInitial: 1e9 });
  store.evictionScore = (entry) => Math.abs(entry.t - 10);
  const result = await store.prefetch({ lod: 0, cells: [[0, 0]], t: 10, concurrency: 1 });
  assert.equal(result.planned, 24, 'the whole axis fits the compressed tier');
  assert.equal(result.fetched, 24);
  assert.ok(result.compressedOnly >= 18, `most chunks are far from t=10 and were not decoded (${result.compressedOnly})`);
  const info = store.cacheInfo();
  assert.ok(info.entries <= 4 + 1, `decoded tier holds only the nearest chunks (${info.entries})`);
  assert.equal(info.compressedEntries, 24);
  const before = chunkReads(readable).length;
  const far = await store.getRaw(0, 0, 0, 23);
  assert.ok(far instanceof Uint16Array);
  assert.equal(chunkReads(readable).length, before, 'served from the compressed tier');
  assert.equal(store.peekRaw(0, 0, 0, 10) instanceof Uint16Array, true, 'the timestep being viewed is decoded');
});

test('budgets can be read and changed at any time', async () => {
  const readable = buildSyntheticStore(SMALL);
  const store = await openStore('memory://budgets', { store: readable, workers: 0, maxCacheBytes: SMALL_CHUNK * 8, compressedBytes: SMALL_CHUNK * 8, speculativeBytesInitial: 1234 });
  assert.deepEqual(store.budgets(), { decodedBytes: SMALL_CHUNK * 8, compressedBytes: SMALL_CHUNK * 8, speculativeBytesInitial: 1234 });
  assert.equal(store.maxCacheBytes, SMALL_CHUNK * 8);
  for (let t = 0; t < 8; t++) await store.getRaw(0, 0, 0, t);
  assert.equal(store.cacheInfo().entries, 8);
  store.setBudgets({ decodedBytes: SMALL_CHUNK * 3 });
  assert.equal(store.cacheInfo().entries, 3);
  assert.equal(store.cacheInfo().compressedEntries, 8, 'the other tier is untouched');
  store.setBudgets({ compressedBytes: SMALL_CHUNK * 2 });
  assert.ok(store.cacheInfo().compressedEntries <= 2);
  assert.deepEqual(store.budgets(), { decodedBytes: SMALL_CHUNK * 3, compressedBytes: SMALL_CHUNK * 2, speculativeBytesInitial: 1234 });
  assert.equal(store.maxCacheBytes, SMALL_CHUNK * 3);
  assert.throws(() => store.setBudgets({ decodedBytes: -1 }), /decodedBytes must be a number of bytes >= 0/);
  assert.throws(() => store.setBudgets({ compressedBytes: NaN }), /compressedBytes/);
});

// ---- bandwidth and the speculative allowance ----

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

test('bandwidthEstimate is null at first, then follows the measured transfers', async () => {
  const readable = buildSyntheticStore(LARGE);
  const link = simulatedLink(readable, 2_000_000);
  const store = await openStore('memory://bw', { store: link.store, workers: 0, clock: link.clock });
  assert.equal(store.bandwidthEstimate(), null);
  for (let t = 0; t < 4; t++) await store.getRaw(0, 0, 0, t);
  const estimate = store.bandwidthEstimate();
  assert.ok(Math.abs(estimate - 2_000_000) / 2_000_000 < 0.1, `estimate ${estimate} B/s for a 2 MB/s link`);
});

test('speculative fetching starts with its initial allowance and waits for a bandwidth measurement after that', async () => {
  const readable = buildSyntheticStore(LARGE);
  const initial = LARGE_CHUNK * 0.7 * 2.5;
  // A frozen clock: transfers take no measured time, so no bandwidth estimate ever forms, however fast or slow the machine is.
  const store = await openStore('memory://allowance', { store: readable, workers: 0, speculativeBytesInitial: initial, clock: () => 0 });
  const abort = new AbortController();
  const done = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 1, signal: abort.signal });
  await sleep(80);
  abort.abort();
  const result = await done;
  assert.ok(result.fetched >= 2 && result.fetched <= 3, `2 or 3 chunks fit an allowance of 2.5 estimates (${result.fetched})`);
  assert.ok(store.stats.cache.speculativeBytes <= initial + LARGE_CHUNK, `speculative bytes ${store.stats.cache.speculativeBytes}`);
  assert.ok(result.planned > result.fetched, 'the rest of the window is waiting');
});

test('with no allowance and no measured bandwidth nothing speculative is fetched; demand reads are unaffected', async () => {
  const readable = buildSyntheticStore(LARGE);
  const store = await openStore('memory://none-allowed', { store: readable, workers: 0, speculativeBytesInitial: 0, clock: () => 0 });
  const abort = new AbortController();
  const done = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 2, signal: abort.signal });
  await sleep(60);
  assert.equal(chunkReads(readable).length, 0, 'no speculative chunk requested');
  assert.ok((await store.getRaw(0, 0, 0, 2)) instanceof Uint16Array, 'a demand read goes through');
  abort.abort();
  assert.equal((await done).fetched, 0);
});

/** Prefetch `cells` of a simulated link for `steps` steps of fake time (setTimeout faked, the store's clock is the link's), after `warmup` demand reads. */
async function prefetchOnFakeLink(t, { rate, steps, warmup, nTime = 200 }) {
  const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setImmediate(resolve));
  };
  const readable = buildSyntheticStore({ ...LARGE, nTime });
  const link = simulatedLink(readable, rate);
  const store = await openStore('memory://fake-link', { store: link.store, workers: 0, clock: link.clock, speculativeBytesInitial: 0, maxCacheBytes: 1e9, compressedBytes: 1e9 });
  const opened = link.now;
  for (let read = 0; read < warmup; read++) await store.getRaw(0, 0, 0, read);
  const shareBefore = store.stats.cache.speculativeShare;
  store.resetStats();
  const abort = new AbortController();
  const done = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 1, signal: abort.signal });
  await settle();
  for (let step = 0; step < steps; step++) {
    link.now += 100;
    t.mock.timers.tick(100);
    await settle();
  }
  abort.abort();
  const result = await done;
  return { store, bytes: store.stats.cache.speculativeBytes, elapsedS: (link.now - opened) / 1000, fetched: result.fetched, planned: result.planned, shareBefore, shareAfter: store.stats.cache.speculativeShare };
}

test('while the estimate is young the allowance grows with the measured bandwidth: half the link, spent at estimated size', async (t) => {
  // Nothing here waits on real time: setTimeout is faked and the link's clock advances only when the test says so.
  // 20 and 80 MB/s links move a 128 KB chunk in 6.5 and 1.6 ms, so the few dozen chunks fetched stay far below
  // the second of transfer time after which the estimate counts as established.
  t.mock.timers.enable({ apis: ['setTimeout'] });
  /** The allowance is spent at the *estimated* compressed size of a chunk (0.7 of decoded until chunks have been seen, rising toward the observed ratio), so real bytes can exceed what was earned by at most 1 / 0.7. */
  const MIN_ESTIMATE_RATIO = 0.7;
  const runs = [];
  for (const rate of [20_000_000, 80_000_000]) {
    const run = await prefetchOnFakeLink(t, { rate, steps: 1, warmup: 2 });
    assert.equal(run.shareBefore, 0.5);
    assert.equal(run.shareAfter, 0.5, 'still young at the end');
    const earned = 0.5 * rate * run.elapsedS;
    assert.ok(run.bytes > 0, 'a measured link earns allowance even though it started with none');
    assert.ok(run.bytes <= earned / MIN_ESTIMATE_RATIO, `${rate / 1e6} MB/s link stays within what it earned: ${run.bytes} <= ${earned} / ${MIN_ESTIMATE_RATIO}`);
    assert.ok(run.fetched < run.planned - 2, `${rate / 1e6} MB/s link is throttled (${run.fetched} of ${run.planned})`);
    runs.push(run);
  }
  assert.ok(runs[1].bytes > 2 * runs[0].bytes, `the faster link prefetched more: ${runs[1].bytes} vs ${runs[0].bytes}`);
});

test('once the estimate is established and nothing is pending, speculation is not throttled below the link', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  // A 400 KB/s link takes 0.33 s a chunk: after four demand reads (over a second of transfer) it is established.
  const run = await prefetchOnFakeLink(t, { rate: 400_000, steps: 1, warmup: 4, nTime: 60 });
  assert.equal(run.shareBefore, 1.0);
  assert.equal(run.fetched, run.planned - 4, 'the whole window within one step (the four chunks the warm-up reads already cached are skipped)');
  // The same link while young would have been held to half of it by the allowance:
  const young = await prefetchOnFakeLink(t, { rate: 400_000, steps: 1, warmup: 2, nTime: 60 });
  assert.equal(young.shareBefore, 0.5);
  assert.ok(young.fetched < run.fetched, `young: ${young.fetched} chunks, established: ${run.fetched}`);
});

test('the speculative share is 0.5 while the estimate is young or a demand read is pending, 1.0 when idle and established', async () => {
  const readable = buildSyntheticStore({ ...LARGE, nTime: 24 });
  const link = simulatedLink(readable, 500_000);
  let gate = null;
  const gated = {
    get: link.store.get,
    async getRange(key, range, options) {
      if (gate && range.offset !== undefined) await gate.promise;
      return link.store.getRange(key, range, options);
    },
  };
  const store = await openStore('memory://share', { store: gated, workers: 0, clock: link.clock, speculativeBytesInitial: 0, maxCacheBytes: 1e9, compressedBytes: 1e9 });
  const share = () => store.stats.cache.speculativeShare;
  assert.equal(share(), 0.5, 'nothing measured yet');
  await store.getRaw(0, 0, 0, 0);
  await store.getRaw(0, 0, 0, 1);
  assert.equal(share(), 0.5, 'two chunks at 0.5 MB/s are half a second of transfer: still young');
  for (const t of [2, 3, 4]) await store.getRaw(0, 0, 0, t);
  assert.ok(store.bandwidthEstimate() > 0);
  assert.equal(share(), 1.0, 'over a second of transfer and nothing pending: established');

  let release;
  gate = { promise: new Promise((resolve) => (release = resolve)) };
  const pending = store.getRaw(0, 0, 0, 5);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(share(), 0.5, 'a demand read is waiting: back to the lower share');
  release();
  await pending;
  gate = null;
  assert.equal(share(), 1.0, 'and up again once it has been served');
});

// ---- demand is never starved; stale work is cancelled ----

test('speculative fetches leave the last request slots to demand', async () => {
  const readable = buildSyntheticStore({ ...SMALL, nTime: 40, delayMs: 30 });
  let inflight = 0;
  let peak = 0;
  const counting = {
    get: readable.get.bind(readable),
    async getRange(key, range, options) {
      inflight++;
      peak = Math.max(peak, inflight);
      try {
        return await readable.getRange(key, range, options);
      } finally {
        inflight--;
      }
    },
  };
  const store = await openStore('memory://reserve', { store: counting, workers: 0, maxRequests: 4, speculativeBytesInitial: 1e9 });
  const abort = new AbortController();
  const prefetching = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 8, signal: abort.signal });
  await sleep(100);
  assert.ok(peak <= 3, `background requests peaked at ${peak} of 4 slots`);
  abort.abort();
  await prefetching;
});

test('a demand read issued during prefetch starts at once', async () => {
  const readable = buildSyntheticStore({ ...SMALL, nTime: 40, delayMs: 40 });
  const store = await openStore('memory://demand-first', { store: readable, workers: 0, maxRequests: 4, speculativeBytesInitial: 1e9 });
  const abort = new AbortController();
  const prefetching = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 8, signal: abort.signal });
  await sleep(60);
  const before = readable.log.length;
  const demand = store.getRaw(0, 0, 0, 37);
  // One turn of the event loop (all microtasks, no timers): no slot or queue stands between a demand read and the store while only background requests are in flight.
  await new Promise((resolve) => setImmediate(resolve));
  const started = readable.log.slice(before).find((c) => c.range?.offset !== undefined || c.range?.suffixLength !== undefined);
  assert.ok(started, 'the demand request was sent at once although prefetch was running');
  await demand;
  abort.abort();
  await prefetching;
});

test('aborting a prefetch cancels its in-flight fetches that nobody else waits for', async () => {
  const readable = buildSyntheticStore({ ...SMALL, nTime: 40, delayMs: 60 });
  const store = await openStore('memory://stale', { store: readable, workers: 0, speculativeBytesInitial: 1e9 });
  await store.getRaw(0, 0, 0, 0);
  const abort = new AbortController();
  const done = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 3, signal: abort.signal });
  await sleep(20);
  const inFlight = chunkReads(readable).filter((c) => c.signal && !c.signal.aborted);
  assert.ok(inFlight.length >= 2, 'fetches are in flight');
  abort.abort();
  const result = await done;
  assert.ok(chunkReads(readable).slice(1).every((c) => c.signal.aborted), 'every speculative fetch of this job was cancelled');
  assert.equal(result.errors.length, 0, 'cancellation is not an error');
  assert.equal(store.cacheInfo().entries, 1, 'only the demand chunk is cached');
});

test('a speculative fetch that a demand read joined survives the abort of its prefetch', async () => {
  const readable = buildSyntheticStore({ ...SMALL, nTime: 40, delayMs: 50 });
  const store = await openStore('memory://joined', { store: readable, workers: 0, speculativeBytesInitial: 1e9 });
  await store.getRaw(0, 0, 0, 0);
  const abort = new AbortController();
  const done = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 1, signal: abort.signal });
  await sleep(20);
  assert.equal(store.peekRaw(0, 0, 0, 1), undefined, 'the prefetch is still fetching t=1');
  const joined = store.getRaw(0, 0, 0, 1);
  abort.abort();
  await done;
  const raw = await joined;
  assert.ok(raw instanceof Uint16Array, 'the demand caller still gets its chunk');
  assert.ok(store.peekRaw(0, 0, 0, 1));
  assert.equal(store.stats.network.deduped, 1);
});

test('seek: other speculative requests are cancelled and the target timestep is fetched first, outside the allowance', async () => {
  const readable = buildSyntheticStore({ ...SMALL, nTime: 40, delayMs: 40 });
  const store = await openStore('memory://seek', { store: readable, workers: 0, speculativeBytesInitial: 0 });
  const a = new AbortController();
  const aWork = store.prefetch({ lod: 0, cells: [[0, 0]], t: 0, concurrency: 4, signal: a.signal });
  // With no allowance nothing speculative starts; give it some so there is something in flight to cancel.
  store.setBudgets({ speculativeBytesInitial: 1e9 });
  await sleep(80);
  const inFlightBefore = chunkReads(readable).filter((c) => !c.signal.aborted);
  assert.ok(inFlightBefore.length >= 1, 'speculative fetches are in flight');
  const mark = readable.log.length;
  const b = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, seek: true, concurrency: 4 });
  assert.ok(inFlightBefore.every((c) => c.signal.aborted), 'the stale speculative requests were cancelled at once');
  await sleep(1);
  a.abort();
  await aWork;
  const bFirst = chunkReads(readable, mark).slice(0, 2).map((c) => c.range.offset);
  assert.equal(bFirst.length, 2);
  store.setBudgets({ speculativeBytesInitial: 1e9 });
  await b;
  assert.ok(store.peekRaw(0, 0, 0, 30), 'the target timestep is resident');
  assert.ok(store.peekRaw(0, 0, 0, store.anchorOf(30)), 'with its anchor');
});

test('seek targets are fetched even when the allowance is empty', async () => {
  const readable = buildSyntheticStore({ ...SMALL, nTime: 40 });
  const store = await openStore('memory://seek-free', { store: readable, workers: 0, speculativeBytesInitial: 0 });
  const abort = new AbortController();
  const done = store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, seek: true, concurrency: 2, signal: abort.signal });
  await sleep(40);
  abort.abort();
  const result = await done;
  assert.equal(result.fetched, 2, 'exactly the anchor and the delta of t=30');
  assert.ok(store.peekRaw(0, 0, 0, 30) && store.peekRaw(0, 0, 0, store.anchorOf(30)));
  assert.equal(store.stats.cache.speculativeBytes, 0, 'nothing was charged to the allowance');
});

// ---- deduplication ----

test('concurrent requests for one chunk share a fetch and are counted as deduplicated', async () => {
  const readable = buildSyntheticStore({ ...SMALL, delayMs: 10 });
  const store = await openStore('memory://dedupe', { store: readable, workers: 0 });
  const [a, b, c] = await Promise.all([store.getRaw(0, 0, 0, 1), store.getRaw(0, 0, 0, 1), store.getRaw(0, 0, 0, 1)]);
  assert.ok(a === b && b === c);
  assert.equal(store.stats.cache.joins, 2);
  assert.equal(store.stats().network.deduped, 2, 'two requests were saved');
  store.resetStats();
  assert.equal(store.stats.network.deduped, 0);
});

test('two chunks of one cold shard share one index read, and the second is counted as deduplicated', async () => {
  const readable = buildSyntheticStore({ ...SMALL, delayMs: 10 });
  const store = await openStore('memory://index-dedupe', { store: readable, workers: 0 });
  await Promise.all([store.getRaw(0, 0, 0, 1), store.getRaw(0, 0, 0, 2)]);
  const indexReads = readable.log.filter((c) => c.range && 'suffixLength' in c.range);
  assert.equal(indexReads.length, 1);
  assert.equal(store.stats.network.deduped, 1);
});

// ---- coarse frame ----

const PYRAMID = { nTime: 8, nBand: 2, height: 96, width: 96, chunk: 32, anchorInterval: 4, sharded: true, nLevels: 3 };

test('getCoarseFrame loads the anchor and delta of every cell at demand priority and leaves them in the cache', async () => {
  const readable = buildSyntheticStore(PYRAMID);
  const store = await openStore('memory://coarse', { store: readable, workers: 0 });
  const events = [];
  store.probe = (event) => events.push(event);
  assert.deepEqual([store.levels[2].gridRows, store.levels[2].gridCols], [1, 1]);
  const frame = await store.getCoarseFrame(1, [[0, 0], [0, 1], [1, 0], [1, 1]], 6);
  assert.equal(frame.lod, 1);
  assert.equal(frame.t, 6);
  assert.equal(frame.anchorT, 4);
  assert.equal(frame.cells.length, 4);
  assert.equal(events.length, 8, 'an anchor and a delta per cell');
  assert.ok(events.every((e) => e.background === false), 'all at demand priority');
  const values = defaultValues('uint16');
  for (const { row, col, anchor, delta } of frame.cells) {
    assert.equal(anchor, store.peekRaw(1, row, col, 4));
    assert.equal(delta, store.peekRaw(1, row, col, 6));
    assert.equal((anchor[3] + delta[3]) & 0xffff, values(6, 0, row * 32, col * 32 + 3, 1), `cell ${row},${col} pixel`);
  }
});

test('getCoarseFrame at an anchor timestep returns no delta; under encoding "none" there never is one', async () => {
  const store = await openStore('memory://coarse-anchor', { store: buildSyntheticStore(PYRAMID), workers: 0 });
  const frame = await store.getCoarseFrame(2, [[0, 0]], 4);
  assert.equal(frame.cells[0].delta, null);
  const none = await openStore('memory://coarse-none', { store: buildSyntheticStore({ ...PYRAMID, encoding: 'none' }), workers: 0 });
  const plain = await none.getCoarseFrame(2, [[0, 0]], 5);
  assert.equal(plain.cells[0].delta, null);
  assert.equal(plain.anchorT, 5);
});

test('getCoarseFrame holds its chunks against eviction until it resolves, even with a tiny cache', async () => {
  const store = await openStore('memory://coarse-pinned', { store: buildSyntheticStore(PYRAMID), workers: 0, maxCacheBytes: 2 * 2 * 32 * 32 * 2, compressedBytes: 0 });
  const cells = [[0, 0], [0, 1], [1, 0]];
  const frame = await store.getCoarseFrame(1, cells, 6);
  for (const [row, col] of cells) {
    assert.ok(store.peekRaw(1, row, col, 4) && store.peekRaw(1, row, col, 6), `cell ${row},${col} resident although the budget holds 2 chunks`);
  }
  assert.equal(frame.cells.length, 3);
});

test('getCoarseFrame rejects when its signal aborts, and on bad cells', async () => {
  const readable = buildSyntheticStore({ ...PYRAMID, delayMs: 30 });
  const store = await openStore('memory://coarse-abort', { store: readable, workers: 0 });
  const abort = new AbortController();
  const pending = store.getCoarseFrame(0, [[0, 0], [1, 1]], 6, { signal: abort.signal });
  await sleep(5);
  abort.abort();
  await assert.rejects(pending, { name: 'AbortError' });
  await assert.rejects(store.getCoarseFrame(0, [[7, 7]], 0), /cell \(7, 7\) outside 3x3 grid at lod 0/);
  await assert.rejects(store.getCoarseFrame(9, [[0, 0]], 0), /lod 9 out of range/);
});

test('coarse-first open: the deepest level costs a handful of requests and arrives first', async () => {
  const readable = buildSyntheticStore({ ...PYRAMID, consolidated: true, shardBytes: true, specVersion: '0.2.0' });
  const store = await openStore('memory://coarse-first', { store: readable, workers: 0 });
  const before = readable.log.length;
  const frame = await store.getCoarseFrame(2, [[0, 0]], 5);
  assert.equal(frame.cells.length, 1);
  assert.equal(readable.log.length - before, 2 + 1, 'index + anchor + delta of one cell... shard index read once, chunks twice');
});

test('the two tiers add up: decoded chunks plus chunks held only compressed', async () => {
  // Budgets for 4 decoded chunks and 14 compressed ones (estimated at 0.7 of decoded, so room for 20): a window of floor(0.9 x 24) = 21 chunks.
  const readable = buildSyntheticStore({ ...SMALL, nTime: 60 });
  const store = await openStore('memory://additive', { store: readable, workers: 0, maxCacheBytes: SMALL_CHUNK * 4, compressedBytes: SMALL_CHUNK * 14, speculativeBytesInitial: 1e9 });
  store.evictionScore = (entry) => Math.abs(entry.t - 30);
  const result = await store.prefetch({ lod: 0, cells: [[0, 0]], t: 30, concurrency: 1 });
  assert.equal(result.planned, 21);
  assert.equal(result.fetched, 18, 'until both tiers were full');
  assert.equal(result.budgetReached, true);
  assert.equal(result.compressedOnly, 14);
  const info = store.cacheInfo();
  assert.equal(info.entries, 4, 'the four nearest chunks are decoded');
  assert.equal(info.compressedEntries, 14, 'the copies of those four gave way to 14 chunks that exist nowhere else');
  const decodedTimes = [];
  for (let t = 0; t < 60; t++) if (store.peekRaw(0, 0, 0, t)) decodedTimes.push(t);
  assert.deepEqual(decodedTimes, [28, 29, 30, 31]);
  assert.equal(store.stats.cache.compressedEvictions, 4);
  const before = chunkReads(readable).length;
  for (const t of [28, 29, 30, 31]) await store.getRaw(0, 0, 0, t);
  assert.equal(chunkReads(readable).length, before, 'no refetch for the decoded ones');
});
