// The v0.3 browser delivery benchmark, end to end, in one command from the repository root:
//
//   node bench/v03/run.mjs [--rounds 5] [--profiles natural,50Mbit-40ms,10Mbit-100ms] [--views overview,zoom]
//        [--systems A,B,C] [--steps 30] [--attempts 3] [--titiler-workers 4] [--no-values] [--no-warm-files]
//        [--out <dir>] [--resume]
//
// It prepares what is missing (per-date COGs from the store with `chronozarr export-cog`, the zarr-layer bundle, the
// pinned TiTiler image), starts the servers, checks that the three systems read and show the same pixel values, then runs
// the rounds. A round runs every link profile x view cell with the systems interleaved (A, B, C in round 1, then
// B, C, A and C, A, B in the next, and so on), each in a fresh browser; system C gets a fresh, warmed TiTiler container per session. A session whose idle animation-frame median is
// not nominal, or that fails, is repeated (up to --attempts) and the discarded attempt stays in runs.jsonl.
//
// Output: <data>/bench/v03/results/<timestamp>/ with environment.json, values.json, rounds.jsonl (load at every round start),
// runs.jsonl (every attempt, appended as it finishes), results.json (the kept sessions) and summary.md / summary.json
// (summarize.mjs). `--resume --out <that directory>` continues an interrupted run: finished (round, profile, view,
// system) cells are skipped.
//
// Systems: A chronozarr viewer on the v0.3 store; B zarr-layer on the same store; C one COG per date (exported from the
// store) through TiTiler. See README.md in this directory for the method and its limits.

