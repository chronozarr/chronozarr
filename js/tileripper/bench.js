// Benchmarks for the chronozarr decoder and the TileRipper viewer, run against the store that is
// currently open. From the page console: `await tileripper.bench()`.
//
//   cold open       loadStore -> first complete frame at LOD 0, HTTP cache bypassed, with and without
//                   consolidated metadata in the root zarr.json.
//   warm switch     goToTime -> frame finished on the GPU, with every chunk already decoded in the cache:
//                   stepping one timestep at a time with pauses (the GPU window keeps neighbours resident,
//                   so a step is a uniform change), jumping several timesteps at once, and with the
//                   texture pool emptied so every switch uploads from the decoded cache.
//   product switch  setProduct -> frame finished.
//   decode          per-chunk zarrita decode time, replayed from recorded bytes (no network).
//   cpu add         the JS star-delta loop the GPU path replaces, for reference.
//
// `await tileripper.scrubBench()` measures what a user feels while stepping and dragging the time
// slider; see runScrubBenchmarks. `await tileripper.playBench({ stepsPerSecond: 4 })` plays one movie
// loop and reports the achieved rate and the holds; see playBench.

import * as zarr from 'zarrita';
import { applyDelta, openStore } from '../chronozarr/decoder.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function summarize(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
  return { n: sorted.length, median: at(0.5), p95: at(0.95), max: sorted[sorted.length - 1] };
}

const round = (x) => Math.round(x * 100) / 100;
const noStoreFetch = (request) => fetch(new Request(request, { cache: 'no-store' }));

function sleepUnlessAborted(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException('Aborted', 'AbortError'));
    const timer = setTimeout(resolve, Math.max(0, ms));
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(new DOMException('Aborted', 'AbortError'));
    }, { once: true });
  });
}

/**
 * A fetch that behaves like a remote bucket behind HTTP/2: every request pays `rttMs` before its first byte,
 * request concurrency is unlimited, and response bodies share one link of `mbps` megabits per second
 * (first come, first served). A request aborted mid-transfer still occupies the link, as the bytes are in flight.
 */
export function simulatedRemoteFetch({ rttMs, mbps }) {
  const bytesPerMs = (mbps * 1e6) / 8 / 1000;
  let linkFreeAt = 0;
  return async (request) => {
    await sleepUnlessAborted(rttMs, request.signal);
    const response = await noStoreFetch(request);
    if (request.method === 'HEAD') return response;
    const body = await response.arrayBuffer();
    linkFreeAt = Math.max(performance.now(), linkFreeAt) + body.byteLength / bytesPerMs;
    await sleepUnlessAborted(linkFreeAt - performance.now(), request.signal);
    return new Response(body, { status: response.status, headers: response.headers });
  };
}

function withoutConsolidatedMetadata(rootUrl) {
  return async (request) => {
    const response = await noStoreFetch(request);
    if (request.url !== `${rootUrl}/zarr.json`) return response;
    const root = await response.json();
    delete root.consolidated_metadata;
    return new Response(JSON.stringify(root), { status: response.status, headers: { 'Content-Type': 'application/json' } });
  };
}

async function coldOpen(viewer, url, fetchImpl, runs) {
  const rows = [];
  for (let run = 0; run < runs; run++) {
    await sleep(300);
    performance.clearResourceTimings();
    const { openMs, firstPaintMs } = await viewer.loadStore(url, { lod: 0, fetch: fetchImpl });
    const { requests, bytes } = viewer.store.stats.network;
    rows.push({
      run,
      openMs: round(openMs),
      firstPaintMs: round(firstPaintMs),
      httpRequests: requests,
      wireBytes: bytes,
      resourceEntries: performance.getEntriesByType('resource').length,
    });
  }
  return rows;
}

