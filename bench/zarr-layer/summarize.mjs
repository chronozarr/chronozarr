// Markdown tables of results/zarr-layer-vs-chronozarr-<source>-<profile>.json: per source (the published store, or the
// same files from the local range server), link profile and tool, the median over repetitions (range in parentheses)
// of the open and of both scrub modes.
//
//   node zarr-layer/summarize.mjs [remote|local]

import { readFile } from 'node:fs/promises';
import path from 'node:path';

const resultsDir = path.join(import.meta.dirname, '../results');
const PROFILES = ['natural', '50Mbit-40ms', '10Mbit-100ms'];
const TOOLS = ['chronozarr', 'zarr-layer'];
const sources = process.argv[2] ? [process.argv[2]] : ['remote', 'local'];

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted[Math.floor(sorted.length / 2)];
};
const fmtMs = (ms) => (ms >= 10000 ? `${(ms / 1000).toFixed(1)} s` : ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`);
/** "median (min-max)" of a metric over runs; the range only for more than one run. */
const spread = (runs, pick, fmt) => {
  const values = runs.map(pick);
  const m = median(values);
  return runs.length > 1 ? `${fmt(m)} (${fmt(Math.min(...values))}-${fmt(Math.max(...values))})` : fmt(m);
};
const header = (cols) => [`| ${cols.join(' | ')} |`, `|${cols.map(() => '---').join('|')}|`];
const aborted = (r) => Object.values(r.scrub.byKind).reduce((n, k) => n + (k.aborted ?? 0), 0);

const out = [];
for (const source of sources) {
  const files = {};
  for (const profile of PROFILES) {
    try {
      files[profile] = JSON.parse(await readFile(path.join(resultsDir, `zarr-layer-vs-chronozarr-${source}-${profile}.json`), 'utf8'));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  const available = PROFILES.filter((p) => files[p]);
  if (available.length === 0) continue;
  const select = (profile, tool, mode) => files[profile].runs.filter((r) => r.tool === tool && r.mode === mode);
  const label = source === 'remote' ? 'published store (data.chronozarr.org)' : 'same files, local range server';

  out.push(`### Cold open, level 1, 9 cells: ${label}`, '', 'Median over repetitions, range in parentheses. Burst and paced runs both open the store the same way and are pooled.', '');
  out.push(...header(['link', 'tool', 'runs', 'time to first complete frame', 'of which until the shard-index reads are done', 'requests', 'MB']));
  for (const profile of available) {
    for (const tool of TOOLS) {
      const runs = select(profile, tool, 'burst').concat(select(profile, tool, 'paced'));
      out.push(`| ${profile} | ${tool} | ${runs.length} | ${spread(runs, (r) => r.open.ms, fmtMs)} | ${spread(runs, (r) => r.open.indexReadsDoneMs, fmtMs)} | ${spread(runs, (r) => r.open.requests, String)} | ${spread(runs, (r) => r.open.bytes / 1e6, (v) => v.toFixed(1))} |`);
    }
  }

  for (const mode of ['paced', 'burst']) {
    out.push('', mode === 'paced' ? `### Scrub forward 20 timesteps, paced: ${label}` : `### Scrub forward 20 timesteps, burst (one step every 100 ms): ${label}`, '');
    out.push(...header(['link', 'tool', 'runs', 'requests', 'of which aborted', 'MB transferred', 'scrub duration', 'steps shown exactly', 'step latency median / p95', 'time showing mixed-timestep frames', 'MB in the next 3 s']));
    for (const profile of available) {
      for (const tool of TOOLS) {
        const runs = select(profile, tool, mode);
        if (runs.length === 0) continue;
        out.push(
          `| ${profile} | ${tool} | ${runs.length} | ${spread(runs, (r) => r.scrub.requests, String)} | ${spread(runs, aborted, String)} | ${spread(runs, (r) => r.scrub.bytes / 1e6, (v) => v.toFixed(0))} | ${spread(runs, (r) => r.scrub.settleMs, fmtMs)} | ${spread(runs, (r) => r.scrub.stepsShownExactly, String)} of 20 | ${fmtMs(median(runs.map((r) => r.scrub.lagMs.median)))} / ${fmtMs(median(runs.map((r) => r.scrub.lagMs.p95)))} | ${spread(runs, (r) => r.scrub.mixedFrameMs ?? 0, fmtMs)} | ${spread(runs, (r) => r.trailing3s.bytes / 1e6, (v) => v.toFixed(0))} |`,
        );
      }
    }
  }
  out.push('');
}
console.log(out.join('\n'));