import './private-tmp.mjs';
import { execFile } from 'node:child_process';
import { appendFile, mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { COG_DIR, COG_REL, PROFILE_NAMES, REPO_ROOT, RESULTS_ROOT, STORE_DIR, STORE_REL, SYSTEMS, TITILER, VIEWS } from './config.mjs';
import { captureEnvironment } from './environment.mjs';
import { ensureTitilerImage, startAppServer, startDataServer, startTitiler, warmFileCache } from './servers.mjs';
import { runSession } from './session.mjs';
import { summarize } from './summarize.mjs';
import { resolveView } from './view.mjs';

const run = promisify(execFile);
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const list = (name, fallback) => flag(name, fallback).split(',').map((s) => s.trim()).filter(Boolean);
const rounds = Number(flag('rounds', 5));
const profiles = list('profiles', PROFILE_NAMES.join(','));
const views = list('views', Object.keys(VIEWS).join(','));
/** The systems that run in a view: the ones asked for, and only those the view is defined for. */
const systemsOf = (viewName) => systems.filter((system) => !VIEWS[viewName].only || VIEWS[viewName].only.includes(system));
/** Interleaving: the order of the systems rotates with the round (A,B,C; B,C,A; C,A,B; ...), so no system always follows the same neighbour. */
const rotated = (list, by) => list.map((_, i) => list[(i + by) % list.length]);
const systems = list('systems', Object.keys(SYSTEMS).join(','));
const steps = Number(flag('steps', 30));
const attempts = Number(flag('attempts', 3));
const titilerWorkers = Number(flag('titiler-workers', TITILER.workers));
const withValues = !args.includes('--no-values');
const warmFiles = !args.includes('--no-warm-files');
const resume = args.includes('--resume');

for (const [what, chosen, allowed] of [['profile', profiles, PROFILE_NAMES], ['view', views, Object.keys(VIEWS)], ['system', systems, Object.keys(SYSTEMS)]]) {
  for (const name of chosen) if (!allowed.includes(name)) throw new Error(`unknown ${what} "${name}"; use ${allowed.join(', ')}`);
}
if (resume && !flag('out', null)) throw new Error('--resume needs --out <the results directory of the interrupted run>');

const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const outDir = flag('out', path.join(RESULTS_ROOT, stamp));
await mkdir(outDir, { recursive: true });
const runsFile = path.join(outDir, 'runs.jsonl');

const log = (message) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${message}`);
const exists = (file) => stat(file).then(() => true, () => false);
const cellKey = (round, profile, view, system) => `${round}|${profile}|${view}|${system}`;

// ---- preparation ----

const store = JSON.parse(await readFile(path.join(STORE_DIR, 'zarr.json'), 'utf8').catch(() => {
  throw new Error(`the v0.3 store is not at ${STORE_DIR}. Set BENCH_DATA to the directory that holds ${STORE_REL}, or build the store (see README.md).`);
}));
const times = store.attributes.chronozarr.times;
const cogName = (t) => `L0_${times[t].slice(0, 10)}.tif`;

async function ensureCogs() {
  const present = new Set(await readdir(COG_DIR).catch(() => []));
  if (times.every((_, t) => present.has(cogName(t)))) return;
  log(`exporting ${times.length} COGs from the store to ${COG_DIR} (about 10 minutes)`);
  await mkdir(COG_DIR, { recursive: true });
  await run('uv', ['run', '--extra', 'geo', 'chronozarr', 'export-cog', STORE_DIR, COG_DIR, '--level', '0'], { cwd: REPO_ROOT, maxBuffer: 64 * 1024 * 1024, timeout: 3 * 60 * 60 * 1000 });
  const after = new Set(await readdir(COG_DIR));
  const missing = times.map((_, t) => cogName(t)).filter((name) => !after.has(name));
  if (missing.length) throw new Error(`export-cog left ${missing.length} COGs missing, first: ${missing[0]}`);
}

async function ensureBundle() {
  if (await exists(path.join(REPO_ROOT, 'bench/zarr-layer/dist/page.js'))) return;
  if (!(await exists(path.join(REPO_ROOT, 'bench/node_modules/playwright')))) throw new Error('bench dependencies are not installed: run `npm ci` in bench/ (see README.md).');
  await run('node', [path.join(REPO_ROOT, 'bench/zarr-layer/build.mjs')], { cwd: path.join(REPO_ROOT, 'bench') });
}

await ensureBundle();
if (systems.includes('C')) {
  await ensureCogs();
  await ensureTitilerImage();
}

// A resumed run continues the attempts already on disk.
const previous = resume && (await exists(runsFile)) ? (await readFile(runsFile, 'utf8')).split('\n').filter(Boolean).map((line) => JSON.parse(line)) : [];
const kept = previous.filter((entry) => !entry.discarded);
const discards = previous.filter((entry) => entry.discarded).map((entry) => ({ round: entry.round, attempt: entry.attempt, profile: entry.profile, view: entry.view, system: entry.system, reason: entry.discarded }));
const done = new Set(kept.map((entry) => cellKey(entry.round, entry.profile, entry.view, entry.system)));
const attemptsMade = new Map();
for (const entry of previous) attemptsMade.set(cellKey(entry.round, entry.profile, entry.view, entry.system), Math.max(attemptsMade.get(cellKey(entry.round, entry.profile, entry.view, entry.system)) ?? 0, entry.attempt));

const app = await startAppServer();
const data = await startDataServer();
const dataPort = new URL(data.url).port;
const servers = {
  app,
  data,
  store: `${data.url}/${STORE_REL}`,
  timesteps: times.length,
  cogUrl: (t) => `http://host.docker.internal:${dataPort}/${COG_REL}/${cogName(t)}`,
  titiler: null,
};