async function switchTimings(viewer, sequence, { emptyPool = false, pauseMs = 0 } = {}) {
  const { renderer, store } = viewer;
  const rows = [];
  for (const t of sequence) {
    if (pauseMs) await sleep(pauseMs);
    if (emptyPool) renderer.clearResident();
    const before = { requests: store.stats.network.requests, bytes: store.stats.network.bytes, uploads: renderer.stats.uploads, misses: store.stats.cache.misses };
    const started = performance.now();
    viewer.goToTime(t);
    const frame = viewer.renderNow();
    renderer.finish();
    const ms = performance.now() - started;
    rows.push({
      t,
      ms,
      complete: frame.complete,
      requests: store.stats.network.requests - before.requests,
      bytes: store.stats.network.bytes - before.bytes,
      uploads: renderer.stats.uploads - before.uploads,
      misses: store.stats.cache.misses - before.misses,
    });
  }
  return rows;
}

function describeSwitches(rows) {
  return {
    ...Object.fromEntries(Object.entries(summarize(rows.map((r) => r.ms))).map(([k, v]) => [k, round(v)])),
    incomplete: rows.filter((r) => !r.complete).length,
    networkRequests: rows.reduce((n, r) => n + r.requests, 0),
    wireBytes: rows.reduce((n, r) => n + r.bytes, 0),
    uploadsPerSwitch: rows.reduce((n, r) => n + r.uploads, 0) / rows.length,
    cacheMisses: rows.reduce((n, r) => n + r.misses, 0),
  };
}

/** 0,1,...,T-1,T-2,...,1,0,... (one step at a time) for `count` steps starting after `current`. */
function pingPongSequence(nTime, current, count) {
  const sequence = [];
  let t = current;
  let direction = current === nTime - 1 ? -1 : 1;
  for (let i = 0; i < count; i++) {
    if (t + direction < 0 || t + direction >= nTime) direction = -direction;
    t += direction;
    sequence.push(t);
  }
  return sequence;
}

function jumpSequence(nTime, current, count) {
  const sequence = [];
  let t = current;
  for (let i = 0; i < count; i++) {
    t = (t + 1 + (i % 3)) % nTime;
    if (t === (sequence.at(-1) ?? current)) t = (t + 1) % nTime;
    sequence.push(t);
  }
  return sequence;
}

function recordingStore(inner, records) {
  return {
    async get(key, options) {
      const bytes = await inner.get(key, options);
      records.set(key, bytes);
      return bytes;
    },
    async getRange(key, range, options) {
      const bytes = await inner.getRange(key, range, options);
      records.set(`${key}|${JSON.stringify(range)}`, bytes);
      return bytes;
    },
  };
}

/** Like a real store, hands out fresh bytes on every read (the decode pool takes ownership of what it is given). */
function replayStore(records) {
  return {
    get: async (key) => records.get(key)?.slice(),
    getRange: async (key, range) => records.get(`${key}|${JSON.stringify(range)}`)?.slice(),
  };
}

/** Per-chunk decode (zstd/gzip + bytes codec) replayed from recorded bytes, so the network is out of the loop. */
async function decodeTimings(url, repeats) {
  const records = new Map();
  const recorder = await openStore(url, { store: recordingStore(new zarr.FetchStore(url, { fetch: noStoreFetch }), records), workers: 0 });
  const timesteps = Math.min(recorder.times.length, 8);
  for (let t = 0; t < timesteps; t++) await recorder.getRaw(0, 0, 0, t);
  const replayer = await openStore(url, { store: replayStore(records), workers: 0 });
  await replayer.getRaw(0, 0, 0, 0);

  const anchors = [];
  const deltas = [];
  for (let t = 0; t < timesteps; t++) {
    for (let i = 0; i < repeats; i++) {
      replayer.clearCache();
      const started = performance.now();
      await replayer.getRaw(0, 0, 0, t);
      (replayer.isAnchor(t) ? anchors : deltas).push(performance.now() - started);
    }
  }
  const level = replayer.levels[0];

  // The same chunks decoded in parallel by the worker pool versus one after another on this thread.
  const chunkTimes = Array.from({ length: timesteps }, (_, t) => t);
  replayer.clearCache();
  let started = performance.now();
  for (const t of chunkTimes) await replayer.getRaw(0, 0, 0, t);
  const mainThreadMs = performance.now() - started;
  const pooled = await openStore(url, { store: replayStore(records) });
  await pooled.getRaw(0, 0, 0, 0);
  await sleep(200);
  pooled.clearCache();
  started = performance.now();
  await Promise.all(chunkTimes.map((t) => pooled.getRaw(0, 0, 0, t)));
  const poolMs = performance.now() - started;
  pooled.close();

  const format = (values) => (values.length ? Object.fromEntries(Object.entries(summarize(values)).map(([k, v]) => [k, round(v)])) : null);
  return { chunkRawBytes: level.chunkBytes, anchor: format(anchors), delta: format(deltas), parallel: { chunks: timesteps, mainThreadMs: round(mainThreadMs), workerPoolMs: round(poolMs) } };
}

