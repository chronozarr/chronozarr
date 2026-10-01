// zarr-layer against the TileRipper viewer on the same store, view, level and timestep, in a fresh headless Chromium
// per run, with CDP network throttling. Measures
//
//   open   from the call that opens the store to the first frame that shows every visible cell of the fixed level
//   scrub  20 timesteps forward from timestep 40, in one of two modes:
//          burst  one input every 100 ms (the viewer's ArrowRight, zarr-layer's setSelector), like tileripper's
//                 interactionBench "scrub forward 20"; a step a tool skipped is satisfied by the later frame that shows
//                 a later step (interactionBench's rule)
//          paced  the next input 100 ms after the frame of the previous step is complete, so every step is shown and its
//                 latency is the time to load and draw it
//
// Requests and bytes come from CDP events of the page (see lib/harness.mjs), identically for both tools.
//
//   node zarr-layer/compare.mjs --tool zarr-layer|tileripper --profile natural|50Mbit-40ms|10Mbit-100ms
//        --mode burst|paced [--source remote|local] [--reps 1] [--steps 20] [--start 40] [--timeout 900000] [--out results/file.json]
//
// --source remote (default) reads the published store, https://data.tileripper.com/ucayali_santa_maria/chronozarr-3; `natural`
// is then the real link to that CDN. --source local reads the same files from this repository's range server (HTTP/1.1, so
// at most 6 connections per origin for either tool); CDP throttling then defines the whole link.
//
// zarr-layer opens the store unmodified, with the constructor options `crs` and `bounds` it needs and `zarrVersion: 3` (docs/comparisons.md).

import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { PROFILES, REPO_ROOT, applyProfile, attachNet, launchBrowser, newPage, round, startServer, subtract } from '../lib/harness.mjs';

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const tool = flag('tool', 'zarr-layer');
const profileName = flag('profile', 'natural');
const mode = flag('mode', 'burst');
const reps = Number(flag('reps', 1));
const steps = Number(flag('steps', 20));
const startT = Number(flag('start', 40));
const timeoutMs = Number(flag('timeout', 900000));
const out = flag('out', null);
const source = flag('source', 'remote');
const REMOTE_STORE = 'https://data.tileripper.com/ucayali_santa_maria/chronozarr-3';
const LOCAL_STORE_PATH = '/data/stores/ucayali_santa_maria/chronozarr-3';

if (!(profileName in PROFILES)) throw new Error(`unknown profile ${profileName}; use ${Object.keys(PROFILES).join(', ')}`);
if (!['zarr-layer', 'tileripper'].includes(tool)) throw new Error(`unknown tool ${tool}`);
if (!['burst', 'paced'].includes(mode)) throw new Error(`unknown mode ${mode}`);
if (!['remote', 'local'].includes(source)) throw new Error(`unknown source ${source}`);
const shardBytes = JSON.parse(await readFile(path.join(REPO_ROOT, LOCAL_STORE_PATH, 'zarr.json'), 'utf8')).attributes.chronozarr.shard_bytes;

// The fixed view: the whole AOI of the Ucayali store (3 x 3 cells of level 1, 20 m) on a 1800 x 1700 px window at one CSS
// pixel per device pixel. zarr-layer draws level 1 at map zoom 12; TileRipper is pinned to level 1 and given the same
// ground scale (the AOI is 1457.6 px wide in both) and centre. Its canvas (1500 x 1592 px) shows the whole AOI.
const FIXED_LEVEL = 1;
const BOUNDS = [485650, 9142230, 513240, 9169880];
const CRS = 'EPSG:32718';
const ZOOM = 12;
const AOI_WIDTH_PX = 1457.64;
const LEVEL0_WIDTH = 2759;
const CENTER = [(BOUNDS[0] + BOUNDS[2]) / 2, (BOUNDS[1] + BOUNDS[3]) / 2];
const CADENCE_MS = 100;
const THINK_MS = 100;