try {
  // ---- environment and values ----
  if (warmFiles) log(`file cache: ${JSON.stringify(await warmFileCache([STORE_DIR, ...(systems.includes('C') ? [COG_DIR] : [])]))}`);
  const probeTitiler = systems.includes('C') ? await startTitiler({ cogUrl: servers.cogUrl, workers: titilerWorkers }) : null;
  try {
    if (!(await exists(path.join(outDir, 'environment.json')))) {
      const environment = await captureEnvironment({ servers, titiler: probeTitiler, store, cogNames: times.map((_, t) => cogName(t)), args, outDir, titilerWorkers });
      await writeFile(path.join(outDir, 'environment.json'), `${JSON.stringify(environment, null, 1)}\n`);
    } else {
      await appendFile(path.join(outDir, 'resumes.jsonl'), `${JSON.stringify({ at: new Date().toISOString(), command: args, loadavg: os.loadavg(), kept: kept.length })}\n`);
    }
    if (withValues && !(await exists(path.join(outDir, 'values.json')))) {
      const { checkValues } = await import('./values.mjs');
      servers.titiler = probeTitiler;
      const values = await checkValues({ servers, cogName, cogDir: COG_DIR });
      await writeFile(path.join(outDir, 'values.json'), `${JSON.stringify(values, null, 1)}\n`);
      log(`pixel values: ${values.summary}`);
    }
  } finally {
    await probeTitiler?.stop();
  }

  // ---- rounds ----
  log(`${rounds} rounds x ${profiles.length} profiles x ${views.length} views x ${systems.length} systems; results in ${outDir}`);
  for (let round = 1; round <= rounds; round++) {
    if (profiles.every((p) => views.every((v) => systemsOf(v).every((s) => done.has(cellKey(round, p, v, s)))))) continue;
    if (warmFiles && round > 1) await warmFileCache([STORE_DIR, ...(systems.includes('C') ? [COG_DIR] : [])]);
    const uptime = (await run('uptime')).stdout.trim();
    const busiest = (await run('ps', ['-Ao', 'pcpu,comm', '-r'])).stdout.split('\n').slice(1, 7).map((line) => line.trim().replace(/\s+/, ' '));
    await appendFile(path.join(outDir, 'rounds.jsonl'), `${JSON.stringify({ round, at: new Date().toISOString(), uptime, loadavg: os.loadavg(), ncpu: os.cpus().length, busiestProcesses: busiest })}\n`);
    log(`round ${round}: ${uptime}`);
    for (const profileName of profiles) {
      for (const viewName of views) {
        const view = resolveView(VIEWS[viewName]);
        for (const system of rotated(systemsOf(viewName), round - 1)) {
          const key = cellKey(round, profileName, viewName, system);
          if (done.has(key)) continue;
          let final = null;
          for (let attempt = (attemptsMade.get(key) ?? 0) + 1; attempt <= attempts; attempt++) {
            servers.titiler = system === 'C' ? await startTitiler({ cogUrl: servers.cogUrl, workers: titilerWorkers }) : { url: '' };
            let record;
            try {
              record = await runSession({ system, profileName, view, servers, steps });
            } finally {
              if (system === 'C') await servers.titiler.stop();
            }
            const discard = record.error ? `error: ${record.error.slice(0, 160)}` : record.rafNominal ? null : `animation-frame median ${record.raf.preMedianMs}/${record.raf.postMedianMs} ms`;
            const entry = { round, attempt, discarded: discard, ...record };
            await appendFile(runsFile, `${JSON.stringify(entry)}\n`);
            log(`round ${round} ${profileName} ${viewName} ${system} attempt ${attempt}: ${discard ? `DISCARDED (${discard})` : headline(record)}`);
            if (!discard) {
              final = entry;
              break;
            }
            discards.push({ round, attempt, profile: profileName, view: viewName, system, reason: discard });
          }
          if (final) kept.push(final);
          else log(`round ${round} ${profileName} ${viewName} ${system}: no usable session in ${attempts} attempts`);
        }
      }
    }
  }
} finally {
  await data.close();
  await app.close();
}

const roundsLog = (await readFile(path.join(outDir, 'rounds.jsonl'), 'utf8').catch(() => '')).split('\n').filter(Boolean).map((line) => JSON.parse(line));
const results = { stamp, outDir, args, rounds, profiles, views, systems, steps, titilerWorkers, discards, roundsLog, runs: kept };
await writeFile(path.join(outDir, 'results.json'), `${JSON.stringify(results)}\n`);
const summary = summarize(results);
await writeFile(path.join(outDir, 'summary.json'), `${JSON.stringify(summary.json, null, 1)}\n`);
await writeFile(path.join(outDir, 'summary.md'), summary.markdown);
log(`done: ${kept.length} sessions kept; ${path.join(outDir, 'summary.md')}`);

function headline(record) {
  const mb = (bytes) => `${(bytes / 1e6).toFixed(1)} MB`;
  const stepMs = record.step.steps.map((s) => s.ms).filter((v) => v !== null).sort((a, b) => a - b);
  return `open ${Math.round(record.open.ms)} ms ${mb(record.open.net.bytes)}, step median ${stepMs.length ? Math.round(stepMs[stepMs.length >> 1]) : 'n/a'} ms, jump ${Math.round(record.jump.steps[0].ms ?? -1)} ms, total ${mb(record.session.net.bytes)}, load ${record.load.loadavg1}/${os.cpus().length} cpus`;
}
