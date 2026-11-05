// The speed gates of the viewer (repo CLAUDE.md), measured for two builds on the same machine, and the verdict whether the
// second is slower (compare.js). The workflow .github/workflows/speed-gates.yml runs it on pull requests that change js/.
//
//   node support/gate-bench/run.mjs --base <viewer root> --head <viewer root> --store <store dir>
//        [--pairs 5] [--cache default|compact] [--gpu swiftshader|metal] [--ignore-guards] [--out results.json] [--summary summary.md]
//
// A viewer root holds demo/ as support/build-viewer.mjs writes it; the store is the one store.mjs writes. Each run is a new
// headless Chromium that opens the store in the viewer and then measures:
//
//   1. `chronozarr.bench({ coldRuns, switches })` of that build: the cold open at level 0 from the loadStore call to the
//      first complete frame (36 cells, HTTP cache bypassed) and the decode per chunk, replayed from recorded bytes on the
//      main thread (`workers: 0`). A regression in the decode workers or their pool reaches the job only through the cold open.
//   2. Warm switches, by this script: every chunk of level 0 at every timestep is loaded into a 2 GiB cache first, then the
//      viewer steps through time one timestep at a time with pauses (the sequence of bench()) and jumps (the same), each
//      switch timed from goToTime to the frame finished on the GPU. The data server counts the requests that reach it from
//      the first switch until 1 s after the last, and the reader counts its own requests and bytes: all must be 0.
//
// bench() has warm loops too, but they do not establish a warm switch on 36 cells: its prefetch stops at the idle allowance
// (32 MiB, about 23 of the 36 chunks of one timestep), so its first steps draw incomplete frames and fetch what they miss:
// 7 of 20 frames per run on this store, and its stepping loop reports 228 requests (321 MB). Its figures stay in the JSON.
//
// Before each counted run, a new browser makes a first visit (first-visit.mjs): an empty profile, the HTTP cache on, the page
// and its first open of the store, none of it warm. Its marks (the first chunk request, shader compile and link, the start of a
// decode worker, the codec, the first whole frame) are in the summary and the JSON, for information: no gate reads them.
//
// The builds alternate (base then head, then head then base, ...) after one warm-up pair that is not counted, so that the
// load of a shared runner falls on both, and each pair is compared on its own.
//
// A local run refuses to start on a machine whose load or power source would make the timings noise, and discards a run whose
// browser was not drawing at 60 Hz (guards.js); `--ignore-guards` measures anyway. CI is not guarded.
//
// --gpu swiftshader (the default) draws on the CPU, as the browser tests and a runner without a GPU do; --gpu metal draws on
// the GPU of a Mac, for the gates' own figures on a quiet machine.
//
// --cache compact measures the cold open and the warm switches with the cache that the public demo store gets. The cache
// policy (js/demo/cache-policy.js) packs every chunk of a uint16 store in memory, losslessly, when the loop of level 0 is more
// than 0.9 of the cache cap: the demo's loop is 8.4 GiB against 1.5 GiB. The gate store's 576 MiB loop never is, so the default
// pass never runs that path: packing in the decode workers, unpacking on the main thread, the GPU reading packed words. The
// compact pass opens the store with a cap that the loop exceeds and that the packed loop (72 % of it) fits, so that the warm
// loops still hold everything. Decode is the same code packed or not, and only the default pass measures it.

