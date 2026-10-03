import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FrameMonitor, MB, analyzeLatency, distribution, emptyStats, formatBytes, formatMs, formatPhaseTable, formatRate, frameCompleteness, hitRate, isWholeFrame, readStats, statsDelta } from '../demo/perf.js';

// The reader's shape: stats() with network and cache counters, cacheInfo() for both cache tiers, bandwidthEstimate().
const reader = () => ({
  stats: () => ({ network: { requests: 40, bytes: 50 * MB, deduped: 7, inflight: 3 }, cache: { hits: 100, misses: 12, joins: 7, evictions: 2, speculativeBytes: 20 * MB } }),
  cacheInfo: () => ({ bytes: 300 * MB, compressedBytes: 120 * MB, entries: 9 }),
  bandwidthEstimate: () => 12 * MB,
});

test('readStats flattens stats(), the cache sizes and the bandwidth estimate', () => {
  assert.deepEqual(readStats(reader()), {
    cacheHits: 100,
    cacheMisses: 12,
    evictions: 2,
    dedupedRequests: 7,
    requests: 40,
    inflight: 3,
    transferredBytes: 50 * MB,
    decodedBytes: 300 * MB,
    compressedBytes: 120 * MB,
    speculativeBytes: 20 * MB,
    bandwidth: 12 * MB,
  });
});

test('readStats leaves a counter the reader does not report null', () => {
  const store = { stats: () => ({ network: { requests: 5, bytes: 1000 }, cache: { hits: 1, misses: 2 } }), cacheInfo: () => ({ bytes: 4096 }), bandwidthEstimate: () => null };
  const stats = readStats(store);
  assert.deepEqual([stats.cacheHits, stats.cacheMisses, stats.requests, stats.transferredBytes, stats.decodedBytes], [1, 2, 5, 1000, 4096]);
  for (const key of ['evictions', 'dedupedRequests', 'inflight', 'compressedBytes', 'speculativeBytes', 'bandwidth']) assert.equal(stats[key], null, key);
});

test('statsDelta subtracts the cumulative counters and keeps the levels; emptyStats is a fresh store', () => {
  const before = { ...readStats(reader()) };
  const after = { ...before, cacheHits: 130, requests: 50, transferredBytes: 80 * MB, decodedBytes: 400 * MB, inflight: 1 };
  const delta = statsDelta(before, after);
  assert.equal(delta.cacheHits, 30);
  assert.equal(delta.requests, 10);
  assert.equal(delta.transferredBytes, 30 * MB);
  assert.equal(delta.decodedBytes, 400 * MB, 'a level is not a difference');
  assert.equal(delta.inflight, 1);
  assert.equal(statsDelta({ ...before, requests: null }, after).requests, null, 'unknown on one side: unknown');
  const fresh = emptyStats();
  assert.deepEqual([fresh.requests, fresh.cacheMisses, fresh.transferredBytes, fresh.bandwidth], [0, 0, 0, null]);
  assert.equal(statsDelta(fresh, after).requests, 50);
});

test('formatting: bytes, rates, times and hit rate', () => {
  assert.equal(formatBytes(null), '–');
  assert.equal(formatBytes(512), '1 kB');
  assert.equal(formatBytes(1.5 * MB), '1.5 MB');
  assert.equal(formatBytes(250 * MB), '250 MB');
  assert.equal(formatBytes(3 * 1024 * MB), '3.00 GB');
  assert.equal(formatRate(12.5 * MB), '12.5 MB/s (105 Mbit/s)');
  assert.equal(formatRate(null), '–');
  assert.equal(formatMs(null), '–');
  assert.equal(formatMs(141.6), '142 ms');
  assert.equal(formatMs(2345), '2.35 s');
  assert.equal(hitRate(95, 5), '95 %');
  assert.equal(hitRate(0, 0), '–');
  assert.equal(hitRate(null, 3), '–');
});

test('FrameMonitor counts frames over 17 and 33 ms between start and stop', () => {
  let callback = null;
  let cancelled = 0;
  const monitor = new FrameMonitor({ requestFrame: (fn) => { callback = fn; return 1; }, cancelFrame: () => { cancelled++; } });
  monitor.start();
  monitor.start();
  for (const time of [0, 16.7, 33.4, 70, 86.7, 103.4, 103.4 + 40]) callback(time);
  assert.deepEqual(monitor.snapshot(), { frames: 6, over16_7ms: 2, over33ms: 2, maxGapMs: 40 }, 'the gaps are 16.7, 16.7, 36.6, 16.7, 16.7, 40');
  monitor.reset();
  assert.equal(monitor.frames, 0);
  monitor.stop();
  assert.equal(monitor.running, false);
  assert.equal(cancelled, 1);
  const count = monitor.frames;
  callback?.(1e6);
  assert.equal(monitor.frames, count, 'a frame arriving after stop is ignored');
});

