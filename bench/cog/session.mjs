// One session against two representations of the same imagery, over a local range server:
//
//   chronozarr  the Zarr v3 store (sharded, zstd, one chunk per timestep and cell), read with the chronozarr JS reader
//   cog         one Cloud Optimized GeoTIFF per date (DEFLATE + predictor, 512 px tiles, overviews), read with geotiff.js 3.0.5
//               as `fromUrl(url)` (its defaults: no block cache, so a header is read in many small requests)
//   cogBlocked  the same files, read with `fromUrl(url, { blockSize: 65536 })` (64 KB aligned blocks, a cache of 400 blocks per file)
//
// Session (fixed level 1, a 3 x 3 cell view = the whole 20 m level; level 0 for the zoom and the pixel history):
//
//   open       the first date: metadata/header, then the 9 cells of level 1
//   scrub      the next 20 dates, one after the other (each step waits for the previous one)
//   jump       one distant date
//   playback   the next 20 consecutive dates, pipelined (no step waits for another)
//   zoom       an uncached 2 x 2 cell area of level 0 at the date on screen
//   history    one pixel's values at every date, level 0
//
// Every HTTP request of every phase is recorded (see recorder.mjs) and classified as metadata/header/index or pixel
// data; delivery time per link profile comes from the model in model.mjs, wall-clock is measured locally.
//
//   node cog/session.mjs [--reps 3] [--out results/cog-vs-chronozarr.json]
//
// Needs data/cogs/ucayali_santa_maria (uv run chronozarr export-cog ... --level 0) and results/pixel-reference-*.json
// (uv run python cog/pixel_reference.py ...); see docs/comparisons.md for the exact commands.

import { readFile, readdir, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fromFile, fromUrl } from 'geotiff';
import { openStore } from '../../js/chronozarr/decoder.js';
import { startStaticServer } from '../../js/support/static-server.js';
import { simulatePhase } from './model.mjs';
import { context, installRecorder, parseRange } from './recorder.mjs';

const REPO_ROOT = path.resolve(import.meta.dirname, '../..');
const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const reps = Number(flag('reps', 3));
const out = flag('out', path.join(REPO_ROOT, 'bench/results/cog-vs-chronozarr.json'));

const STORE_DIR = path.join(REPO_ROOT, 'data/stores/ucayali_santa_maria/chronozarr-3');
const COG_DIR = path.join(REPO_ROOT, 'data/cogs/ucayali_santa_maria');
const REFERENCE = path.join(REPO_ROOT, 'bench/results/pixel-reference-L0-r1388-c1380.json');
const REMOTE_STORE = 'https://data.chronozarr.org/ucayali_santa_maria/chronozarr-3';

const range = (from, to) => Array.from({ length: to - from }, (_, i) => from + i);
const grid = (rows, cols) => rows.flatMap((r) => cols.map((c) => [r, c]));
const PLAN = {
  viewLevel: 1,
  viewCells: grid(range(0, 3), range(0, 3)),
  openT: 40,
  scrubTs: range(41, 61),
  jumpT: 100,
  playbackTs: range(61, 81),
  zoomLevel: 0,
  zoomCells: grid([2, 3], [2, 3]),
  zoomT: 80,
  pixel: { level: 0, row: 1388, col: 1380 },
};
const CELL = 512;

// Delivery model parameters.
const PARALLEL = { chronozarr: 12, cog: 6, cogBlocked: 6 };
const REQUEST_OVERHEAD_BYTES = 500;

const root = JSON.parse(await readFile(path.join(STORE_DIR, 'zarr.json'), 'utf8'));
const times = root.attributes.chronozarr.times;
const shardBytes = root.attributes.chronozarr.shard_bytes;
const stem = (iso) => iso.slice(0, 10);

// ---- the natural link: measured from this machine to the published store ----

// The fetch the recorder will replace, for the probes that run while it is installed.
const realFetch = globalThis.fetch;

