// One recorded cold open of the deployed viewer (bare https://chronozarr.org/demo/, HTTP cache disabled, new browser),
// with every request, its size and its timing, split at the first complete frame.
//
//   node scripts/fieldperf/cold_open_record.mjs [tag]
//
// Output: OUT_DIR/cold_open_<tag>.json (the whole record) and a printed summary.

import fs from 'node:fs';
import path from 'node:path';
import { OUT_DIR, SITE, describeRequest, launch, rafMedian, sleep, systemState } from './lib.mjs';

const tag = process.argv[2] ?? 'bare';
const url = process.argv[3] ?? SITE;
const TAIL_MS = 3000;

const { browser, page, requests, errors, consoleErrors } = await launch();
try {
  await page.goto('about:blank');
  const rafBefore = await rafMedian(page, 800);
  const state = systemState();
  await page.goto(url, { waitUntil: 'commit' });
  await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 90000, polling: 25 });
  // In the page: the instant `ready` resolves, on the page clock, plus the clock origin, so it can be placed among the requests.
  const done = await page.evaluate(async () => {
    const result = await window.chronozarr.ready;
    const now = performance.now();
    return { result, nowMs: now, epochMs: performance.timeOrigin + now };
  });
  await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 90000, polling: 25 });
  const after = await page.evaluate(() => {
    const v = window.chronozarr.viewer;
    const nav = performance.getEntriesByType('navigation')[0];
    return {
      viewTiming: v.viewTiming,
      net: { ...v.store.stats.network },
      times: v.store.times.length,
      levels: v.store.levels.map((l) => ({ lod: l.lod, width: l.width, height: l.height, gridRows: l.gridRows, gridCols: l.gridCols })),
      t: v.t,
      canvas: { w: v.canvas.width, h: v.canvas.height },
      nav: nav ? { domContentLoadedMs: nav.domContentLoadedEventEnd, loadMs: nav.loadEventEnd, responseEndMs: nav.responseEnd } : null,
    };
  });
  await sleep(TAIL_MS);
  const rafAfter = await rafMedian(page, 800);

  const first = requests[0];
  const t0Ts = first.requestTs;
  const t0Wall = first.wallTime; // epoch seconds of the navigation request
  const firstFrameEpochS = done.epochMs / 1000;
  const described = requests.map((r) => ({ ...describeRequest(r, t0Ts), beforeFirstFrame: r.wallTime <= firstFrameEpochS }));
  const record = {
    tag,
    url,
    at: new Date().toISOString(),
    ready: done.result,
    firstFramePageMs: done.nowMs,
    viewTiming: after.viewTiming,
    store: { times: after.times, levels: after.levels, t: after.t, canvas: after.canvas },
    viewerNetwork: after.net,
    nav: after.nav,
    rafBeforeMs: rafBefore,
    rafAfterMs: rafAfter,
    state,
    navWallTime: t0Wall,
    requests: described,
    errors,
    consoleErrors,
  };
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const file = path.join(OUT_DIR, `cold_open_${tag}.json`);
  fs.writeFileSync(file, `${JSON.stringify(record, null, 1)}\n`);

  const sum = (rows, key) => rows.reduce((n, r) => n + (r[key] ?? 0), 0);
  const pre = described.filter((r) => r.beforeFirstFrame);
  const byHost = (rows) => {
    const m = {};
    for (const r of rows) {
      const k = `${r.host} ${r.type}`;
      m[k] ??= { n: 0, bytes: 0 };
      m[k].n += 1;
      m[k].bytes += r.encodedDataLength ?? 0;
    }
    return m;
  };
  console.log(JSON.stringify({ file, ready: done.result, firstFramePageMs: done.nowMs, viewTiming: after.viewTiming, levels: after.levels, t: after.t, nav: after.nav, requestsBeforeFirstFrame: pre.length, requestsTotal: described.length, bytesBeforeFirstFrame: sum(pre, 'encodedDataLength'), bytesTotal: sum(described, 'encodedDataLength'), byHostBefore: byHost(pre), byHostAll: byHost(described), cf: described.reduce((m, r) => ((m[r.cfCacheStatus ?? 'none'] = (m[r.cfCacheStatus ?? 'none'] ?? 0) + 1), m), {}), colos: [...new Set(described.map((r) => r.colo).filter(Boolean))], errors, consoleErrors }, null, 1));
} finally {
  await browser.close();
}