const tileripperPage = async ({ url, lod, viewSearch, steps, mode, cadenceMs, thinkMs, timeoutMs }) => {
  const perf = await import('/js/tileripper/perf.js');
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const waitUntil = async (predicate, limitMs) => {
    const began = performance.now();
    while (!predicate()) {
      if (performance.now() - began > limitMs) return false;
      await sleep(5);
    }
    return true;
  };
  const viewer = window.tileripper.viewer;
  const events = [];
  viewer.probe = (event) => events.push(event);
  const n0 = await window.netSnapshot();
  const started = performance.now();
  const opened = await viewer.loadStore(url, { lod, viewSearch });
  const openedAt = performance.now();
  const n1 = await window.netSnapshot();
  const readerAfterOpen = viewer.store.stats();
  const openPaint = events.find((e) => e.type === 'paint' && e.complete);
  const canvas = { width: viewer.canvas.width, height: viewer.canvas.height };

  await waitUntil(() => viewer.paintedT === viewer.t, timeoutMs);
  const scrubFirst = events.length;
  const from = viewer.t;
  const inputs = [];
  const press = () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true, cancelable: true }));
  const paintsSince = () => events.slice(scrubFirst).filter((e) => e.type === 'paint');
  const shows = (input) => paintsSince().some((e) => e.complete && e.at >= input.at && e.t >= input.t);
  const begin = performance.now();
  let settled = true;
  for (let i = 0; i < steps; i++) {
    if (mode === 'burst') {
      const wait = begin + i * cadenceMs - performance.now();
      if (wait > 0) await sleep(wait);
    } else await sleep(thinkMs);
    const input = { at: performance.now(), t: from + i + 1 };
    inputs.push(input);
    press();
    if (mode === 'paced') settled = (await waitUntil(() => shows(input), timeoutMs)) && settled;
  }
  if (mode === 'burst') settled = await waitUntil(() => shows(inputs.at(-1)), timeoutMs);
  const lastPaint = paintsSince().find((e) => e.complete && e.at >= inputs.at(-1).at && e.t >= inputs.at(-1).t);
  const settledAt = lastPaint ? lastPaint.at : performance.now();
  await sleep(50);
  const n2 = await window.netSnapshot();
  const readerAfterScrub = viewer.store.stats();
  const scrubEvents = events.slice(scrubFirst);
  const paints = paintsSince();
  const lags = inputs.map((input) => {
    const paint = paints.find((p) => p.at >= input.at && p.t >= input.t && p.complete);
    return paint ? paint.at - input.at : null;
  });
  const exact = inputs.filter((input) => paints.some((e) => e.complete && e.at >= input.at && e.t === input.t)).length;
  const completeness = perf.frameCompleteness(scrubEvents);
  let mixedMs = 0;
  for (let k = 0; k + 1 < paints.length; k++) if (!perf.isWholeFrame(paints[k])) mixedMs += paints[k + 1].at - paints[k].at;
  await sleep(3000);
  const n3 = await window.netSnapshot();
  return {
    canvas,
    lod: openPaint?.lod ?? null,
    cells: openPaint?.cells ?? null,
    open: { ms: openedAt - started, metadataMs: opened.openMs, snapshotBefore: n0, snapshotAfter: n1, reader: readerAfterOpen.network },
    scrub: {
      steps,
      settled,
      settleMs: settledAt - begin,
      lags,
      shownExactly: exact,
      mixedSteps: completeness.partial,
      mixedMs,
      paintedFrames: completeness.painted,
      levelFallbacks: completeness.levelFallbacks,
      keptFrames: completeness.keptFrames,
      reader: { requests: readerAfterScrub.network.requests - readerAfterOpen.network.requests, bytes: readerAfterScrub.network.bytes - readerAfterOpen.network.bytes },
      snapshotBefore: n1,
      snapshotAfter: n2,
    },
    trailing: { snapshotAfter: n3 },
  };
};

