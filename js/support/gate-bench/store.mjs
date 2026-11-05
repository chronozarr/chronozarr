// The store that the speed-gate job (run.mjs) measures, generated so that every run of the job reads the same bytes.
//
//   node support/gate-bench/store.mjs <dir> [--times <n>]      writes the store to <dir> (which must not exist)
//
// 36 cells at level 0 (6 x 6 chunks of 512 x 512 pixels, the grid of the gate "cold open ≤ 500 ms at LOD 0 for 36 cells"),
// 4 Sentinel-2 bands so that every product of the viewer is available, 8 timesteps (the bench decodes up to 8), 4 levels,
// one object per chunk, zstd level 5 and consolidated metadata, as the encoder writes by default. The values are the smooth
// synthetic ones with 6 bits of noise, the recipe of the codec benchmark's chunks: a 2 MiB chunk compresses to 1.39 to
// 1.41 MB, as the chunks of the real Ucayali store do (1.38 to 1.39 MB, docs/evidence.md), so a chunk costs what a real one costs to decode.
// About 510 MB on disk (400 chunk objects); level 0 at all 8 timesteps is 576 MiB decoded, which the warm loop of run.mjs holds in memory.
//
// `--times 48` (link.mjs) gives the store a longer time axis, for scrubbing and playing over more steps than a cache holds
// at once: timestep t of the long store is a hard link to the chunk files of timestep t % 8, so it costs the same disk and
// reads, compresses and decodes like the real chunks, but repeats its picture every 8 steps. 48 steps at level 0 are 3.4 GiB decoded.

import { existsSync } from 'node:fs';
import { link, mkdir } from 'node:fs/promises';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { COMPRESSORS, compressedStore, writeTree } from '../../e2e/make-node-fixtures.mjs';
import { defaultValues } from '../synthetic-store.js';

const smooth = defaultValues('uint16');

/** 0..63 from the pixel's coordinates alone, so the value of a pixel does not depend on the order chunks are written in. */
function noise(t, b, y, x) {
  let h = Math.imul(x, 0x9e3779b1) ^ Math.imul(y, 0x85ebca77) ^ Math.imul(t * 4 + b, 0xc2b2ae3d);
  h ^= h >>> 15;
  h = Math.imul(h, 0x2c1b3c6d);
  h ^= h >>> 12;
  return h & 63;
}

export const GATE_STORE = {
  nTime: 8,
  nBand: 4,
  bands: ['B02', 'B03', 'B04', 'B08'],
  height: 3072,
  width: 3072,
  chunk: 512,
  nLevels: 4,
  sharded: false,
  consolidated: true,
  nodata: 0,
  transform: [10, 0, 500000, 0, -10, 4000000],
  values: (t, b, y, x, lod = 0) => smooth(t, b, y, x, lod) + noise(t, b, y * 2 ** lod, x * 2 ** lod),
};

const text = new TextEncoder();
const json = (value) => text.encode(JSON.stringify(value, null, 1));
const isArray = (meta) => meta?.node_type === 'array';

/** The timestamps of `times` continued to `count` entries at the step of its first two (the store writes one a day). */
function extendTimes(times, count) {
  const first = Date.parse(times[0]);
  const step = Date.parse(times[1]) - first;
  return Array.from({ length: count }, (_, i) => new Date(first + i * step).toISOString().replace('.000Z', 'Z'));
}

/**
 * The files of an unsharded store with `baseSteps` timesteps, as a store of `nTime` timesteps: every array and the
 * root's timestamps are rewritten for the longer axis, and each chunk of a timestep that has none is a hard link to the
 * chunk of the timestep at the same position in the first `baseSteps`. Resolves to the files to write and the links to make.
 * @param {Map<string, Uint8Array>} files  by path, as compressedStore returns them
 * @returns {{ files: Map<string, Uint8Array>, links: { path: string, target: string }[] }}
 */
export function extendTimeAxis(files, baseSteps, nTime) {
  if (!Number.isInteger(nTime) || nTime < baseSteps) throw new RangeError(`a store of ${nTime} timesteps cannot be made from one of ${baseSteps}: --times must be an integer of at least ${baseSteps}`);
  if (nTime === baseSteps) return { files, links: [] };
  const extended = new Map();
  const links = [];
  for (const [key, bytes] of files) {
    if (key.endsWith('/zarr.json')) {
      const meta = JSON.parse(new TextDecoder().decode(bytes));
      if (isArray(meta)) meta.shape[0] = nTime;
      if (key === '/zarr.json') {
        for (const entry of Object.values(meta.consolidated_metadata?.metadata ?? {})) if (isArray(entry)) entry.shape[0] = nTime;
        const { chronozarr } = meta.attributes;
        chronozarr.times = extendTimes(chronozarr.times, nTime);
        for (const level of chronozarr.levels ?? []) level.shape[0] = nTime;
      }
      extended.set(key, json(meta));
      continue;
    }
    extended.set(key, bytes);
    // An unsharded data chunk: /<level>/data/c/<t>/<band>/<row>/<col>.
    const chunk = /^(\/\d+\/data\/c\/)(\d+)(\/.+)$/.exec(key);
    if (!chunk) continue;
    const t = Number(chunk[2]);
    for (let linked = t + baseSteps; linked < nTime; linked += baseSteps) links.push({ path: `${chunk[1]}${linked}${chunk[3]}`, target: key });
  }
  return { files: extended, links };
}

/** Write `files` to a new `dir`, then make `links` inside it (each `target` is a path in the store). */
export async function writeLinkedTree(dir, { files, links }) {
  await writeTree(dir, files);
  for (const { path: linkPath, target } of links) {
    await mkdir(path.dirname(path.join(dir, linkPath)), { recursive: true });
    await link(path.join(dir, target), path.join(dir, linkPath));
  }
}

export async function writeGateStore(dir, { nTime = GATE_STORE.nTime } = {}) {
  if (existsSync(dir)) throw new Error(`${dir} exists: the gate store is written to a new directory`);
  await writeLinkedTree(dir, extendTimeAxis(compressedStore(GATE_STORE, COMPRESSORS.zstd), GATE_STORE.nTime, nTime));
  return dir;
}

if (import.meta.filename === process.argv[1]) {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: { times: { type: 'string', default: String(GATE_STORE.nTime) } } });
  const [dir] = positionals;
  if (!dir) {
    console.error('Usage: node support/gate-bench/store.mjs <dir> [--times <n>]');
    process.exit(1);
  }
  console.log(`Gate store written to ${await writeGateStore(path.resolve(dir), { nTime: Number(values.times) })}`);
}
