// Turns the kept sessions of a run into tables: per link profile and view, one row per system, each cell the median over
// the rounds with the range of the rounds in parentheses.
//
//   node bench/v03/summarize.mjs <results.json>          prints the markdown tables
//
// A session's own numbers (derive): time to the first complete frame, bytes and requests until it, the median and
// 95th percentile of the latency of STEPS single-date steps (a step is a date not shown before), the bytes per step (the
// whole phase, so a viewer's speculative fetching counts), the same for the steps back over dates already shown, the jump
// to a date 50 away, the bytes of the idle seconds after it, and the session total. The table cells are medians of
// these per-session numbers over the rounds; `pooled` in summary.json is the distribution of all steps of all rounds.

import { readFile } from 'node:fs/promises';
import { SYSTEMS } from './config.mjs';
import { describe, median, percentile, spread } from './stats.mjs';

const MB = 1e6;

/** One session record -> the numbers that go in the tables (null where the session lacks them). */
export function derive(run) {
  const latencies = (phase) => run[phase].steps.map((s) => s.ms);
  const step = latencies('step');
  const revisit = latencies('revisit');
  const jump = run.jump.steps[0];
  const perStep = (phase) => run[phase].net.bytes / run[phase].steps.length;
  const perStepRequests = (phase) => run[phase].net.requests / run[phase].steps.length;
  const level = run.open.level ?? run.open.tileZoom ?? null;
  const resolutionM = run.open.groundMetresPerPixel ?? (run.open.level === null || run.open.level === undefined ? null : 10 * 2 ** run.open.level);
  return {
    level,
    resolutionM,
    cells: run.open.cells,
    openMs: run.open.ms,
    openCoarseMs: run.open.coarseMs ?? null,
    openBytes: run.open.net.bytes,
    openRequests: run.open.net.requests,
    stepMedianMs: median(step),
    stepP95Ms: percentile(step, 0.95),
    stepFirstWholeMedianMs: median(run.step.steps.map((s) => s.firstWholeMs ?? null)),
    stepNeverCompleted: step.filter((v) => v === null).length,
    stepBytesPerStep: perStep('step'),
    stepRequestsPerStep: perStepRequests('step'),
    stepLevels: [...new Set(run.step.steps.map((s) => s.level ?? s.tileZoom ?? null))],
    revisitMedianMs: median(revisit),
    revisitP95Ms: percentile(revisit, 0.95),
    revisitBytesPerStep: perStep('revisit'),
    jumpMs: jump.ms,
    jumpFirstWholeMs: jump.firstWholeMs ?? null,
    jumpBytes: run.jump.net.bytes,
    jumpRequests: run.jump.net.requests,
    idleBytes: run.idle.net.bytes,
    sessionBytes: run.session.net.bytes,
    sessionRequests: run.session.net.requests,
    originRequests: run.session.origin.requests,
    originRangeBytes: run.session.origin.rangeBytes,
    rafMedianMs: Math.max(run.raf.preMedianMs, run.raf.postMedianMs),
    loadavg1: run.load.loadavg1,
  };
}

const fmtMs = (v) => (v === null || v === undefined ? 'n/a' : v >= 10000 ? `${(v / 1000).toFixed(1)} s` : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : v >= 100 ? `${Math.round(v)} ms` : v >= 10 ? `${v.toFixed(0)} ms` : `${v.toFixed(1)} ms`);
const fmtMB = (v) => (v === null || v === undefined ? 'n/a' : v >= 100 * MB ? `${(v / MB).toFixed(0)} MB` : v >= MB ? `${(v / MB).toFixed(1)} MB` : `${(v / 1000).toFixed(0)} kB`);
const fmtN = (v) => (v === null || v === undefined ? 'n/a' : Number.isInteger(v) ? String(v) : v.toFixed(1));
const fmtM = (v) => (v === null || v === undefined ? 'n/a' : `${v >= 10 ? v.toFixed(0) : v.toFixed(1)} m`);

/** Group the kept sessions by profile, view and system, with the derived numbers of every round. */
export function group(results) {
  const groups = new Map();
  for (const run of results.runs) {
    const key = `${run.profile}|${run.view}|${run.system}`;
    if (!groups.has(key)) groups.set(key, { profile: run.profile, view: run.view, system: run.system, rounds: [], derived: [], loads: [], steps: [], revisits: [] });
    const g = groups.get(key);
    g.rounds.push(run.round);
    g.derived.push(derive(run));
    g.loads.push(run.load.loadavg1);
    g.steps.push(...run.step.steps.map((s) => s.ms));
    g.revisits.push(...run.revisit.steps.map((s) => s.ms));
  }
  return groups;
}

