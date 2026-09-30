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

function replayStore(records) {
  return {
    get: async (key) => records.get(key),
    getRange: async (key, range) => records.get(`${key}|${JSON.stringify(range)}`),
  };
}

/** Per-chunk decode (zstd/gzip + bytes codec) replayed from recorded bytes, so the network is out of the loop. */
async function decodeTimings(url, repeats) {
  const records = new Map();
  const recorder = await openStore(url, { store: recordingStore(new zarr.FetchStore(url, { fetch: noStoreFetch }), records) });
  const timesteps = Math.min(recorder.times.length, 8);
  for (let t = 0; t < timesteps; t++) await recorder.getRaw(0, 0, 0, t);
  const replayer = await openStore(url, { store: replayStore(records) });
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
  const format = (values) => (values.length ? Object.fromEntries(Object.entries(summarize(values)).map(([k, v]) => [k, round(v)])) : null);
  return { chunkRawBytes: level.chunkBytes, anchor: format(anchors), delta: format(deltas) };
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
