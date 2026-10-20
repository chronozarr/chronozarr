// 60 s of timestep stepping in the viewer, the way a held arrow key or a slider drag does, and what it fetches.
//
//   node scripts/fieldperf/scrub.mjs [--runs 3] [--seconds 60] [--period 250] [--t0 0] [--idle 1500] [--mbs 0 --latency 30] [--file name]
//
// One new Chromium process per run, HTTP cache disabled over CDP (scripts/fieldperf/lib.mjs). The viewer opens at ?t=<t0>; the
// `idle` ms (1.5 s) after its first complete frame are a window of their own (what it fetches by itself), then goToTime(t) is called every
// `period` ms (250 ms: 4 steps/s), ping-pong over the whole time axis starting at t0 going up. The viewer's paint events say
// which timestep and level every frame drew. --mbs N limits the link to N MB/s down and up (CDP), with --latency ms added.
//
// Per run: the wire bytes of the store requests in the 1.5 s window and in the scrub window (by kind and level), the viewer's own
// network counters, the steps drawn and how soon (a frame counts for a step only when drawn within 1 s of the request), the
// levels drawn, rAF gaps and long tasks. Raw rows go to OUT_DIR/<file>.jsonl.

import path from 'node:path';
import { OUT_DIR, appendJsonl, describeRequest, launch, quantile, rafMedian, sleep, systemState, viewerUrl, wireSummary } from './lib.mjs';

const args = process.argv.slice(2);
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const runs = Number(opt('runs', 3));
const seconds = Number(opt('seconds', 60));
const period = Number(opt('period', 250));
const t0 = Number(opt('t0', 0));
const mbs = Number(opt('mbs', 0));
const throttle = mbs > 0 ? { bytesPerSecond: mbs * 1e6, latencyMs: Number(opt('latency', 30)) } : null;
const FILE = path.join(OUT_DIR, `${opt('file', 'scrub')}.jsonl`);
const IDLE_MS = Number(opt('idle', 1500));
// A frame counts for a step only if it was drawn within this long of the request: on the way back a timestep comes round again.
const WINDOW_MS = 1000;

const PAGE_SCRUB = ({ from, periodMs, durationMs }) =>
  new Promise((resolve) => {
    const v = window.chronozarr.viewer;
    const hi = v.store.times.length - 1;
    const steps = [];
    const paints = [];
    v.probe = (e) => {
      if (e.type === 'paint') paints.push([e.at, e.t, e.lod, e.targetLod, e.kind === 'target' ? 1 : 0, e.fallback ? 1 : 0]);
    };
    let t = from;
    let dir = 1;
    let n = 0;
    const startedAt = performance.now();
    const net0 = { ...v.store.stats.network };
    const tick = () => {
      const now = performance.now();
      if (now - startedAt >= durationMs) {
        v.probe = null;
        resolve({ startedAt, endedAt: now, steps, paints, net: { requests: v.store.stats.network.requests - net0.requests, bytes: v.store.stats.network.bytes - net0.bytes }, cache: v.store.cacheInfo() });
        return;
      }
      steps.push({ t, requestedAt: now });
      v.goToTime(t);
      if (t + dir > hi || t + dir < 0) dir = -dir;
      t += dir;
      n++;
      setTimeout(tick, Math.max(0, startedAt + n * periodMs - performance.now()));
    };
    tick();
  });

function stepMetrics(result) {
  const { steps, paints } = result;
  const rows = steps.map((step, i) => {
    const nextAt = steps[i + 1]?.requestedAt ?? Infinity;
    const mine = paints.filter((p) => p[1] === step.t && p[0] >= step.requestedAt && p[0] < step.requestedAt + WINDOW_MS);
    const any = mine[0];
    const target = mine.find((p) => p[4] === 1 && p[2] === p[3]);
    return { t: step.t, anyMs: any ? any[0] - step.requestedAt : null, targetMs: target ? target[0] - step.requestedAt : null, level: (target ?? any)?.[2] ?? null, before: Boolean(any && any[0] < nextAt) };
  });
  const levels = {};
  for (const r of rows) if (r.level !== null) levels[r.level] = (levels[r.level] ?? 0) + 1;
  const latencies = rows.filter((r) => r.anyMs !== null).map((r) => r.anyMs).sort((a, b) => a - b);
  return {
    steps: rows.length,
    drawn: rows.filter((r) => r.anyMs !== null).length,
    drawnBeforeNext: rows.filter((r) => r.before).length,
    drawnAtTarget: rows.filter((r) => r.targetMs !== null).length,
    latencyMs: { p50: quantile(latencies, 0.5), p95: quantile(latencies, 0.95) },
    levels,
  };
}

