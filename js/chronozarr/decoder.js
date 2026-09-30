// chronozarr v0.1 reader. No DOM. Runs in browsers (import map for "zarrita", decode workers) and Node.
//
// Coordinates: (lod, row, col, t). One cell is one spatial chunk of one level; its stored bytes for a
// timestep are one zarr inner chunk of shape (1, n_band, chunkH, chunkW). Star-delta: anchors hold true
// uint16 values; every other timestep holds an int16 residual (stored as uint16 bits) against
// `delta_reference[t]`.
//
// Reading is done here rather than through zarrita's Array so that fetching and decoding are separate
// steps: chunk bytes are fetched with one range request (shard index cached per cell, cancellable),
// then decoded in a worker pool. zarrita supplies the HTTP store and the zstd/gzip codecs.

import * as zarr from 'zarrita';
import { RequestLimiter } from './limiter.js';
import { MainThreadDecoder, DecodePool } from './pool.js';
import { indexByteLength, parseShardIndex } from './shard.js';

const SUPPORTED_SPEC = /^0\.1\./;
const DEFAULT_MAX_CACHE_BYTES = 1024 * 1024 * 1024;
const DEFAULT_PREFETCH_CONCURRENCY = 6;
const WINDOW_BUDGET_FRACTION = 0.9;
const MAX_DEFAULT_WORKERS = 8;
const DEFAULT_MAX_REQUESTS = 12;
const PREFETCH_COOLDOWN_MS = 30000;
const DEFAULT_RETRY_DELAYS_MS = [200, 600, 1500];

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

/** decode = clamp(anchor + int16(delta), 0, 65535); both inputs are uint16 arrays of equal length. */
export function applyDelta(anchor, delta) {
  const residual = new Int16Array(delta.buffer, delta.byteOffset, delta.length);
  const out = new Uint16Array(anchor.length);
  for (let i = 0; i < out.length; i++) {
    const v = anchor[i] + residual[i];
    out[i] = v < 0 ? 0 : v > 65535 ? 65535 : v;
  }
  return out;
}

const abortError = () => new DOMException('Aborted', 'AbortError');
const isAbort = (error) => error?.name === 'AbortError';

/** A request to the store that failed for good: carries the URL, HTTP status (if any) and attempt count. */
export class FetchError extends Error {
  constructor({ url, method, range, status, statusText, cause, attempts }) {
    const what = status ? `HTTP ${status}${statusText ? ` ${statusText}` : ''}` : `${cause.name}: ${cause.message}`;
    super(`${method} ${url}${range ? ` [${range}]` : ''}: ${what} (${attempts} attempt${attempts === 1 ? '' : 's'})`);
    this.name = 'FetchError';
    Object.assign(this, { url, method, range, status, attempts, cause });
  }
}

function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const timer = setTimeout(resolve, ms);
    signal?.addEventListener('abort', () => {
      clearTimeout(timer);
      reject(abortError());
    }, { once: true });
  });
}

/**
 * Wraps fetch for the store: counts requests and bytes (Content-Length of non-HEAD responses), retries
 * network errors, 5xx and 429 after each delay in `delaysMs` (jittered +-30%), and turns everything that
 * still fails, or is not 200/206/404, into a FetchError. Every failed attempt is logged to the console.
 */
function resilientFetch(fetchImpl, network, delaysMs) {
  return async (request) => {
    const range = request.headers.get('Range');
    for (let attempt = 1; ; attempt++) {
      let status;
      let statusText;
      let cause;
      try {
        network.requests++;
        const response = await fetchImpl(request);
        if (response.status === 200 || response.status === 206 || response.status === 404) {
          if (request.method !== 'HEAD') network.bytes += Number(response.headers.get('Content-Length') ?? 0);
          return response;
        }
        ({ status, statusText } = response);
        cause = new Error(`HTTP ${status}`);
      } catch (error) {
        if (isAbort(error)) throw error;
        cause = error;
      }
      const retryable = status === undefined || status >= 500 || status === 429;
      const delayMs = retryable && attempt <= delaysMs.length ? delaysMs[attempt - 1] * (0.7 + 0.6 * Math.random()) : null;
      const details = { url: request.url, method: request.method, range, status, error: `${cause.name}: ${cause.message}`, attempt, of: delaysMs.length + 1, retryInMs: delayMs === null ? null : Math.round(delayMs) };
      if (delayMs === null) {
        console.error('chronozarr: request failed, giving up', details);
        throw new FetchError({ url: request.url, method: request.method, range, status, statusText, cause, attempts: attempt });
      }
      console.warn('chronozarr: request failed, retrying', details);
      await sleep(delayMs, request.signal);
    }
  };
}