async function cpuAddTimings(store, repeats) {
  const nonAnchor = store.times.findIndex((_, t) => !store.isAnchor(t));
  if (nonAnchor < 0) return null;
  const anchor = await store.getRaw(0, 0, 0, store.anchorOf(nonAnchor));
  const delta = await store.getRaw(0, 0, 0, nonAnchor);
  const times = [];
  for (let i = 0; i < repeats; i++) {
    const started = performance.now();
    applyDelta(anchor, delta);
    times.push(performance.now() - started);
  }
  const perChunk = summarize(times).median;
  return { msPerChunk: round(perChunk), cells: cellCount(store), msPerFrame: round(perChunk * cellCount(store)) };
}

const cellCount = (store) => store.levels[0].gridRows * store.levels[0].gridCols;

export async function runBenchmarks(viewer, { coldRuns = 5, switches = 30 } = {}) {
  const url = viewer.store.url.replace(/\/$/, '');
  const results = { store: url, userAgent: navigator.userAgent };

  results.coldOpenConsolidated = await coldOpen(viewer, url, noStoreFetch, coldRuns);
  results.coldOpenPerArrayMetadata = await coldOpen(viewer, url, withoutConsolidatedMetadata(url), coldRuns);
  await coldOpen(viewer, url, noStoreFetch, 1);

  const prefetch = await viewer.prefetchNow();
  results.prefetch = { fetched: prefetch.fetched, skipped: prefetch.skipped, budgetReached: prefetch.budgetReached, errors: prefetch.errors.length };
  results.cache = { ...viewer.store.cacheInfo(), gpuSlots: viewer.renderer.slots };

  const nTime = viewer.store.times.length;
  viewer.goToTime(0);
  viewer.renderNow();
  await sleep(300);
  results.warmSwitchStepping = describeSwitches(await switchTimings(viewer, pingPongSequence(nTime, viewer.t, switches), { pauseMs: 120 }));
  results.warmSwitchJumping = describeSwitches(await switchTimings(viewer, jumpSequence(nTime, viewer.t, switches)));
  results.warmSwitchUploadFromCache = describeSwitches(await switchTimings(viewer, jumpSequence(nTime, viewer.t, switches), { emptyPool: true }));

  const productRows = [];
  for (let i = 0; i < viewer.products.length; i++) {
    if (!viewer.products[i].available) continue;
    for (let rep = 0; rep < 5; rep++) {
      const started = performance.now();
      viewer.setProduct(i);
      viewer.renderNow();
      viewer.renderer.finish();
      productRows.push({ product: viewer.products[i].id, ms: performance.now() - started });
    }
  }
  results.productSwitch = Object.fromEntries(
    [...new Set(productRows.map((r) => r.product))].map((id) => [id, round(summarize(productRows.filter((r) => r.product === id).map((r) => r.ms)).median)]),
  );

  results.decodePerChunk = await decodeTimings(url, 10);
  results.cpuStarDeltaAdd = await cpuAddTimings(viewer.store, 20);

  console.log(JSON.stringify(results, null, 2));
  window.__benchResults = results;
  return results;
}

// ---- perceived scrub latency ----

const TIMEOUT_MS = 6000;
const OPEN_TIMEOUT_MS = 60000;

