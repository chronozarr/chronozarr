// Summarise playback.jsonl: frame times and stalls of 60 s of movie playback, per run and pooled per mode.
//
//   node scripts/fieldperf/analyze_playback.mjs [--file playback]
//
// Frame time: gaps between consecutive requestAnimationFrame callbacks over the window (p50/p95/p99, share above 33 ms
// and above 50 ms). Displayed timestep: the first complete paint of each new timestep, from the viewer's paint events.
// Stall: wall time in which the displayed timestep did not advance when it was due. The schedule is the viewer's own:
// one step per interval (1000 / effective steps per second); a gap between two displayed steps longer than the interval
// plus one display frame counts the excess, as does the wait from the play() call to the first step beyond one interval.
// The playback object restarts its schedule after a buffering pause instead of catching up, so each stall counts once.
// stallRatio = stalled time / window. The viewer's own counters (buffering pauses and ms) are listed next to it.

import fs from 'node:fs';
import path from 'node:path';
import { OUT_DIR, quantile, readJsonl } from './lib.mjs';

const args = process.argv.slice(2);
const fileName = args.includes('--file') ? args[args.indexOf('--file') + 1] : 'playback';
const all = readJsonl(path.join(OUT_DIR, `${fileName}.jsonl`));
const runs = all.filter((r) => !r.error);

function analyze(run) {
  const { play } = run;
  const frames = play.frames;
  const deltas = frames.slice(1).map((x, i) => x - frames[i]);
  const sorted = [...deltas].sort((a, b) => a - b);
  const windowMs = frames.at(-1) - frames[0];
  const frameMs = quantile(sorted, 0.5);
  const interval = 1000 / play.effective;

  // First complete paint of each new timestep after play() was called.
  const steps = [];
  let lastT = play.startT;
  for (const [at, t, lod, complete] of play.paints) {
    if (at < play.tStart || !complete) continue;
    if (t !== lastT) {
      steps.push({ at, t, lod });
      lastT = t;
    }
  }
  const gaps = steps.slice(1).map((s, i) => s.at - steps[i].at);
  const tol = frameMs;
  const firstWait = steps.length ? steps[0].at - play.tStart : windowMs;
  const initialExcess = Math.max(0, firstWait - interval - tol);
  const excess = gaps.map((g) => Math.max(0, g - interval - tol));
  const lastStepAt = steps.length ? steps.at(-1).at : play.tStart;
  // Time after the last displayed step until the window closed also counts when it exceeds one interval.
  const tailExcess = Math.max(0, play.tStart + windowMs - lastStepAt - interval - tol);
  const stalledMs = initialExcess + excess.reduce((a, b) => a + b, 0) + tailExcess;
  const stallEvents = excess.filter((x) => x > 0).length + (initialExcess > 0 ? 1 : 0) + (tailExcess > 0 ? 1 : 0);
  const spanMs = steps.length > 1 ? steps.at(-1).at - steps[0].at : NaN;
  const lods = {};
  for (const s of steps) lods[s.lod] = (lods[s.lod] ?? 0) + 1;
  return {
    mode: run.mode,
    index: run.index,
    at: run.at,
    windowS: windowMs / 1000,
    frames: frames.length,
    frameMs: { p50: quantile(sorted, 0.5), p95: quantile(sorted, 0.95), p99: quantile(sorted, 0.99), max: sorted.at(-1) },
    shareOver33: deltas.filter((d) => d > 33).length / deltas.length,
    shareOver50: deltas.filter((d) => d > 50).length / deltas.length,
    stepsDisplayed: steps.length,
    stepsIdeal: Math.floor(windowMs / interval),
    achievedStepsPerSecond: Number.isFinite(spanMs) ? ((steps.length - 1) / spanMs) * 1000 : 0,
    requestedStepsPerSecond: run.requestedStepsPerSecond,
    effectiveStepsPerSecond: play.effective,
    refreshHz: play.refreshHz,
    firstStepAfterMs: firstWait,
    longestGapMs: gaps.length ? Math.max(...gaps) : NaN,
    stalledMs,
    stallEvents,
    stallRatio: stalledMs / windowMs,
    // The same without the wait before the first step (the viewer buffers two seconds of frames before it starts).
    stallRatioAfterStart: (stalledMs - initialExcess) / windowMs,
    initialExcessMs: initialExcess,
    viewer: { steps: play.stats.steps, bufferingPauses: play.stats.bufferingPauses, wrapPauses: play.stats.wrapPauses, bufferingMs: play.stats.bufferingMs, initialBufferMs: play.stats.initialBufferMs, longestBufferingMs: play.stats.longestBufferingMs },
    movie: { baseLod: play.movie1.baseLod, lod: play.movie1.lod, reason: play.movie1.reason },
    lodsShown: lods,
    longTasks: { n: play.longTasks.length, over100: play.longTasks.filter((x) => x[1] > 100).length, maxMs: Math.max(0, ...play.longTasks.map((x) => x[1])) },
    network: { requests: play.network.requests, MB: play.network.bytes / 1e6 },
    openToFirstFrameMs: run.open.nowMs,
    cf: run.cfCacheStatus,
    load1: run.state.load1,
    rafBeforeMs: run.rafBeforeMs,
  };
}