const zarrLayerPage = async ({ store, extra, bounds, crs, zoom, startT, steps, mode, cadenceMs, thinkMs, timeoutMs }) => {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const selector = (t) => ({ band: ['B04', 'B03', 'B02'], time: { selected: t, type: 'index' } });
  const view = zl.aoiView({ bounds, crs, zoom });
  await zl.createMap({ center: view.center, zoom });
  const events = [];
  const n0 = await window.netSnapshot();
  const started = performance.now();
  const layer = await zl.addLayer({ source: store, extra, selector: selector(startT) });
  const stop = zl.watch(layer, events);
  const complete = await zl.whenComplete(layer, { timeoutMs });
  const n1 = await window.netSnapshot();
  const openState = { level: complete.level, total: complete.total };
  await sleep(50);

  const scrubFirst = events.length;
  const v0 = zl.layerState(layer).version;
  const inputs = [];
  const applied = [];
  const reached = (e, input) => e.version >= input.version && e.total > 0 && e.loaded === e.total;
  const showsSince = (input) => events.slice(scrubFirst).find((e) => e.at >= input.at && reached(e, input));
  const waitShown = async (input) => {
    const began = performance.now();
    while (!showsSince(input)) {
      if (performance.now() - began > timeoutMs) return false;
      window.map.triggerRepaint();
      await sleep(20);
    }
    return true;
  };
  const begin = performance.now();
  let settled = true;
  for (let i = 0; i < steps; i++) {
    if (mode === 'burst') {
      const wait = begin + i * cadenceMs - performance.now();
      if (wait > 0) await sleep(wait);
    } else await sleep(thinkMs);
    const input = { at: performance.now(), t: startT + i + 1, version: v0 + i + 1 };
    inputs.push(input);
    const call = layer.setSelector(selector(input.t));
    applied.push(call);
    if (mode === 'paced') {
      await call;
      settled = (await waitShown(input)) && settled;
    }
  }
  await Promise.all(applied);
  if (mode === 'burst') settled = await waitShown(inputs.at(-1));
  const finalVersion = layer.regionRenderer.selectorVersion;
  const lastEvent = showsSince(inputs.at(-1));
  const settledAt = lastEvent ? lastEvent.at : performance.now();
  await sleep(50);
  const n2 = await window.netSnapshot();
  const scrubEvents = events.slice(scrubFirst);
  const lags = inputs.map((input) => {
    const e = showsSince(input);
    return e ? e.at - input.at : null;
  });
  const exact = inputs.filter((input) => scrubEvents.some((e) => e.at >= input.at && e.version === input.version && e.total > 0 && e.loaded === e.total)).length;
  // A render that shows some regions at a step and others at an older one is a mixed frame; count the steps whose
  // wait for a complete frame included at least one.
  const mixedSteps = inputs.filter((input) => {
    const shown = showsSince(input);
    const end = shown ? shown.at : Infinity;
    return scrubEvents.some((e) => e.at >= input.at && e.at < end && e.version >= input.version && e.total > 0 && e.loaded > 0 && e.loaded < e.total);
  }).length;
  let mixedMs = 0;
  for (let k = 0; k + 1 < scrubEvents.length; k++) {
    const e = scrubEvents[k];
    if (e.total > 0 && e.loaded > 0 && e.loaded < e.total) mixedMs += scrubEvents[k + 1].at - e.at;
  }
  const levels = [...new Set(events.map((e) => e.level).filter((l) => l !== null))];
  await sleep(3000);
  const n3 = await window.netSnapshot();
  stop();
  return {
    canvas: { width: window.innerWidth, height: window.innerHeight },
    lod: openState.level,
    cells: openState.total,
    open: { ms: complete.at - started, snapshotBefore: n0, snapshotAfter: n1 },
    scrub: {
      steps,
      settled,
      versionsApplied: finalVersion - v0,
      settleMs: settledAt - begin,
      lags,
      shownExactly: exact,
      mixedSteps,
      mixedMs,
      paintedFrames: scrubEvents.length,
      levelsSeen: levels,
      snapshotBefore: n1,
      snapshotAfter: n2,
    },
    trailing: { snapshotAfter: n3 },
  };
};

const pageFunctions = { tileripper: tileripperPage, 'zarr-layer': zarrLayerPage };

const sortedNumbers = (values) => [...values].sort((a, b) => a - b);
const median = (values) => {
  const sorted = sortedNumbers(values);
  return sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
};
const percentile = (values, q) => {
  const sorted = sortedNumbers(values);
  return sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] : null;
};

