// Runs compare.mjs for both tools and both scrub modes at one link profile, interleaved (tool order alternates with the
// repetition) so that drift of the link or of this machine hits both tools alike, and writes the runs to
// results/zarr-layer-vs-chronozarr-<source>-<profile>.json.
//
//   node zarr-layer/run-all.mjs --profile natural --reps 3 [--source remote|local] [--warmup]
//
// --warmup first runs one unrecorded open + scrub of each tool, so that the CDN edge holds the byte ranges of the first
// timesteps (the first request for an object range is slower than the next).

import { execFile } from 'node:child_process';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

const run = promisify(execFile);
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const profile = flag('profile', 'natural');
const source = flag('source', 'remote');
const reps = Number(flag('reps', 1));
const warmup = args.includes('--warmup');
const scratch = await mkdtemp(path.join(os.tmpdir(), 'zl-compare-'));
const compare = path.join(import.meta.dirname, 'compare.mjs');

async function once(tool, mode, rep) {
  const out = path.join(scratch, `${tool}-${mode}-${rep}.json`);
  const started = Date.now();
  // A run that fails (the page did not start, the network dropped) is repeated once: the failure is not a measurement.
  for (let attempt = 1; ; attempt++) {
    try {
      await run('node', [compare, '--tool', tool, '--profile', profile, '--mode', mode, '--source', source, '--out', out], { maxBuffer: 64 * 1024 * 1024 });
      break;
    } catch (error) {
      console.warn(`${tool} ${mode} rep ${rep} attempt ${attempt} failed: ${String(error.stderr ?? error.message).split('\n').slice(0, 6).join(' | ')}`);
      if (attempt === 2) throw error;
    }
  }
  const { results } = JSON.parse(await readFile(out, 'utf8'));
  console.log(`${tool} ${mode} rep ${rep}: ${Math.round((Date.now() - started) / 1000)} s`);
  return results[0];
}

if (warmup) for (const tool of ['chronozarr', 'zarr-layer']) await once(tool, 'paced', 0);
const runs = [];
for (let rep = 1; rep <= reps; rep++) {
  const tools = rep % 2 === 1 ? ['chronozarr', 'zarr-layer'] : ['zarr-layer', 'chronozarr'];
  for (const mode of ['burst', 'paced']) for (const tool of tools) runs.push(await once(tool, mode, rep));
}
const file = path.join(import.meta.dirname, '..', 'results', `zarr-layer-vs-chronozarr-${source}-${profile}.json`);
await writeFile(file, `${JSON.stringify({ source, profile, store: 'ucayali_santa_maria/chronozarr-3', level: 1, startT: 40, steps: 20, runs }, null, 1)}\n`);
console.log(`wrote ${file}`);