const column = (g, key) => g.derived.map((d) => d[key]);

export function summarize(results) {
  const groups = group(results);
  const json = [];
  const lines = [];
  const discards = results.discards ?? [];
  const loads = results.runs.map((r) => r.load.loadavg1);
  const ncpu = results.runs[0]?.load.ncpu;
  lines.push(
    `PROVISIONAL. Medians over rounds, range of the rounds in parentheses; ${results.runs.length} sessions kept, ${discards.length} attempts discarded. The machine was shared: 1-minute load average at session start had median ${fmtN(median(loads))} (range ${fmtN(Math.min(...loads))} to ${fmtN(Math.max(...loads))}) on ${ncpu} CPUs.`,
    '',
  );
  for (const profile of results.profiles) {
    for (const view of results.views) {
      const rows = results.systems.map((system) => groups.get(`${profile}|${view}|${system}`)).filter(Boolean);
      if (rows.length === 0) continue;
      lines.push(`#### ${profile}, ${view} view`, '');
      lines.push('| system | pixel size (units) | first complete frame | MB / requests to it | step median / p95 | MB / requests per step | revisit median | jump 50 dates | session MB / requests |', '|---|---|---|---|---|---|---|---|---|');
      for (const g of rows) {
        const resolution = describe(column(g, 'resolutionM')).median;
        const cells = describe(column(g, 'cells')).median;
        const unit = g.system === 'C' ? 'tiles' : 'cells';
        const coarse = g.system === 'A' ? ` (first whole frame ${spread(column(g, 'openCoarseMs'), fmtMs)})` : '';
        const never = g.derived.reduce((n, d) => n + d.stepNeverCompleted, 0);
        lines.push(
          `| ${g.system} ${SYSTEMS[g.system].name} (n=${g.derived.length}) | ${fmtM(resolution)} (${fmtN(cells)} ${unit}) | ${spread(column(g, 'openMs'), fmtMs)}${coarse} | ${spread(column(g, 'openBytes'), fmtMB)} / ${spread(column(g, 'openRequests'), fmtN)} | ${spread(column(g, 'stepMedianMs'), fmtMs)} / ${spread(column(g, 'stepP95Ms'), fmtMs)}${never ? ` (${never} steps never completed)` : ''} | ${spread(column(g, 'stepBytesPerStep'), fmtMB)} / ${spread(column(g, 'stepRequestsPerStep'), fmtN)} | ${spread(column(g, 'revisitMedianMs'), fmtMs)} | ${spread(column(g, 'jumpMs'), fmtMs)} (${spread(column(g, 'jumpBytes'), fmtMB)}) | ${spread(column(g, 'sessionBytes'), fmtMB)} / ${spread(column(g, 'sessionRequests'), fmtN)} |`,
        );
        const keys = Object.keys(g.derived[0]).filter((k) => k !== 'stepLevels');
        json.push({
          profile,
          view,
          system: g.system,
          rounds: g.rounds,
          loadavg1: describe(g.loads),
          ...Object.fromEntries(keys.map((k) => [k, describe(column(g, k))])),
          pooled: { stepMs: describe(g.steps), revisitMs: describe(g.revisits) },
          perRound: g.derived,
        });
      }
      const c = rows.find((g) => g.system === 'C');
      if (c) lines.push('', `C reads the COGs through TiTiler: its origin reads were ${spread(column(c, 'originRangeBytes'), fmtMB)} in ${spread(column(c, 'originRequests'), fmtN)} range requests per session (server side, not on the throttled link).`);
      lines.push('');
    }
  }
  if (discards.length) lines.push(`Discarded attempts: ${discards.map((d) => `${d.profile} ${d.view} ${d.system} round ${d.round}: ${d.reason}`).join('; ')}.`, '');
  return { json, markdown: lines.join('\n') };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const file = process.argv[2];
  if (!file) throw new Error('usage: node bench/v03/summarize.mjs <results.json>');
  console.log(summarize(JSON.parse(await readFile(file, 'utf8'))).markdown);
}
