// N cold and N warm opens of the deployed viewer (https://chronozarr.org/demo/?t=<t>) from this machine.
//
//   node scripts/fieldperf/browser_opens.mjs [--n 30] [--seed 20261016] [--pause 4000] [--file name] [--exclude-t-dir <date dir>] [--forced-cold]
//
// For each of N distinct timesteps t (a seeded random draw from the 117) it opens the viewer twice, each time in a new
// Chromium process (empty DNS/TLS/HTTP caches, the HTTP cache disabled over CDP): first at ?t=<t>, which is likely a
// cold edge for that timestep's chunks, then again at the same address, when the edge holds them. The intent is only a
// plan: each run records the cf-cache-status of every data response, and the analysis labels runs by it.
// Time to first frame is the viewer's own marks: ready.coarseMs (first frame covering the view, from the loadStore call),
// ready.firstPaintMs (complete frame at the target level, from the call) and the page clock at the moment `ready` resolves
// (from the start of navigation). Appends one JSON line per run to OUT_DIR/browser_opens.jsonl; resumes where it stopped.

import path from 'node:path';
import { DATA_ROOT, OUT_DIR, STORE, appendJsonl, describeRequest, launch, rafMedian, readJsonl, rng, sleep, systemState, viewerUrl, wireSummary } from './lib.mjs';

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const N = Number(opt('n', 30));
const seed = Number(opt('seed', 20261016));
const excludeDir = opt('exclude-t-dir', null);
// --forced-cold: open ?store=<store>?cb=<unique>&t=<t>, so every object (root metadata and chunks) is a miss at the edge. Cold runs only.
const forcedCold = args.includes('--forced-cold');
// --retry-warm: for every timestep with a cold-labelled run (no level-1 chunk a HIT) and no warm-labelled one (all nine HIT), open it
// again, up to 4 more times. Cloudflare answers from different PoPs on different connections (IAD or EWR on 2026-10-18), so the
// second open often lands on a PoP that does not hold the chunks yet.
const retryWarm = args.includes('--retry-warm');
const pauseMs = Number(opt('pause', 4000));
const FILE = path.join(OUT_DIR, `${opt('file', 'browser_opens')}.jsonl`);
const N_TIME = 117;
const AFTER_FRAME_MS = 1500;

function drawTimes(n, rand) {
  // Timesteps already opened on an earlier date are probably still in the edge cache there: leave them out of the draw.
  const used = new Set(excludeDir ? readJsonl(path.join(DATA_ROOT, excludeDir, 'browser_opens.jsonl')).filter((r) => !r.error).map((r) => r.t) : []);
  const pool = Array.from({ length: N_TIME }, (_, i) => i).filter((t) => !used.has(t));
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  return pool.slice(0, n);
}