/** loadStore with a deadline, so a run that can never paint fails instead of hanging the benchmark. */
function openWithin(viewer, url, options) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`no complete first frame within ${OPEN_TIMEOUT_MS / 1000} s`)), OPEN_TIMEOUT_MS);
  });
  return Promise.race([viewer.loadStore(url, options), deadline]).finally(() => clearTimeout(timer));
}

function percentile(sorted, q) {
  return sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] : null;
}

function distribution(values) {
  const sorted = [...values].sort((a, b) => a - b);
  return { n: sorted.length, median: round(percentile(sorted, 0.5)), p95: round(percentile(sorted, 0.95)), max: round(sorted.at(-1) ?? null) };
}

function timelinePoint(viewer, t) {
  const rect = document.getElementById('timeline-track').getBoundingClientRect();
  const frac = viewer.store.times.length > 1 ? t / (viewer.store.times.length - 1) : 0;
  return { x: rect.left + 8 + frac * (rect.width - 16), y: rect.top + rect.height / 2 };
}

function pointer(target, type, point) {
  target.dispatchEvent(new PointerEvent(type, { clientX: point.x, clientY: point.y, bubbles: true, pointerId: 1 }));
}

async function waitUntil(predicate, timeoutMs) {
  const started = performance.now();
  while (!predicate()) {
    if (performance.now() - started > timeoutMs) return false;
    await sleep(5);
  }
  return true;
}

/** Records long tasks and the gaps between animation frames until stop() is called. */
function observeMainThread() {
  const longTasks = [];
  const frameGaps = [];
  const observer = new PerformanceObserver((list) => longTasks.push(...list.getEntries().map((e) => e.duration)));
  observer.observe({ type: 'longtask' });
  let running = true;
  let last = performance.now();
  const onFrame = (now) => {
    frameGaps.push(now - last);
    last = now;
    if (running) requestAnimationFrame(onFrame);
  };
  requestAnimationFrame(onFrame);
  return {
    stop() {
      running = false;
      observer.disconnect();
      return { longTasks, frameGaps };
    },
  };
}

/** Camera that shows about nine LOD 0 cells (3x3) centred on a cell near the middle of the mosaic. */
function nineCellCamera(viewer) {
  const level = viewer.store.levels[0];
  const row = Math.min(2, level.gridRows - 1);
  const col = Math.min(2, level.gridCols - 1);
  return {
    cx: (col + 0.5) * level.chunkWidth,
    cy: (row + 0.5) * level.chunkHeight,
    scale: viewer.canvas.width / (2.9 * level.chunkWidth),
  };
}

/** Runs the input script and returns the raw inputs and viewer events. */
async function driveScrub(viewer, { mode, steps, cadenceMs, startT }) {
  const events = [];
  const inputs = [];
  const monitor = observeMainThread();
  viewer.probe = (event) => events.push(event);
  const network = viewer.store.stats.network;
  const before = { requests: network.requests, bytes: network.bytes, misses: viewer.store.stats.cache.misses };
  const track = document.getElementById('timeline-track');
  const begin = performance.now();
  for (let i = 0; i < steps; i++) {
    const due = begin + i * cadenceMs;
    const wait = due - performance.now();
    if (wait > 0) await sleep(wait);
    const t = startT + 1 + i;
    inputs.push({ at: performance.now(), t });
    if (mode === 'keys') {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }));
    } else if (i === 0) {
      pointer(track, 'pointerdown', timelinePoint(viewer, t));
    } else {
      pointer(window, 'pointermove', timelinePoint(viewer, t));
    }
  }
  if (mode === 'drag') pointer(window, 'pointerup', timelinePoint(viewer, startT + steps));
  const finalT = startT + steps;
  const settled = await waitUntil(() => events.some((e) => e.type === 'paint' && e.complete && e.t === finalT), TIMEOUT_MS);
  await sleep(50);
  viewer.probe = null;
  const { longTasks, frameGaps } = monitor.stop();
  return {
    events, inputs, longTasks, frameGaps, settled,
    network: { requests: network.requests - before.requests, bytes: network.bytes - before.bytes, misses: viewer.store.stats.cache.misses - before.misses },
  };
}

