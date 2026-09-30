// chronozarr v0.1 reader. No DOM. Runs in browsers (import map for "zarrita") and Node.
//
// Coordinates: (lod, row, col, t). One cell is one spatial chunk of one level; its stored
// bytes for a timestep are one zarr inner chunk of shape (1, n_band, chunkH, chunkW).
// Star-delta: anchors hold true uint16 values; every other timestep holds an int16 residual
// (stored as uint16 bits) against `delta_reference[t]`.

import * as zarr from 'zarrita';

const SUPPORTED_SPEC = /^0\.1\./;
const DEFAULT_MAX_CACHE_BYTES = 1024 * 1024 * 1024;
const DEFAULT_PREFETCH_CONCURRENCY = 6;

export function chunkKey(lod, row, col, t) {
  return `${lod}/${row}/${col}/${t}`;
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

/**
 * zarrita 0.7.5 always reads a shard index from the end of the shard object and ignores
 * `index_location`. For arrays whose sharding codec declares `index_location: "start"`, this
 * wrapper answers zarrita's suffix-range index read with a prefix range of the same length.
 * Chunk offsets inside a shard index are absolute within the shard object, so they pass through.
 */
class ShardIndexLocationStore {
  #inner;
  #startIndexed = new Set();

  constructor(inner) {
    this.#inner = inner;
  }

  async get(key, options) {
    const bytes = await this.#inner.get(key, options);
    if (bytes && key.endsWith('/zarr.json')) this.#learn(key, bytes);
    return bytes;
  }

  getRange(key, range, options) {
    if ('suffixLength' in range && this.#isStartIndexed(key)) {
      return this.#inner.getRange(key, { offset: 0, length: range.suffixLength }, options);
    }
    return this.#inner.getRange(key, range, options);
  }

  #learn(key, bytes) {
    const meta = JSON.parse(new TextDecoder().decode(bytes));
    if (meta.node_type !== 'array') return;
    const sharding = meta.codecs.find((c) => c.name === 'sharding_indexed');
    if (sharding?.configuration.index_location === 'start') {
      this.#startIndexed.add(key.slice(0, key.length - 'zarr.json'.length));
    }
  }

  #isStartIndexed(key) {
    for (const prefix of this.#startIndexed) if (key.startsWith(prefix)) return true;
    return false;
  }
}

/** Serves an already-fetched root zarr.json so metadata layers on top do not fetch it twice. */
function withPreloadedRoot(store, rootBytes) {
  return {
    get: (key, options) => (key === '/zarr.json' ? Promise.resolve(rootBytes) : store.get(key, options)),
    getRange: (key, range, options) => store.getRange(key, range, options),
  };
}

function countingFetch(fetchImpl, network) {
  return async (request) => {
    network.requests++;
    const response = await fetchImpl(request);
    if (request.method !== 'HEAD') network.bytes += Number(response.headers.get('Content-Length') ?? 0);
    return response;
  };
}

function requireAttr(condition, baseUrl, message) {
  if (!condition) throw new Error(`${baseUrl}: not a valid chronozarr v0.1 store: ${message}`);
}

/**
 * Open a chronozarr store.
 *
 * @param {string} baseUrl  URL of the store root (the directory holding zarr.json).
 * @param {object} [options]
 * @param {typeof fetch} [options.fetch]   fetch implementation (default: globalThis.fetch at call time).
 * @param {object} [options.store]         a zarrita AsyncReadable to use instead of a FetchStore.
 * @param {number} [options.maxCacheBytes] decoded-chunk cache budget (default 1 GiB).
 * @param {boolean} [options.suffixRequests] send `Range: bytes=-N` for shard indexes (one request, but a CORS
 *   preflight on cross-origin hosts) instead of zarrita's default HEAD + range (two simple requests).
 */
