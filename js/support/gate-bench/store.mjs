// The store that the speed-gate job (run.mjs) measures, generated so that every run of the job reads the same bytes.
//
//   node support/gate-bench/store.mjs <dir>      writes the store to <dir> (which must not exist)
//
// 36 cells at level 0 (6 x 6 chunks of 512 x 512 pixels, the grid of the gate "cold open ≤ 500 ms at LOD 0 for 36 cells"),
// 4 Sentinel-2 bands so that every product of the viewer is available, 8 timesteps (the bench decodes up to 8), 4 levels,
// one object per chunk, zstd level 5 and consolidated metadata, as the encoder writes by default. The values are the smooth
// synthetic ones with 6 bits of noise, the recipe of the codec benchmark's chunks: a 2 MiB chunk compresses to 1.39 to
// 1.41 MB, as the chunks of the real Ucayali store do (1.38 to 1.39 MB, docs/evidence.md), so a chunk costs what a real one costs to decode.
// About 510 MB on disk (400 chunk objects); level 0 at all 8 timesteps is 576 MiB decoded, which the warm loop of run.mjs holds in memory.

import { existsSync } from 'node:fs';
import path from 'node:path';
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

export async function writeGateStore(dir) {
  if (existsSync(dir)) throw new Error(`${dir} exists: the gate store is written to a new directory`);
  await writeTree(dir, compressedStore(GATE_STORE, COMPRESSORS.zstd));
  return dir;
}

if (import.meta.filename === process.argv[1]) {
  const [dir] = process.argv.slice(2);
  if (!dir) {
    console.error('Usage: node support/gate-bench/store.mjs <dir>');
    process.exit(1);
  }
  console.log(`Gate store written to ${await writeGateStore(path.resolve(dir))}`);
}