async function runOnce(index) {
  const { browser, page, cdp, requests, errors, consoleErrors } = await launch({ throttle });
  try {
    await page.goto('about:blank');
    const rafBefore = await rafMedian(page, 600);
    const state = systemState();
    await page.goto(viewerUrl({ t: t0 }), { waitUntil: 'commit' });
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 300000, polling: 25 });
    const first = await page.evaluate(async () => {
      const result = await window.chronozarr.ready;
      const now = performance.now();
      return { result, nowMs: now, epochMs: performance.timeOrigin + now };
    });
    await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 300000, polling: 25 });
    await sleep(Math.max(0, first.epochMs + IDLE_MS - Date.now()));
    await page.evaluate(`window.__scrub = ${PAGE_SCRUB.toString()};`);
    const lod = await page.evaluate(() => window.chronozarr.viewer.movieInfo.baseLod);
    const startS = Date.now() / 1000;
    const out = await page.evaluate(
      async (opts) => {
        const rafs = [];
        const longTasks = [];
        const observer = new PerformanceObserver((list) => longTasks.push(...list.getEntries().map((x) => [Math.round(x.startTime), Math.round(x.duration)])));
        try {
          observer.observe({ type: 'longtask' });
        } catch {
          // long task entries are optional
        }
        let running = true;
        const loop = (now) => {
          rafs.push(Math.round(now * 10) / 10);
          if (running) requestAnimationFrame(loop);
        };
        requestAnimationFrame(loop);
        const result = await window.__scrub(opts);
        running = false;
        await new Promise((resolve) => setTimeout(resolve, 600));
        observer.disconnect();
        return { result, rafs, longTasks };
      },
      { from: t0, periodMs: period, durationMs: seconds * 1000 },
    );
    const endS = Date.now() / 1000;
    await sleep(300);
    const t0Ts = requests[0].requestTs;
    const described = requests.map((r) => describeRequest(r, t0Ts));
    const firstS = first.epochMs / 1000;
    const gaps = out.rafs.slice(1).map((x, i) => x - out.rafs[i]).sort((a, b) => a - b);
    const metrics = stepMetrics(out.result);
    const record = {
      index,
      at: new Date().toISOString(),
      throttle,
      seconds,
      period,
      t0,
      state,
      rafBeforeMs: rafBefore,
      baseLod: lod,
      firstFrameMs: first.nowMs,
      idle: { ms: IDLE_MS, ...wireSummary(described, firstS, firstS + IDLE_MS / 1000) },
      scrubWire: wireSummary(described, startS, endS),
      viewerNetwork: out.result.net,
      cache: out.result.cache,
      metrics,
      rafGapMs: { n: gaps.length, p50: quantile(gaps, 0.5), p95: quantile(gaps, 0.95), max: gaps.at(-1) },
      longTasks: { n: out.longTasks.length, maxMs: Math.max(0, ...out.longTasks.map((x) => x[1])) },
      errors,
      consoleErrors,
    };
    appendJsonl(FILE, record);
    const mb = (n) => (n / 1e6).toFixed(0);
    console.log(
      `scrub #${index}: first frame ${Math.round(first.nowMs)} ms; idle ${mb(record.idle.bytes)} MB ${record.idle.requests} req (data ${JSON.stringify(Object.fromEntries(Object.entries(record.idle.data.byLevel).map(([k, v]) => [k, v.n])))}); scrub ${mb(record.scrubWire.bytes)} MB ${record.scrubWire.requests} req (${record.scrubWire.aborted} aborted, viewer counter ${mb(record.viewerNetwork.bytes)} MB); drawn ${metrics.drawn}/${metrics.steps}, before next ${metrics.drawnBeforeNext}, latency p50 ${metrics.latencyMs.p50?.toFixed(0)} ms, levels ${JSON.stringify(metrics.levels)}; rAF p95 ${record.rafGapMs.p95.toFixed(1)} max ${record.rafGapMs.max.toFixed(0)}; load ${state.load1}`,
    );
  } finally {
    await cdp.detach().catch(() => {});
    await browser.close();
  }
}

for (let i = 0; i < runs; i++) {
  try {
    await runOnce(i);
  } catch (error) {
    console.error(`scrub #${i} failed: ${String(error.message).slice(0, 300)}`);
    appendJsonl(FILE, { index: i, at: new Date().toISOString(), error: String(error.message).slice(0, 500) });
  }
  await sleep(3000);
}
console.log('done');
