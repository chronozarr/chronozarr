// chronozarr reader (spec 0.1 and 0.2). No DOM. Runs in browsers (decode workers) and Node.
//
// Coordinates: (lod, row, col, t). One cell is one spatial chunk of one level; its stored bytes for a
// timestep are one zarr inner chunk of shape (1, n_band, chunkH, chunkW), typed by the store's dtype
// (uint8, uint16, int16 or float32). With temporal encoding "star-delta" anchors hold true values and every
// other timestep holds a residual against `delta_reference[t]`, added back modulo 2^bits; with "none" every
// timestep is stored as is.
//
// Reading is done here rather than through zarrita's Array so that fetching and decoding are separate
// steps: chunk bytes are fetched with one range request (shard index cached per shard, cancellable), then
// decoded in a worker pool. zarrita's codec registry (zstd, gzip, blosc) comes from js/vendor, never a CDN.
//
// Memory is tiered (cache.js): decoded arrays for chunks near the view, compressed bytes for chunks farther
// away. Background prefetch draws from a speculative allowance (16 MB at open, then a share of the measured
// bandwidth) and never takes the last request slots from demand reads.

import { BandwidthEstimator } from './bandwidth.js';
import { ChunkCache, SpeculativeBudget } from './cache.js';
import { FetchError, HttpStore, LimitedReadable, abortError, isAbort, sleep } from './http.js';
import { RequestLimiter } from './limiter.js';
import { DTYPES, decodeSpec, normalizeBands, parseRoot, parseStorage, requireStore } from './metadata.js';
import { DecodePool, MainThreadDecoder, leaseDecodePool } from './pool.js';
import { parseShardIndex, shardIndexRange } from './shard.js';
import { registry } from '../vendor/zarrita/codecs.js';

export { FetchError };

/** The part of zarrita the reader uses: the codec registry. Workers load the same module by URL. */
const ZARRITA_CODECS = { registry };
const ZARRITA_CODECS_URL = new URL('../vendor/zarrita/codecs.js', import.meta.url).href;
const MIB = 1024 * 1024;
const GIB = 1024 * MIB;
const DEFAULT_PREFETCH_CONCURRENCY = 6;
const WINDOW_BUDGET_FRACTION = 0.9;
const MAX_DEFAULT_WORKERS = 8;
const DEFAULT_MAX_REQUESTS = 12;
const PREFETCH_COOLDOWN_MS = 30000;
const DEFAULT_RETRY_DELAYS_MS = [200, 600, 1500];
const DEFAULT_SPECULATIVE_INITIAL_BYTES = 16 * MIB;
/**
 * Fraction of the measured bandwidth that speculative fetching earns once the initial allowance is spent:
 * a half while a demand read is pending or the estimate is young (under a second of transfer time), the whole
 * link when nothing is waiting and the estimate is established. Demand requests keep their own request slots
 * and go first, and prefetch starts nothing new while one is pending, so a busy link costs them little. A
 * smaller idle share caps cold-loop playback on a fast link at that fraction of the link: on the live Ucayali
 * store (a link of about 20 MB/s, 10 steps/s asked, median of 3 runs) an idle share of 0.5 achieved 8.2 steps/s,
 * 0.9 achieved 9.1 and 1.0 achieved 9.7.
 */
const SPECULATIVE_SHARE_PENDING = 0.5;
const SPECULATIVE_SHARE_IDLE = 1.0;
const AUX_CACHE_BYTES = 128 * MIB;
/** Compressed size over decoded size assumed until chunks have been seen (Sentinel-2 reflectance: 0.65 to 0.72). */
const INITIAL_COMPRESSION_RATIO = 0.7;

/**
 * Decoded-chunk cache budget: 2 GiB on machines reporting at least 8 GB of memory (navigator.deviceMemory, which
 * browsers cap at 8 and which Firefox and Safari do not provide), otherwise 1 GiB.
 */
export function defaultCacheBytes(deviceMemoryGb) {
  return deviceMemoryGb >= 8 ? 2 * GIB : GIB;
}

/** Compressed-chunk cache budget: 1 GiB from 8 GB of device memory, otherwise 512 MiB. */
export function defaultCompressedBytes(deviceMemoryGb) {
  return deviceMemoryGb >= 8 ? GIB : 512 * MIB;
}

export function chunkKey(lod, row, col, t) {
  return `${lod}/${row}/${col}/${t}`;
}

/**
 * How expensive it is to keep or fetch a timestep at signed distance `dt` from the one being viewed:
 * behind the scrub direction counts `behindFactor` times as much (double by default; movie playback never
 * goes back, so it uses a much larger factor). Prefetch order and eviction order both use it.
 */
export function scrubCost(dt, direction, behindFactor = 2, period = null) {
  if (period === null) return dt * direction >= 0 ? Math.abs(dt) : behindFactor * Math.abs(dt);
  // Circular time (a looping movie): a timestep is ahead by `ahead` steps or behind by the rest of the loop.
  const ahead = (((dt * direction) % period) + period) % period;
  return Math.min(ahead, behindFactor * (period - ahead));
}

/**
 * Timesteps 0..nTime-1 from cheapest to most expensive to keep around `t` (see scrubCost). With `loop`, time
 * is circular, so the first timesteps come right after the last ones: what a looping movie needs ahead of it.
 */
export function windowOrder(nTime, t, { direction = 1, behindFactor = 2, loop = false } = {}) {
  const period = loop ? nTime : null;
  const cost = (step) => scrubCost(step - t, direction, behindFactor, period);
  return Array.from({ length: nTime }, (_, i) => i).sort((a, b) => cost(a) - cost(b) || Math.abs(a - t) - Math.abs(b - t));
}

/**
 * Stored values of one pixel, per band, from an anchor chunk and, for non-anchor timesteps, its residual chunk
 * (null for anchors): (anchor + residual) modulo 2^bits, the arithmetic of the typed array. (x, y) are offsets
 * inside the chunk. The result has the chunk's typed array type.
 */
