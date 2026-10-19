// Summarise browser_opens.jsonl: time to first frame of the deployed viewer from this machine, cold and warm edge.
// A run is labelled by the cf-cache-status of the nine level-1 chunk responses it read (and the level-3 chunk, the
// root zarr.json): cold = none of the level-1 chunks was a HIT, warm = all nine were, mixed = anything between.
//
//   node scripts/fieldperf/analyze_opens.mjs [--md] [--file browser_opens]

import fs from 'node:fs';
import path from 'node:path';
import { OUT_DIR, readJsonl, summarize } from './lib.mjs';

const args = process.argv.slice(2);
const md = args.includes('--md');
const fileName = args.includes('--file') ? args[args.indexOf('--file') + 1] : 'browser_opens';
const runs = readJsonl(path.join(OUT_DIR, `${fileName}.jsonl`)).filter((r) => !r.error);
const failed = readJsonl(path.join(OUT_DIR, `${fileName}.jsonl`)).filter((r) => r.error);

function label(r) {
  const b = r.beforeFirstFrame;
  if (b.level1Chunks === 0) return 'none';
  if (b.level1Hit === 0) return 'cold';
  if (b.level1Hit === b.level1Chunks) return 'warm';
  return 'mixed';
}

/** Throughput of the level-1 batch as the browser saw it: bytes over the span from the first request start to the last finish. */
function batchThroughput(r) {
  const l1 = r.requests.filter((q) => q.host === 'data.chronozarr.org' && q.path.includes('/1/data/c/') && q.beforeFirstFrame && q.finishMs != null);
  if (l1.length === 0) return null;
  const start = Math.min(...l1.map((q) => q.startMs));
  const end = Math.max(...l1.map((q) => q.finishMs));
  return { bytes: l1.reduce((n, q) => n + q.encodedDataLength, 0), spanMs: end - start, firstTtfbMs: median(l1.map((q) => q.ttfbMs)), startMs: start };
}

function median(values) {
  const s = [...values].filter(Number.isFinite).sort((a, b) => a - b);
  return s.length ? s[Math.floor(s.length / 2)] : NaN;
}

for (const r of runs) {
  r.label = label(r);
  r.batch = batchThroughput(r);
  r.mbps = r.batch ? r.batch.bytes / 1e6 / (r.batch.spanMs / 1000) : NaN;
  const zarr = r.requests.find((q) => q.path.split('?')[0].endsWith('/zarr.json') && q.host === 'data.chronozarr.org');
  r.metaMs = zarr ? zarr.finishMs - zarr.startMs : NaN;
  r.metaTtfb = zarr?.ttfbMs ?? NaN;
  r.metaCf = zarr?.cfCacheStatus ?? null;
}

const METRICS = {
  'metadata (zarr.json) fetch': (r) => r.metaMs,
  'first whole frame, from loadStore call': (r) => r.ready.coarseMs,
  'first whole frame, from navigation start': (r) => r.coarseFramePageMs,
  'complete frame at target level, from loadStore call': (r) => r.ready.firstPaintMs,
  'complete frame at target level, from navigation start': (r) => r.firstFramePageMs,
  'level-1 batch throughput (MB/s)': (r) => r.mbps,
};

const out = { generatedAt: new Date().toISOString(), file: fileName, runs: runs.length, failed: failed.length, labels: {}, byLabel: {}, byIntent: {}, throughput: summarize(runs.map((r) => r.mbps)), load1: summarize(runs.map((r) => r.state.load1)) };
for (const r of runs) out.labels[r.label] = (out.labels[r.label] ?? 0) + 1;
const groupings = { byLabel: (r) => r.label, byIntent: (r) => r.intent };
for (const [name, keyOf] of Object.entries(groupings)) {
  for (const key of [...new Set(runs.map(keyOf))]) {
    const sub = runs.filter((r) => keyOf(r) === key);
    out[name][key] = { n: sub.length, level1Cf: sum(sub.map((r) => r.beforeFirstFrame.level1Cf)), dataCf: sum(sub.map((r) => r.beforeFirstFrame.dataCf)), colos: [...new Set(sub.flatMap((r) => r.beforeFirstFrame.colos))], metrics: Object.fromEntries(Object.entries(METRICS).map(([m, f]) => [m, summarize(sub.map(f), { seed: 99 })])) };
  }
}
function sum(maps) {
  const t = {};
  for (const m of maps) for (const [k, v] of Object.entries(m)) t[k] = (t[k] ?? 0) + v;
  return t;
}
fs.writeFileSync(path.join(OUT_DIR, `${fileName}_summary.json`), `${JSON.stringify(out, null, 1)}\n`);

if (md) {
  const f = (x, d = 0) => (Number.isFinite(x) ? x.toFixed(d) : 'n/a');
  for (const grouping of ['byLabel', 'byIntent']) {
    console.log(`\n### ${grouping}\n`);
    console.log('| Group | N | cf-cache-status of L1 chunks | Metric | p50 [95 % CI] | p95 [95 % CI] |');
    console.log('|---|--:|---|---|---|---|');
    for (const [key, g] of Object.entries(out[grouping])) {
      for (const [m, s] of Object.entries(g.metrics)) {
        const d = m.includes('MB/s') ? 2 : 0;
        console.log(`| ${key} | ${g.n} | ${Object.entries(g.level1Cf).map(([k, v]) => `${k} ${v}`).join(', ')} | ${m} | ${f(s.p50, d)} [${f(s.p50ci[0], d)}, ${f(s.p50ci[1], d)}] | ${f(s.p95, d)} [${f(s.p95ci[0], d)}, ${f(s.p95ci[1], d)}] |`);
      }
    }
  }
}
console.log(`runs ${runs.length} (failed ${failed.length}); labels ${JSON.stringify(out.labels)}; throughput ${f2(out.throughput.p50)} MB/s median; load1 median ${f2(out.load1.p50)}`);
function f2(x) {
  return Number.isFinite(x) ? x.toFixed(2) : 'n/a';
}
