// How the cold open of the deployed viewer scales with round-trip time, measured with CDP latency emulation (no bandwidth cap).
//
//   node scripts/fieldperf/latency_sweep.mjs [--runs 5] [--latencies 0,50,100,200,400]
//
// Network.emulateNetworkConditions delays every request by `latency` ms (it is a per-request delay, not a per-packet one, so TCP,
// TLS and QUIC handshakes are not stretched). The slope of a frame time on the added latency is therefore the number of sequential
// requests in front of that frame. Bare visit at the default timestep (t = 116, an edge that normally holds it). The runs of the
// different latencies are interleaved. Output: OUT_DIR/latency_sweep.jsonl and a printed least-squares fit.

import path from 'node:path';
import { OUT_DIR, SITE, appendJsonl, describeRequest, launch, rafMedian, readJsonl, sleep, systemState } from './lib.mjs';

const args = process.argv.slice(2);
const opt = (name, fallback) => (args.includes(`--${name}`) ? args[args.indexOf(`--${name}`) + 1] : fallback);
const runs = Number(opt('runs', 5));
const latencies = opt('latencies', '0,50,100,200,400').split(',').map(Number);
const FILE = path.join(OUT_DIR, 'latency_sweep.jsonl');

async function once(latencyMs, index) {
  // latency 0 is a plain browser; the others get the emulation with no throughput cap (-1).
  const { browser, page, requests, cdp } = await launch({ throttle: latencyMs > 0 ? { latencyMs, bytesPerSecond: -1 } : null });
  try {
    await page.goto('about:blank');
    const raf = await rafMedian(page, 400);
    const state = systemState();
    await page.goto(SITE, { waitUntil: 'commit' });
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 120000, polling: 25 });
    const done = await page.evaluate(async () => {
      const result = await window.chronozarr.ready;
      return { result, nowMs: performance.now() };
    });
    const t0 = requests[0].requestTs;
    const cat = requests.find((q) => q.url.endsWith('catalog.json'));
    const zj = requests.find((q) => q.url.split('?')[0].endsWith('/zarr.json') && q.url.includes('data.chronozarr.org'));
    const d = (q) => (q ? describeRequest(q, t0) : null);
    const record = {
      index,
      latencyMs,
      at: new Date().toISOString(),
      coarseFromCallMs: done.result.coarseMs,
      targetFromCallMs: done.result.firstPaintMs,
      targetPageMs: done.nowMs,
      coarsePageMs: done.nowMs - done.result.firstPaintMs + done.result.coarseMs,
      bootMs: cat ? (cat.requestTs - t0) * 1000 : null,
      metaFinishMs: d(zj)?.finishMs ?? null,
      cf: requests.filter((q) => q.url.startsWith('https://data.chronozarr.org/') && q.status).reduce((m, q) => ((m[q.cfCacheStatus ?? 'none'] = (m[q.cfCacheStatus ?? 'none'] ?? 0) + 1), m), {}),
      rafMs: raf,
      load1: state.load1,
    };
    appendJsonl(FILE, record);
    console.log(`latency ${latencyMs} #${index}: boot ${Math.round(record.bootMs)} ms, coarse (nav) ${Math.round(record.coarsePageMs)} ms, target (nav) ${Math.round(record.targetPageMs)} ms`);
  } finally {
    await cdp.detach().catch(() => {});
    await browser.close();
  }
}

const have = readJsonl(FILE);
for (let i = 0; i < runs; i++) {
  for (const L of latencies) {
    if (have.some((r) => r.latencyMs === L && r.index === i)) continue;
    try {
      await once(L, i);
    } catch (error) {
      console.error(`latency ${L} #${i} failed: ${String(error.message).slice(0, 200)}`);
    }
    await sleep(1500);
  }
}

// least squares of each time on the added latency
const all = readJsonl(FILE);
const fit = (key) => {
  const xs = all.map((r) => r.latencyMs);
  const ys = all.map((r) => r[key]);
  const mx = xs.reduce((a, b) => a + b, 0) / xs.length;
  const my = ys.reduce((a, b) => a + b, 0) / ys.length;
  const slope = xs.reduce((a, x, i) => a + (x - mx) * (ys[i] - my), 0) / xs.reduce((a, x) => a + (x - mx) ** 2, 0);
  const ssr = ys.reduce((a, y, i) => a + (y - (my + slope * (xs[i] - mx))) ** 2, 0);
  const sst = ys.reduce((a, y) => a + (y - my) ** 2, 0);
  return { slope: Number(slope.toFixed(2)), intercept: Math.round(my - slope * mx), r2: Number((1 - ssr / sst).toFixed(3)) };
};
console.log(JSON.stringify({ n: all.length, boot: fit('bootMs'), metaFinish: fit('metaFinishMs'), coarsePage: fit('coarsePageMs'), targetPage: fit('targetPageMs') }));