const perRun = runs.map(analyze);
const pooled = {};
for (const mode of [...new Set(runs.map((r) => r.mode))]) {
  const sub = runs.filter((r) => r.mode === mode);
  const deltas = sub.flatMap((r) => r.play.frames.slice(1).map((x, i) => x - r.play.frames[i])).sort((a, b) => a - b);
  const mine = perRun.filter((r) => r.mode === mode);
  const avg = (f) => mine.reduce((n, r) => n + f(r), 0) / mine.length;
  pooled[mode] = {
    runs: mine.length,
    frames: deltas.length,
    frameMs: { p50: quantile(deltas, 0.5), p95: quantile(deltas, 0.95), p99: quantile(deltas, 0.99), max: deltas.at(-1) },
    shareOver33: deltas.filter((d) => d > 33).length / deltas.length,
    shareOver50: deltas.filter((d) => d > 50).length / deltas.length,
    stallRatio: { mean: avg((r) => r.stallRatio), min: Math.min(...mine.map((r) => r.stallRatio)), max: Math.max(...mine.map((r) => r.stallRatio)) },
    achievedStepsPerSecond: { mean: avg((r) => r.achievedStepsPerSecond), min: Math.min(...mine.map((r) => r.achievedStepsPerSecond)), max: Math.max(...mine.map((r) => r.achievedStepsPerSecond)) },
  };
}
const out = { generatedAt: new Date().toISOString(), file: fileName, failedRuns: all.filter((r) => r.error).length, perRun, pooled };
fs.writeFileSync(path.join(OUT_DIR, `${fileName}_summary.json`), `${JSON.stringify(out, null, 1)}\n`);

const f = (x, d = 1) => (Number.isFinite(x) ? x.toFixed(d) : 'n/a');
console.log('| Mode | Run | rAF p50 | p95 | p99 | max (ms) | frames > 33 ms | stall ratio | stall ratio after first step | stall events | steps shown / ideal | achieved steps/s | viewer buffering pauses (ms) | movie level | MB | open->first frame (ms) | load1 |');
console.log('|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|');
for (const r of perRun) {
  console.log(`| ${r.mode} | ${r.index} | ${f(r.frameMs.p50)} | ${f(r.frameMs.p95)} | ${f(r.frameMs.p99)} | ${f(r.frameMs.max, 0)} | ${f(r.shareOver33 * 100, 2)} % | ${f(r.stallRatio * 100, 1)} % | ${f(r.stallRatioAfterStart * 100, 1)} % | ${r.stallEvents} | ${r.stepsDisplayed} / ${r.stepsIdeal} | ${f(r.achievedStepsPerSecond, 2)} | ${r.viewer.bufferingPauses} (${f(r.viewer.bufferingMs, 0)}) | ${r.movie.lod} (base ${r.movie.baseLod}) | ${f(r.network.MB)} | ${f(r.openToFirstFrameMs, 0)} | ${f(r.load1, 1)} |`);
}
console.log('\npooled:', JSON.stringify(pooled, null, 1));