/** One round: 9 round-trip times (1-byte range reads) and 9 transfer rates (8 MB range reads, round trip taken off). */
async function probeRound() {
  const get = async (range) => {
    const started = performance.now();
    const response = await realFetch(`${REMOTE_STORE}/0/data/c/0/0/0/0`, { headers: { Range: `bytes=${range}` }, cache: 'no-store' });
    const bytes = (await response.arrayBuffer()).byteLength;
    if (response.status !== 206) throw new Error(`link probe: expected 206 from ${REMOTE_STORE}, got ${response.status}`);
    return { bytes, ms: performance.now() - started };
  };
  await get('0-0');
  const rtts = [];
  for (let i = 0; i < 9; i++) rtts.push((await get('0-0')).ms);
  const rttMs = [...rtts].sort((a, b) => a - b)[4];
  const rates = [];
  for (let i = 0; i < 9; i++) {
    const start = 40_000_000 + i * 8_000_000;
    const { bytes, ms } = await get(`${start}-${start + 7_999_999}`);
    rates.push((bytes * 8) / 1e6 / ((ms - rttMs) / 1000));
  }
  return { rtts: rtts.map(Math.round), rates: rates.map(Math.round) };
}

/** The natural link as the medians over all rounds: at the start, in the middle and at the end of the session. */
function summarizeProbes(rounds) {
  const medianOf = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
  const rtts = rounds.flatMap((r) => r.rtts);
  const rates = rounds.flatMap((r) => r.rates);
  return { rttMs: Math.round(medianOf(rtts)), mbps: Math.round(medianOf(rates)), rounds, rangeMbps: [Math.min(...rates), Math.max(...rates)], rangeRttMs: [Math.min(...rtts), Math.max(...rtts)] };
}

// ---- sessions ----

class ChronozarrSession {
  constructor(storeUrl) {
    this.storeUrl = storeUrl;
    this.store = null;
  }

  async open() {
    this.store = await openStore(this.storeUrl, { workers: 0 });
  }

  /** Cells [row, col] of a level at date t; resolves to the decoded cells. */
  show(level, cells, t) {
    return Promise.all(cells.map(([row, col]) => this.store.getCell(level, row, col, t)));
  }

  async pixel({ level, row, col }, t) {
    const r = Math.floor(row / CELL);
    const c = Math.floor(col / CELL);
    const cell = await this.store.getCell(level, r, c, t);
    const plane = cell.chunkWidth * cell.chunkHeight;
    const offset = (row - r * CELL) * cell.chunkWidth + (col - c * CELL);
    return Array.from({ length: cell.bands }, (_, band) => Number(cell.data[band * plane + offset]));
  }

  close() {
    this.store?.close();
  }
}

class CogSession {
  constructor(baseUrl, options = undefined) {
    this.baseUrl = baseUrl;
    this.options = options;
    this.files = new Map();
  }

  async open() {}

  file(t) {
    let opened = this.files.get(t);
    if (!opened) {
      opened = fromUrl(`${this.baseUrl}/L0_${stem(times[t])}.tif`, this.options);
      this.files.set(t, opened);
    }
    return opened;
  }

  /** The cells of a level as the tile window they span; geotiff.js reads the tiles that intersect it. */
  async show(level, cells, t) {
    const tiff = await this.file(t);
    const image = await tiff.getImage(level);
    const rows = cells.map(([row]) => row);
    const cols = cells.map(([, col]) => col);
    const window = [Math.min(...cols) * CELL, Math.min(...rows) * CELL, Math.min((Math.max(...cols) + 1) * CELL, image.getWidth()), Math.min((Math.max(...rows) + 1) * CELL, image.getHeight())];
    return image.readRasters({ window, interleave: false });
  }

  async pixel({ level, row, col }, t) {
    const tiff = await this.file(t);
    const image = await tiff.getImage(level);
    const bands = await image.readRasters({ window: [col, row, col + 1, row + 1], interleave: false });
    return Array.from(bands, (band) => Number(band[0]));
  }

  close() {}
}

/** Runs the phases of the session; every phase tags its requests. Returns phase timings (ms). */
async function runSession(session) {
  const phases = [];
  const phase = async (name, sequential, stepFns) => {
    const started = performance.now();
    if (sequential) for (let i = 0; i < stepFns.length; i++) await context.run({ phase: name, step: i }, stepFns[i]);
    else await Promise.all(stepFns.map((fn, i) => context.run({ phase: name, step: i }, fn)));
    phases.push({ name, sequential, steps: stepFns.length, wallMs: performance.now() - started });
  };
  const view = (t) => () => session.show(PLAN.viewLevel, PLAN.viewCells, t);
  await phase('open', true, [async () => {
    await session.open();
    await session.show(PLAN.viewLevel, PLAN.viewCells, PLAN.openT);
  }]);
  await phase('scrub forward 20', true, PLAN.scrubTs.map(view));
  await phase('jump to a distant date', true, [view(PLAN.jumpT)]);
  await phase('playback, 20 consecutive dates', false, PLAN.playbackTs.map(view));
  await phase('zoom to 4 uncached cells, next finer level', true, [() => session.show(PLAN.zoomLevel, PLAN.zoomCells, PLAN.zoomT)]);
  const history = new Array(times.length);
  await phase('one pixel, complete history', false, times.map((_, t) => async () => {
    history[t] = await session.pixel(PLAN.pixel, t);
  }));
  return { phases, history };
}

