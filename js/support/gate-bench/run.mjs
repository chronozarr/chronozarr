// The speed gates of the viewer (repo CLAUDE.md), measured for two builds on the same machine, and the verdict whether the
// second is slower (compare.js). The workflow .github/workflows/speed-gates.yml runs it on pull requests that change js/.
//
//   node support/gate-bench/run.mjs --base <viewer root> --head <viewer root> --store <store dir>
//        [--pairs 5] [--gpu swiftshader|metal] [--out results.json] [--summary summary.md]
//
// A viewer root holds demo/ as support/build-viewer.mjs writes it; the store is the one store.mjs writes. Each run is a new
// headless Chromium that opens the store in the viewer and then measures:
//
//   1. `chronozarr.bench({ coldRuns, switches })` of that build: the cold open at level 0 from the loadStore call to the
//      first complete frame (36 cells, HTTP cache bypassed) and the decode per chunk, replayed from recorded bytes.
//   2. Warm switches, by this script: every chunk of level 0 at every timestep is loaded into a 2 GiB cache first, then the
//      viewer steps through time one timestep at a time with pauses (the sequence of bench()) and jumps (the same), each
//      switch timed from goToTime to the frame finished on the GPU. The data server counts the requests that reach it from
//      the first switch until 1 s after the last, and the reader counts its own requests and bytes: all must be 0.
//
// bench() has warm loops too, but they do not establish a warm switch on 36 cells: its prefetch stops at the idle allowance
// (32 MiB, about 23 of the 36 chunks of one timestep), so its first steps draw incomplete frames and fetch what they miss
// (7 of 20 frames and 60 to 74 requests per run on this store), and it counts wire bytes inside each switch, which is
// synchronous, so no response can arrive in it: it reported 0 bytes for those requests. Its figures stay in the JSON.
//
// The builds alternate (base then head, then head then base, ...) after one warm-up pair that is not counted, so that the
// load of a shared runner falls on both, and each pair is compared on its own.
//
// --gpu swiftshader (the default) draws on the CPU, as the browser tests and a runner without a GPU do; --gpu metal draws on
// the GPU of a Mac, for the gates' own figures on a quiet machine.