import { mkdir, writeFile, appendFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { startStaticServer } from '../static-server.js';
import { BROWSER_ARGS, VIEWPORT, launchBrowser, measureFrameInterval, rendererInfo, withTimeout } from './browser.mjs';
import { COMPACT_METRICS, METRICS, compareBuilds, summaryMarkdown } from './compare.js';
import { firstVisit } from './first-visit.mjs';
import { guardsMode, guardsSummary, measureGuarded, readMachine, refuseNoisyMachine } from './guards.js';

// Five cold opens per run: with three, one slow open moved a run's median by up to 59 % on GitHub's runners.
const COLD_RUNS = 5;
// bench()'s own warm loops are not the measurement (see above): a few switches each.
const BENCH_SWITCHES = 10;
const SWITCHES = 20;
const STEP_PAUSE_MS = 120;
const SETTLE_MS = 1000;
const WARM_CACHE_BYTES = 2 * 1024 ** 3;
// The cap of the compact pass: the loop of level 0 (576 MiB) is above 0.9 of it, and the packed loop (about 414 MiB) is below it.
const COMPACT_TOTAL_BYTES = 560 * 1024 ** 2;
const RUN_TIMEOUT_MS = 5 * 60_000;
const CELLS = 36;

const { values: args } = parseArgs({
  options: {
    base: { type: 'string' },
    head: { type: 'string' },
    store: { type: 'string' },
    pairs: { type: 'string', default: '5' },
    cache: { type: 'string', default: 'default' },
    gpu: { type: 'string', default: 'swiftshader' },
    'ignore-guards': { type: 'boolean', default: false },
    out: { type: 'string' },
    summary: { type: 'string' },
  },
});
if (!args.base || !args.head || !args.store || !BROWSER_ARGS[args.gpu] || !['default', 'compact'].includes(args.cache)) {
  console.error('Usage: node support/gate-bench/run.mjs --base <viewer root> --head <viewer root> --store <store dir> [--pairs 5] [--cache default|compact] [--gpu swiftshader|metal] [--ignore-guards] [--out results.json] [--summary summary.md]');
  process.exit(2);
}
const pairs = Number(args.pairs);
const compact = args.cache === 'compact';
const guardMode = guardsMode({ ignore: args['ignore-guards'] });
const guards = guardMode.enabled;
if (guards) refuseNoisyMachine(readMachine());

const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
const load = () => os.loadavg()[0].toFixed(1);

/** In the page: the cold opens of bench(), with the cache budgets of the compact pass (the viewer opens a store with the cap it is given). */
async function compactColdOpens({ runs, totalBytes }) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const noStoreFetch = (request) => fetch(new Request(request, { cache: 'no-store' }));
  const { viewer } = window.chronozarr;
  const url = viewer.store.url.replace(/\/$/, '');
  const rows = [];
  for (let run = 0; run < runs; run++) {
    await sleep(300);
    const { openMs, firstPaintMs } = await viewer.loadStore(url, { lod: 0, fetch: noStoreFetch, totalBytes });
    rows.push({ run, openMs, firstPaintMs, compact: viewer.store.compactCacheEnabled });
  }
  return rows;
}

/**
 * In the page: open the store again with the cache budgets `cache` names (loadStore's options), load every chunk of level 0, and
 * wait until no request is in flight. `ensureRaw` fills the cache like `getRaw` and does not unpack a packed chunk to return it.
 */
async function loadLevel0(cache) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const { viewer } = window.chronozarr;
  await viewer.loadStore(viewer.store.url, { lod: 0, ...cache });
  const { store } = viewer;
  const level = store.levels[0];
  const reads = [];
  for (let t = 0; t < store.times.length; t++) {
    for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) reads.push(store.ensureRaw(0, row, col, t));
  }
  await Promise.all(reads);
  for (let quiet = 0; quiet < 10; quiet = store.stats.network.inflight === 0 ? quiet + 1 : 0) await sleep(50);
  return {
    chunks: reads.length,
    timesteps: store.times.length,
    cells: level.gridRows * level.gridCols,
    loopBytes: reads.length * level.chunkBytes,
    decodedBytes: store.stats.cache.decodedBytes,
    compact: store.compactCacheEnabled,
    compactRatio: store.compactCacheRatio,
  };
}

/** In the page: the warm switches of bench() (stepping with pauses, then jumping) on a store whose level 0 is all in memory. */
async function warmLoops({ switches, pauseMs, settleMs }) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const { viewer } = window.chronozarr;
  const { store, renderer } = viewer;
  const nTime = store.times.length;
  const switchTo = (t) => {
    const started = performance.now();
    viewer.goToTime(t);
    const frame = viewer.renderNow();
    renderer.finish();
    return { t, ms: performance.now() - started, complete: frame.complete, lod: frame.lod, cells: frame.cells };
  };
  const stepping = [];
  const jumping = [];
  viewer.goToTime(0);
  viewer.renderNow();
  await sleep(300);
  const network = store.stats.network;
  const before = { requests: network.requests, bytes: network.bytes, evictions: store.stats.cache.evictions };
  let t = 0;
  let direction = 1;
  for (let i = 0; i < switches; i++) {
    if (t + direction < 0 || t + direction >= nTime) direction = -direction;
    t += direction;
    await sleep(pauseMs);
    stepping.push(switchTo(t));
  }
  for (let i = 0; i < switches; i++) {
    let next = (t + 1 + (i % 3)) % nTime;
    if (next === t) next = (next + 1) % nTime;
    t = next;
    jumping.push(switchTo(t));
  }
  await sleep(settleMs);
  return { stepping, jumping, readerRequests: network.requests - before.requests, readerBytes: network.bytes - before.bytes, evictions: store.stats.cache.evictions - before.evictions };
}

