// The speed gates of CLAUDE.md, measured in the deployed viewer from this machine with the viewer's own benchmark:
// `await chronozarr.bench()` (js/demo/bench.js) with its defaults (5 cold runs per variant, 30 switches), against the public default store.
//
//   node scripts/fieldperf/bench_gates.mjs [tag]
//
// bench() does: a cold open at LOD 0 (every cell of the 6 x 6 grid, HTTP cache bypassed) with and without consolidated
// metadata, then warm timestep switches (stepping, jumping, from the decoded cache), product switches, and per-chunk
// decode replayed from recorded bytes. One LOD-0 open of this store reads 37 requests and 39 MB; the bench makes 10 of them
// (about 0.4 GB) plus its prefetch, so it is run a few times only, never in a loop.

import fs from 'node:fs';
import path from 'node:path';
import { OUT_DIR, launch, rafMedian, sleep, systemState, viewerUrl } from './lib.mjs';

const tag = process.argv[2] ?? 'deployed';
const { browser, page, requests, errors, consoleErrors } = await launch();
try {
  await page.goto('about:blank');
  const rafBefore = await rafMedian(page, 600);
  const state = systemState();
  await page.goto(viewerUrl({ t: 30 }), { waitUntil: 'commit' });
  await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 180000, polling: 50 });
  await page.evaluate(() => window.chronozarr.ready);
  await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 180000, polling: 50 });
  await sleep(1000);
  const started = Date.now();
  const results = await page.evaluate(() => window.chronozarr.bench());
  const wallMs = Date.now() - started;
  const record = { tag, at: new Date().toISOString(), wallMs, state, rafBeforeMs: rafBefore, results, errors, consoleErrors, requestsDuringBench: requests.length };
  fs.writeFileSync(path.join(OUT_DIR, `bench_gates_${tag}.json`), `${JSON.stringify(record, null, 1)}\n`);
  const r = results;
  console.log(JSON.stringify({ wallMs, coldOpenConsolidated: r.coldOpenConsolidated, coldOpenPerArrayMetadata: r.coldOpenPerArrayMetadata, warmSwitchStepping: r.warmSwitchStepping, warmSwitchJumping: r.warmSwitchJumping, warmSwitchUploadFromCache: r.warmSwitchUploadFromCache, productSwitch: r.productSwitch, decodePerChunk: r.decodePerChunk, prefetch: r.prefetch, cache: r.cache }, null, 1));
} finally {
  await browser.close();
}