// ---- classification of the recorded requests ----

const cogHeaderEnd = new Map();
async function headerEndOf(file) {
  if (cogHeaderEnd.has(file)) return cogHeaderEnd.get(file);
  const tiff = await fromFile(file);
  const count = await tiff.getImageCount();
  let end = Infinity;
  for (let i = 0; i < count; i++) {
    const image = await tiff.getImage(i);
    const offsets = image.fileDirectory.TileOffsets ?? (await image.fileDirectory.loadValue('TileOffsets'));
    end = Math.min(end, ...offsets);
  }
  cogHeaderEnd.set(file, end);
  return end;
}

/** role: 'metadata' (root zarr.json), 'index' (shard index) and 'header' (COG header) are not pixels; 'pixel' is. */
async function classify(entry, representation, storeBase) {
  const bytesRange = parseRange(entry.range);
  if (representation === 'chronozarr') {
    const key = new URL(entry.url).pathname.slice(new URL(storeBase).pathname.length);
    if (key === '/zarr.json') return { role: 'metadata', key };
    // A sharded read is a range of the shard file, whose name is the key of its first inner chunk: <level>/data/c/<t shard>/<band>/<row>/<col>.
    const match = /^\/(\d+)\/data\/c\/(\d+)\/(\d+)\/(\d+)\/(\d+)$/.exec(key);
    if (!match) throw new Error(`unclassified chronozarr request ${entry.method} ${entry.url} ${entry.range}`);
    if (entry.method === 'HEAD') return { role: 'index', key };
    const [, level, shardT, , row, col] = match;
    const size = shardBytes[level]?.[`${shardT}/${row}/${col}`];
    if (!bytesRange || size === undefined) throw new Error(`cannot classify ${entry.method} ${entry.url} ${entry.range}: no byte range or no shard size`);
    return { role: bytesRange[1] + 1 === size ? 'index' : 'pixel', key };
  }
  const file = path.join(COG_DIR, path.basename(entry.url));
  const headerEnd = await headerEndOf(file);
  const start = bytesRange ? bytesRange[0] : 0;
  return { role: start < headerEnd ? 'header' : 'pixel', key: path.basename(entry.url) };
}

async function summarizeRun(log, representation, urls) {
  const classified = [];
  for (const entry of log) {
    classified.push({ phase: entry.ctx?.phase ?? null, step: entry.ctx?.step ?? null, method: entry.method, range: entry.range, status: entry.status, bytes: entry.bytes, startedAt: Math.round(entry.startedAt * 100) / 100, endedAt: Math.round(entry.endedAt * 100) / 100, ...(await classify(entry, representation, urls.store)) });
  }
  return classified;
}

// ---- model ----

/**
 * Requests this one waits for, among `mine` (the requests of its step, in the order they were issued). A request of the
 * same file or shard that had finished when this one started was issued after its answer arrived (a header read that
 * leads to the next, a shard index that leads to the chunk): on the local server a request that has to wait for an answer
 * cannot start before it, and requests issued together start together. A shard index read also waits for the store
 * metadata that names it.
 */
function dependencies(request, index, mine) {
  const waits = [];
  mine.forEach((other, k) => {
    if (k !== index && other.key === request.key && other.endedAt <= request.startedAt) waits.push(k);
  });
  if (request.role === 'index') {
    const metadata = mine.findIndex((o) => o.role === 'metadata');
    if (metadata >= 0 && mine[metadata].endedAt <= request.startedAt) waits.push(metadata);
  }
  return waits;
}