async function oneRun(server, rep) {
  const store = source === 'remote' ? REMOTE_STORE : `${server.url}${LOCAL_STORE_PATH}`;
  const browser = await launchBrowser();
  try {
    const { context, page } = await newPage(browser, { width: 1800, height: 1700, blockCatalog: tool === 'tileripper' });
    const messages = [];
    page.on('console', (m) => {
      if (['warning', 'error'].includes(m.type())) messages.push(`${m.type()}: ${m.text().slice(0, 300)}`);
    });
    page.on('pageerror', (e) => messages.push(`pageerror: ${String(e.message).slice(0, 300)}`));
    const { cdp, counter } = await attachNet(context, page, [source === 'remote' ? new URL(store).origin : `${server.url}/data/`]);
    await page.exposeFunction('netSnapshot', () => counter.snapshot());
    await applyProfile(cdp, null);
    const pageUrl = tool === 'tileripper' ? `${server.url}/js/tileripper/index.html` : `${server.url}/bench/zarr-layer/page.html`;
    await page.goto(pageUrl);
    await page.waitForFunction(() => Boolean(window.tileripper || window.zl), null, { timeout: 60000 });
    const warmup = counter.snapshot();
    await applyProfile(cdp, PROFILES[profileName]);

    const common = { steps, mode, cadenceMs: CADENCE_MS, thinkMs: THINK_MS, timeoutMs };
    const input =
      tool === 'tileripper'
        ? { ...common, url: store, lod: FIXED_LEVEL, viewSearch: `?t=${startT}&z=${AOI_WIDTH_PX / LEVEL0_WIDTH}&c=${CENTER[0]},${CENTER[1]}` }
        : { ...common, store, extra: { crs: CRS, bounds: BOUNDS, zarrVersion: 3 }, bounds: BOUNDS, crs: CRS, zoom: ZOOM, startT };
    const raw = await page.evaluate(pageFunctions[tool], input);
    await applyProfile(cdp, null);
    const entries = counter.entries();
    const openEntries = entries.slice(raw.open.snapshotBefore.requests, raw.open.snapshotAfter.requests);
    const t0 = Math.min(...openEntries.map((e) => e.startedAt));
    const isIndexRead = (e) => {
      const m = /bytes=(\d+)-(\d+)/.exec(e.range ?? '');
      const shard = /\/(\d+)\/data\/c\/(\d+)\/\d+\/(\d+)\/(\d+)$/.exec(new URL(e.url).pathname);
      return Boolean(m && shard && shardBytes[shard[1]]?.[`${shard[2]}/${shard[3]}/${shard[4]}`] === Number(m[2]) + 1);
    };
    const openTimeline = openEntries.map((e) => {
      const m = /bytes=(\d+)-(\d+)/.exec(e.range ?? '');
      return { kind: e.kind, role: e.kind === 'metadata' ? 'metadata' : isIndexRead(e) ? 'index' : e.kind === 'head' ? 'head' : 'data', path: new URL(e.url).pathname.replace(/^.*chronozarr-3/, ''), bytes: m ? Number(m[2]) - Number(m[1]) + 1 : null, startMs: round((e.startedAt - t0) * 1000), firstByteMs: e.responseAt === null ? null : round((e.responseAt - t0) * 1000), endMs: e.endedAt === null ? null : round((e.endedAt - t0) * 1000), cache: e.cache, protocol: e.protocol };
    });
    if (process.env.BENCH_DUMP) await writeFile(process.env.BENCH_DUMP, JSON.stringify(counter.entries()));
    const total = counter.snapshot();
    await context.close();

    const openNet = subtract(raw.open.snapshotAfter, raw.open.snapshotBefore);
    const scrubNet = subtract(raw.scrub.snapshotAfter, raw.scrub.snapshotBefore);
    const trailing = subtract(raw.trailing.snapshotAfter, raw.scrub.snapshotAfter);
    const lags = raw.scrub.lags.filter((v) => v !== null);
    const whole = subtract(total, warmup);
    return {
      tool,
      source,
      profile: profileName,
      mode,
      rep,
      level: raw.lod,
      cells: raw.cells,
      canvas: raw.canvas,
      openTimeline,
      open: { indexReadsDoneMs: round(Math.max(0, ...openTimeline.filter((e) => e.role === 'index').map((e) => e.endMs))), ms: round(raw.open.ms), metadataMs: round(raw.open.metadataMs), requests: openNet.requests, bytes: openNet.bytes, byKind: openNet.byKind, edgeCache: openNet.cache, readerStats: raw.open.reader ?? null },
      scrub: {
        steps: raw.scrub.steps,
        settled: raw.scrub.settled,
        settleMs: round(raw.scrub.settleMs),
        requests: scrubNet.requests,
        bytes: scrubNet.bytes,
        byKind: scrubNet.byKind,
        edgeCache: scrubNet.cache,
        stepsShownExactly: raw.scrub.shownExactly,
        stepsNeverCompleted: raw.scrub.lags.filter((v) => v === null).length,
        lagMs: { n: lags.length, median: round(median(lags)), p95: round(percentile(lags, 0.95)), max: round(Math.max(...lags)) },
        lags: raw.scrub.lags.map((v) => round(v)),
        stepsWithMixedFrame: raw.scrub.mixedSteps,
        mixedFrameMs: round(raw.scrub.mixedMs),
        paintedFrames: raw.scrub.paintedFrames,
        readerStats: raw.scrub.reader ?? null,
        extra: { levelFallbacks: raw.scrub.levelFallbacks ?? null, keptFrames: raw.scrub.keptFrames ?? null, versionsApplied: raw.scrub.versionsApplied ?? null, levelsSeen: raw.scrub.levelsSeen ?? null },
      },
      trailing3s: { requests: trailing.requests, bytes: trailing.bytes },
      sessionTotal: { requests: whole.requests, bytes: whole.bytes, byKind: whole.byKind },
      messages: messages.slice(0, 8),
    };
  } finally {
    await browser.close();
  }
}

const server = await startServer();
const results = [];
try {
  for (let rep = 1; rep <= reps; rep++) {
    const result = await oneRun(server, rep);
    results.push(result);
    console.log(JSON.stringify({ ...result, scrub: { ...result.scrub, lags: undefined } }));
  }
} finally {
  await server.close();
}
if (out) await writeFile(out, `${JSON.stringify({ source, tool, profile: profileName, mode, startT, steps, level: FIXED_LEVEL, results }, null, 1)}\n`);