async function openOnce(t, intent, index) {
  const { browser, page, requests, errors, consoleErrors } = await launch();
  try {
    await page.goto('about:blank');
    const rafBefore = await rafMedian(page, 600);
    const state = systemState();
    const cb = forcedCold ? `?cb=fp${Date.now().toString(36)}${Math.floor(Math.random() * 1e9).toString(36)}` : '';
    const url = forcedCold ? viewerUrl({ t, store: STORE + cb }) : viewerUrl({ t });
    await page.goto(url, { waitUntil: 'commit' });
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 120000, polling: 25 });
    const done = await page.evaluate(async () => {
      const result = await window.chronozarr.ready;
      const now = performance.now();
      return { result, nowMs: now, epochMs: performance.timeOrigin + now };
    });
    await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 120000, polling: 25 });
    const after = await page.evaluate(() => {
      const v = window.chronozarr.viewer;
      return { shownT: v.t, paintedT: v.paintedT, net: { ...v.store.stats.network }, viewTiming: v.viewTiming };
    });
    // What the viewer fetches by itself after the first frame: the wire bytes of the store requests that start in the next AFTER_FRAME_MS.
    await sleep(Math.max(0, done.epochMs + AFTER_FRAME_MS + 200 - Date.now()));
    await sleep(500);
    const rafAfter = await rafMedian(page, 500);
    const t0Ts = requests[0].requestTs;
    const firstFrameEpochS = done.epochMs / 1000;
    const described = requests.map((r) => ({ ...describeRequest(r, t0Ts), beforeFirstFrame: r.wallTime <= firstFrameEpochS }));
    const data = described.filter((r) => r.url.startsWith(STORE) && r.beforeFirstFrame);
    const chunks = data.filter((r) => r.path.includes('/data/c/'));
    const level1 = chunks.filter((r) => r.path.includes('/1/data/c/'));
    const record = {
      index,
      t,
      intent,
      at: new Date().toISOString(),
      url,
      ready: done.result,
      firstFramePageMs: done.nowMs,
      loadStoreStartPageMs: done.nowMs - done.result.firstPaintMs,
      coarseFramePageMs: done.nowMs - done.result.firstPaintMs + (done.result.coarseMs ?? NaN),
      viewTiming: after.viewTiming,
      shownT: after.shownT,
      viewerNetwork: after.net,
      rafBeforeMs: rafBefore,
      rafAfterMs: rafAfter,
      state,
      afterFirstFrame: { ms: AFTER_FRAME_MS, ...wireSummary(described, firstFrameEpochS, firstFrameEpochS + AFTER_FRAME_MS / 1000) },
      beforeFirstFrame: {
        requests: described.filter((r) => r.beforeFirstFrame).length,
        bytes: described.filter((r) => r.beforeFirstFrame).reduce((n, r) => n + (r.encodedDataLength ?? 0), 0),
        dataRequests: data.length,
        dataBytes: data.reduce((n, r) => n + (r.encodedDataLength ?? 0), 0),
        level1Chunks: level1.length,
        level1Bytes: level1.reduce((n, r) => n + (r.encodedDataLength ?? 0), 0),
        level1Hit: level1.filter((r) => r.cfCacheStatus === 'HIT').length,
        dataCf: data.reduce((m, r) => ((m[r.cfCacheStatus ?? 'none'] = (m[r.cfCacheStatus ?? 'none'] ?? 0) + 1), m), {}),
        level1Cf: level1.reduce((m, r) => ((m[r.cfCacheStatus ?? 'none'] = (m[r.cfCacheStatus ?? 'none'] ?? 0) + 1), m), {}),
        colos: [...new Set(described.map((r) => r.colo).filter(Boolean))],
      },
      requests: described,
      errors,
      consoleErrors,
    };
    appendJsonl(FILE, record);
    const b = record.beforeFirstFrame;
    console.log(`#${index} t=${t} ${intent}: coarse ${Math.round(done.result.coarseMs)} ms, target ${Math.round(done.result.firstPaintMs)} ms, nav->frame ${Math.round(done.nowMs)} ms, L1 HIT ${b.level1Hit}/${b.level1Chunks}, data ${(b.dataBytes / 1e6).toFixed(1)} MB, load ${state.load1}`);
    return record;
  } finally {
    await browser.close();
  }
}

const done = readJsonl(FILE);
if (retryWarm) {
  const hits = (r) => r.beforeFirstFrame?.level1Hit;
  const need = [...new Set(done.filter((r) => !r.error && hits(r) === 0).map((r) => r.t))].filter((t) => !done.some((r) => !r.error && r.t === t && hits(r) === 9));
  console.log(`retry-warm: ${need.length} timesteps have a cold run and no warm run`);
  for (const t of need) {
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const rec = await openOnce(t, 'warm-retry', done.find((r) => r.t === t).index);
        if (hits(rec) === 9) break;
      } catch (error) {
        console.error(`t=${t} warm-retry failed: ${String(error.message).slice(0, 200)}`);
      }
      await sleep(pauseMs);
    }
  }
  console.log('done');
  process.exit(0);
}
const times = drawTimes(N, rng(seed));
for (let i = 0; i < times.length; i++) {
  const t = times[i];
  for (const intent of forcedCold ? ['forced-cold'] : ['cold', 'warm']) {
    if (done.some((r) => r.t === t && r.intent === intent && !r.error)) continue;
    try {
      await openOnce(t, intent, i);
    } catch (error) {
      console.error(`#${i} t=${t} ${intent} failed: ${String(error.message).slice(0, 200)}`);
      appendJsonl(FILE, { index: i, t, intent, at: new Date().toISOString(), error: String(error.message).slice(0, 500) });
    }
    await sleep(pauseMs);
  }
}
console.log('done');