function modelPhases(requests, phaseMeta, link, parallel) {
  const result = {};
  for (const meta of phaseMeta) {
    const own = requests.filter((r) => r.phase === meta.name);
    const steps = Array.from({ length: meta.steps }, (_, step) => {
      const mine = own.filter((r) => r.step === step);
      return mine.map((r, i) => ({ id: i, bytes: r.bytes + REQUEST_OVERHEAD_BYTES, after: dependencies(r, i, mine) }));
    });
    result[meta.name] = simulatePhase(steps, { ...link, parallel }, { sequential: meta.sequential });
  }
  return result;
}

// ---- helpers ----

async function directoryBytes(dir) {
  let total = 0;
  for (const entry of await readdir(dir, { recursive: true, withFileTypes: true })) {
    if (entry.isFile()) total += (await stat(path.join(entry.parentPath, entry.name))).size;
  }
  return total;
}

/** Milliseconds during which at least one of the requests was in flight (the union of their intervals). */
function inFlightMs(requests) {
  const spans = requests.map((r) => [r.startedAt, r.endedAt]).sort((a, b) => a[0] - b[0]);
  let total = 0;
  let end = -Infinity;
  for (const [start, stop] of spans) {
    if (stop <= end) continue;
    total += stop - Math.max(start, end);
    end = stop;
  }
  return total;
}

/** "phase:requests:bytes" for every phase, to check that repetitions made the same requests. */
const fingerprint = (requests) => [...new Set(requests.map((r) => r.phase))].map((phase) => { const own = requests.filter((r) => r.phase === phase); return `${phase}:${own.length}:${own.reduce((n, r) => n + r.bytes, 0)}`; }).join('|');

const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];

// ---- the overviews of the two representations ----

/** Level 1 of the first date as read from the COG's first overview and from the store, over the pixels both have. */
async function overviewCheck(urls) {
  const cog = new CogSession(urls.cog);
  const zarr = new ChronozarrSession(urls.store);
  await zarr.open();
  const rasters = await cog.show(PLAN.viewLevel, PLAN.viewCells, PLAN.openT);
  const cells = await zarr.show(PLAN.viewLevel, PLAN.viewCells, PLAN.openT);
  zarr.close();
  const image = await (await cog.file(PLAN.openT)).getImage(PLAN.viewLevel);
  const width = image.getWidth();
  const height = image.getHeight();
  const level = root.attributes.chronozarr.levels[PLAN.viewLevel];
  const plane = CELL * CELL;
  let equal = 0;
  let within1 = 0;
  let sumAbs = 0;
  let maxAbs = 0;
  let count = 0;
  for (let band = 0; band < rasters.length; band++) {
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const cell = cells[Math.floor(y / CELL) * 3 + Math.floor(x / CELL)];
        const fromStore = cell.data[band * plane + (y % CELL) * CELL + (x % CELL)];
        const diff = Math.abs(rasters[band][y * width + x] - fromStore);
        count++;
        if (diff === 0) equal++;
        if (diff <= 1) within1++;
        sumAbs += diff;
        maxAbs = Math.max(maxAbs, diff);
      }
    }
  }
  return { date: times[PLAN.openT], cogShape: [height, width], storeShape: [level.shape[2], level.shape[3]], pixelsCompared: count, equalFraction: equal / count, withinOneFraction: within1 / count, meanAbsDiff: sumAbs / count, maxAbsDiff: maxAbs };
}

// ---- main ----

const probeRounds = [await probeRound()];

