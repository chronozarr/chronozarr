// Runs the viewer's scrubBench against a store in a headless Chrome of its own and prints one row per run:
// stepping with the arrow key and dragging the timeline slider, cold and after idle prefetch, at the overview
// zoom and zoomed to about nine LOD 0 cells.
//
//   PLAYWRIGHT_CORE=/path/to/playwright-core CHROME=/path/to/chrome \
//   node js/support/reader-bench/scrub.mjs /data/spike/stress48 ['{"steps":20,"startT":40,"idleMs":5000}']
//
// A store URL starting with "/" is a path on the local static server (BENCH_ROOT, default the repository root).

import { createRequire } from 'node:module';
import path from 'node:path';
import { startStaticServer } from '../static-server.js';

const REPO_ROOT = path.resolve(import.meta.dirname, '../../..');
const [storePath, optionsJson = '{}'] = process.argv.slice(2);
const playwrightPath = process.env.PLAYWRIGHT_CORE;
const chromePath = process.env.CHROME;
if (!storePath || !playwrightPath || !chromePath) {
  console.error('usage: PLAYWRIGHT_CORE=<dir> CHROME=<binary> node scrub.mjs <store url or /path> [scrubBench options json]');
  process.exit(2);
}
const { chromium } = createRequire(import.meta.url)(playwrightPath);
const options = JSON.parse(optionsJson);

const server = await startStaticServer(process.env.BENCH_ROOT ?? REPO_ROOT);
const browser = await chromium.launch({ executablePath: chromePath, headless: true, args: ['--ignore-gpu-blocklist', '--use-angle=metal'] });
try {
  const storeUrl = storePath.startsWith('/') ? `${server.url}${storePath}` : storePath;
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  page.on('pageerror', (error) => console.error('pageerror', error.message));
  page.on('console', () => {});
  await page.goto(`${server.url}/js/tileripper/index.html?store=${encodeURIComponent(storeUrl)}`);
  await page.waitForFunction(() => window.tileripper?.ready, null, { timeout: 60000 });
  await page.evaluate(() => window.tileripper.ready);
  const results = await page.evaluate((benchOptions) => window.tileripper.scrubBench(benchOptions), options);
  const rows = Object.entries(results.runs).map(([name, run]) => ({
    run: name,
    lod: run.lod,
    cells: run.visibleCells,
    shownExactly: `${run.stepsShownExactly}/${results.steps}`,
    neverCaughtUp: run.stepsNeverCaughtUp,
    lagMedianMs: run.lagMs?.median ?? null,
    lagP95Ms: run.lagMs?.p95 ?? null,
    settleMs: run.settleMs,
    framesOver33ms: run.mainThread.framesOver33ms,
    networkMB: Math.round(run.network.bytes / 1048576),
    requests: run.network.requests,
  }));
  console.log(JSON.stringify({ store: results.store, steps: results.steps, startT: results.startT, rows }, null, 1));
} finally {
  await browser.close();
  await server.close();
}