function requireStore(condition, baseUrl, message) {
  if (!condition) throw new Error(`${baseUrl}: not a valid chronozarr v0.1 store: ${message}`);
}

/** How one level's `data` array is laid out in the store, from its zarr.json. */
function parseStorage(meta, path, baseUrl) {
  const where = `${baseUrl}: level ${path}`;
  requireStore(meta.zarr_format === 3 && meta.node_type === 'array', baseUrl, `${path}/zarr.json is not a Zarr v3 array`);
  requireStore(meta.data_type === 'uint16', baseUrl, `${where} dtype is ${meta.data_type}, expected uint16`);
  requireStore(meta.chunk_grid?.name === 'regular', baseUrl, `${where} chunk grid ${meta.chunk_grid?.name} is not supported`);
  const encoding = meta.chunk_key_encoding ?? { name: 'default' };
  const separator = encoding.configuration?.separator ?? (encoding.name === 'v2' ? '.' : '/');
  requireStore(encoding.name === 'default' || encoding.name === 'v2', baseUrl, `${where} chunk_key_encoding ${encoding.name} is not supported`);
  const storage = {
    fillValue: Number(meta.fill_value ?? 0),
    keyOf: (coords) => (encoding.name === 'default' ? ['c', ...coords] : coords).join(separator),
  };
  const sharding = meta.codecs.find((c) => c.name === 'sharding_indexed');
  if (!sharding) {
    return { ...storage, sharded: false, innerShape: meta.chunk_grid.configuration.chunk_shape, innerCodecs: meta.codecs };
  }
  const cfg = sharding.configuration;
  const indexCodecs = cfg.index_codecs.map((c) => c.name);
  requireStore(
    (indexCodecs.length === 1 && indexCodecs[0] === 'bytes') || (indexCodecs.length === 2 && indexCodecs[0] === 'bytes' && indexCodecs[1] === 'crc32c'),
    baseUrl,
    `${where} index_codecs [${indexCodecs}] are not supported`,
  );
  const shardShape = meta.chunk_grid.configuration.chunk_shape;
  const [shardT, shardB, shardH, shardW] = shardShape;
  const [innerT, innerB, innerH, innerW] = cfg.chunk_shape;
  requireStore(innerT === 1 && shardB === innerB && shardH === innerH && shardW === innerW, baseUrl, `${where} shard shape (${shardShape}) must be (n_time, n_band, y, x) over inner chunks (${cfg.chunk_shape})`);
  return {
    ...storage,
    sharded: true,
    shardTime: shardT,
    innerShape: cfg.chunk_shape,
    innerCodecs: cfg.codecs,
    indexAtStart: (cfg.index_location ?? 'end') === 'start',
    indexHasCrc: indexCodecs.includes('crc32c'),
  };
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
 * @param {object} [options.store]         a zarrita AsyncReadable to use instead of a FetchStore.
 * @param {number} [options.maxCacheBytes] decoded-chunk cache budget (default 1 GiB).
 * @param {number} [options.maxRequests]   cap on concurrent requests to the store (default 12).
 * @param {number[]} [options.retryDelaysMs] delays before each retry of a failed request (default [200, 600, 1500]).
 * @param {number} [options.workers]       decode workers (default: cores - 1, at most 8, none without Worker).
 * @param {() => object} [options.spawnWorker] creates a worker (tests); default: js/chronozarr/decode-worker.js.
 * @param {boolean} [options.suffixRequests] send `Range: bytes=-N` for shard indexes (one request, but a CORS
 *   preflight on cross-origin hosts) instead of zarrita's default HEAD + range (two simple requests).
 */
export async function openStore(baseUrl, options = {}) {
  const network = { requests: 0, bytes: 0 };
  const fetchImpl = options.fetch ?? ((request) => globalThis.fetch(request));
  const readable =
    options.store ??
    new zarr.FetchStore(baseUrl, {
      fetch: resilientFetch(fetchImpl, network, options.retryDelaysMs ?? DEFAULT_RETRY_DELAYS_MS),
      useSuffixRequest: options.suffixRequests ?? false,
    });

  const rootBytes = await readable.get('/zarr.json');
  requireStore(rootBytes, baseUrl, 'root zarr.json not found');
  const root = JSON.parse(new TextDecoder().decode(rootBytes));
  const cz = root.attributes?.chronozarr;
  requireStore(cz, baseUrl, 'root attributes have no "chronozarr" entry');
  requireStore(SUPPORTED_SPEC.test(String(cz.spec_version)), baseUrl, `unsupported spec_version ${cz.spec_version}`);
  requireStore(cz.temporal?.encoding === 'star-delta', baseUrl, `unsupported temporal encoding ${cz.temporal?.encoding}`);
  const datasets = root.attributes.multiscales?.[0]?.datasets;
  requireStore(Array.isArray(datasets) && datasets.length > 0, baseUrl, 'multiscales[0].datasets is missing');

  // One GET for everything when the root carries consolidated metadata, else one GET per level array.
  const consolidated = root.consolidated_metadata?.metadata ?? {};
  const arrayMetas = await Promise.all(
    datasets.map(async (ds) => {
      const path = `${ds.path}/${cz.variable}`;
      if (consolidated[path]) return consolidated[path];
      const bytes = await readable.get(`/${path}/zarr.json`);
      requireStore(bytes, baseUrl, `${path}/zarr.json not found`);
      return JSON.parse(new TextDecoder().decode(bytes));
    }),
  );
  const levels = datasets.map((ds, lod) => ({ path: ds.path, meta: arrayMetas[lod], storage: parseStorage(arrayMetas[lod], ds.path, baseUrl) }));
  const codecSignature = JSON.stringify(levels[0].storage.innerCodecs);
  requireStore(levels.every((l) => JSON.stringify(l.storage.innerCodecs) === codecSignature), baseUrl, 'levels use different chunk codecs');

  const spec = { dtype: 'uint16', shape: levels[0].storage.innerShape, codecs: levels[0].storage.innerCodecs };
  const workers = options.workers ?? defaultWorkerCount();
  const decoder =
    workers > 0
      ? new DecodePool({
          size: workers,
          spawn: options.spawnWorker ?? (() => new Worker(new URL('./decode-worker.js', import.meta.url), { type: 'module' })),
          init: { type: 'init', zarritaUrl: import.meta.resolve('zarrita'), spec },
          fallback: new MainThreadDecoder(zarr, spec),
        })
      : new MainThreadDecoder(zarr, spec);
  return new ChronoStore({ url: baseUrl, attrs: cz, levels, readable, decoder, network, maxCacheBytes: options.maxCacheBytes ?? DEFAULT_MAX_CACHE_BYTES, maxRequests: options.maxRequests ?? DEFAULT_MAX_REQUESTS });
}

export class ChronoStore {
  #levelStorage;
  #readable;
  #decoder;
  #anchors;
  #deltaReference;
  #cache = new Map();
  #bytes = 0;
  #clock = 0;
  #inflight = new Map();
  #shardIndexes = new Map();
  #demandInflight = 0;
  #demandIdleWaiters = [];
  #limiter;
  #cellFailures = new Map();

  /**
   * Eviction order: called with {lod,row,col,t,anchor}; the highest score is evicted first. The default
   * is least recently used. A viewer sets this to "farthest from what is on screen".
   */
  evictionScore = (entry) => -entry.used;
  /** Called with {type:'chunk', key, background, requestedAt, fetchedAt, decodedAt, bytes} per loaded chunk. */
  probe = null;

  constructor({ url, attrs, levels, readable, decoder, network, maxCacheBytes, maxRequests }) {
    this.url = url;
    this.variable = attrs.variable;
    this.times = attrs.times;
    this.bands = attrs.bands;
    this.nodata = attrs.nodata;
    this.crs = attrs.crs;
    this.temporal = attrs.temporal;
    this.maxCacheBytes = maxCacheBytes;
    this.#readable = readable;
    this.#decoder = decoder;
    this.#limiter = new RequestLimiter(maxRequests);
    this.#levelStorage = levels.map((l) => l.storage);
    this.#anchors = new Set(attrs.temporal.anchor_indices);
    this.#deltaReference = new Map(Object.entries(attrs.temporal.delta_reference).map(([t, a]) => [Number(t), a]));

    this.levels = levels.map(({ path, meta, storage }, lod) => {
      const [nTime, nBand, height, width] = meta.shape;
      const [, chunkB, chunkHeight, chunkWidth] = storage.innerShape;
      requireStore(nTime === this.times.length, url, `level ${lod} has ${nTime} timesteps, times attr has ${this.times.length}`);
      requireStore(nBand === this.bands.length, url, `level ${lod} has ${nBand} bands, bands attr has ${this.bands.length}`);
      requireStore(chunkB === nBand, url, `level ${lod} chunks must hold all ${nBand} bands, got ${chunkB}`);
      return {
        lod,
        path,
        nTime,
        nBand,
        height,
        width,
        chunkHeight,
        chunkWidth,
        gridRows: Math.ceil(height / chunkHeight),
        gridCols: Math.ceil(width / chunkWidth),
        chunkBytes: nBand * chunkHeight * chunkWidth * 2,
      };
    });
    for (let t = 0; t < this.times.length; t++) {
      requireStore(this.#anchors.has(t) || this.#deltaReference.has(t), url, `timestep ${t} is neither an anchor nor in delta_reference`);
    }

    this.stats = {
      network,
      cache: { hits: 0, misses: 0, joins: 0, anchorHits: 0, anchorMisses: 0, evictions: 0 },
      loads: { count: 0, fetchMs: 0, decodeMs: 0 },
    };
  }

  isAnchor(t) {
    return this.#anchors.has(t);
  }

  /** The anchor timestep needed to decode t (t itself for anchors). */
  anchorOf(t) {
    if (this.#anchors.has(t)) return t;
    const anchor = this.#deltaReference.get(t);
    if (anchor === undefined) throw new RangeError(`timestep ${t} out of range 0..${this.times.length - 1}`);
    return anchor;
  }

  get anchorIndices() {
    return [...this.#anchors].sort((a, b) => a - b);
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
    Object.assign(this.stats.cache, { hits: 0, misses: 0, joins: 0, anchorHits: 0, anchorMisses: 0, evictions: 0 });
    Object.assign(this.stats.loads, { count: 0, fetchMs: 0, decodeMs: 0 });
    Object.assign(this.stats.network, { requests: 0, bytes: 0 });
  }

  clearCache() {
    this.#cache.clear();
    this.#bytes = 0;
  }

  /** Abort every in-flight fetch and stop the decode workers. The store cannot be used afterwards. */
  close() {
    for (const entry of this.#inflight.values()) entry.controller.abort();
    this.#decoder.close();
  }

  cacheInfo() {
    let anchors = 0;
    for (const entry of this.#cache.values()) if (entry.anchor) anchors++;
    return { entries: this.#cache.size, anchors, deltas: this.#cache.size - anchors, bytes: this.#bytes };
  }

  /** Cached raw chunk (anchor: true values; delta: int16 residual bits) or undefined. Never fetches. */
  peekRaw(lod, row, col, t) {
    const entry = this.#cache.get(chunkKey(lod, row, col, t));
    if (!entry) return undefined;
    entry.used = ++this.#clock;
    return entry.data;
  }

  /**
   * Raw stored chunk for (lod,row,col,t) as a uint16 array laid out [band][y][x] over the full (padded)
   * chunk. Cached; concurrent requests for one key share one fetch. Treat as read-only.
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
    const cached = this.#cache.get(key);
    if (cached) {
      cached.used = ++this.#clock;
      this.stats.cache.hits++;
      if (anchor) this.stats.cache.anchorHits++;
      return cached.data;
    }
    let entry = this.#inflight.get(key);
    if (entry && !entry.controller.signal.aborted) {
      this.stats.cache.joins++;
    } else {
      this.stats.cache.misses++;
      if (anchor) this.stats.cache.anchorMisses++;
      entry = this.#start(key, { lod, row, col, t, anchor }, { background: false });
    }
    return this.#subscribe(entry, signal);
  }

  /**
   * Decoded values for (lod,row,col,t): a new uint16 array [band][y][x] over the padded chunk
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
    const plane = level.chunkHeight * level.chunkWidth;
    const offset = y * level.chunkWidth + x;
    const values = new Uint16Array(level.nBand);
    for (let b = 0; b < level.nBand; b++) {
      const a = anchor[b * plane + offset];
      if (!delta) {
        values[b] = a;
        continue;
      }
      const d = delta[b * plane + offset];
      const v = a + (d >= 32768 ? d - 65536 : d);
      values[b] = v < 0 ? 0 : v > 65535 ? 65535 : v;
    }
    return values;
  }

  /**
   * Background fetch of a time window around t for the given cells at one level, nearest first
   * (scrubCost order, so the scrub direction reaches further), pulling in a delta's anchor just before
   * the delta itself. The window is as wide as the cache budget allows for these cells: the whole time
   * axis when it fits, otherwise a slice around t. Chunks that would not fit are never fetched, and a
   * new chunk only displaces cached ones that score worse (see `evictionScore`). Waits while any
   * demand fetch is in flight. A cell whose chunk failed is left alone for PREFETCH_COOLDOWN_MS. Cancel by aborting `signal`; in-flight chunks finish and stay cached.
   * Resolves with counts and per-chunk errors (nothing is thrown or hidden).
   *
   * @param {{lod:number, cells:Array<[number, number]>, t:number, direction?:1|-1, behindFactor?:number, loop?:boolean,
   *   concurrency?:number, signal?:AbortSignal, onChunk?:(lod,row,col,t)=>void}} job
   */
  async prefetch({ lod, cells, t, direction = 1, behindFactor = 2, loop = false, concurrency = DEFAULT_PREFETCH_CONCURRENCY, signal, onChunk }) {
    const level = this.level(lod);
    const queue = this.#windowPlan(level, cells, t, { direction, behindFactor, loop });
    const result = { planned: queue.length, fetched: 0, skipped: 0, budgetReached: false, errors: [] };
    let next = 0;
    const worker = async () => {
      while (next < queue.length && !signal?.aborted && !result.budgetReached) {
        const [row, col, tt] = queue[next++];
        const key = chunkKey(lod, row, col, tt);
        const failedAt = this.#cellFailures.get(`${lod}/${row}/${col}`);
        if (this.#cache.has(key) || this.#inflight.has(key) || (failedAt !== undefined && performance.now() - failedAt < PREFETCH_COOLDOWN_MS)) {
          result.skipped++;
          continue;
        }
        await this.#demandIdle();
        if (signal?.aborted) return;
        const meta = { lod, row, col, t: tt, anchor: this.isAnchor(tt), used: this.#clock };
        if (this.#bytes + level.chunkBytes > this.maxCacheBytes && this.#worstScore() <= this.evictionScore(meta)) {
          result.budgetReached = true;
          return;
        }
        try {
          await this.#start(key, meta, { background: true }).promise;
          result.fetched++;
          onChunk?.(lod, row, col, tt);
        } catch (error) {
          if (isAbort(error)) continue;
          result.errors.push({ key, error });
          this.#cellFailures.set(`${lod}/${row}/${col}`, performance.now());
        }
      }
    };
    await Promise.all(Array.from({ length: concurrency }, worker));
    return result;
  }

  /** Chunks [row, col, t] to prefetch, in fetch order. */
  #windowPlan(level, cells, t, order) {
    const perCellLimit = Math.max(1, Math.floor((this.maxCacheBytes * WINDOW_BUDGET_FRACTION) / level.chunkBytes / Math.max(1, cells.length)));
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

  #checkCell(level, row, col) {
    if (!Number.isInteger(row) || !Number.isInteger(col) || row < 0 || col < 0 || row >= level.gridRows || col >= level.gridCols) {
      throw new RangeError(`cell (${row}, ${col}) outside ${level.gridRows}x${level.gridCols} grid at lod ${level.lod}`);
    }
  }

  // ---- loading ----

  /** Begin loading a chunk (fetch then decode) and register it as in flight. */
  #start(key, meta, { background }) {
    const controller = new AbortController();
    const entry = { controller, waiters: 0, sticky: background, promise: null };
    if (!background) this.#demandInflight++;
    entry.promise = this.#load(key, meta, controller.signal, background)
      .finally(() => {
        this.#inflight.delete(key);
        if (!background && --this.#demandInflight === 0) for (const wake of this.#demandIdleWaiters.splice(0)) wake();
      });
    this.#inflight.set(key, entry);
    return entry;
  }

  async #load(key, meta, signal, background) {
    const requestedAt = performance.now();
    const bytes = await this.#fetchBytes(meta, signal, background ? 1 : 0);
    const fetchedAt = performance.now();
    const level = this.levels[meta.lod];
    const data =
      bytes === undefined
        ? new Uint16Array(level.nBand * level.chunkHeight * level.chunkWidth).fill(this.#levelStorage[meta.lod].fillValue)
        : await this.#decoder.decode(bytes, background ? 1 : 0, signal);
    const decodedAt = performance.now();
    this.stats.loads.count++;
    this.stats.loads.fetchMs += fetchedAt - requestedAt;
    this.stats.loads.decodeMs += decodedAt - fetchedAt;
    this.probe?.({ type: 'chunk', key, t: meta.t, background, requestedAt, fetchedAt, decodedAt, bytes: bytes?.length ?? 0 });
    this.#insert(key, { ...meta, data, used: ++this.#clock }, { background });
    return data;
  }

  /** Compressed bytes of one chunk, or undefined when the store has no such chunk (fill value). */
  async #fetchBytes({ lod, row, col, t }, signal, priority) {
    const storage = this.#levelStorage[lod];
    const base = `/${this.levels[lod].path}/${this.variable}/`;
    if (!storage.sharded) {
      return this.#limiter.run(priority, signal, () => this.#readable.get(base + storage.keyOf([t, 0, row, col]), { signal }));
    }
    const shardKey = base + storage.keyOf([Math.floor(t / storage.shardTime), 0, row, col]);
    const index = await this.#shardIndex(lod, row, col, shardKey, storage, priority);
    if (index === undefined) return undefined;
    const at = t % storage.shardTime;
    const offset = index[2 * at];
    if (offset < 0) return undefined;
    return this.#limiter.run(priority, signal, () => this.#readable.getRange(shardKey, { offset, length: index[2 * at + 1] }, { signal }));
  }

  /** Shard index for a cell: read once, shared by every chunk of the shard, never tied to one caller's signal. */
  #shardIndex(lod, row, col, shardKey, storage, priority) {
    const cacheKey = `${lod}/${row}/${col}/${shardKey}`;
    let promise = this.#shardIndexes.get(cacheKey);
    if (!promise) {
      promise = (async () => {
        const length = indexByteLength(storage.shardTime, storage.indexHasCrc);
        const range = storage.indexAtStart ? { offset: 0, length } : { suffixLength: length };
        const bytes = await this.#limiter.run(priority, undefined, () => this.#readable.getRange(shardKey, range));
        return bytes && parseShardIndex(bytes, storage.shardTime, storage.indexHasCrc, `${this.url}${shardKey}`);
      })();
      promise.catch(() => this.#shardIndexes.delete(cacheKey));
      this.#shardIndexes.set(cacheKey, promise);
    }
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
        if (entry.waiters === 0 && !entry.sticky) entry.controller.abort();
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

  #demandIdle() {
    return this.#demandInflight === 0 ? Promise.resolve() : new Promise((resolve) => this.#demandIdleWaiters.push(resolve));
  }

  // ---- cache ----

  #worstScore() {
    let worst = -Infinity;
    for (const entry of this.#cache.values()) worst = Math.max(worst, this.evictionScore(entry));
    return worst;
  }

  /** Add a chunk, evicting the highest-scoring entries while over budget. Background inserts never displace a better chunk. */
  #insert(key, entry, { background }) {
    const size = entry.data.byteLength;
    this.#cache.set(key, entry);
    this.#bytes += size;
    while (this.#bytes > this.maxCacheBytes) {
      let victimKey = null;
      let victimScore = -Infinity;
      for (const [k, candidate] of this.#cache) {
        if (k === key) continue;
        const score = this.evictionScore(candidate);
        if (score > victimScore) {
          victimKey = k;
          victimScore = score;
        }
      }
      if (victimKey === null) return;
      if (background && victimScore <= this.evictionScore(entry)) {
        this.#cache.delete(key);
        this.#bytes -= size;
        return;
      }
      this.#bytes -= this.#cache.get(victimKey).data.byteLength;
      this.#cache.delete(victimKey);
      this.stats.cache.evictions++;
    }
  }
}
