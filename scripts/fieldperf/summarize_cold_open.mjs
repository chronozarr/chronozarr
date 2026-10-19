// Summary of a recorded cold open (cold_open_<tag>.json): what the viewer fetches up to its first complete frame, by kind,
// and the milestones of the waterfall.
//
//   node scripts/fieldperf/summarize_cold_open.mjs <date dir name> [tag]     e.g. 2026-10-18 bare1

import fs from 'node:fs';
import path from 'node:path';
import { DATA_ROOT } from './lib.mjs';

const dir = process.argv[2] ?? '2026-10-18';
const tag = process.argv[3] ?? 'bare1';
const rec = JSON.parse(fs.readFileSync(path.join(DATA_ROOT, dir, `cold_open_${tag}.json`), 'utf8'));
const kindOf = (q) => {
  if (q.host === 'chronozarr.org') {
    if (q.type === 'Document') return 'document (/demo/)';
    if (q.type === 'Script') return 'JavaScript modules';
    if (q.path.includes('catalog.json')) return 'catalog.json';
    return 'favicon';
  }
  const p = q.path.split('?')[0];
  if (p.endsWith('/zarr.json')) return 'root zarr.json (consolidated metadata)';
  const m = p.match(/\/(\d)\/data\/c\//);
  if (m) return `chunks, level ${m[1]}`;
  return q.type === 'Script' || p.endsWith('.wasm') ? 'worker / WASM codec' : 'other';
};
const pre = rec.requests.filter((q) => q.beforeFirstFrame);
const post = rec.requests.filter((q) => !q.beforeFirstFrame);
const group = (rows) => {
  const g = {};
  for (const q of rows) {
    const k = kindOf(q);
    g[k] ??= { n: 0, bytes: 0, firstStartMs: Infinity, lastFinishMs: 0, cf: {}, protocols: new Set() };
    g[k].n += 1;
    g[k].bytes += q.encodedDataLength ?? 0;
    g[k].firstStartMs = Math.min(g[k].firstStartMs, q.startMs);
    g[k].lastFinishMs = Math.max(g[k].lastFinishMs, q.finishMs ?? 0);
    g[k].cf[q.cfCacheStatus ?? 'none'] = (g[k].cf[q.cfCacheStatus ?? 'none'] ?? 0) + 1;
    g[k].protocols.add(q.protocol ?? '?');
  }
  return g;
};
const f = (x) => (Number.isFinite(x) ? Math.round(x) : 'n/a');
console.log(`cold open record ${dir}/${tag}: ${rec.at}; page ${rec.url}; t=${rec.store.t}; levels ${rec.store.levels.map((l) => `${l.lod}:${l.gridRows}x${l.gridCols}`).join(' ')}; colos ${[...new Set(rec.requests.map((q) => q.colo).filter(Boolean))].join(',')}; load1 ${rec.state.load1}`);
console.log(`ready: ${JSON.stringify(rec.ready)}; first complete frame on the page clock ${f(rec.firstFramePageMs)} ms`);
for (const [title, rows] of [['BEFORE the first complete frame', pre], ['AFTER it, within the 3 s tail', post]]) {
  console.log(`\n${title}: ${rows.length} requests, ${(rows.reduce((n, q) => n + (q.encodedDataLength ?? 0), 0) / 1e6).toFixed(2)} MB`);
  console.log('| Kind | Requests | Bytes (wire) | First start (ms) | Last finish (ms) | cf-cache-status | Protocol |');
  console.log('|---|--:|--:|--:|--:|---|---|');
  for (const [k, v] of Object.entries(group(rows)).sort((a, b) => a[1].firstStartMs - b[1].firstStartMs)) console.log(`| ${k} | ${v.n} | ${v.bytes.toLocaleString('en-US')} | ${f(v.firstStartMs)} | ${f(v.lastFinishMs)} | ${Object.entries(v.cf).map(([a, b]) => `${a} ${b}`).join(', ')} | ${[...v.protocols].join(',')} |`);
}
// Module waterfall depth: groups of script requests that start together.
const scripts = pre.filter((q) => q.type === 'Script' && q.host === 'chronozarr.org').sort((a, b) => a.startMs - b.startMs);
const levels = [];
for (const q of scripts) {
  const last = levels.at(-1);
  if (last && q.startMs - last.at(-1).startMs <= 15) last.push(q);
  else levels.push([q]);
}
console.log(`\nmodule graph: ${scripts.length} scripts in ${levels.length} sequential start groups: ${levels.map((l) => `${l.length}@${f(l[0].startMs)}ms`).join(', ')}`);
const cat = rec.requests.find((q) => q.path.includes('catalog.json'));
console.log(`catalog.json requested at ${f(cat.startMs)} ms (after the module graph and its evaluation)`);