export function samplePixelFrom(anchor, delta, { nBand, chunkWidth, chunkHeight }, x, y) {
  const plane = chunkHeight * chunkWidth;
  const offset = y * chunkWidth + x;
  const values = new anchor.constructor(nBand);
  for (let b = 0; b < nBand; b++) {
    const a = anchor[b * plane + offset];
    values[b] = delta ? a + delta[b * plane + offset] : a;
  }
  return values;
}

/** decode = (anchor + residual) modulo 2^bits; both inputs are arrays of one unsigned type (uint8 or uint16) and equal length. */
export function applyDelta(anchor, delta) {
  const out = new anchor.constructor(anchor.length);
  for (let i = 0; i < out.length; i++) out[i] = anchor[i] + delta[i];
  return out;
}

function defaultWorkerCount() {
  if (typeof Worker === 'undefined') return 0;
  return Math.max(1, Math.min(MAX_DEFAULT_WORKERS, (globalThis.navigator?.hardwareConcurrency ?? 4) - 1));
}

/**
 * Open a chronozarr store.
 *
 * @param {string} baseUrl  URL of the store root (the directory holding zarr.json).
 * @param {object} [options]
 * @param {typeof fetch} [options.fetch]   fetch implementation (default: globalThis.fetch at call time).
 * @param {object} [options.store]         a zarrita AsyncReadable to use instead of HTTP.
 * @param {number} [options.decodedBytes]  decoded-chunk cache budget (default: see defaultCacheBytes). `maxCacheBytes` is the same setting.
 * @param {number} [options.compressedBytes] compressed-chunk cache budget (default: see defaultCompressedBytes; 0 turns the tier off).
 * @param {number} [options.speculativeBytesInitial] bytes of prefetch allowed before bandwidth is measured (default 16 MB).
 * @param {number} [options.maxRequests]   cap on concurrent requests to the store (default 12).
 * @param {number[]} [options.retryDelaysMs] delays before each retry of a failed request (default [200, 600, 1500]).
 * @param {number} [options.workers]       decode workers (default: cores - 1, at most 8, none without Worker).
 * @param {() => object} [options.spawnWorker] creates a worker (tests); default: js/chronozarr/decode-worker.js.
 * @param {boolean} [options.suffixRequests] send `Range: bytes=-N` for shard indexes when the shard's size is not
 *   known (one request, but a CORS preflight on cross-origin hosts) instead of HEAD + range (two simple requests).
 * @param {() => number} [options.clock]   millisecond clock for bandwidth and the speculative allowance (default performance.now).
 */
export async function openStore(baseUrl, options = {}) {
  const clock = options.clock ?? (() => performance.now());
  const network = { requests: 0, bytes: 0, deduped: 0 };
  const bandwidth = new BandwidthEstimator(clock);
  const maxRequests = options.maxRequests ?? DEFAULT_MAX_REQUESTS;
  const limiter = new RequestLimiter(maxRequests, { reserve: Math.floor(maxRequests / 4) });
  const io = { limiter, network, bandwidth };
  const readable = options.store
    ? new LimitedReadable(options.store, io)
    : new HttpStore(baseUrl, {
        ...io,
        fetch: options.fetch ?? ((request) => globalThis.fetch(request)),
        retryDelaysMs: options.retryDelaysMs ?? DEFAULT_RETRY_DELAYS_MS,
        suffixRequests: options.suffixRequests ?? false,
      });

  const rootBytes = await readable.get('/zarr.json');
  requireStore(rootBytes, baseUrl, 'root zarr.json not found');
  const root = JSON.parse(new TextDecoder().decode(rootBytes));
  const { cz, datasets } = parseRoot(root, baseUrl);
  const variable = cz.variable ?? 'data';

  // One GET for everything when the root carries consolidated metadata, else one GET per array.
  const consolidated = root.consolidated_metadata?.metadata ?? {};
  const readMeta = async (path, { required }) => {
    if (consolidated[path]) return consolidated[path];
    const bytes = await readable.get(`/${path}/zarr.json`);
    requireStore(bytes || !required, baseUrl, `${path}/zarr.json not found`);
    return bytes ? JSON.parse(new TextDecoder().decode(bytes)) : null;
  };
  const auxName = (declared, fallback) => declared ?? (consolidated[`${datasets[0].path}/${fallback}`] ? fallback : null);
  const names = { data: variable, mask: auxName(cz.mask_variable, 'mask'), coverage: auxName(cz.coverage_variable, 'coverage') };
  const readArrays = (name) => Promise.all(datasets.map((ds) => readMeta(`${ds.path}/${name}`, { required: true })));
  const [dataMetas, maskMetas, coverageMetas, levelGroup] = await Promise.all([
    readArrays(names.data),
    names.mask ? readArrays(names.mask) : null,
    names.coverage ? readArrays(names.coverage) : null,
    // Level groups carry the affine `transform`; the `levels` mirror makes reading one unnecessary.
    Array.isArray(cz.levels) ? null : readMeta(datasets[0].path, { required: false }),
  ]);

  const storage = {
    data: dataMetas.map((meta, lod) => parseStorage(meta, { path: `${datasets[lod].path}/${names.data}`, rank: 4, baseUrl })),
    mask: maskMetas?.map((meta, lod) => parseStorage(meta, { path: `${datasets[lod].path}/${names.mask}`, rank: 3, baseUrl })) ?? null,
    coverage: coverageMetas?.map((meta, lod) => parseStorage(meta, { path: `${datasets[lod].path}/${names.coverage}`, rank: 3, baseUrl })) ?? null,
  };
  const specs = {};
  for (const kind of ['data', 'mask', 'coverage']) {
    if (!storage[kind]) continue;
    specs[kind] = decodeSpec(storage[kind][0]);
    requireStore(storage[kind].every((s) => decodeSpec(s).key === specs[kind].key), baseUrl, `levels use different chunk codecs or shapes in ${names[kind]}`);
  }
  for (const kind of ['mask', 'coverage']) {
    requireStore(!storage[kind] || storage[kind][0].dtype === 'uint8', baseUrl, `${names[kind]} must be uint8, got ${storage[kind]?.[0].dtype}`);
  }

  const mem = globalThis.navigator?.deviceMemory;
  const budgets = {
    decodedBytes: options.decodedBytes ?? options.maxCacheBytes ?? defaultCacheBytes(mem),
    compressedBytes: options.compressedBytes ?? defaultCompressedBytes(mem),
    speculativeBytesInitial: options.speculativeBytesInitial ?? DEFAULT_SPECULATIVE_INITIAL_BYTES,
  };

  const workers = options.workers ?? defaultWorkerCount();
  let decoder;
  if (workers > 0) {
    const fallback = new MainThreadDecoder(ZARRITA_CODECS);
    const init = { type: 'init', zarritaUrl: ZARRITA_CODECS_URL };
    decoder = options.spawnWorker
      ? new DecodePool({ size: workers, spawn: options.spawnWorker, init, fallback })
      : leaseDecodePool({ key: `${ZARRITA_CODECS_URL}|${workers}`, size: workers, spawn: () => new Worker(new URL('./decode-worker.js', import.meta.url), { type: 'module' }), init, fallback });
  } else {
    decoder = new MainThreadDecoder(ZARRITA_CODECS);
  }
  for (const spec of Object.values(specs)) decoder.warm?.(spec);

  const transform = Array.isArray(cz.levels) ? cz.levels[0]?.transform : levelGroup?.attributes?.transform;
  return new ChronoStore({
    url: baseUrl,
    cz,
    datasets,
    names,
    levelMirror: cz.levels ?? null,
    transform: Array.isArray(transform) && transform.length === 6 && transform.every(Number.isFinite) ? transform : null,
    dataMetas,
    storage,
    specs,
    readable,
    decoder,
    network,
    bandwidth,
    limiter,
    clock,
    budgets,
  });
}

