// Re-measure the public demo (https://chronozarr.org/demo/) from headless Chromium on this machine.
// Read-only: it only GETs the demo and the public store. Output: one JSON line per run on stdout and in OUT.
//
//   node live/measure.mjs open-warm   N     cold browser, edge cache as it is (normally HIT)
//   node live/measure.mjs open-cold   N     cold browser and cold edge: a unique ?cb= query on the store URL
//   node live/measure.mjs play        N     playBench at 60/s warm after prefetch (4-cell level 2, then the 9-cell overview)
//   node live/measure.mjs play-cold   N     playBench at 10/s cold on the overview
//
// Method follows bench/v03 and the scrub A/B harness: Playwright chromium (channel 'chromium'), --use-angle=metal,
// 1440x900 at DPR 2, a new browser for every run, the HTTP cache disabled over CDP. Not the Claude app browser pane.

import fs from 'node:fs';
import os from 'node:os';
import { chromium } from 'playwright';

const mode = process.argv[2];
const runs = Number(process.argv[3] ?? 5);
const OUT = process.env.OUT ?? `live_${mode}.jsonl`;
const BASE = 'https://chronozarr.org/demo/';
const STORE = 'https://data.chronozarr.org/ucayali_santa_maria_v03';

function rafMedian(page, ms = 1500) {
  return page.evaluate(
    (duration) =>
      new Promise((resolve) => {
        const gaps = [];
        let last = performance.now();
        const start = last;
        const tick = (now) => {
          gaps.push(now - last);
          last = now;
          if (now - start < duration) requestAnimationFrame(tick);
          else {
            gaps.sort((a, b) => a - b);
            resolve(gaps[Math.floor(gaps.length / 2)]);
          }
        };
        requestAnimationFrame(tick);
      }),
    ms,
  );
}

async function launch() {
  const browser = await chromium.launch({ channel: 'chromium', headless: true, args: ['--ignore-gpu-blocklist', '--use-angle=metal'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2, serviceWorkers: 'block' });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.setCacheDisabled', { cacheDisabled: true });
  const cf = {};
  let responses = 0;
  page.on('response', (response) => {
    const url = response.url();
    if (!url.startsWith('https://data.chronozarr.org/')) return;
    responses += 1;
    const status = response.headers()['cf-cache-status'] ?? 'none';
    cf[status] = (cf[status] ?? 0) + 1;
  });
  const errors = [];
  page.on('pageerror', (error) => errors.push(String(error.message).slice(0, 200)));
  return { browser, page, cf, errors, responses: () => responses };
}

function record(obj) {
  const line = JSON.stringify(obj);
  fs.appendFileSync(OUT, `${line}\n`);
  console.log(line);
}

async function openRun(index, coldEdge) {
  const { browser, page, cf, errors } = await launch();
  try {
    const rafBefore = await (async () => {
      await page.goto('about:blank');
      return rafMedian(page, 800);
    })();
    const cb = coldEdge ? `?cb=${Date.now()}${Math.random().toString(36).slice(2, 8)}` : '';
    const url = coldEdge ? `${BASE}?store=${encodeURIComponent(STORE + cb)}` : BASE;
    const t0 = Date.now();
    await page.goto(url, { waitUntil: 'commit' });
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 60000 });
    const open = await page.evaluate(() => window.chronozarr.ready);
    // wait for the complete frame at the target level, then read timings
    await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 60000 });
    const timing = await page.evaluate(() => {
      const v = window.chronozarr.viewer;
      return { viewTiming: v.viewTiming, net: v.store.stats.network, lod: v.shownFrame ?? null, times: v.store.times.length };
    });
    const rafAfter = await rafMedian(page, 800);
    record({
      mode: coldEdge ? 'open-cold-edge' : 'open-warm-edge',
      index,
      at: new Date().toISOString(),
      wallMs: Date.now() - t0,
      open: open && typeof open === 'object' ? { openMs: open.openMs, firstPaintMs: open.firstPaintMs, coarseMs: open.coarseMs, coarseAfterMetadataMs: open.coarseAfterMetadataMs } : String(open),
      ...timing,
      cfCacheStatus: cf,
      rafBeforeMs: rafBefore,
      rafAfterMs: rafAfter,
      load1: os.loadavg()[0],
      errors,
    });
  } finally {
    await browser.close();
  }
}

async function playRun(index, spec) {
  const { browser, page, cf, errors } = await launch();
  try {
    await page.goto(BASE, { waitUntil: 'commit' });
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 60000 });
    await page.evaluate(() => window.chronozarr.ready);
    await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 60000 });
    const rafBefore = await rafMedian(page, 800);
    const result = await page.evaluate((options) => window.chronozarr.playBench(options), spec.options);
    const rafAfter = await rafMedian(page, 800);
    record({
      mode: spec.name,
      index,
      at: new Date().toISOString(),
      options: spec.options,
      result,
      cfCacheStatus: cf,
      rafBeforeMs: rafBefore,
      rafAfterMs: rafAfter,
      load1: os.loadavg()[0],
      errors,
    });
  } finally {
    await browser.close();
  }
}

const PLAY = {
  play: [
    { name: 'play60-warm-lod2-4cells', options: { stepsPerSecond: 60, loops: 2, lod: 2, warm: 'prefetch' } },
    { name: 'play60-warm-overview', options: { stepsPerSecond: 60, loops: 2, warm: 'prefetch' } },
  ],
  'play-cold': [{ name: 'play10-cold-overview', options: { stepsPerSecond: 10, loops: 1, cold: true } }],
};

for (let i = 0; i < runs; i += 1) {
  try {
    if (mode === 'open-both') {
      await openRun(i, false);
      await openRun(i, true);
    } else if (mode === 'open-warm') await openRun(i, false);
    else if (mode === 'open-cold') await openRun(i, true);
    else if (PLAY[mode]) for (const spec of PLAY[mode]) await playRun(i, spec);
    else throw new Error(`unknown mode ${mode}`);
  } catch (error) {
    record({ mode, index: i, error: String(error.message ?? error).slice(0, 500) });
  }
}