export async function openStore(baseUrl, options = {}) {
  const network = { requests: 0, bytes: 0 };
  const fetchImpl = options.fetch ?? ((request) => globalThis.fetch(request));
  const base =
    options.store ??
    new zarr.FetchStore(baseUrl, {
      fetch: countingFetch(fetchImpl, network),
      useSuffixRequest: options.suffixRequests ?? false,
    });

  const rootBytes = await base.get('/zarr.json');
  requireAttr(rootBytes, baseUrl, 'root zarr.json not found');
  const consolidated = await zarr.withMaybeConsolidatedMetadata(withPreloadedRoot(base, rootBytes), {
    format: 'v3',
  });
  const readable = new ShardIndexLocationStore(consolidated);

  const rootLocation = zarr.root(readable);
  const group = await zarr.open.v3(rootLocation, { kind: 'group' });
  const cz = group.attrs.chronozarr;
  requireAttr(cz, baseUrl, 'root attributes have no "chronozarr" entry');
  requireAttr(SUPPORTED_SPEC.test(String(cz.spec_version)), baseUrl, `unsupported spec_version ${cz.spec_version}`);
  requireAttr(cz.temporal?.encoding === 'star-delta', baseUrl, `unsupported temporal encoding ${cz.temporal?.encoding}`);
  const datasets = group.attrs.multiscales?.[0]?.datasets;
  requireAttr(Array.isArray(datasets) && datasets.length > 0, baseUrl, 'multiscales[0].datasets is missing');

  const arrays = await Promise.all(
    datasets.map((ds) => zarr.open.v3(rootLocation.resolve(`${ds.path}/${cz.variable}`), { kind: 'array' })),
  );
  const store = new ChronoStore(baseUrl, cz, arrays, network, options.maxCacheBytes ?? DEFAULT_MAX_CACHE_BYTES);
  return store;
}

export class ChronoStore {
  #arrays;
  #anchors;
  #deltaReference;
  #anchorCache = new Map();
  #deltaCache = new Map();
  #inflight = new Map();
  #bytes = 0;

