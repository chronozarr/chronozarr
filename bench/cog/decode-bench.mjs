// Decode cost of the same 9-cell view of level 1, with no network: the bytes are in memory and only decoding is timed.
//
//   chronozarr reader   zstd level 5 chunks (WASM, main thread), band-planar
//   COG, DEFLATE        what `chronozarr export-cog` writes: DEFLATE + predictor 2, read by geotiff.js (pako)
//   COG, ZSTD           the same pixels as ZSTD level 5 + predictor 2 (codec_sizes.py), read by geotiff.js (zstddec, WASM)
//
//   node cog/decode-bench.mjs [timestep=2] [repeats=7]
//
// Needs the COG for that date in data/cogs/ucayali_santa_maria and data/cogs-variants/<yyyy-mm>/zstd5_pred2.tif
// (uv run python cog/codec_sizes.py ...).

import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fromArrayBuffer } from 'geotiff';
import { openStore } from '../../js/chronozarr/decoder.js';

const REPO_ROOT = path.resolve(import.meta.dirname, '../..');
const STORE_DIR = path.join(REPO_ROOT, 'data/stores/ucayali_santa_maria/chronozarr-3');
const timestep = Number(process.argv[2] ?? 2);
const repeats = Number(process.argv[3] ?? 7);
const times = JSON.parse(await readFile(path.join(STORE_DIR, 'zarr.json'), 'utf8')).attributes.chronozarr.times;
const date = times[timestep].slice(0, 10);
const cogDeflate = path.join(REPO_ROOT, `data/cogs/ucayali_santa_maria/L0_${date}.tif`);
const cogZstd = path.join(REPO_ROOT, `data/cogs-variants/${date.slice(0, 7)}/zstd5_pred2.tif`);

const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
const timeRuns = async (fn) => {
  await fn();
  const ms = [];
  for (let i = 0; i < repeats; i++) {
    const started = performance.now();
    await fn();
    ms.push(performance.now() - started);
  }
  return { medianMs: Math.round(median(ms) * 10) / 10, minMs: Math.round(Math.min(...ms) * 10) / 10 };
};

async function cogDecode(file) {
  const buffer = await readFile(file);
  const tiff = await fromArrayBuffer(buffer.buffer.slice(buffer.byteOffset, buffer.byteOffset + buffer.byteLength));
  const image = await tiff.getImage(1);
  return timeRuns(() => image.readRasters({ interleave: false }));
}

// The reader over an in-memory copy of the shards: every range is read from disk once, then served from memory.
const shardCache = new Map();
const memoryStore = {
  async get(key) {
    return readOnce(key, null);
  },
  async getRange(key, range) {
    return readOnce(key, range);
  },
};
async function readOnce(key, range) {
  const id = `${key}|${JSON.stringify(range)}`;
  if (!shardCache.has(id)) {
    const file = path.join(STORE_DIR, key);
    let bytes;
    try {
      bytes = new Uint8Array(await readFile(file));
    } catch (error) {
      if (error.code === 'ENOENT') return undefined;
      throw error;
    }
    if (range?.suffixLength !== undefined) bytes = bytes.subarray(bytes.length - range.suffixLength);
    else if (range) bytes = bytes.subarray(range.offset, range.offset + range.length);
    shardCache.set(id, bytes);
  }
  return shardCache.get(id).slice();
}

const result = {
  date,
  view: 'level 1, 3 x 3 cells (1380 x 1383 pixels, 4 bands, uint16)',
  repeats,
  'COG, DEFLATE + predictor 2 (geotiff.js, pako)': await cogDecode(cogDeflate),
  'COG, ZSTD 5 + predictor 2 (geotiff.js, zstddec WASM)': await cogDecode(cogZstd),
};
const store = await openStore('http://store.invalid', { store: memoryStore, workers: 0 });
const cells = Array.from({ length: 9 }, (_, i) => [Math.floor(i / 3), i % 3]);
result['chronozarr reader, zstd 5 chunks (WASM, one thread)'] = await timeRuns(async () => {
  store.clearCache();
  await Promise.all(cells.map(([row, col]) => store.getCell(1, row, col, timestep)));
});
store.close();
console.log(JSON.stringify(result, null, 1));