const server = await startStaticServer(REPO_ROOT);
const urls = { store: `${server.url}/data/stores/ucayali_santa_maria/chronozarr-3`, cog: `${server.url}/data/cogs/ucayali_santa_maria` };
const recorder = installRecorder();
const makers = { chronozarr: () => new ChronozarrSession(urls.store), cog: () => new CogSession(urls.cog), cogBlocked: () => new CogSession(urls.cog, { blockSize: 65536, cacheSize: 400 }) };
const reference = JSON.parse(await readFile(REFERENCE, 'utf8'));
const runs = { chronozarr: [], cog: [], cogBlocked: [] };
let representatives = {};
try {
  // Rep 0 warms the operating system's file cache and is discarded; the order of the representations rotates.
  for (let rep = 0; rep <= reps; rep++) {
    const names = Object.keys(makers);
    const order = [...names.slice(rep % names.length), ...names.slice(0, rep % names.length)];
    for (const representation of order) {
      recorder.log.length = 0;
      const session = makers[representation]();
      const { phases, history } = await runSession(session);
      session.close();
      const requests = await summarizeRun(recorder.log, representation, urls);
      const identical = history.every((values, t) => values.length === reference.values[t].length && values.every((v, b) => v === reference.values[t][b]));
      if (!identical) throw new Error(`${representation}: pixel history differs from the reference read straight from the Zarr arrays`);
      console.log(`rep ${rep} ${representation}: ${phases.map((p) => `${p.name} ${Math.round(p.wallMs)} ms`).join(', ')}`);
      if (rep > 0) runs[representation].push({ rep, phases, requests, fingerprint: fingerprint(requests) });
      if (rep === 1) representatives[representation] = { requests, phases };
    }
    if (rep === Math.ceil(reps / 2)) probeRounds.push(await probeRound());
  }
  probeRounds.push(await probeRound());
} finally {
  recorder.restore();
}
const natural = summarizeProbes(probeRounds);
console.log(`natural link to ${new URL(REMOTE_STORE).host}: ${natural.rttMs} ms round trip, ${natural.mbps} Mbit/s (medians of ${probeRounds.length} rounds; observed ${natural.rangeMbps.join('-')} Mbit/s, ${natural.rangeRttMs.join('-')} ms)`);
const LINKS = { natural: { rttMs: natural.rttMs, mbps: natural.mbps, observedMbps: natural.rangeMbps, observedRttMs: natural.rangeRttMs, probeRounds }, '50Mbit-40ms': { rttMs: 40, mbps: 50 }, '10Mbit-100ms': { rttMs: 100, mbps: 10 } };
let overview;
try {
  overview = await overviewCheck(urls);
  console.log(`level 1 of ${overview.date}: ${(overview.equalFraction * 100).toFixed(2)} % of ${overview.pixelsCompared} values identical, max difference ${overview.maxAbsDiff}`);
} finally {
  await server.close();
}

const phaseMeta = representatives.chronozarr.phases.map(({ name, sequential, steps }) => ({ name, sequential, steps }));
const model = {};
for (const [representation, parallel] of Object.entries(PARALLEL)) {
  model[representation] = {};
  for (const [linkName, link] of Object.entries(LINKS)) model[representation][linkName] = modelPhases(representatives[representation].requests, phaseMeta, link, parallel);
}
const identicalRequests = Object.fromEntries(Object.entries(runs).map(([representation, list]) => [representation, list.every((r) => r.fingerprint === list[0].fingerprint)]));
for (const [representation, same] of Object.entries(identicalRequests)) if (!same) console.warn(`warning: repetitions of ${representation} made different requests (see the fingerprints in the output)`);
const inFlight = Object.fromEntries(
  Object.entries(representatives).map(([representation, { requests }]) => [representation, Object.fromEntries(phaseMeta.map((meta) => [meta.name, inFlightMs(requests.filter((r) => r.phase === meta.name))]))]),
);
const wall = {};
for (const representation of Object.keys(runs)) {
  wall[representation] = Object.fromEntries(phaseMeta.map((meta) => [meta.name, { reps: runs[representation].map((r) => r.phases.find((p) => p.name === meta.name).wallMs), medianMs: median(runs[representation].map((r) => r.phases.find((p) => p.name === meta.name).wallMs)) }]));
}

const sizes = { storeBytes: await directoryBytes(STORE_DIR), cogBytes: await directoryBytes(COG_DIR), cogFiles: (await readdir(COG_DIR)).length };
await writeFile(
  out,
  `${JSON.stringify(
    {
      plan: PLAN,
      parallel: PARALLEL,
      requestOverheadBytes: REQUEST_OVERHEAD_BYTES,
      links: LINKS,
      reps,
      pixelHistory: { reference: path.relative(REPO_ROOT, REFERENCE), identical: true, timesteps: times.length, bands: reference.values[0].length },
      sizes,
      overviewCheck: overview,
      phases: phaseMeta,
      requests: Object.fromEntries(Object.entries(representatives).map(([name, r]) => [name, r.requests])),
      model,
      wallClock: wall,
      requestsInFlightMs: inFlight,
      identicalRequestsAcrossRepetitions: identicalRequests,
      requestFingerprints: Object.fromEntries(Object.entries(runs).map(([representation, list]) => [representation, list.map((r) => r.fingerprint)])),
    },
    null,
    1,
  )}\n`,
);
console.log(`wrote ${out}`);
