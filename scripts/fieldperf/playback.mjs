// 60 s of playback in the deployed viewer (https://chronozarr.org/demo/, bare visit), unthrottled or on a slow link.
//
//   node scripts/fieldperf/playback.mjs <unthrottled|throttled> [--runs 3] [--seconds 60] [--speed 4] [--file name]
//
// throttled = CDP Network.emulateNetworkConditions at 0.7 MB/s (700,000 B/s) down and up, 100 ms latency: a slow link
// measured on 2026-10-15. The emulation is on from the first request, so the page, its 61 modules and the first frame
// are fetched under it too; the 60 s window starts at the viewer's own first complete frame.
//
// In the page: requestAnimationFrame timestamps for the whole window, the viewer's paint events (viewer.probe: the
// timestep and level of every frame it draws), long tasks, the playback object's own counters (viewer.playback.stats)
// and the reader's network counters. Raw arrays go to OUT_DIR/<file>.jsonl; summarising is in analyze_playback.mjs.

import path from 'node:path';
import { OUT_DIR, SITE, appendJsonl, describeRequest, launch, rafMedian, sleep, systemState } from './lib.mjs';

const args = process.argv.slice(2);
const mode = args[0];
const opt = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const runs = Number(opt('runs', 3));
const seconds = Number(opt('seconds', 60));
const speed = Number(opt('speed', 4));
const FILE = path.join(OUT_DIR, `${opt('file', 'playback')}.jsonl`);
const THROTTLE = { bytesPerSecond: 700000, latencyMs: 100 };
if (!['unthrottled', 'throttled'].includes(mode)) {
  console.error('usage: playback.mjs unthrottled|throttled [options]');
  process.exit(2);
}

async function runOnce(index) {
  const { browser, page, cdp, requests, errors, consoleErrors } = await launch({ throttle: mode === 'throttled' ? THROTTLE : null });
  try {
    await page.goto('about:blank');
    const rafBefore = await rafMedian(page, 600);
    const state = systemState();
    const openStarted = Date.now();
    await page.goto(SITE, { waitUntil: 'commit' });
    await page.waitForFunction(() => window.chronozarr?.ready, null, { timeout: 180000, polling: 50 });
    const open = await page.evaluate(async () => {
      const result = await window.chronozarr.ready;
      return { result, nowMs: performance.now() };
    });
    await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 180000, polling: 50 });
    const openWallMs = Date.now() - openStarted;
    await sleep(500);

    const netAtPlay = requests.length;
    const play = await page.evaluate(
      async ({ durationMs, stepsPerSecond }) => {
        const v = window.chronozarr.viewer;
        v.setSpeed(stepsPerSecond);
        const frames = [];
        const paints = [];
        const longTasks = [];
        v.probe = (e) => {
          if (e.type === 'paint') paints.push([Math.round(e.at * 10) / 10, e.t, e.lod, e.kind === 'target' ? 1 : 0, e.fallback ? 1 : 0, e.cells]);
        };
        const observer = new PerformanceObserver((list) => longTasks.push(...list.getEntries().map((x) => [Math.round(x.startTime), Math.round(x.duration)])));
        try {
          observer.observe({ type: 'longtask' });
        } catch {
          // long task entries are optional
        }
        const net0 = { ...v.store.stats.network };
        const tStart = performance.now();
        const startT = v.t;
        const movie0 = v.movieInfo;
        let last = false;
        const done = new Promise((resolve) => {
          const onFrame = (now) => {
            frames.push(Math.round(now * 10) / 10);
            if (now - tStart < durationMs) requestAnimationFrame(onFrame);
            else {
              last = true;
              resolve();
            }
          };
          requestAnimationFrame(onFrame);
        });
        v.play();
        await done;
        const stats = { ...v.playback.stats };
        const movie1 = v.movieInfo;
        const refreshHz = v.playback.refreshHz;
        const effective = v.playback.effectiveStepsPerSecond;
        const stillPlaying = v.playback.playing;
        const buffering = v.playback.buffering;
        const endT = v.t;
        v.pause();
        v.probe = null;
        observer.disconnect();
        const net1 = { ...v.store.stats.network };
        return {
          tStart,
          startT,
          endT,
          frames,
          paints,
          longTasks,
          stats,
          movie0,
          movie1,
          refreshHz,
          effective,
          stillPlaying,
          buffering,
          network: { requests: net1.requests - net0.requests, bytes: net1.bytes - net0.bytes },
          cache: v.store.cacheInfo(),
          last,
        };
      },
      { durationMs: seconds * 1000, stepsPerSecond: speed },
    );
    await sleep(300);
    const t0Ts = requests[0].requestTs;
    const dataRequests = requests.slice(netAtPlay).filter((r) => r.host !== 'chronozarr.org').map((r) => describeRequest(r, t0Ts));
    const cf = {};
    for (const r of requests) if (r.url.startsWith('https://data.chronozarr.org/')) cf[r.cfCacheStatus ?? 'none'] = (cf[r.cfCacheStatus ?? 'none'] ?? 0) + 1;
    const record = {
      mode,
      index,
      at: new Date().toISOString(),
      throttle: mode === 'throttled' ? THROTTLE : null,
      seconds,
      requestedStepsPerSecond: speed,
      open: { ...open, wallMs: openWallMs },
      rafBeforeMs: rafBefore,
      state,
      play,
      playRequests: { n: dataRequests.length, failed: dataRequests.filter((r) => r.failed).length, canceled: dataRequests.filter((r) => r.canceled).length },
      cfCacheStatus: cf,
      errors,
      consoleErrors,
    };
    appendJsonl(FILE, record);
    const deltas = play.frames.slice(1).map((x, i) => x - play.frames[i]).sort((a, b) => a - b);
    const q = (p) => deltas[Math.min(deltas.length - 1, Math.floor(p * deltas.length))];
    console.log(`${mode} #${index}: open ${Math.round(open.nowMs)} ms; frames ${play.frames.length}, rAF p50 ${q(0.5).toFixed(1)} p95 ${q(0.95).toFixed(1)} p99 ${q(0.99).toFixed(1)} max ${deltas.at(-1).toFixed(0)} ms; steps ${play.stats.steps}, buffering pauses ${play.stats.bufferingPauses} (${Math.round(play.stats.bufferingMs)} ms), initial buffer ${Math.round(play.stats.initialBufferMs ?? -1)} ms; ${play.network.requests} req, ${(play.network.bytes / 1e6).toFixed(1)} MB; movie lod ${play.movie1.lod} (base ${play.movie1.baseLod}, ${play.movie1.reason}); load ${state.load1}`);
    return record;
  } finally {
    await cdp.detach().catch(() => {});
    await browser.close();
  }
}

for (let i = 0; i < runs; i++) {
  try {
    await runOnce(i);
  } catch (error) {
    console.error(`${mode} #${i} failed: ${String(error.message).slice(0, 300)}`);
    appendJsonl(FILE, { mode, index: i, at: new Date().toISOString(), error: String(error.message).slice(0, 500) });
  }
  await sleep(5000);
}
console.log('done');