import { mkdir, writeFile, appendFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { chromium } from '@playwright/test';
import { startStaticServer } from '../static-server.js';
import { compareBuilds, summaryMarkdown } from './compare.js';

const BROWSER_ARGS = {
  // The flags of playwright.config.js: WebGL2 on the CPU, the same on a laptop as on a runner without a GPU.
  swiftshader: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
  // Those of scripts/fieldperf/lib.mjs.
  metal: ['--ignore-gpu-blocklist', '--use-angle=metal'],
};
const VIEWPORT = { width: 1280, height: 720 };
// Five cold opens per run: with three, one slow open moved a run's median by up to 59 % on GitHub's runners.
const COLD_RUNS = 5;
// bench()'s own warm loops are not the measurement (see above): a few switches each.
const BENCH_SWITCHES = 10;
const SWITCHES = 20;
const STEP_PAUSE_MS = 120;
const SETTLE_MS = 1000;
const WARM_CACHE_BYTES = 2 * 1024 ** 3;
const RUN_TIMEOUT_MS = 5 * 60_000;
const CELLS = 36;

const { values: args } = parseArgs({
  options: {
    base: { type: 'string' },
    head: { type: 'string' },
    store: { type: 'string' },
    pairs: { type: 'string', default: '5' },
    gpu: { type: 'string', default: 'swiftshader' },
    out: { type: 'string' },
    summary: { type: 'string' },
  },
});
if (!args.base || !args.head || !args.store || !BROWSER_ARGS[args.gpu]) {
  console.error('Usage: node support/gate-bench/run.mjs --base <viewer root> --head <viewer root> --store <store dir> [--pairs 5] [--gpu swiftshader|metal] [--out results.json] [--summary summary.md]');
  process.exit(2);
}
const pairs = Number(args.pairs);

const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
const load = () => os.loadavg()[0].toFixed(1);

/** In the page: open the store again with room for every chunk of level 0, load them all, and wait until no request is in flight. */
async function loadLevel0({ maxCacheBytes }) {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const { viewer } = window.chronozarr;
  await viewer.loadStore(viewer.store.url, { lod: 0, maxCacheBytes });
  const { store } = viewer;
  const level = store.levels[0];
  const reads = [];
  for (let t = 0; t < store.times.length; t++) {
    for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) reads.push(store.getRaw(0, row, col, t));
  }
  await Promise.all(reads);
  for (let quiet = 0; quiet < 10; quiet = store.stats.network.inflight === 0 ? quiet + 1 : 0) await sleep(50);
  return { chunks: reads.length, timesteps: store.times.length, cells: level.gridRows * level.gridCols, decodedBytes: store.stats.cache.decodedBytes };
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

function rendererInfo() {
  const gl = document.createElement('canvas').getContext('webgl2');
  const debug = gl?.getExtension('WEBGL_debug_renderer_info');
  return { userAgent: navigator.userAgent, renderer: gl ? gl.getParameter(debug ? debug.UNMASKED_RENDERER_WEBGL : gl.RENDERER) : null };
}

function withTimeout(promise, what) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${what} took more than ${RUN_TIMEOUT_MS / 1000} s`)), RUN_TIMEOUT_MS);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

async function measure({ variant, pair, appUrl, storeUrl, data }) {
  const browser = await chromium.launch({ headless: true, args: BROWSER_ARGS[args.gpu] });
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: VIEWPORT, deviceScaleFactor: 1, serviceWorkers: 'block' });
    page.on('console', (message) => message.type() === 'error' && errors.push(`console.error: ${message.text()}`));
    page.on('pageerror', (error) => errors.push(`uncaught: ${error.stack || error.message}`));
    await page.route((url) => !['127.0.0.1', 'localhost'].includes(url.hostname), (route) => route.abort('blockedbyclient'));
    const loadAtStart = load();
    await page.goto(`${appUrl}/demo/index.html?store=${encodeURIComponent(storeUrl)}`);
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 120_000 });
    if (!(await page.evaluate(() => window.chronozarr.ready))) throw new Error('the viewer did not open the store');
    const bench = await withTimeout(page.evaluate((options) => window.chronozarr.bench(options), { coldRuns: COLD_RUNS, switches: BENCH_SWITCHES }), 'bench()');
    const loaded = await withTimeout(page.evaluate(loadLevel0, { maxCacheBytes: WARM_CACHE_BYTES }), 'loading level 0');
    const serverBefore = data.requests.length;
    const warm = await withTimeout(page.evaluate(warmLoops, { switches: SWITCHES, pauseMs: STEP_PAUSE_MS, settleMs: SETTLE_MS }), 'the warm loops');
    const serverRequests = data.requests.length - serverBefore;
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
        coldOpenMs: median(bench.coldOpenConsolidated.map((row) => row.firstPaintMs)),
        warmStepMs: median(warm.stepping.map((frame) => frame.ms)),
        warmJumpMs: median(warm.jumping.map((frame) => frame.ms)),
        decodeMs: bench.decodePerChunk.chunk.median,
      },
      warmLoop: { serverRequests, readerRequests: warm.readerRequests, readerBytes: warm.readerBytes, incomplete: frames.filter((frame) => !frame.complete).length },
      loaded,
      warm,
      bench: {
        coldOpenConsolidated: bench.coldOpenConsolidated,
        coldOpenPerArrayMetadata: bench.coldOpenPerArrayMetadata,
        warmSwitchStepping: bench.warmSwitchStepping,
        warmSwitchJumping: bench.warmSwitchJumping,
        warmSwitchUploadFromCache: bench.warmSwitchUploadFromCache,
        productSwitch: bench.productSwitch,
        decodePerChunk: bench.decodePerChunk,
      },
    };
  } finally {
    await browser.close();
  }
}

const describe = (r) =>
  `cold ${r.figures.coldOpenMs.toFixed(0)} ms, step ${r.figures.warmStepMs.toFixed(1)} ms, jump ${r.figures.warmJumpMs.toFixed(1)} ms, decode ${r.figures.decodeMs.toFixed(1)} ms; ` +
  `warm loop ${r.warmLoop.serverRequests} server requests, ${r.warmLoop.readerBytes} bytes, ${r.warmLoop.incomplete} incomplete (load ${r.load.start} to ${r.load.end})`;

const data = await startStaticServer(path.resolve(args.store));
const apps = { base: await startStaticServer(path.resolve(args.base)), head: await startStaticServer(path.resolve(args.head)) };
const storeUrl = `${data.url}/`;
const records = [];
const startedAt = new Date().toISOString();
const loadBefore = load();
try {
  for (let pair = -1; pair < pairs; pair++) {
    const order = pair % 2 === 0 ? ['base', 'head'] : ['head', 'base'];
    for (const variant of order) {
      const record = await measure({ variant, pair, appUrl: apps[variant].url, storeUrl, data });
      console.log(`${pair < 0 ? 'warm-up' : `pair ${pair + 1}/${pairs}`} ${variant}: ${describe(record)}`);
      if (pair >= 0) records.push(record);
    }
  }
} finally {
  await Promise.all([data.close(), apps.base.close(), apps.head.close()]);
}

const result = compareBuilds(records);
const { browser } = records[0];
const environment = [
  `${browser.userAgent.match(/(?:Headless)?Chrome\/[\d.]+/)?.[0] ?? browser.userAgent}, WebGL renderer: ${browser.renderer}`,
  `${os.cpus().length} × ${os.cpus()[0]?.model ?? 'unknown CPU'}, ${(os.totalmem() / 1024 ** 3).toFixed(0)} GB; 1-minute load ${loadBefore} at the start, ${load()} at the end`,
  `Viewport ${VIEWPORT.width} × ${VIEWPORT.height} at pixel ratio 1; per run ${COLD_RUNS} cold opens, ${SWITCHES} steps and ${SWITCHES} jumps on a fully loaded level 0, decode of 80 chunks`,
];
const summary = summaryMarkdown(result, { environment });
console.log(`\n${summary}`);
if (args.out) {
  await mkdir(path.dirname(path.resolve(args.out)), { recursive: true });
  await writeFile(args.out, `${JSON.stringify({ startedAt, pairs, environment, result, records }, null, 1)}\n`);
}
if (args.summary) await appendFile(args.summary, summary);
process.exitCode = result.failures.length ? 1 : 0;