async function measure({ variant, pair, appUrl, storeUrl, data }) {
  // Informational: a visit that fails is logged and kept in the record, and does not stop the gates. The compact pass and the warm-up pair skip it.
  const visit =
    compact || pair < 0
      ? undefined
      : await firstVisit({ gpu: args.gpu, appUrl, storeUrl }).catch((error) => {
          console.warn(`first visit of the ${variant} build, pair ${pair + 1}, failed: ${error.message}`);
          return { error: error.message };
        });
  const browser = await launchBrowser(args.gpu);
  const errors = [];
  try {
    const frameBefore = guards ? await measureFrameInterval(browser) : null;
    const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 1, serviceWorkers: 'block' });
    page.on('console', (message) => message.type() === 'error' && errors.push(`console.error: ${message.text()}`));
    page.on('pageerror', (error) => errors.push(`uncaught: ${error.stack || error.message}`));
    await page.route((url) => !['127.0.0.1', 'localhost'].includes(url.hostname), (route) => route.abort('blockedbyclient'));
    const loadAtStart = load();
    await page.goto(`${appUrl}/demo/index.html?store=${encodeURIComponent(storeUrl)}`);
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 120_000 });
    if (!(await page.evaluate(() => window.chronozarr.ready))) throw new Error('the viewer did not open the store');
    let bench = null;
    let coldOpens;
    if (compact) {
      coldOpens = await withTimeout(page.evaluate(compactColdOpens, { runs: COLD_RUNS, totalBytes: COMPACT_TOTAL_BYTES }), 'the cold opens', RUN_TIMEOUT_MS);
      if (!coldOpens.every((row) => row.compact)) throw new Error(`the cold opens of the compact pass did not pack the cache (cap ${COMPACT_TOTAL_BYTES / 1024 ** 2} MiB): the loop of the store is not above 0.9 of it`);
    } else {
      bench = await withTimeout(page.evaluate((options) => window.chronozarr.bench(options), { coldRuns: COLD_RUNS, switches: BENCH_SWITCHES }), 'bench()', RUN_TIMEOUT_MS);
      coldOpens = bench.coldOpenConsolidated;
    }
    const loaded = await withTimeout(page.evaluate(loadLevel0, compact ? { totalBytes: COMPACT_TOTAL_BYTES } : { maxCacheBytes: WARM_CACHE_BYTES }), 'loading level 0', RUN_TIMEOUT_MS);
    if (loaded.compact !== compact) throw new Error(`the ${args.cache} pass held its cache ${loaded.compact ? 'packed' : 'unpacked'}: the cap of the pass and the size of the store no longer agree`);
    const serverBefore = data.requests.length;
    const warm = await withTimeout(page.evaluate(warmLoops, { switches: SWITCHES, pauseMs: STEP_PAUSE_MS, settleMs: SETTLE_MS }), 'the warm loops', RUN_TIMEOUT_MS);
    const serverRequests = data.requests.length - serverBefore;
    const frameAfter = guards ? await measureFrameInterval(browser) : null;
    const browserInfo = await page.evaluate(rendererInfo);
    const frames = [...warm.stepping, ...warm.jumping];
    const wrongView = frames.filter((frame) => frame.lod !== 0 || frame.cells !== CELLS);
    if (wrongView.length) throw new Error(`${wrongView.length} warm frames were not level 0 with ${CELLS} cells (first: level ${wrongView[0].lod}, ${wrongView[0].cells} cells): the view does not show the whole store`);
    if (errors.length) throw new Error(`the page logged errors:\n${errors.join('\n')}`);
    return {
      variant,
      pair,
      load: { start: Number(loadAtStart), end: Number(load()) },
      browser: browserInfo,
      figures: {
        coldOpenMs: median(coldOpens.map((row) => row.firstPaintMs)),
        warmStepMs: median(warm.stepping.map((frame) => frame.ms)),
        warmJumpMs: median(warm.jumping.map((frame) => frame.ms)),
        ...(compact ? {} : { decodeMs: bench.decodePerChunk.chunk.median }),
      },
      warmLoop: { serverRequests, readerRequests: warm.readerRequests, readerBytes: warm.readerBytes, incomplete: frames.filter((frame) => !frame.complete).length },
      loaded,
      warm,
      bench: bench && {
        coldOpenConsolidated: bench.coldOpenConsolidated,
        coldOpenPerArrayMetadata: bench.coldOpenPerArrayMetadata,
        warmSwitchStepping: bench.warmSwitchStepping,
        warmSwitchJumping: bench.warmSwitchJumping,
        warmSwitchUploadFromCache: bench.warmSwitchUploadFromCache,
        productSwitch: bench.productSwitch,
        decodePerChunk: bench.decodePerChunk,
      },
      coldOpens,
      ...(visit && { firstVisit: visit }),
      ...(guards && { frameInterval: { before: frameBefore, after: frameAfter } }),
    };
  } finally {
    await browser.close();
  }
}