export class ChronoStore {
  #readable;
  #decoder;
  #storage;
  #specs;
  #names;
  #anchors;
  #deltaReference;
  #cache;
  #aux = new Map();
  #auxBytes = 0;
  #clock;
  #now;
  #inflight = new Map();
  #shardIndexes = new Map();
  #shardBytes;
  #demandInflight = 0;
  #demandIdleWaiters = [];
  #limiter;
  #bandwidth;
  #speculative;
  #compressionRatio = INITIAL_COMPRESSION_RATIO;
  #cellFailures = new Map();
  #network;
  #counters;

  /**
   * Eviction order: called with {lod,row,col,t,anchor,used}; the highest score is evicted first, in both cache
   * tiers. The default is least recently used. A viewer sets this to "farthest from what is on screen".
   */
  evictionScore = (entry) => -entry.used;
  /** Called with {type:'chunk', key, background, requestedAt, fetchedAt, decodedAt, bytes} per loaded chunk. */
  probe = null;

  constructor({ url, cz, datasets, names, levelMirror, transform, dataMetas, storage, specs, readable, decoder, network, bandwidth, limiter, clock, budgets }) {
    this.url = url;
    this.variable = names.data;
    this.times = cz.times;
    this.crs = cz.crs;
    /** Affine [a, b, c, d, e, f] from level-0 pixel (col, row) to projected x, y; null if the store declares none. */
    this.transform = transform;
    this.temporal = cz.temporal;
    this.#readable = readable;
    this.#decoder = decoder;
    this.#limiter = limiter;
    this.#bandwidth = bandwidth;
    this.#clock = clock;
    this.#now = () => performance.now();
    this.#network = network;
    this.#storage = storage;
    this.#specs = specs;
    this.#names = names;
    this.#shardBytes = cz.shard_bytes ?? null;
    this.#cache = new ChunkCache({ decodedBytes: budgets.decodedBytes, compressedBytes: budgets.compressedBytes, score: (entry) => this.evictionScore(entry) });
    this.#speculative = new SpeculativeBudget({ initial: budgets.speculativeBytesInitial, share: SPECULATIVE_SHARE_PENDING, clock });

    const { bands, bandNames } = normalizeBands(cz, url);
    this.bands = bandNames;
    const nTime = this.times.length;
    const encoding = cz.temporal.encoding;
    if (encoding === 'star-delta') {
      requireStore(storage.data[0].dtype === 'uint8' || storage.data[0].dtype === 'uint16', url, `star-delta needs uint8 or uint16 data, got ${storage.data[0].dtype}`);
      this.#anchors = new Set(cz.temporal.anchor_indices);
      this.#deltaReference = new Map(Object.entries(cz.temporal.delta_reference).map(([t, a]) => [Number(t), a]));
    } else {
      this.#anchors = new Set(Array.from({ length: nTime }, (_, t) => t));
      this.#deltaReference = new Map();
    }
    for (let t = 0; t < nTime; t++) {
      requireStore(this.#anchors.has(t) || this.#deltaReference.has(t), url, `timestep ${t} is neither an anchor nor in delta_reference`);
    }
    this.encoding = encoding;
    this.dtype = storage.data[0].dtype;
    const bytesPerElement = DTYPES[this.dtype].bytes;

    this.levels = dataMetas.map((meta, lod) => {
      const [levelTime, nBand, height, width] = meta.shape;
      const [, chunkB, chunkHeight, chunkWidth] = storage.data[lod].innerShape;
      requireStore(levelTime === nTime, url, `level ${lod} has ${levelTime} timesteps, times attr has ${nTime}`);
      requireStore(nBand === bands.length, url, `level ${lod} has ${nBand} bands, bands attr has ${bands.length}`);
      requireStore(chunkB === nBand, url, `level ${lod} chunks must hold all ${nBand} bands, got ${chunkB}`);
      const tile = datasets[lod].pixels_per_tile;
      requireStore(tile === undefined || (tile === chunkWidth && tile === chunkHeight), url, `level ${lod} pixels_per_tile is ${tile}, but its chunks are ${chunkHeight}x${chunkWidth}`);
      for (const kind of ['mask', 'coverage']) {
        const aux = storage[kind]?.[lod];
        if (aux) requireStore(aux.innerShape[1] === chunkHeight && aux.innerShape[2] === chunkWidth, url, `level ${lod} ${names[kind]} chunks are ${aux.innerShape.slice(1)}, data chunks are ${chunkHeight}x${chunkWidth}`);
      }
      const mirror = levelMirror?.[lod];
      const a = transform?.[0];
      return {
        lod,
        path: datasets[lod].path,
        nTime,
        nBand,
        height,
        width,
        chunkHeight,
        chunkWidth,
        gridRows: Math.ceil(height / chunkHeight),
        gridCols: Math.ceil(width / chunkWidth),
        chunkBytes: nBand * chunkHeight * chunkWidth * bytesPerElement,
        resolution: mirror?.resolution ?? (a === undefined ? null : Math.abs(a) * 2 ** lod),
        transform: mirror?.transform ?? (transform ? [transform[0] * 2 ** lod, transform[1], transform[2], transform[3], transform[4] * 2 ** lod, transform[5]] : null),
      };
    });

    this.hasMask = storage.mask !== null;
    this.hasCoverage = storage.coverage !== null;
    this.nodata = typeof cz.nodata === 'number' ? cz.nodata : null;
    /** The chronozarr block of the root, normalized: band objects with scale and offset, names, levels, flags. */
    this.attrs = {
      ...cz,
      spec_version: String(cz.spec_version),
      bands,
      band_names: bandNames,
      bandNames,
      nodata: this.nodata,
      encoding,
      dtype: this.dtype,
      levels: this.levels.map((l) => ({ path: l.path, resolution: l.resolution, transform: l.transform, shape: [l.nTime, l.nBand, l.height, l.width], grid: [l.gridRows, l.gridCols] })),
      hasMask: this.hasMask,
      hasCoverage: this.hasCoverage,
      provenance: cz.provenance ?? null,
    };

    this.#counters = {
      cache: { hits: 0, misses: 0, joins: 0, anchorHits: 0, anchorMisses: 0, compressedHits: 0, speculativeBytes: 0 },
      loads: { count: 0, fetchMs: 0, decodeMs: 0 },
    };
    Object.defineProperty(network, 'inflight', { get: () => limiter.active, enumerable: true });
    const cacheStats = this.#counters.cache;
    Object.defineProperties(cacheStats, {
      decodedBytes: { get: () => this.#cache.decodedBytes, enumerable: true },
      speculativeShare: { get: () => this.#speculative.share, enumerable: true },
      compressedBytes: { get: () => this.#cache.compressedBytes, enumerable: true },
      evictions: { get: () => this.#cache.evictions.decoded, enumerable: true },
      compressedEvictions: { get: () => this.#cache.evictions.compressed, enumerable: true },
    });
    const snapshot = () => ({ network: { ...network }, cache: { ...cacheStats }, loads: { ...this.#counters.loads } });
    /** `stats()` is a snapshot; `stats.network`, `stats.cache` and `stats.loads` are live objects. */
    this.stats = Object.assign(snapshot, { network, cache: cacheStats, loads: this.#counters.loads });
  }

  isAnchor(t) {
    return this.#anchors.has(t);
  }

  /** The anchor timestep needed to decode t (t itself for anchors, and for every timestep under encoding "none"). */
  anchorOf(t) {
    if (this.#anchors.has(t)) return t;
    const anchor = this.#deltaReference.get(t);
    if (anchor === undefined) throw new RangeError(`timestep ${t} out of range 0..${this.times.length - 1}`);
    return anchor;
  }

  get anchorIndices() {
    return [...this.#anchors].sort((a, b) => a - b);
  }

  /** The decoded-tier byte budget (what `loopFits` and the prefetch window are sized by). */
  get maxCacheBytes() {
    return this.#cache.maxDecoded;
  }

  budgets() {
    return { decodedBytes: this.#cache.maxDecoded, compressedBytes: this.#cache.maxCompressed, speculativeBytesInitial: this.#speculative.initial };
  }

  /** Change any of the three budgets; the caches evict down to smaller ones at once. */
  setBudgets({ decodedBytes, compressedBytes, speculativeBytesInitial } = {}) {
    for (const [name, value] of Object.entries({ decodedBytes, compressedBytes, speculativeBytesInitial })) {
      if (value !== undefined && !(Number.isFinite(value) && value >= 0)) throw new RangeError(`${name} must be a number of bytes >= 0, got ${value}`);
    }
    this.#cache.setBudgets({ decodedBytes, compressedBytes });
    if (speculativeBytesInitial !== undefined) this.#speculative.setInitial(speculativeBytesInitial);
  }

  /** Download rate in bytes per second (smoothed over recent transfers), or null before anything was measured. */
  bandwidthEstimate() {
    return this.#bandwidth.estimate;
  }

  level(lod) {
    const level = this.levels[lod];
    if (!level) throw new RangeError(`lod ${lod} out of range 0..${this.levels.length - 1}`);
    return level;
  }

  /** Valid (unpadded) pixel extent of a cell. Edge cells are smaller than the chunk. */
  cellExtent(lod, row, col) {
    const level = this.level(lod);
    this.#checkCell(level, row, col);
    return {
      width: Math.min(level.chunkWidth, level.width - col * level.chunkWidth),
      height: Math.min(level.chunkHeight, level.height - row * level.chunkHeight),
    };
  }

  resetStats() {
    const { cache, loads } = this.#counters;
    for (const name of ['hits', 'misses', 'joins', 'anchorHits', 'anchorMisses', 'compressedHits', 'speculativeBytes']) cache[name] = 0;
    Object.assign(loads, { count: 0, fetchMs: 0, decodeMs: 0 });
    Object.assign(this.#network, { requests: 0, bytes: 0, deduped: 0 });
    this.#cache.evictions.decoded = 0;
    this.#cache.evictions.compressed = 0;
  }

  clearCache() {
    this.#cache.clear();
    this.#aux.clear();
    this.#auxBytes = 0;
  }

  /** Abort every in-flight fetch and release the decode workers. The store cannot be used afterwards. */
  close() {
    for (const entry of this.#inflight.values()) entry.controller.abort();
    this.#decoder.close();
  }

  cacheInfo() {
    return this.#cache.info();
  }

  /** Decoded raw chunk (anchor or every timestep under "none": true values; delta: residual) or undefined. Never fetches. */
  peekRaw(lod, row, col, t) {
    return this.#cache.decoded(chunkKey(lod, row, col, t));
  }

  /**
   * Raw stored chunk for (lod,row,col,t) as a typed array of the store's dtype laid out [band][y][x] over the
   * full (padded) chunk. Cached; concurrent requests for one key share one fetch. Treat as read-only. A chunk
   * that is only in the compressed tier is decoded without touching the network.
   *
   * With a `signal`, aborting rejects this call with an AbortError, and the network request is
   * cancelled once no caller wants it any more (a caller without a signal keeps it alive).
   */
  async getRaw(lod, row, col, t, { signal } = {}) {
    const level = this.level(lod);
    this.#checkCell(level, row, col);
    if (!Number.isInteger(t) || t < 0 || t >= level.nTime) {
      throw new RangeError(`timestep ${t} out of range 0..${level.nTime - 1}`);
    }
    const key = chunkKey(lod, row, col, t);
    const anchor = this.isAnchor(t);
    const counters = this.#counters.cache;
    const decoded = this.#cache.decoded(key);
    if (decoded) {
      counters.hits++;
      if (anchor) counters.anchorHits++;
      return decoded;
    }
    const meta = { kind: 'data', lod, row, col, t, anchor };
    let entry = this.#inflight.get(key);
    if (entry && !entry.controller.signal.aborted) {
      this.#join(entry);
    } else if (this.#cache.get(key)?.compressed) {
      counters.hits++;
      counters.compressedHits++;
      if (anchor) counters.anchorHits++;
      entry = this.#start(key, meta, { background: false });
    } else {
      counters.misses++;
      if (anchor) counters.anchorMisses++;
      entry = this.#start(key, meta, { background: false });
    }
    const data = await this.#subscribe(entry, signal);
    // A prefetch that joined above kept only compressed bytes: decode them now.
    return data ?? this.getRaw(lod, row, col, t, { signal });
  }

  /**
   * Decoded values for (lod,row,col,t): a typed array [band][y][x] over the padded chunk
   * (the cached array itself for anchors). Fetches the anchor and delta chunks in parallel.
   */
  async getCell(lod, row, col, t) {
    const level = this.level(lod);
    const anchorT = this.anchorOf(t);
    const [anchor, delta] = await Promise.all([
      this.getRaw(lod, row, col, anchorT),
      anchorT === t ? null : this.getRaw(lod, row, col, t),
    ]);
    const { width, height } = this.cellExtent(lod, row, col);
    return {
      data: delta ? applyDelta(anchor, delta) : anchor,
      bands: level.nBand,
      chunkWidth: level.chunkWidth,
      chunkHeight: level.chunkHeight,
      width,
      height,
    };
  }

  /**
   * Decoded per-band values of one pixel from cached chunks only (no fetch, no whole-chunk pass).
   * Returns null when the anchor or delta chunk is not cached. (x, y) are pixel offsets inside the cell.
   */
  samplePixel(lod, row, col, t, x, y) {
    const level = this.level(lod);
    const anchor = this.peekRaw(lod, row, col, this.anchorOf(t));
    const delta = this.isAnchor(t) ? null : this.peekRaw(lod, row, col, t);
    if (!anchor || (!this.isAnchor(t) && !delta)) return null;
    return samplePixelFrom(anchor, delta, level, x, y);
  }

  /**
   * The visible cells of one level for timestep `t`, at demand priority: resolves when the anchor and (if any)
   * delta chunk of every cell are decoded, with `peekRaw` returning them (they are held in the cache until then).
   * Rejects when `signal` aborts or a chunk cannot be loaded. Meant for a coarse-first cold open: ask for the
   * deepest level's few cells, paint, then refine.
   *
   * @param {number} lod
   * @param {Array<[number, number]>} cells  [row, col] pairs, loaded in this order
   * @param {number} t
   * @returns {Promise<{lod:number, t:number, anchorT:number, cells:Array<{row:number, col:number, anchor:ArrayBufferView, delta:ArrayBufferView|null}>}>}
   */
  async getCoarseFrame(lod, cells, t, { signal } = {}) {
    const level = this.level(lod);
    for (const [row, col] of cells) this.#checkCell(level, row, col);
    const anchorT = this.anchorOf(t);
    const wanted = [...new Set([anchorT, t])];
    const keys = cells.flatMap(([row, col]) => wanted.map((ct) => chunkKey(lod, row, col, ct)));
    this.#cache.pin(keys);
    try {
      const loaded = await Promise.all(
        cells.map(async ([row, col]) => {
          const [anchor, delta] = await Promise.all([
            this.getRaw(lod, row, col, anchorT, { signal }),
            anchorT === t ? null : this.getRaw(lod, row, col, t, { signal }),
          ]);
          return { row, col, anchor, delta };
        }),
      );
      return { lod, t, anchorT, cells: loaded };
    } finally {
      this.#cache.unpin(keys);
    }
  }

  /** The validity mask chunk of a cell (uint8 [y][x] over the padded chunk, 1 = valid), or null when the store has no mask. */
  getMask(lod, row, col, t, { signal } = {}) {
    return this.#getAux('mask', lod, row, col, t, signal);
  }

  /** The coverage chunk of a cell (uint8 [y][x], observations behind each pixel, 0 = gap-filled), or null when the store has none. */
  getCoverage(lod, row, col, t, { signal } = {}) {
    return this.#getAux('coverage', lod, row, col, t, signal);
  }

  /** Cached mask chunk, undefined when not loaded yet, null when the store has no mask. Never fetches. */
  peekMask(lod, row, col, t) {
    return this.hasMask ? this.#peekAux('mask', lod, row, col, t) : null;
  }

  peekCoverage(lod, row, col, t) {
    return this.hasCoverage ? this.#peekAux('coverage', lod, row, col, t) : null;
  }

  /** Whether `timesteps` timesteps (default: the whole axis) of `cellCount` cells at `lod` fit the window budget of the decoded cache. */
  loopFits(lod, cellCount, timesteps = this.level(lod).nTime) {
    return cellCount * timesteps * this.level(lod).chunkBytes <= this.maxCacheBytes * WINDOW_BUDGET_FRACTION;
  }

  /**
   * Background fetch of a time window around t for the given cells at one level, nearest first
   * (scrubCost order, so the scrub direction reaches further), pulling in a delta's anchor just before
   * the delta itself. The window is as wide as the cache budgets allow for these cells: the whole time
   * axis when it fits, otherwise a slice around t. Chunks near t are decoded; chunks the decoded tier cannot
   * hold are kept as compressed bytes when that tier has room. A new chunk only displaces cached ones that
   * score worse (see `evictionScore`).
   *
   * Speculative traffic is metered: 16 MB may start at once, after that half the measured bandwidth. Prefetch
   * waits while any demand fetch is in flight and never takes the last request slots. A cell whose chunk
   * failed is left alone for PREFETCH_COOLDOWN_MS. Aborting `signal` cancels this job's fetches that nobody else
   * waits for. With `seek`, the view just jumped: every other speculative request is cancelled, and the chunks
   * of timestep `t` itself (its anchor and delta) are fetched at demand priority outside the allowance before
   * the rest of the window. Resolves with counts and per-chunk errors (nothing is thrown or hidden).
   *
   * @param {{lod:number, cells:Array<[number, number]>, t:number, direction?:1|-1, behindFactor?:number, loop?:boolean, seek?:boolean,
   *   concurrency?:number, signal?:AbortSignal, onChunk?:(lod,row,col,t)=>void}} job
   */
  async prefetch({ lod, cells, t, direction = 1, behindFactor = 2, loop = false, seek = false, concurrency = DEFAULT_PREFETCH_CONCURRENCY, signal, onChunk }) {
    const level = this.level(lod);
    if (seek) this.#cancelSpeculative();
    const targets = new Set([this.anchorOf(t), t]);
    const queue = this.#windowPlan(level, cells, t, { direction, behindFactor, loop }).map(([row, col, ct]) => ({ row, col, t: ct, free: seek && targets.has(ct) }));
    const result = { planned: queue.length, fetched: 0, skipped: 0, compressedOnly: 0, budgetReached: false, errors: [] };
    let next = 0;
    const worker = async () => {
      while (next < queue.length && !signal?.aborted && !result.budgetReached) {
        const { row, col, t: tt, free } = queue[next++];
        const key = chunkKey(lod, row, col, tt);
        const failedAt = this.#cellFailures.get(`${lod}/${row}/${col}`);
        if (this.#cache.has(key) || this.#inflight.has(key) || (failedAt !== undefined && this.#now() - failedAt < PREFETCH_COOLDOWN_MS)) {
          result.skipped++;
          continue;
        }
        try {
          if (!free) await this.#demandIdle(signal);
          if (signal?.aborted) return;
          const meta = { kind: 'data', lod, row, col, t: tt, anchor: this.isAnchor(tt) };
          if (!this.#canPlace(meta, level.chunkBytes)) {
            result.budgetReached = true;
            return;
          }
          if (!free) await this.#awaitSpeculative(level.chunkBytes * this.#compressionRatio, signal);
          if (signal?.aborted) return;
          const data = await this.#start(key, meta, { background: !free, owner: signal ?? true }).promise;
          result.fetched++;
          if (data) onChunk?.(lod, row, col, tt);
          else result.compressedOnly++;
        } catch (error) {
          if (isAbort(error)) continue;
          result.errors.push({ key, error });
          this.#cellFailures.set(`${lod}/${row}/${col}`, this.#now());
        }
      }
    };
    await Promise.all(Array.from({ length: concurrency }, worker));
    return result;
  }

  /** Chunks [row, col, t] to prefetch, in fetch order. */
  #windowPlan(level, cells, t, order) {
    const decodedChunks = this.#cache.maxDecoded / level.chunkBytes;
    const compressedChunks = this.#cache.maxCompressed / (level.chunkBytes * this.#compressionRatio);
    // Decoded chunks plus chunks held only compressed: copies of decoded chunks give way when the compressed tier fills.
    const capacity = this.#cache.maxCompressed > 0 ? decodedChunks + compressedChunks : decodedChunks;
    const perCellLimit = Math.max(1, Math.floor((capacity * WINDOW_BUDGET_FRACTION) / Math.max(1, cells.length)));
    const timesteps = windowOrder(level.nTime, t, order);
    const chosen = [];
    const seen = new Set();
    outer: for (const tt of timesteps) {
      for (const ct of [this.anchorOf(tt), tt]) {
        if (seen.has(ct)) continue;
        if (seen.size >= perCellLimit) break outer;
        seen.add(ct);
        chosen.push(ct);
      }
    }
    return chosen.flatMap((ct) => cells.map(([row, col]) => [row, col, ct]));
  }

  /** Whether a background chunk of this cell would be kept by either cache tier. */
  #canPlace(meta, decodedSize) {
    return this.#cache.canHoldDecoded(meta, decodedSize) || this.#cache.canHoldCompressed(meta, decodedSize * this.#compressionRatio);
  }

  /** Wait until `bytes` of speculative traffic may start, then spend them. */
  async #awaitSpeculative(bytes, signal) {
    for (;;) {
      this.#updateSpeculativeShare();
      const waitMs = this.#speculative.waitMs(bytes, this.#bandwidth.estimate);
      if (waitMs === 0) break;
      // Unknown bandwidth: look again soon, since demand traffic or a new budget can change that.
      await sleep(Number.isFinite(waitMs) ? Math.min(Math.max(waitMs, 10), 250) : 50, signal);
    }
    this.#speculative.spend(bytes);
  }

  /** The allowance earns at the idle share only while no demand read is pending and the bandwidth estimate is established. */
  #updateSpeculativeShare() {
    const idle = this.#demandInflight === 0 && this.#bandwidth.mature;
    this.#speculative.setShare(idle ? SPECULATIVE_SHARE_IDLE : SPECULATIVE_SHARE_PENDING, this.#bandwidth.estimate);
  }

  #checkCell(level, row, col) {
    if (!Number.isInteger(row) || !Number.isInteger(col) || row < 0 || col < 0 || row >= level.gridRows || col >= level.gridCols) {
      throw new RangeError(`cell (${row}, ${col}) outside ${level.gridRows}x${level.gridCols} grid at lod ${level.lod}`);
    }
  }

  // ---- mask and coverage ----

  #auxKey(kind, lod, row, col, t) {
    return `${kind}/${chunkKey(lod, row, col, t)}`;
  }

  #peekAux(kind, lod, row, col, t) {
    const entry = this.#aux.get(this.#auxKey(kind, lod, row, col, t));
    if (!entry) return undefined;
    entry.used = ++this.#auxClock;
    return entry.data;
  }

  #auxClock = 0;

  async #getAux(kind, lod, row, col, t, signal) {
    if (this.#storage[kind] === null) return null;
    const level = this.level(lod);
    this.#checkCell(level, row, col);
    if (!Number.isInteger(t) || t < 0 || t >= level.nTime) throw new RangeError(`timestep ${t} out of range 0..${level.nTime - 1}`);
    const key = this.#auxKey(kind, lod, row, col, t);
    const cached = this.#peekAux(kind, lod, row, col, t);
    if (cached) return cached;
    let entry = this.#inflight.get(key);
    if (entry && !entry.controller.signal.aborted) this.#join(entry);
    else entry = this.#start(key, { kind, lod, row, col, t, anchor: false }, { background: false });
    return this.#subscribe(entry, signal);
  }

  #insertAux(key, data) {
    this.#aux.set(key, { data, used: ++this.#auxClock });
    this.#auxBytes += data.byteLength;
    while (this.#auxBytes > AUX_CACHE_BYTES && this.#aux.size > 1) {
      let oldest = null;
      for (const [k, entry] of this.#aux) if (k !== key && (oldest === null || entry.used < this.#aux.get(oldest).used)) oldest = k;
      this.#auxBytes -= this.#aux.get(oldest).data.byteLength;
      this.#aux.delete(oldest);
    }
  }

  // ---- loading ----

  /** A demand caller joins a fetch that is already running: count it, and move it to demand priority. */
  #join(entry) {
    this.#counters.cache.joins++;
    this.#network.deduped++;
    if (entry.priority.value !== 0) {
      entry.priority.value = 0;
      const index = this.#shardIndexes.get(entry.indexKey);
      if (index) index.priority.value = 0;
      this.#limiter.reprioritize();
      this.#decoder.reprioritize?.();
    }
  }

  /**
   * Begin loading a chunk (fetch then decode, or decode alone for a chunk held compressed) and register it as in
   * flight. `owner` is the prefetch signal (or true) that wants a background chunk; the fetch is cancelled when
   * its owner aborts and no caller is waiting on it.
   */
  #start(key, meta, { background, owner = null }) {
    const controller = new AbortController();
    const entry = { controller, waiters: 0, sticky: false, owned: owner !== null, background, priority: { value: background ? 1 : 0 }, promise: null };
    if (owner && owner !== true) {
      owner.addEventListener(
        'abort',
        () => {
          entry.owned = false;
          this.#cancelIfUnwanted(entry);
        },
        { once: true },
      );
    }
    if (!background) {
      this.#demandInflight++;
      this.#updateSpeculativeShare();
    }
    entry.promise = this.#load(key, meta, entry)
      .finally(() => {
        this.#inflight.delete(key);
        if (!background && --this.#demandInflight === 0) {
          this.#updateSpeculativeShare();
          for (const wake of this.#demandIdleWaiters.splice(0)) wake();
        }
      });
    this.#inflight.set(key, entry);
    return entry;
  }

  #cancelIfUnwanted(entry) {
    if (entry.waiters === 0 && !entry.sticky && !entry.owned) entry.controller.abort();
  }

  /** The view jumped: cancel every speculative fetch nobody is waiting for. */
  #cancelSpeculative() {
    for (const entry of this.#inflight.values()) {
      if (!entry.background) continue;
      entry.owned = false;
      this.#cancelIfUnwanted(entry);
    }
  }

  async #load(key, meta, entry) {
    const { kind, lod } = meta;
    const signal = entry.controller.signal;
    const spec = this.#specs[kind];
    const requestedAt = performance.now();
    const held = kind === 'data' ? this.#cache.get(key)?.compressed : null;
    const bytes = held ?? (await this.#fetchBytes(meta, signal, entry));
    const fetchedAt = performance.now();
    const level = this.levels[lod];
    const storage = this.#storage[kind][lod];
    const TypedArray = DTYPES[storage.dtype].Array;
    const size = kind === 'data' ? level.chunkBytes : level.chunkHeight * level.chunkWidth;
    let data = null;
    let retained = null;
    if (bytes === undefined) {
      data = new TypedArray(size / DTYPES[storage.dtype].bytes).fill(storage.fillValue);
    } else {
      const keepCompressed = kind === 'data' && this.#cache.maxCompressed > 0;
      if (!held && kind === 'data') {
        this.#compressionRatio = 0.9 * this.#compressionRatio + 0.1 * (bytes.length / size);
        if (entry.background) this.#counters.cache.speculativeBytes += bytes.length;
      }
      // The pool takes ownership of what it decodes, so a copy goes in when the bytes are also being kept.
      retained = held ?? (keepCompressed ? (bytes.byteOffset === 0 && bytes.buffer.byteLength === bytes.byteLength ? bytes : bytes.slice()) : null);
      const decodeNow = !entry.background || kind !== 'data' || !keepCompressed || this.#cache.canHoldDecoded(meta, size);
      if (decodeNow) data = await this.#decoder.decode(retained ? retained.slice() : bytes, entry.priority, signal, spec);
    }
    const decodedAt = performance.now();
    this.#counters.loads.count++;
    if (!held) this.#counters.loads.fetchMs += fetchedAt - requestedAt;
    this.#counters.loads.decodeMs += decodedAt - fetchedAt;
    this.probe?.({ type: 'chunk', key, t: meta.t, background: entry.background, requestedAt, fetchedAt, decodedAt, bytes: held ? 0 : (bytes?.length ?? 0), decoded: data !== null });
    if (kind === 'data') {
      this.#cache.insert(key, { kind, lod, row: meta.row, col: meta.col, t: meta.t, anchor: meta.anchor }, { data, compressed: held ? null : retained }, { background: entry.background && entry.priority.value !== 0 });
    } else {
      this.#insertAux(key, data);
    }
    return data ?? undefined;
  }

  /** Compressed bytes of one chunk, or undefined when the store has no such chunk (fill value). */
  async #fetchBytes({ kind, lod, row, col, t }, signal, entry) {
    const priority = entry.priority;
    const storage = this.#storage[kind][lod];
    const base = `/${this.levels[lod].path}/${this.#names[kind]}/`;
    const coords = (time) => (kind === 'data' ? [time, 0, row, col] : [time, row, col]);
    if (!storage.sharded) return this.#readable.get(base + storage.keyOf(coords(t)), { signal, priority });
    const shardIndex = Math.floor(t / storage.shardTime);
    const shardKey = base + storage.keyOf(coords(shardIndex));
    entry.indexKey = `${lod}/${shardKey}`;
    const index = await this.#shardIndex(kind, lod, row, col, shardIndex, shardKey, storage, priority);
    if (index === undefined) return undefined;
    const at = t % storage.shardTime;
    const offset = index[2 * at];
    if (offset < 0) return undefined;
    return this.#readable.getRange(shardKey, { offset, length: index[2 * at + 1] }, { signal, priority });
  }

  /** Shard index: read once, shared by every chunk of the shard, never tied to one caller's signal. */
  #shardIndex(kind, lod, row, col, shardIndex, shardKey, storage, priority) {
    const cacheKey = `${lod}/${shardKey}`;
    const cached = this.#shardIndexes.get(cacheKey);
    if (cached) {
      if (!cached.settled) {
        this.#network.deduped++;
        if (priority.value === 0 && cached.priority.value !== 0) {
          cached.priority.value = 0;
          this.#limiter.reprioritize();
        }
      }
      return cached.promise;
    }
    const handle = { value: priority.value };
    const shardBytes = kind === 'data' ? this.#shardBytes?.[this.levels[lod].path]?.[`${shardIndex}/${row}/${col}`] : undefined;
    const range = shardIndexRange(storage.shardTime, storage.indexHasCrc, storage.indexAtStart, shardBytes);
    const promise = (async () => {
      const bytes = await this.#readable.getRange(shardKey, range, { priority: handle });
      return bytes && parseShardIndex(bytes, storage.shardTime, storage.indexHasCrc, `${this.url}${shardKey}`);
    })();
    const cacheEntry = { promise, priority: handle, settled: false };
    promise.then(
      () => (cacheEntry.settled = true),
      () => this.#shardIndexes.delete(cacheKey),
    );
    this.#shardIndexes.set(cacheKey, cacheEntry);
    return promise;
  }

  /** Each caller gets its own promise; a caller that aborts is released, and the last one out cancels the fetch. */
  #subscribe(entry, signal) {
    if (!signal) {
      entry.sticky = true;
      return entry.promise;
    }
    return new Promise((resolve, reject) => {
      entry.waiters++;
      let done = false;
      const finish = () => {
        done = true;
        signal.removeEventListener('abort', onAbort);
        entry.waiters--;
      };
      const onAbort = () => {
        if (done) return;
        finish();
        reject(abortError());
        this.#cancelIfUnwanted(entry);
      };
      if (signal.aborted) {
        onAbort();
        return;
      }
      signal.addEventListener('abort', onAbort, { once: true });
      entry.promise.then(
        (data) => {
          if (done) return;
          finish();
          resolve(data);
        },
        (error) => {
          if (done) return;
          finish();
          reject(error);
        },
      );
    });
  }

  #demandIdle(signal) {
    if (this.#demandInflight === 0) return Promise.resolve();
    return new Promise((resolve) => {
      this.#demandIdleWaiters.push(resolve);
      signal?.addEventListener('abort', resolve, { once: true });
    });
  }
}