  constructor(url, attrs, arrays, network, maxCacheBytes) {
    this.url = url;
    this.times = attrs.times;
    this.bands = attrs.bands;
    this.nodata = attrs.nodata;
    this.crs = attrs.crs;
    this.temporal = attrs.temporal;
    this.maxCacheBytes = maxCacheBytes;
    this.#arrays = arrays;
    this.#anchors = new Set(attrs.temporal.anchor_indices);
    this.#deltaReference = new Map(Object.entries(attrs.temporal.delta_reference).map(([t, a]) => [Number(t), a]));

    this.levels = arrays.map((array, lod) => {
      const [nTime, nBand, height, width] = array.shape;
      const [chunkT, chunkB, chunkHeight, chunkWidth] = array.chunks;
      requireAttr(array.dtype === 'uint16', url, `level ${lod} dtype is ${array.dtype}, expected uint16`);
      requireAttr(nTime === this.times.length, url, `level ${lod} has ${nTime} timesteps, times attr has ${this.times.length}`);
      requireAttr(nBand === this.bands.length, url, `level ${lod} has ${nBand} bands, bands attr has ${this.bands.length}`);
      requireAttr(chunkT === 1 && chunkB === nBand, url, `level ${lod} chunk shape must be (1, ${nBand}, y, x), got (${array.chunks})`);
      return {
        lod,
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
      requireAttr(this.#anchors.has(t) || this.#deltaReference.has(t), url, `timestep ${t} is neither an anchor nor in delta_reference`);
    }

    this.stats = {
      network,
      cache: { hits: 0, misses: 0, joins: 0, anchorHits: 0, anchorMisses: 0, evictions: 0 },
      loads: { count: 0, totalMs: 0 },
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
    Object.assign(this.stats.loads, { count: 0, totalMs: 0 });
    Object.assign(this.stats.network, { requests: 0, bytes: 0 });
  }

  clearCache() {
    this.#anchorCache.clear();
    this.#deltaCache.clear();
    this.#bytes = 0;
  }

  cacheInfo() {
    return {
      entries: this.#anchorCache.size + this.#deltaCache.size,
      anchors: this.#anchorCache.size,
      deltas: this.#deltaCache.size,
      bytes: this.#bytes,
    };
  }

  /** Cached raw chunk (anchor: true values; delta: int16 residual bits) or undefined. Never fetches. */
  peekRaw(lod, row, col, t) {
    return this.#cacheGet(chunkKey(lod, row, col, t), this.isAnchor(t));
  }

  /**
   * Raw stored chunk for (lod,row,col,t) as a uint16 array laid out [band][y][x] over the full
   * (padded) chunk. Cached; concurrent requests for one key share one fetch. Treat as read-only.
   */
  async getRaw(lod, row, col, t) {
    const level = this.level(lod);
    this.#checkCell(level, row, col);
    if (!Number.isInteger(t) || t < 0 || t >= level.nTime) {
      throw new RangeError(`timestep ${t} out of range 0..${level.nTime - 1}`);
    }
    const key = chunkKey(lod, row, col, t);
    const anchor = this.isAnchor(t);
    const cached = this.#cacheGet(key, anchor);
    if (cached) {
      this.stats.cache.hits++;
      if (anchor) this.stats.cache.anchorHits++;
      return cached;
    }
    if (this.#inflight.has(key)) {
      this.stats.cache.joins++;
      return this.#inflight.get(key);
    }
    this.stats.cache.misses++;
    if (anchor) this.stats.cache.anchorMisses++;
    return this.#load(key, lod, row, col, t, { evict: true });
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
   * Background fetch for the given cells at one level: all anchors first (nearest to t first),
   * then deltas outward from t, starting in `direction` (+1 forward, -1 backward). Never evicts:
   * stops when the cache budget is reached. Cancel by aborting `signal`; in-flight chunks finish
   * and stay cached. Resolves with counts and any per-chunk errors (nothing is thrown or hidden).
   *
   * @param {{lod:number, cells:Array<[number, number]>, t:number, direction?:1|-1,
   *   concurrency?:number, signal?:AbortSignal, onChunk?:(lod,row,col,t)=>void}} job
   */
  async prefetch({ lod, cells, t, direction = 1, concurrency = DEFAULT_PREFETCH_CONCURRENCY, signal, onChunk }) {
    const level = this.level(lod);
    const queue = this.#prefetchOrder(level, cells, t, direction);
    const result = { fetched: 0, skipped: 0, budgetReached: false, errors: [] };
    let next = 0;
    const worker = async () => {
      while (next < queue.length && !signal?.aborted && !result.budgetReached) {
        const [row, col, tt] = queue[next++];
        const key = chunkKey(lod, row, col, tt);
        if (this.#cacheGet(key, this.isAnchor(tt), false) || this.#inflight.has(key)) {
          result.skipped++;
          continue;
        }
        if (this.#bytes + level.chunkBytes > this.maxCacheBytes) {
          result.budgetReached = true;
          break;
        }
        try {
          await this.#load(key, lod, row, col, tt, { evict: false });
          result.fetched++;
          onChunk?.(lod, row, col, tt);
        } catch (error) {
          result.errors.push({ key, error });
        }
      }
    };
    await Promise.all(Array.from({ length: concurrency }, worker));
    return result;
  }

  #prefetchOrder(level, cells, t, direction) {
    const order = [];
    const anchors = this.anchorIndices.sort((a, b) => Math.abs(a - t) - Math.abs(b - t));
    for (const a of anchors) for (const [row, col] of cells) order.push([row, col, a]);
    for (let d = 1; d < level.nTime; d++) {
      for (const tt of [t + direction * d, t - direction * d]) {
        if (tt < 0 || tt >= level.nTime || this.isAnchor(tt)) continue;
        for (const [row, col] of cells) order.push([row, col, tt]);
      }
    }
    return order;
  }

  #checkCell(level, row, col) {
    if (!Number.isInteger(row) || !Number.isInteger(col) || row < 0 || col < 0 || row >= level.gridRows || col >= level.gridCols) {
      throw new RangeError(`cell (${row}, ${col}) outside ${level.gridRows}x${level.gridCols} grid at lod ${level.lod}`);
    }
  }

  #cacheGet(key, anchor, touch = true) {
    const cache = anchor ? this.#anchorCache : this.#deltaCache;
    const data = cache.get(key);
    if (data && touch) {
      cache.delete(key);
      cache.set(key, data);
    }
    return data;
  }

  #load(key, lod, row, col, t, { evict }) {
    const promise = (async () => {
      const started = performance.now();
      const chunk = await this.#arrays[lod].getChunk([t, 0, row, col]);
      this.stats.loads.count++;
      this.stats.loads.totalMs += performance.now() - started;
      const data = chunk.data;
      if (!(data instanceof Uint16Array)) throw new Error(`${this.url}: chunk ${key} decoded to ${data.constructor.name}, expected Uint16Array`);
      this.#insert(key, data, this.isAnchor(t), evict);
      return data;
    })().finally(() => this.#inflight.delete(key));
    this.#inflight.set(key, promise);
    return promise;
  }

  #insert(key, data, anchor, evict) {
    (anchor ? this.#anchorCache : this.#deltaCache).set(key, data);
    this.#bytes += data.byteLength;
    if (!evict) return;
    // Deltas go first (least recently used), anchors only when no deltas remain.
    for (const cache of [this.#deltaCache, this.#anchorCache]) {
      while (this.#bytes > this.maxCacheBytes && cache.size > 0) {
        const [oldestKey, oldest] = cache.entries().next().value;
        if (oldestKey === key) break;
        cache.delete(oldestKey);
        this.#bytes -= oldest.byteLength;
        this.stats.cache.evictions++;
      }
    }
  }
}