const describe = (r) =>
  `cold ${r.figures.coldOpenMs.toFixed(0)} ms, step ${r.figures.warmStepMs.toFixed(1)} ms, jump ${r.figures.warmJumpMs.toFixed(1)} ms${compact ? '' : `, decode ${r.figures.decodeMs.toFixed(1)} ms`}; ` +
  `warm loop ${r.warmLoop.serverRequests} server requests, ${r.warmLoop.readerBytes} bytes, ${r.warmLoop.incomplete} incomplete (load ${r.load.start} to ${r.load.end})`;

const data = await startStaticServer(path.resolve(args.store));
const apps = { base: await startStaticServer(path.resolve(args.base)), head: await startStaticServer(path.resolve(args.head)) };
const storeUrl = `${data.url}/`;
const records = [];
const discarded = [];
const startedAt = new Date().toISOString();
const loadBefore = load();
try {
  for (let pair = -1; pair < pairs; pair++) {
    const order = pair % 2 === 0 ? ['base', 'head'] : ['head', 'base'];
    for (const variant of order) {
      const label = `${pair < 0 ? 'warm-up' : `pair ${pair + 1}`} ${variant} run`;
      const record = await measureGuarded(() => measure({ variant, pair, appUrl: apps[variant].url, storeUrl, data }), { enabled: guards, label, discarded });
      console.log(`${pair < 0 ? 'warm-up' : `pair ${pair + 1}/${pairs}`} ${variant}: ${describe(record)}`);
      if (pair >= 0) records.push(record);
    }
  }
} finally {
  await Promise.all([data.close(), apps.base.close(), apps.head.close()]);
}

const result = compareBuilds(records, { metrics: compact ? COMPACT_METRICS : METRICS });
const { browser, loaded } = records[0];
const environment = [
  `${browser.userAgent.match(/(?:Headless)?Chrome\/[\d.]+/)?.[0] ?? browser.userAgent}, WebGL renderer: ${browser.renderer}`,
  `${os.cpus().length} × ${os.cpus()[0]?.model ?? 'unknown CPU'}, ${(os.totalmem() / 1024 ** 3).toFixed(0)} GB; 1-minute load ${loadBefore} at the start, ${load()} at the end`,
  `Viewport ${VIEWPORT.width} × ${VIEWPORT.height} at pixel ratio 1; per run ${COLD_RUNS} cold opens, ${SWITCHES} steps and ${SWITCHES} jumps on a fully loaded level 0${
    compact
      ? `, held packed: cap ${COMPACT_TOTAL_BYTES / 1024 ** 2} MiB, level 0 of ${loaded.timesteps} timesteps ${Math.round(loaded.loopBytes / 1024 ** 2)} MiB unpacked, packed to ${Math.round(loaded.compactRatio * 100)} % of it`
      : ', decode of 80 chunks'
  }`,
  guardsSummary({ mode: guardMode, cores: os.cpus().length, discarded: discarded.length }),
];
const summary = summaryMarkdown(result, { environment, ...(compact && { title: 'Speed gates, compact cache: head against base' }) });
console.log(`\n${summary}`);
if (args.out) {
  await mkdir(path.dirname(path.resolve(args.out)), { recursive: true });
  await writeFile(args.out, `${JSON.stringify({ startedAt, pairs, guards: guardMode, environment, result, records, discarded }, null, 1)}\n`);
}
if (args.summary) await appendFile(args.summary, summary);
process.exitCode = result.failures.length ? 1 : 0;