/**
 * Per-step lag and phase breakdown from the raw events. The lag of a step is the time from its input
 * event to the first paint that shows that timestep or a later one (a step the viewer skipped over is
 * satisfied by the later frame that replaced it), so a viewer that falls behind is not flattered by
 * only counting the frames it managed to show. The phase breakdown covers steps shown exactly.
 */
function analyzeScrub(run) {
  const { events, inputs } = run;
  const paints = events.filter((e) => e.type === 'paint');
  const steps = inputs.map((input) => {
    const later = (p) => p.at >= input.at && p.t >= input.t;
    const caughtUp = paints.find((p) => later(p) && p.complete);
    const firstCell = paints.find((p) => later(p) && p.ready > 0);
    const exact = paints.find((p) => p.at >= input.at && p.t === input.t && p.complete);
    const row = { t: input.t, shownExactly: Boolean(exact), lagMs: caughtUp ? caughtUp.at - input.at : null, firstCellMs: firstCell ? firstCell.at - input.at : null };
    if (exact) {
      const loadStart = events.find((e) => e.type === 'load-start' && e.t === input.t && e.at >= input.at);
      const lastReady = events.filter((e) => e.type === 'cell-ready' && e.t === input.t && e.at >= input.at).at(-1);
      row.queueMs = loadStart ? loadStart.at - input.at : 0;
      row.loadMs = loadStart && lastReady ? lastReady.at - loadStart.at : 0;
      row.uploadMs = exact.uploadMs;
      row.renderMs = exact.renderMs;
      row.otherMs = exact.at - input.at - row.queueMs - row.loadMs - row.uploadMs - row.renderMs;
    }
    return row;
  });
  const exact = steps.filter((s) => s.shownExactly);
  const mean = (key) => (exact.length ? round(exact.reduce((n, s) => n + s[key], 0) / exact.length) : null);
  const lastPaint = paints.at(-1);
  const finalPaint = paints.findLast((p) => p.complete && p.t === inputs.at(-1).t);
  const gaps = [...run.frameGaps].sort((a, b) => a - b);
  return {
    lod: lastPaint?.lod,
    visibleCells: lastPaint?.cells,
    stepsShownExactly: exact.length,
    stepsNeverCaughtUp: steps.filter((s) => s.lagMs === null).length,
    settled: run.settled,
    settleMs: finalPaint ? round(finalPaint.at - inputs.at(-1).at) : null,
    lagMs: distribution(steps.filter((s) => s.lagMs !== null).map((s) => s.lagMs)),
    firstCellMs: distribution(steps.filter((s) => s.firstCellMs !== null).map((s) => s.firstCellMs)),
    meanPhaseMsOfShownSteps: { queue: mean('queueMs'), load: mean('loadMs'), upload: mean('uploadMs'), render: mean('renderMs'), other: mean('otherMs') },
    mainThread: {
      longTasks: run.longTasks.length,
      longTaskMaxMs: round(Math.max(0, ...run.longTasks)),
      framesOver33ms: run.frameGaps.filter((g) => g > 33).length,
      maxFrameGapMs: round(gaps.at(-1)),
    },
    network: run.network,
  };
}

/**
 * Perceived latency while stepping (ArrowRight every 100 ms) and dragging the timeline slider (one
 * timestep every 40 ms): input event -> first paint that shows the new timestep (first cell / all
 * visible cells), with a breakdown and main-thread stall counts. Each run opens the store from
 * scratch, either scrubs immediately after the first frame ("cold") or after 5 s of idle prefetch,
 * at the overview zoom fit() picks and zoomed to about nine LOD 0 cells.
 */