test('analyzeLatency: first covering paint and first complete paint after each input', () => {
  const paint = (at, t, complete, covered) => ({ type: 'paint', at, t, complete, covered });
  const events = [
    paint(5, 1, true, true),
    { ...paint(130, 2, false, true), ready: 0 },
    paint(400, 2, true, true),
    { ...paint(250, 3, false, false), ready: 1 },
    { type: 'cell-ready', at: 260 },
    paint(900, 3, true, true),
  ];
  const inputs = [{ at: 100, t: 2 }, { at: 200, t: 3 }];
  const reached = (p, input) => p.t >= input.t;
  const latency = analyzeLatency(events, inputs, reached);
  assert.deepEqual(latency.coarse, distribution([30, 700]), 'step 3 was covered only by its complete frame: the partial one at 250 ms was not covered');
  assert.deepEqual(latency.full, distribution([300, 700]));
  assert.equal(latency.neverCompleted, 0);
  const pending = analyzeLatency(events.slice(0, 4), [{ at: 200, t: 3 }], reached);
  assert.equal(pending.neverCompleted, 1);
  assert.equal(pending.full.n, 0);
});

test('analyzeLatency: a viewer that does not report `covered` has no coarse time', () => {
  const events = [{ type: 'paint', at: 50, t: 1, complete: true }];
  const latency = analyzeLatency(events, [{ at: 10, t: 1 }], () => true);
  assert.equal(latency.coarse.n, 0);
  assert.equal(latency.full.median, 40);
});

test('distribution: median, p95 and max of a list; empty gives nulls', () => {
  assert.deepEqual(distribution([]), { n: 0, median: null, p95: null, max: null });
  assert.deepEqual(distribution([3, 1, 2]), { n: 3, median: 2, p95: 3, max: 3 });
  assert.equal(distribution(Array.from({ length: 100 }, (_, i) => i + 1)).p95, 96);
});

test('formatPhaseTable: one markdown row per phase, unknown counters as dashes', () => {
  const stats = { ...emptyStats(), requests: 12, transferredBytes: 5 * MB, cacheHits: 3, cacheMisses: 1, decodedBytes: 40 * MB };
  const table = formatPhaseTable([
    { name: 'open', coarse: distribution([120]), full: distribution([640]), frameCompleteness: { painted: 9, partial: 0, levelFallbacks: 2, keptFrames: 1 }, stats, frames: { frames: 30, over16_7ms: 2, over33ms: 1 }, peakInflight: 4 },
    { name: 'scrub', coarse: null, full: null, stats: { ...stats, bandwidth: 8 * MB }, frames: { frames: 10, over16_7ms: 0, over33ms: 0 }, peakInflight: null },
  ]).split('\n');
  assert.equal(table.length, 4);
  assert.match(table[0], /^\| phase \| coarse med \/ max \| full med \/ max \|/);
  assert.match(table[0], /\| partial \/ fallback \/ kept frames \|/);
  assert.match(table[2], /^\| open \| 120 ms \/ 120 ms \| 640 ms \/ 640 ms \| 0 \/ 2 \/ 1 of 9 \| 12 \| 5\.0 MB \| 3 \/ 1 \| 0 \| 4 \| 2 \/ 1 of 30 \| 40\.0 MB \/ – \| – \|$/);
  assert.match(table[3], /^\| scrub \| – \| – \| – \| 12 \| 5\.0 MB \| 3 \/ 1 \| 0 \| – \| 0 \/ 0 of 10 \| 40\.0 MB \/ – \| 8\.0 MB\/s \(67 Mbit\/s\) \|$/);
});

test('isWholeFrame: atomic viewers say so; for older ones a frame is whole when complete or only a coarser level was drawn', () => {
  assert.equal(isWholeFrame({ partial: false, complete: false }), true, 'a fallback frame: whole, though not at the target level');
  assert.equal(isWholeFrame({ partial: true, complete: true }), false, 'the flag wins');
  assert.equal(isWholeFrame({ complete: true, ready: 4, cells: 4 }), true);
  assert.equal(isWholeFrame({ complete: false, covered: true, ready: 0, cells: 4 }), true, 'a coarse level drawn under no target cell');
  assert.equal(isWholeFrame({ complete: false, covered: true, ready: 2, cells: 4 }), false, 'target cells drawn over a coarse level or the previous timestep');
  assert.equal(isWholeFrame({ complete: false, covered: false, ready: 1, cells: 4 }), false);
});

test('frameCompleteness counts partial frames, level fallbacks and kept frames; a viewer that does not report them gives null', () => {
  const atomic = [
    { type: 'paint', partial: false, complete: true, fallback: false },
    { type: 'paint', partial: false, complete: false, fallback: true },
    { type: 'kept', t: 4 },
    { type: 'paint', partial: false, complete: true, fallback: false },
    { type: 'cell-ready' },
  ];
  assert.deepEqual(frameCompleteness(atomic), { painted: 3, partial: 0, partialFraction: 0, levelFallbacks: 1, keptFrames: 1 });
  const old = [
    { type: 'paint', complete: false, covered: true, ready: 0, cells: 9 },
    { type: 'paint', complete: false, covered: true, ready: 3, cells: 9 },
    { type: 'paint', complete: false, covered: true, ready: 8, cells: 9 },
    { type: 'paint', complete: true, covered: true, ready: 9, cells: 9 },
  ];
  assert.deepEqual(frameCompleteness(old), { painted: 4, partial: 2, partialFraction: 0.5, levelFallbacks: null, keptFrames: null });
  assert.deepEqual(frameCompleteness([]), { painted: 0, partial: 0, partialFraction: 0, levelFallbacks: null, keptFrames: null });
});