export async function runScrubBenchmarks(viewer, { steps = 20, startT = 40, idleMs = 5000, network = null, only = null } = {}) {
  const fetchImpl = network ? simulatedRemoteFetch(network) : noStoreFetch;
  const url = viewer.store.url.replace(/\/$/, '');
  await openWithin(viewer, url, { fetch: fetchImpl });
  const zoomed = nineCellCamera(viewer);
  const results = { store: url, network: network ?? 'as configured by the page', steps, startT, runs: {} };
  const zooms = { overview: undefined, zoomed9cells: zoomed };

  for (const [zoomName, camera] of Object.entries(zooms)) {
    for (const state of ['cold', 'idle']) {
      for (const [mode, cadenceMs] of [['keys', 100], ['drag', 40]]) {
        const name = `${zoomName}/${state}/${mode}`;
        if (only && !only.includes(name)) continue;
        await openWithin(viewer, url, { fetch: fetchImpl, camera });
        viewer.goToTime(startT);
        await waitUntil(() => viewer.paintedT === startT, TIMEOUT_MS);
        if (state === 'idle') await sleep(idleMs);
        const run = await driveScrub(viewer, { mode, steps, cadenceMs, startT });
        results.runs[name] = analyzeScrub(run);
      }
    }
  }
  console.log(JSON.stringify(results, null, 2));
  window.__scrubResults = results;
  return results;
}

// ---- movie playback ----

/**
 * Plays one full loop (every timestep once, from 0 back to 0) at `stepsPerSecond` on the open store and
 * reports what playback actually delivered: the achieved rate between the first and last step, how many
 * steps had to hold because their data was not ready and for how long, how late frames appeared after each
 * step, and main-thread stalls. `cold` reopens the store first (empty caches, HTTP cache bypassed);
 * without it the run is warm by whatever the caches already hold, optionally after `idleMs` of prefetching.
 */
export async function playBench(viewer, { stepsPerSecond = 4, cold = false, idleMs = 0 } = {}) {
  const url = viewer.store.url.replace(/\/$/, '');
  if (cold) await openWithin(viewer, url, { fetch: noStoreFetch });
  viewer.pause();
  viewer.goToTime(0);
  await waitUntil(() => viewer.paintedT === 0, TIMEOUT_MS);
  if (idleMs) await sleep(idleMs);

  const count = viewer.store.times.length;
  viewer.playback.setSpeed(stepsPerSecond);
  const events = [];
  viewer.probe = (event) => events.push(event);
  const monitor = observeMainThread();
  const network = viewer.store.stats.network;
  const before = { requests: network.requests, bytes: network.bytes };
  viewer.play();
  const expectedMs = (count / stepsPerSecond) * 1000;
  const finished = await waitUntil(() => viewer.playback.stats.steps >= count, expectedMs * 5 + 30000);
  viewer.pause();
  await sleep(100);
  viewer.probe = null;
  const { longTasks, frameGaps } = monitor.stop();

  const { stats } = viewer.playback;
  const loopMs = stats.lastStepAt - stats.firstStepAt;
  const paints = events.filter((e) => e.type === 'paint' && e.complete);
  const lags = [];
  for (const input of events.filter((e) => e.type === 'input')) {
    const paint = paints.find((p) => p.at >= input.at && p.t === input.t);
    if (paint) lags.push(paint.at - input.at);
  }
  const results = {
    store: url,
    mode: cold ? 'cold' : idleMs ? `warm after ${idleMs} ms idle` : 'warm',
    requestedStepsPerSecond: stepsPerSecond,
    timesteps: count,
    finishedLoop: finished,
    stepsTaken: stats.steps,
    achievedStepsPerSecond: round(((stats.steps - 1) / loopMs) * 1000),
    loopMs: round(loopMs),
    idealLoopMs: round(((count - 1) / stepsPerSecond) * 1000),
    heldFrames: stats.held,
    longestHoldMs: round(stats.longestHoldMs),
    totalHoldMs: round(stats.totalHoldMs),
    displayLagMs: distribution(lags),
    framesOver33ms: frameGaps.filter((g) => g > 33).length,
    maxFrameGapMs: round(Math.max(0, ...frameGaps)),
    longTasks: longTasks.length,
    network: { requests: network.requests - before.requests, MB: round((network.bytes - before.bytes) / 1048576) },
  };
  console.log(JSON.stringify(results, null, 2));
  return results;
}
