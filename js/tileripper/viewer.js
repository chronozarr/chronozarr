// TileRipper viewer: renders a chronozarr store with WebGL2, scrubs through time, switches
// products on the GPU and shows decoded values on click. Opens ?store=<base url>.

import { chunkKey, openStore, samplePixelFrom, scrubCost, windowOrder } from '../chronozarr/decoder.js';
import { buildSeries, chartRange, gapFilledTimes, seriesPath, seriesSpecs, timeFromX, windowPixels, xFromTime } from './chart.js';
import { ASSUMED_BANDWIDTH, ancestorCells, planCoarseStages, stageLeadMs } from './coarse.js';
import { DEFAULT_STEPS_PER_SECOND, Playback, SPEEDS, chooseMovieLevel, describeReason, linkAllows, snapSpeed, wireRatio } from './playback.js';
import { decodeView, encodeView } from './permalink.js';
import { toggleExportPanel } from './export.js';
import { FrameMonitor, formatBytes, formatMs, formatRate, hitRate, readStats } from './perf.js';
import { Renderer } from './renderer.js';
import {
  computeStretchLo,
  describePixel,
  displayMode,
  findBand,
  inputConversion,
  inputIndices,
  makeTimeFormatter,
  normalizeBands,
  percentileRange,
  resolveProducts,
  toPhysical,
} from './products.js';

const PREFETCH_SETTLE_MS = 30;
const PREFETCH_PLAYBACK_RESTART_MS = 250;
const BEHIND_FACTOR = 2;
const BEHIND_FACTOR_PLAYING = 8;
const GPU_SLICE_FRACTION_OF_FRAME = 0.25;
const POOL_BUDGET_BYTES = 384 * 1024 * 1024;
const GPU_FILL_FRACTION = 0.9;
const GPU_UPLOAD_SLICE_MS = 4;
// Pick the coarsest level that still has at least ~0.7 texels per canvas pixel; a bias of 0 would pick
// the finest level whenever it is even slightly denser than the screen, at 4x the bytes per level.
const LOD_BIAS = 0.5;
const MAX_SCALE = 16;
const CLICK_SLOP_PX = 4;
const CELL_RETRY_DELAY_MS = 4000;
const MAX_CELL_RETRIES = 3;
const TOAST_MS = 12000;
const SPEED_KEY = 'tileripper.stepsPerSecond';
const STRETCH_SAMPLES_PER_CELL = 300;
const URL_SYNC_MS = 300;
const CHART_BATCH = 4;
const CHART_RENDER_MS = 120;
// A time change of more than this many steps is a jump: the speculative fetches around the old position are dropped.
const SEEK_DISTANCE = 4;
const PERF_UPDATE_MS = 250;
const GAP_HATCH_PX = 8;
const GAP_MASK_CACHE = 48;
// Chart geometry in SVG units (the svg scales to the sidebar width).
const CHART = { width: 264, height: 124, left: 34, right: 256, top: 8, bottom: 104 };

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Resolves after `ms`, or as soon as `signal` aborts. */
function delay(ms, signal) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

/** The last playback speed, if localStorage has a usable one (it can be missing or blocked). */
function loadSpeed() {
  try {
    const stored = localStorage.getItem(SPEED_KEY);
    if (stored !== null && Number.isFinite(Number(stored))) return snapSpeed(Number(stored));
  } catch (error) {
    console.warn('could not read the saved playback speed:', error);
  }
  return DEFAULT_STEPS_PER_SECOND;
}

function saveSpeed(stepsPerSecond) {
  try {
    localStorage.setItem(SPEED_KEY, String(stepsPerSecond));
  } catch (error) {
    console.warn('could not save the playback speed:', error);
  }
}

class Viewer {
  canvas = $('gl-canvas');
  renderer;
  store = null;
  products = [];
  productIndex = 0;
  bandChoice = 0;
  /** The store's bands as {name, common_name?, scale, offset, divisor, units?}. */
  bands = [];
  /** Data type of the store: uint8, uint16, int16 or float32. */
  dtype = 'uint16';
  t = 0;
  camera = { cx: 0, cy: 0, scale: 1 };
  /** Force one LOD regardless of zoom (benchmarks). */
  lodOverride = null;
  /** Benchmarks set this to receive timestamped events: input, load-start, cell-ready, paint. */
  probe = null;

  /** Timestep of the last complete frame. */
  paintedT = -1;

  #formatTime = (t) => String(t);
  #direction = 1;
  #stretchLo = null;
  /** Linear stretch of a single band that is not reflectance: the [lo, hi] range (null until measured) and whether the user set it. */
  #linear = { range: null, manual: false };
  #paintPartial = true;
  /** A coarse stage landed that has not been painted yet (a time jump paints without clearing, so it needs asking). */
  #coarsePending = false;
  #paintedLod = 0;
  #view = { lod: 0, cells: new Set() };
  #maxVisibleCells = 0;
  #rafId = 0;
  #prefetchTimer = 0;
  #prefetchStartedAt = -Infinity;
  #gpuFillTimer = 0;
  #prefetchAbort = null;
  #painted = [];
  #storeGeneration = 0;
  #tickElements = [];
  #wave = null;
  #toastTimer = 0;
  #playback = null;
  #lookahead = null;
  #exportHold = null;
  #speed = loadSpeed();
  #movie = { baseLod: 0, lod: 0, reason: null, detail: null };
  #wasPlaying = false;
  #chart = null;
  #chartTimer = 0;
  #urlTimer = 0;
  #viewTiming = null;
  #movieMemo = null;
  #playEpoch = 0;
  #seekPending = false;
  /** How long the store's metadata took to fetch: one round trip to the store, more or less. */
  #roundTripMs = 0;
  #perf = { monitor: new FrameMonitor(), timer: 0 };
  #gaps = { visible: false, masks: new Map(), requested: new Set() };
  #hatch = null;
  /** The store URL to keep in the address bar (?store=), or null for a catalog store. Set by the page. */
  pinnedStore = null;

  constructor() {
    this.renderer = new Renderer(this.canvas);
    this.renderer.evictionScore = (meta) => this.#chunkScore(meta);
    this.#resizeCanvas();
    new ResizeObserver(() => {
      this.#resizeCanvas();
      this.#beginView('camera');
      this.#paintPartial = true;
      this.requestRender();
    }).observe(this.canvas.parentElement);
    this.#bindInput();
    this.#bindChart();
    setInterval(() => this.#updateCacheStats(), 500);
  }

  /**
   * Open a store and resolve after the first complete frame is painted. The result has the time to open the
   * metadata (`openMs`), to the first frame that covers the whole view at some level (`coarseMs`, from the call;
   * `coarseAfterMetadataMs`) and to the first complete frame at the target level (`firstPaintMs`).
   * @param {string} url
   * @param {{lod?:number, fetch?:typeof fetch, maxCacheBytes?:number, workers?:number, camera?:{cx:number,cy:number,scale:number},
   *   viewSearch?:string}} [options]  `viewSearch`: a query string whose t, p, b, z, c parameters restore the view (see permalink.js)
   */
  async loadStore(url, options = {}) {
    this.#playback?.pause();
    this.#clearChart();
    this.#abortBackground();
    this.store?.close();
    const generation = ++this.#storeGeneration;
    this.#hideError();
    this.#setProgress(0.02);
    const started = performance.now();
    let store;
    try {
      store = await openStore(url, { fetch: options.fetch, maxCacheBytes: options.maxCacheBytes, workers: options.workers });
      this.#checkUniformChunks(store);
    } catch (error) {
      this.#showError('Could not open store', error.message);
      this.#setProgress(0);
      throw error;
    }
    if (generation !== this.#storeGeneration) {
      store.close();
      return null;
    }
    const openMs = performance.now() - started;
    this.#roundTripMs = openMs;

    this.store = store;
    store.evictionScore = (entry) => this.#storeScore(entry);
    this.bands = normalizeBands(store.attrs.bands);
    this.dtype = store.dtype;
    this.#configurePool();
    this.#playback = this.#createPlayback();
    this.#movieMemo = null;
    this.#linear = { range: null, manual: false };
    this.#gaps = { visible: false, masks: new Map(), requested: new Set() };
    this.#drawGapOverlay();
    this.products = resolveProducts(this.bands);
    this.productIndex = this.products.findIndex((p) => p.available);
    this.bandChoice = 0;
    this.t = 0;
    this.paintedT = -1;
    this.#direction = 1;
    this.#stretchLo = null;
    this.lodOverride = options.lod ?? null;
    this.#formatTime = makeTimeFormatter(store.times);
    this.#paintPartial = true;
    const bandNames = this.bands.map((band) => band.name);
    const view = options.viewSearch
      ? decodeView(options.viewSearch, { count: store.times.length, productIds: this.products.filter((p) => p.available).map((p) => p.id), bands: bandNames, transform: store.transform })
      : {};
    if (view.t !== undefined) this.t = view.t;
    if (view.productId !== undefined) this.productIndex = this.products.findIndex((p) => p.id === view.productId);
    if (view.bandName !== undefined) this.bandChoice = bandNames.indexOf(view.bandName);
    if (options.camera) this.camera = { ...options.camera };
    else if (view.zoom !== undefined || view.center !== undefined) this.#restoreCamera(view);
    else this.fit();
    this.#buildProducts();
    this.#buildTimeline();
    this.#updateTimeUi();
    this.#updatePlayUi();
    this.#updateMeta();
    this.#updateSidebar(null);
    $('click-hint').classList.remove('hidden');
    this.#updateGapToggle();

    const painted = this.whenPainted();
    this.#beginView('open');
    this.renderNow();
    await painted;
    this.#setProgress(1);
    this.syncUrl();
    const { startedAt, coarseAt } = this.#viewTiming;
    return {
      openMs,
      firstPaintMs: performance.now() - started,
      coarseMs: coarseAt === null ? null : coarseAt - started,
      coarseAfterMetadataMs: coarseAt === null ? null : coarseAt - startedAt,
    };
  }

  /**
   * A new view change starts (store open, camera move, time change): the clock for "time to coarse / full frame".
   * A camera move also ends the prefetch for the old view, which would otherwise keep taking the link from the
   * cells the new view is waiting for.
   */
  #beginView(kind) {
    this.#viewTiming = { kind, startedAt: performance.now(), coarseAt: null, fullAt: null };
    if (kind === 'camera') this.#dropSpeculativeFetches();
  }

  /** How long the last view change took to show a frame covering the whole view (`coarseMs`) and a complete one (`fullMs`); null while pending. */
  get viewTiming() {
    const timing = this.#viewTiming;
    if (!timing) return null;
    return {
      kind: timing.kind,
      coarseMs: timing.coarseAt === null ? null : timing.coarseAt - timing.startedAt,
      fullMs: timing.fullAt === null ? null : timing.fullAt - timing.startedAt,
    };
  }

  fit() {
    const level = this.store.levels[0];
    const { width, height } = this.canvas;
    const scale = Math.min(width / level.width, height / level.height) * 0.94;
    this.camera = { cx: level.width / 2, cy: level.height / 2, scale };
    this.#beginView('camera');
    this.#paintPartial = true;
    this.requestRender();
    this.#scheduleUrlSync();
  }

  /** Camera from a decoded permalink: zoom is CSS pixels per level-0 pixel, the center is in level-0 pixels. */
  #restoreCamera({ zoom, center }) {
    const level = this.store.levels[0];
    const scale = zoom === undefined ? this.fitScale : clamp(zoom * (window.devicePixelRatio || 1), this.fitScale * 0.5, MAX_SCALE);
    this.camera = {
      cx: center ? clamp(center.col, 0, level.width) : level.width / 2,
      cy: center ? clamp(center.row, 0, level.height) : level.height / 2,
      scale,
    };
  }

  get fitScale() {
    const level = this.store.levels[0];
    return Math.min(this.canvas.width / level.width, this.canvas.height / level.height) * 0.94;
  }

  /**
   * Show timestep t. A manual call (keys, buttons, the timeline) pauses playback; playback itself passes
   * `playing` and keeps its direction forward when it loops from the last timestep to the first.
   */
  goToTime(t, { playing = false, direction = null } = {}) {
    if (!this.store) return;
    if (!playing) this.#playback?.pause();
    const next = clamp(t, 0, this.store.times.length - 1);
    if (next === this.t) return;
    if (!playing) {
      this.#beginView('time');
      if (Math.abs(next - this.t) > SEEK_DISTANCE) this.#dropSpeculativeFetches();
    }
    this.#direction = direction ?? (next > this.t ? 1 : -1);
    this.t = next;
    this.#emit({ type: 'input', t: next });
    this.#updateTimeUi();
    if (playing) this.renderNow();
    else {
      this.requestRender();
      this.#scheduleUrlSync();
    }
  }

  /**
   * After a jump in time or a camera move the prefetch window around the old view is stale: cancel what it still has
   * in flight (what the demand fetches for the new view also need stays), and have the next prefetch say so with `seek`.
   */
  #dropSpeculativeFetches() {
    this.#seekPending = true;
    clearTimeout(this.#prefetchTimer);
    this.#prefetchTimer = 0;
    this.#prefetchAbort?.abort();
  }

  /** The address bar keeps the store (when not from the catalog) and whatever differs from the default view. */
  syncUrl() {
    clearTimeout(this.#urlTimer);
    this.#urlTimer = 0;
    if (!this.store || this.#playing) return;
    const level = this.store.levels[0];
    const { cx, cy, scale } = this.camera;
    const atFit = Math.abs(scale / this.fitScale - 1) < 0.01 && Math.abs(cx - level.width / 2) < 1 && Math.abs(cy - level.height / 2) < 1;
    const product = this.products[this.productIndex];
    const view = encodeView(
      {
        t: this.t === 0 ? null : this.t,
        productId: this.productIndex === this.products.findIndex((p) => p.available) ? null : product.id,
        bandName: product.id === 'band' && this.bandChoice !== 0 ? this.bands[this.bandChoice].name : null,
        zoom: atFit ? null : scale / (window.devicePixelRatio || 1),
        center: atFit ? null : { col: cx, row: cy },
      },
      { transform: this.store.transform },
    );
    const query = [this.pinnedStore ? `store=${encodeURIComponent(this.pinnedStore)}` : '', view].filter(Boolean).join('&');
    history.replaceState(null, '', query ? `?${query}` : location.pathname);
  }

  #scheduleUrlSync() {
    if (this.#urlTimer) return;
    this.#urlTimer = setTimeout(() => this.syncUrl(), URL_SYNC_MS);
  }

  get speed() {
    return this.#speed;
  }

  timeLabel(t) {
    return this.#formatTime(t);
  }

  /**
   * An offscreen renderer for exporting the view on screen (same cells, camera center, product and colour
   * stretch), at `scale` times the canvas size. The level is the normal one when `timesteps` steps of the
   * visible cells fit the decoded cache, else the coarser one a movie would play at. It has its own small
   * texture pool and reads the decoded chunk cache, so the viewer keeps working while it runs. See export.js.
   */
  createExportSession({ scale = 1, timesteps = this.store.times.length } = {}) {
    const { store } = this;
    const baseLod = this.#normalLod();
    const deepestLod = Math.max(baseLod, this.#lodForScale(this.camera.scale / 4));
    const lod = this.lodOverride ?? chooseMovieLevel({ baseLod, deepestLod, fits: (candidate) => store.loopFits(candidate, this.#visibleCells(candidate).length, timesteps) }).lod;
    const cells = this.#visibleCells(lod);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(this.canvas.width * scale));
    canvas.height = Math.max(1, Math.round(this.canvas.height * scale));
    const hold = { lod, cells: new Set(cells.map(([row, col]) => `${row}/${col}`)) };
    this.#exportHold = hold;
    clearTimeout(this.#prefetchTimer);
    this.#prefetchTimer = 0;
    this.#prefetchAbort?.abort();
    const renderer = new Renderer(canvas);
    const first = store.levels[0];
    renderer.configure({ dtype: this.dtype, nBand: first.nBand, chunkWidth: first.chunkWidth, chunkHeight: first.chunkHeight, slots: Math.max(4, 2 * cells.length + 4) });
    const camera = { ...this.camera, scale: this.camera.scale * scale };
    const chunksFor = (t) => cells.flatMap(([row, col]) => [...new Set([store.anchorOf(t), t])].map((ct) => [row, col, ct]));
    return {
      canvas,
      lod,
      cellCount: cells.length,
      fits: (timesteps) => store.loopFits(lod, cells.length, timesteps),
      isReady: (t) => chunksFor(t).every(([row, col, ct]) => store.peekRaw(lod, row, col, ct)),
      /** Fetch timestep t for every cell at demand priority. */
      prepare: (t, signal) => Promise.all(chunksFor(t).map(([row, col, ct]) => store.getRaw(lod, row, col, ct, { signal }))),
      /** Draw timestep t; false if some cell's chunks are not in the cache (that cell is left empty). */
      render: (t) => {
        renderer.newFrame();
        const drawable = [];
        let complete = true;
        for (const [row, col] of cells) {
          const slots = this.#slotsFor(lod, row, col, t, renderer);
          if (slots) drawable.push({ row, col, slots });
          else complete = false;
        }
        renderer.beginPaint({ width: canvas.width, height: canvas.height, ...camera, ...this.#productUniforms(), stretchLo: this.#stretchLo ?? 0, nodata: store.nodata ?? null }, { clear: true });
        for (const { row, col, slots } of drawable) this.#drawCell(lod, row, col, slots, renderer);
        return complete;
      },
      readPixels: () => renderer.readFrame(canvas.width, canvas.height),
      close: () => {
        renderer.dispose();
        if (this.#exportHold !== hold) return;
        this.#exportHold = null;
        this.#schedulePrefetch();
      },
    };
  }

  get playback() {
    return this.#playback;
  }

  play() {
    this.#playback?.play();
  }

  pause() {
    this.#playback?.pause();
  }

  togglePlay() {
    this.#playback?.toggle();
  }

  /** Steps per second for movie playback, kept between visits. */
  setSpeed(stepsPerSecond) {
    this.#speed = snapSpeed(stepsPerSecond);
    saveSpeed(this.#speed);
    this.#playback?.setSpeed(this.#speed);
    this.#updateSpeedUi();
  }

  /** Whether every visible cell has the chunks for timestep t in memory, so showing it needs no fetch. */
  isTimestepReady(t) {
    const lod = this.#targetLod();
    const anchorT = this.store.anchorOf(t);
    for (const [row, col] of this.#visibleCells(lod)) {
      for (const ct of new Set([anchorT, t])) {
        if (!this.renderer.isResident(chunkKey(lod, row, col, ct)) && !this.store.peekRaw(lod, row, col, ct)) return false;
      }
    }
    return true;
  }

  setProduct(index) {
    if (!this.products[index]?.available) return;
    this.productIndex = index;
    this.#linear = { range: null, manual: false };
    this.#updateProductUi();
    this.#renderChart();
    this.#paintPartial = true;
    this.requestRender();
    this.#scheduleUrlSync();
  }

  setBandChoice(index) {
    this.bandChoice = index;
    this.#linear = { range: null, manual: false };
    this.#updateStretchUi();
    this.#renderChart();
    this.#paintPartial = true;
    this.requestRender();
    this.#scheduleUrlSync();
  }

  requestRender() {
    if (this.#rafId) return;
    this.#rafId = requestAnimationFrame(() => {
      this.#rafId = 0;
      this.renderNow();
    });
  }

  /** Resolves after the next complete frame. */
  whenPainted() {
    return new Promise((resolve) => this.#painted.push(resolve));
  }

  /**
   * Paint the current view now, cell by cell: every visible cell whose chunks for the current timestep are
   * ready is drawn as soon as it is ready. After a pure time change the previous frame stays underneath
   * (cells still loading keep showing the previous timestep, or the cached coarser level at the new
   * one); camera, product and resize changes clear and repaint. Returns {complete, ms, lod, cells}.
   */
  renderNow() {
    if (this.#rafId) {
      cancelAnimationFrame(this.#rafId);
      this.#rafId = 0;
    }
    if (!this.store) return { complete: false, ms: 0, lod: 0, cells: 0 };
    const started = performance.now();
    const { store, renderer, canvas, t } = this;
    const lod = this.#targetLod();
    this.#updateResHint();
    const cells = this.#visibleCells(lod);
    this.#view = { lod, cells: new Set(cells.map(([row, col]) => `${row}/${col}`)) };

    renderer.newFrame();
    const uploadBefore = renderer.stats.uploadMs;
    const drawable = [];
    const missing = [];
    for (const [row, col] of cells) {
      const slots = this.#slotsFor(lod, row, col, t);
      if (slots) drawable.push({ row, col, slots });
      else missing.push([row, col]);
    }
    const complete = missing.length === 0;
    const clear = this.#paintPartial;
    const painted = clear || drawable.length > 0 || this.#coarsePending;

    if (complete && this.#stretchLo === null) this.#stretchLo = this.#computeStretch(lod, cells, t);
    if (complete && this.#linear.range === null && this.#usesLinearRange()) this.#measureLinear(lod, cells, t);
    // A frame is covered when every visible cell is drawn at the target level or sits under a coarser cached one.
    let covered = false;
    if (painted) {
      renderer.beginPaint(
        {
          width: canvas.width,
          height: canvas.height,
          ...this.camera,
          ...this.#productUniforms(),
          stretchLo: this.#stretchLo ?? 0,
          nodata: store.nodata ?? null,
        },
        { clear },
      );
      const coarser = complete ? new Set() : this.#drawCoarser(lod, t);
      covered = complete || missing.every(([row, col]) => this.#isUnderCoarser(coarser, lod, row, col));
      for (const { row, col, slots } of drawable) this.#drawCell(lod, row, col, slots);
      this.#paintedLod = lod;
      this.#coarsePending = false;
      if (complete) this.paintedT = t;
    }
    const timing = this.#viewTiming;
    if (timing && painted) {
      const now = performance.now();
      if (covered) timing.coarseAt ??= now;
      if (complete) timing.fullAt ??= now;
    }

    if (complete) {
      if ($('error-overlay').classList.contains('toast')) this.#hideError();
      this.#paintPartial = false;
      this.#wave?.controller.abort();
      this.#wave = null;
      this.#setProgress(1);
      this.#scheduleGpuFill();
      this.#schedulePrefetch();
    } else {
      this.#requestCells(lod, missing, t);
      // Cells that failed for good no longer hold anything back: keep prefetching for the ones that work.
      if (missing.every(([row, col]) => this.#wave.failures.has(`${row}/${col}`))) {
        this.#scheduleGpuFill();
        this.#schedulePrefetch();
      }
    }
    const ms = performance.now() - started;
    const uploadMs = renderer.stats.uploadMs - uploadBefore;
    if (painted) this.#emit({ type: 'paint', t, lod, complete, covered, cells: cells.length, ready: drawable.length, uploadMs, renderMs: ms - uploadMs });
    if (this.#gaps.visible) this.#drawGapOverlay();
    if (complete) {
      $('status').innerHTML = `<span class="fast">${Math.round(ms)}ms</span>`;
      for (const resolve of this.#painted.splice(0)) resolve();
    }
    return { complete, ms, lod, cells: cells.length };
  }

  /**
   * Start background prefetch now and resolve with its result when it finishes or is aborted. `movie` plans it
   * for playback (the movie level, time treated as a loop) even while paused; it defaults to whether a movie plays.
   */
  prefetchNow({ movie = this.#playing } = {}) {
    clearTimeout(this.#prefetchTimer);
    this.#prefetchTimer = 0;
    this.#prefetchStartedAt = performance.now();
    this.#prefetchAbort?.abort();
    const abort = new AbortController();
    this.#prefetchAbort = abort;
    const lod = movie ? this.#targetLod({ asPlaying: true }) : this.#view.lod;
    const cells = movie ? this.#visibleCells(lod) : [...this.#view.cells].map((k) => k.split('/').map(Number));
    const seek = !movie && this.#seekPending;
    if (!movie) this.#seekPending = false;
    return this.store
      .prefetch({
        lod,
        cells,
        t: this.t,
        direction: this.#direction,
        behindFactor: movie ? BEHIND_FACTOR_PLAYING : BEHIND_FACTOR,
        loop: movie,
        seek,
        signal: abort.signal,
        onChunk: () => this.#scheduleGpuFill(),
      })
      .then((result) => {
        for (const { key, error } of result.errors) console.error(`prefetch failed for chunk ${key}:`, error);
        return result;
      });
  }

  #createPlayback() {
    return new Playback({
      count: this.store.times.length,
      stepsPerSecond: this.#speed,
      getIndex: () => this.t,
      goTo: (index, direction) => this.goToTime(index, { playing: true, direction }),
      isReady: (index) => this.isTimestepReady(index),
      prepare: (index) => this.#prepareTimestep(index),
      onChange: () => this.#updatePlayUi(),
    });
  }

  /** Playback is waiting on timestep t: fetch it for the visible cells now, at demand priority. */
  #prepareTimestep(t) {
    this.#lookahead?.abort();
    const controller = new AbortController();
    this.#lookahead = controller;
    const { signal } = controller;
    const lod = this.#targetLod();
    const anchorT = this.store.anchorOf(t);
    for (const [row, col] of this.#visibleCells(lod)) {
      Promise.all([this.store.getRaw(lod, row, col, anchorT, { signal }), anchorT === t ? null : this.store.getRaw(lod, row, col, t, { signal })]).catch((error) => {
        if (error.name === 'AbortError') return;
        console.error(`playback: chunk load failed (lod ${lod}, row ${row}, col ${col}, timestep ${t}):`, error);
        this.#playback?.pause();
        this.#showError('Playback paused', `${this.#formatTime(t)}: ${error.name}: ${error.message}`, { toast: true });
      });
    }
  }

  #emit(event) {
    this.probe?.({ at: performance.now(), ...event });
  }

  #abortBackground() {
    this.#lookahead?.abort();
    this.#wave?.controller.abort();
    this.#wave = null;
    clearTimeout(this.#gpuFillTimer);
    clearTimeout(this.#prefetchTimer);
    this.#prefetchTimer = 0;
    this.#prefetchAbort?.abort();
    this.#prefetchAbort = null;
  }

  #checkUniformChunks(store) {
    const first = store.levels[0];
    for (const level of store.levels) {
      if (level.chunkWidth !== first.chunkWidth || level.chunkHeight !== first.chunkHeight) {
        throw new Error(`level ${level.lod} chunk size ${level.chunkWidth}x${level.chunkHeight} differs from level 0 (${first.chunkWidth}x${first.chunkHeight})`);
      }
    }
  }

  #configurePool() {
    const { store, renderer } = this;
    const first = store.levels[0];
    const wanted = store.levels.reduce((n, l) => n + l.gridRows * l.gridCols * l.nTime, 0);
    const bytesPerSample = first.chunkBytes / (first.nBand * first.chunkWidth * first.chunkHeight);
    const slots = renderer.planSlots(first.nBand, first.chunkWidth, first.chunkHeight, POOL_BUDGET_BYTES, wanted, bytesPerSample);
    renderer.configure({ dtype: this.dtype, nBand: first.nBand, chunkWidth: first.chunkWidth, chunkHeight: first.chunkHeight, slots });
    this.#maxVisibleCells = Math.floor(slots / 2);
  }

  #resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.parentElement.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
  }

  /** The level for a camera scale (canvas pixels per level-0 pixel), before limits on the number of cells. */
  #lodForScale(scale) {
    return clamp(Math.floor(Math.log2(1 / scale) + LOD_BIAS + 1e-9), 0, this.store.levels.length - 1);
  }

  /** The level the viewer shows while not playing: sharp enough for the screen, few enough cells for the texture pool. */
  #normalLod() {
    const maxLod = this.store.levels.length - 1;
    let lod = this.#lodForScale(this.camera.scale);
    while (lod < maxLod && this.#visibleCells(lod).length > this.#maxVisibleCells) lod++;
    return lod;
  }

  /**
   * The level to draw at. While a movie plays (or `asPlaying`), the first level from the normal one on where the
   * whole loop for the visible cells fits the decoded cache and, for a loop not in memory yet, where the link can
   * feed it (chooseMovieLevel), but never coarser than the level the viewer would pick at 4x zoom-out. A playing
   * movie keeps its level until the camera, the speed or the playback state changes. A level pinned with
   * lodOverride is left alone.
   */
  #targetLod({ asPlaying = this.#playing } = {}) {
    if (this.lodOverride !== null) return this.lodOverride;
    const baseLod = this.#normalLod();
    let choice = { lod: baseLod, reason: null, detail: null };
    if (asPlaying) {
      const deepestLod = Math.max(baseLod, this.#lodForScale(this.camera.scale / 4));
      const key = `${this.#playEpoch}|${baseLod}|${deepestLod}|${this.#visibleCells(baseLod).length}|${this.#speed}`;
      if (this.#playing && this.#movieMemo?.key === key) choice = this.#movieMemo;
      else {
        const links = new Map();
        const { lod, reason } = chooseMovieLevel({
          baseLod,
          deepestLod,
          fits: (candidate) => this.store.loopFits(candidate, this.#visibleCells(candidate).length),
          linkOk: (candidate) => {
            const link = this.#linkCheck(candidate);
            links.set(candidate, link);
            return link.ok;
          },
        });
        choice = { key, lod, reason, detail: this.#movieDetail(reason, links.get(baseLod)) };
        if (this.#playing) this.#movieMemo = choice;
      }
    }
    this.#movie = { baseLod, lod: choice.lod, reason: choice.reason, detail: choice.detail };
    return choice.lod;
  }

  /**
   * Whether the link can feed a movie at this level: the wire bytes of one step (the visible cells, at the
   * compression the caches show) times the speed, for the part of the loop not in memory yet, against the
   * measured bandwidth (see linkAllows). The numbers go in the hint's tooltip.
   */
  #linkCheck(lod) {
    const { store } = this;
    const bandwidth = store.bandwidthEstimate();
    const cells = this.#visibleCells(lod);
    const level = store.levels[lod];
    const bytesPerPixel = level.chunkBytes / (level.chunkWidth * level.chunkHeight);
    const count = store.times.length;
    let pixels = 0;
    let cold = 0;
    for (const [row, col] of cells) {
      const { width, height } = store.cellExtent(lod, row, col);
      pixels += width * height;
      for (let t = 0; t < count; t++) if (!store.peekRaw(lod, row, col, t)) cold++;
    }
    const bytesPerStep = pixels * bytesPerPixel * wireRatio(readStats(store));
    const stepsPerSecond = this.#playback?.effectiveStepsPerSecond ?? this.#speed;
    const coldFraction = cells.length === 0 ? 0 : cold / (cells.length * count);
    return { ok: linkAllows({ bytesPerStep, stepsPerSecond, coldFraction, bandwidth }), cells: cells.length, bytesPerStep, stepsPerSecond, coldFraction, bandwidth };
  }

  /** The tooltip of the resolution hint: what ruled out the normal level. */
  #movieDetail(reason, baseLink) {
    if (reason === null) return null;
    const parts = [];
    if (reason.includes('memory')) parts.push('the whole loop does not fit the decoded cache at the normal level');
    if (reason.includes('link') && baseLink?.bandwidth) {
      const need = baseLink.bytesPerStep * baseLink.stepsPerSecond * baseLink.coldFraction;
      parts.push(`the link: ${baseLink.cells} cells at about ${formatBytes(baseLink.bytesPerStep)} per step, ${Math.round(baseLink.stepsPerSecond)}/s, ${Math.round(baseLink.coldFraction * 100)} % not in memory needs ${formatRate(need)}; measured ${formatRate(baseLink.bandwidth)}, limit 70 %`);
    }
    return parts.join('; ');
  }

  /** While a movie plays: the normal level, the level it plays at (coarser when the loop would not fit the cache or the link cannot feed it), and why. */
  get movieInfo() {
    return { ...this.#movie, playing: this.#playing };
  }

  /** Cells of `lod` intersecting the canvas, as [row, col]. */
  #visibleCells(lod) {
    const level = this.store.levels[lod];
    const { cx, cy, scale } = this.camera;
    const factor = 2 ** lod;
    const x0 = (cx - this.canvas.width / 2 / scale) / factor;
    const x1 = (cx + this.canvas.width / 2 / scale) / factor;
    const y0 = (cy - this.canvas.height / 2 / scale) / factor;
    const y1 = (cy + this.canvas.height / 2 / scale) / factor;
    if (x1 < 0 || y1 < 0 || x0 >= level.width || y0 >= level.height) return [];
    const colMin = Math.max(0, Math.floor(x0 / level.chunkWidth));
    const colMax = Math.min(level.gridCols - 1, Math.floor(x1 / level.chunkWidth));
    const rowMin = Math.max(0, Math.floor(y0 / level.chunkHeight));
    const rowMax = Math.min(level.gridRows - 1, Math.floor(y1 / level.chunkHeight));
    const cells = [];
    for (let row = rowMin; row <= rowMax; row++) for (let col = colMin; col <= colMax; col++) cells.push([row, col]);
    return cells;
  }

  /** GPU slots for the anchor (and delta) chunk of a cell at t, uploading from the decoded cache if needed. */
  #slotsFor(lod, row, col, t, renderer = this.renderer) {
    const anchorT = this.store.anchorOf(t);
    const anchor = this.#slotForChunk(lod, row, col, anchorT, renderer);
    if (anchor < 0) return null;
    if (anchorT === t) return { anchor, delta: -1 };
    const delta = this.#slotForChunk(lod, row, col, t, renderer);
    return delta < 0 ? null : { anchor, delta };
  }

  #slotForChunk(lod, row, col, t, renderer) {
    const key = chunkKey(lod, row, col, t);
    const resident = renderer.slotOf(key);
    if (resident >= 0) return resident;
    const data = this.store.peekRaw(lod, row, col, t);
    return data ? renderer.upload(key, { lod, row, col, t }, data) : -1;
  }

  #scheduleGpuFill() {
    clearTimeout(this.#gpuFillTimer);
    this.#gpuFillTimer = setTimeout(() => this.#fillGpuWindow(), 0);
  }

  /**
   * Keep a window of timesteps around t resident in the texture pool, as wide as the pool holds for the
   * visible cells and reaching further in the scrub direction (wrapping past the last timestep while a movie plays), so the next steps are uniform changes
   * instead of uploads. Uploads come from the decoded cache in slices of GPU_UPLOAD_SLICE_MS.
   */
  #fillGpuWindow() {
    const { lod, cells } = this.#view;
    const { store, renderer, t } = this;
    if (cells.size === 0) return;
    const anchorShare = store.anchorIndices.length / store.times.length;
    const steps = Math.max(1, Math.floor((renderer.slots * GPU_FILL_FRACTION) / (cells.size * (1 + anchorShare))));
    const timesteps = windowOrder(store.times.length, t, { direction: this.#direction, behindFactor: this.#behindFactor, loop: this.#playing }).slice(0, steps);
    const frameMs = this.#playback?.frameMs ?? 1000 / 60;
    const sliceMs = Math.min(GPU_UPLOAD_SLICE_MS, frameMs * GPU_SLICE_FRACTION_OF_FRAME);
    const started = performance.now();
    for (const tt of timesteps) {
      for (const ct of new Set([store.anchorOf(tt), tt])) {
        for (const key of cells) {
          const [row, col] = key.split('/').map(Number);
          const chunk = chunkKey(lod, row, col, ct);
          const data = renderer.isResident(chunk) ? null : store.peekRaw(lod, row, col, ct);
          if (!data) continue;
          renderer.upload(chunk, { lod, row, col, t: ct }, data, { background: true });
          if (performance.now() - started > sliceMs) {
            this.#gpuFillTimer = setTimeout(() => this.#fillGpuWindow(), 0);
            return;
          }
        }
      }
    }
  }

  /** Eviction order for decoded chunks and texture slots: outside the view first, then by scrub cost from t. */
  #chunkScore(meta) {
    const visible = meta.lod === this.#view.lod && this.#view.cells.has(`${meta.row}/${meta.col}`);
    const cost = scrubCost(meta.t - this.t, this.#direction, this.#behindFactor, this.#playing ? this.store.times.length : null);
    return (visible ? 0 : 1e6) + (this.store.isAnchor(meta.t) ? cost / 2 : cost);
  }

  /**
   * Eviction order for the decoded-chunk cache: like #chunkScore, except the chunks an export is recording
   * from are kept ahead of everything else, so the viewer's own prefetch cannot push them out mid-export.
   */
  #storeScore(meta) {
    const hold = this.#exportHold;
    if (hold && meta.lod === hold.lod && hold.cells.has(`${meta.row}/${meta.col}`)) return -1;
    return this.#chunkScore(meta);
  }

  #drawCell(lod, row, col, slots, renderer = this.renderer) {
    const level = this.store.levels[lod];
    const factor = 2 ** lod;
    const { width, height } = this.store.cellExtent(lod, row, col);
    renderer.drawCell(
      { x: col * level.chunkWidth * factor, y: row * level.chunkHeight * factor, w: width * factor, h: height * factor },
      { w: width, h: height },
      slots.anchor,
      slots.delta,
    );
  }

  /** Progressive LOD: under a partial frame, paint any already-cached coarser cells, coarsest first. Returns the ones drawn as "lod/row/col" keys. */
  #drawCoarser(lod, t) {
    const drawn = new Set();
    for (let coarse = this.store.levels.length - 1; coarse > lod; coarse--) {
      for (const [row, col] of this.#visibleCells(coarse)) {
        const slots = this.#slotsFor(coarse, row, col, t);
        if (!slots) continue;
        this.#drawCell(coarse, row, col, slots);
        drawn.add(`${coarse}/${row}/${col}`);
      }
    }
    return drawn;
  }

  /** Whether some coarser level drawn in `drawn` covers cell (row, col) of `lod`. */
  #isUnderCoarser(drawn, lod, row, col) {
    for (let coarse = lod + 1; coarse < this.store.levels.length; coarse++) {
      const shift = coarse - lod;
      if (drawn.has(`${coarse}/${row >> shift}/${col >> shift}`)) return true;
    }
    return false;
  }

  /** What the shader needs to color the current product: inputs, the stored-to-physical conversion, and the display mode. */
  #productUniforms() {
    const product = this.products[this.productIndex];
    const display = this.#display(product);
    return {
      shader: product.shader,
      inputs: inputIndices(product, this.bandChoice),
      ...inputConversion(product, this.bands, this.bandChoice),
      display: display.mode,
      range: display.range,
    };
  }

  /** How the product is shown (see displayMode): a linear stretch that is not fixed uses the measured or user-set range. */
  #display(product) {
    const display = displayMode(product, this.bands, this.bandChoice, this.dtype);
    return display.mode === 'linear' && !display.fixed ? { ...display, range: this.#linear.range ?? [0, 1] } : display;
  }

  #usesLinearRange() {
    const display = displayMode(this.products[this.productIndex], this.bands, this.bandChoice, this.dtype);
    return display.mode === 'linear' && !display.fixed;
  }

  /** The range of a linear single band from the data on screen: 2nd to 98th percentile of the valid physical values. */
  #measureLinear(lod, cells, t) {
    const { store } = this;
    const band = this.bands[this.bandChoice];
    const values = [];
    for (const [row, col] of cells) {
      const { width, height } = store.cellExtent(lod, row, col);
      const stride = Math.max(1, Math.floor((width * height) / STRETCH_SAMPLES_PER_CELL));
      for (let i = 0; i < width * height; i += stride) {
        const stored = store.samplePixel(lod, row, col, t, i % width, Math.floor(i / width))?.[this.bandChoice];
        if (stored !== undefined && stored !== store.nodata && Number.isFinite(stored)) values.push(toPhysical(stored, band));
      }
    }
    const range = percentileRange(values);
    if (range) this.#linear = { range, manual: false };
    this.#updateStretchUi();
  }

  /** The user typed a stretch range (physical units). */
  setStretch(lo, hi) {
    if (!(Number.isFinite(lo) && Number.isFinite(hi) && hi > lo)) {
      this.#updateStretchUi();
      return;
    }
    this.#linear = { range: [lo, hi], manual: true };
    this.#paintPartial = true;
    this.requestRender();
  }

  /** Back to the range measured from the data on screen. */
  autoStretch() {
    this.#linear = { range: null, manual: false };
    this.#paintPartial = true;
    this.requestRender();
  }

  /** 2nd percentile of tone-mapped true-color samples (physical reflectance), kept fixed while scrubbing. 0 without red, green and blue bands. */
  #computeStretch(lod, cells, t) {
    const { store } = this;
    const idx = ['red', 'green', 'blue'].map((common) => findBand(this.bands, common));
    if (idx.some((i) => i < 0)) return 0;
    const level = store.levels[lod];
    const samples = [];
    for (const [row, col] of cells) {
      const { width, height } = store.cellExtent(lod, row, col);
      const stride = Math.max(1, Math.floor((width * height) / STRETCH_SAMPLES_PER_CELL));
      for (let i = 0; i < width * height; i += stride) {
        const values = store.samplePixel(lod, row, col, t, i % width, Math.floor(i / width));
        if (values) samples.push(idx.map((b) => toPhysical(values[b], this.bands[b])));
      }
    }
    return computeStretchLo(samples);
  }

  /**
   * Fetch the missing cells for timestep t right away, one wave per (level, timestep). A wave for a
   * different timestep or level is stale: it is aborted after the new one has claimed the chunks they
   * share, and requests nobody wants any more are cancelled.
   */
  #requestCells(lod, missing, t) {
    let wave = this.#wave;
    const previous = wave && (wave.lod !== lod || wave.t !== t) ? wave : null;
    if (!wave || previous) wave = { lod, t, controller: new AbortController(), cells: new Set(), failures: new Map() };
    const wanted = missing.filter(([row, col]) => !wave.cells.has(`${row}/${col}`));
    this.#wave = wave;
    if (wanted.length > 0) {
      for (const [row, col] of wanted) wave.cells.add(`${row}/${col}`);
      this.#emit({ type: 'load-start', t, cells: wanted.length });
      this.#setProgress(0.02);
      this.#loadWanted(wave, wanted);
    }
    previous?.controller.abort();
  }

  /**
   * The cells a wave still needs, coarse first: the small coarse levels (see coarse.js), each painted as it lands,
   * then the target level, whose cells paint one by one. A stage starts when the previous one lands or, sooner, a
   * couple of round trips after the previous one started (stageLeadMs), so the requests of one stage wait on the
   * network while the bytes of the one before are still arriving instead of after them.
   */
  async #loadWanted(wave, wanted) {
    const { lod } = wave;
    const { signal } = wave.controller;
    const lead = stageLeadMs(this.#roundTripMs);
    let previous = null;
    for (const stage of this.#coarseStages(lod, wanted, wave.t)) {
      if (previous) await Promise.race([previous, delay(lead, signal)]);
      if (signal.aborted) return;
      previous = this.#loadStage(wave, stage);
    }
    if (previous) await Promise.race([previous, delay(lead, signal)]);
    if (!signal.aborted) this.#loadCells(wave, wanted);
  }

  /** Load one coarse stage and paint it when it lands. Never rejects: a stage that fails only costs the preview. */
  async #loadStage(wave, stage) {
    const { t } = wave;
    const { signal } = wave.controller;
    const started = performance.now();
    try {
      await this.#loadFrame(stage.lod, stage.cells, t, signal);
    } catch (error) {
      if (error.name !== 'AbortError') console.warn(`coarse frame failed (level ${stage.lod}, cells ${JSON.stringify(stage.cells)}, timestep ${t}); the target level loads anyway:`, error);
      return;
    }
    if (signal.aborted) return;
    this.#emit({ type: 'coarse-frame', t, lod: stage.lod, cells: stage.cells.length, ms: performance.now() - started });
    this.#coarsePending = true;
    this.renderNow();
  }

  /** Levels to show before `lod` for the cells about to load at timestep t, coarsest first, as {lod, cells}; none for a pinned level, a movie, or a one-step time change. */
  #coarseStages(lod, cells, t) {
    if (this.lodOverride !== null || this.#playing) return [];
    // Stepping to a nearby timestep keeps the previous one on screen as a stand-in; a jump or a camera move has none.
    if (this.#viewTiming?.kind === 'time' && !this.#seekPending) return [];
    const levels = planCoarseStages({
      targetLod: lod,
      coarsestLod: this.store.levels.length - 1,
      frameBytes: (level) => this.#frameBytes(level, ancestorCells(cells, lod, level), t),
      bandwidth: this.store.bandwidthEstimate() ?? ASSUMED_BANDWIDTH,
      wireRatio: wireRatio(readStats(this.store)),
    });
    return levels.map((level) => ({ lod: level, cells: ancestorCells(cells, lod, level) }));
  }

  /** Decoded bytes of the valid pixels of the chunks of timestep t (its anchor and, unless it is one, its delta) that these cells of one level do not have in memory yet. */
  #frameBytes(lod, cells, t) {
    const { store } = this;
    const level = store.levels[lod];
    const bytesPerPixel = level.chunkBytes / (level.chunkWidth * level.chunkHeight);
    const chunks = [...new Set([store.anchorOf(t), t])];
    let pixels = 0;
    for (const [row, col] of cells) {
      const { width, height } = store.cellExtent(lod, row, col);
      for (const ct of chunks) if (!store.peekRaw(lod, row, col, ct)) pixels += width * height;
    }
    return pixels * bytesPerPixel;
  }

  /** The chunks of timestep t for these cells of one level, decoded and in the cache when this resolves. */
  async #loadFrame(lod, cells, t, signal) {
    await this.store.getCoarseFrame(lod, cells, t, { signal });
  }

  /** Fetch the target-level cells of a wave, repainting as each one arrives. */
  #loadCells(wave, wanted) {
    const { lod, t } = wave;
    const { signal } = wave.controller;
    const anchorT = this.store.anchorOf(t);
    let done = 0;
    for (const [row, col] of wanted) {
      Promise.all([this.store.getRaw(lod, row, col, anchorT, { signal }), anchorT === t ? null : this.store.getRaw(lod, row, col, t, { signal })]).then(
        () => {
          if (signal.aborted) return;
          this.#setProgress((++done / wanted.length) * 0.98);
          this.#emit({ type: 'cell-ready', t, row, col });
          this.renderNow();
        },
        (error) => {
          if (error.name === 'AbortError') return;
          this.#chunkFailed(wave, { lod, row, col, t }, error);
        },
      );
    }
  }

  /**
   * A chunk failed after the decoder's own retries. Not fatal: the cell keeps its coarser level or previous
   * timestep, the failure is shown as a toast with the URL, status and error, and the cell is requested again
   * after a growing pause, up to MAX_CELL_RETRIES times or until the view moves to another timestep.
   */
  #chunkFailed(wave, { lod, row, col, t }, error) {
    const cell = `${row}/${col}`;
    const failures = (wave.failures.get(cell) ?? 0) + 1;
    wave.failures.set(cell, failures);
    console.error(`chunk load failed (lod ${lod}, row ${row}, col ${col}, timestep ${t}, failure ${failures}):`, error);
    const outcome = failures > MAX_CELL_RETRIES ? 'Giving up on it until the timestep changes.' : 'Retrying shortly.';
    this.#showError(
      'Chunk load failed',
      `Level ${lod}, cell (${row}, ${col}), ${this.#formatTime(t)}: ${error.name}: ${error.message}. The cell keeps its previous data. ${outcome}`,
      { toast: true },
    );
    this.#setProgress(0);
    if (failures > MAX_CELL_RETRIES) return;
    setTimeout(() => {
      if (this.#wave !== wave) return;
      wave.cells.delete(cell);
      this.requestRender();
    }, CELL_RETRY_DELAY_MS * failures);
  }

  /**
   * Restart background prefetch shortly after a complete frame. A pending restart is kept rather than pushed
   * back, so frames arriving faster than the settle time (movie playback) cannot starve it, and while playing
   * the window is only re-planned every PREFETCH_PLAYBACK_RESTART_MS.
   */
  #schedulePrefetch() {
    if (this.#prefetchTimer || this.#exportHold) return;
    const sinceStart = performance.now() - this.#prefetchStartedAt;
    const wait = this.#playing ? Math.max(PREFETCH_SETTLE_MS, PREFETCH_PLAYBACK_RESTART_MS - sinceStart) : PREFETCH_SETTLE_MS;
    this.#prefetchTimer = setTimeout(() => this.prefetchNow(), wait);
  }

  get #playing() {
    return this.#playback?.playing ?? false;
  }

  /** Behind the scrub direction costs more while a movie plays: it never goes back. */
  get #behindFactor() {
    return this.#playing ? BEHIND_FACTOR_PLAYING : BEHIND_FACTOR;
  }

  // ---- click to query ----

  async #inspect(clientX, clientY) {
    if (!this.store) return;
    $('click-hint').classList.add('hidden');
    const rect = this.canvas.getBoundingClientRect();
    const px = ((clientX - rect.left) * this.canvas.width) / rect.width;
    const py = ((clientY - rect.top) * this.canvas.height) / rect.height;
    const { cx, cy, scale } = this.camera;
    const worldX = cx + (px - this.canvas.width / 2) / scale;
    const worldY = cy + (py - this.canvas.height / 2) / scale;
    const lod = this.#paintedLod;
    const level = this.store.levels[lod];
    const factor = 2 ** lod;
    const x = Math.floor(worldX / factor);
    const y = Math.floor(worldY / factor);
    if (x < 0 || y < 0 || x >= level.width || y >= level.height) {
      this.#clearChart();
      this.#updateSidebar(null);
      return;
    }
    const row = Math.floor(y / level.chunkHeight);
    const col = Math.floor(x / level.chunkWidth);
    const t = this.t;
    const anchorT = this.store.anchorOf(t);
    const cellX = x - col * level.chunkWidth;
    const cellY = y - row * level.chunkHeight;
    try {
      await Promise.all([this.store.getRaw(lod, row, col, anchorT), anchorT === t ? null : this.store.getRaw(lod, row, col, t)]);
    } catch (error) {
      this.#showError('Chunk load failed', error.message);
      return;
    }
    const observed = await this.#observedAt(lod, row, col, t, cellX, cellY);
    const values = this.store.samplePixel(lod, row, col, t, cellX, cellY);
    this.#updateSidebar({ t, lod, pixel: { x: Math.floor(worldX), y: Math.floor(worldY) }, observed, ...describePixel(values, this.bands, this.store.nodata) });
    this.#startChart({ lod, row, col, x: cellX, y: cellY });
  }

  /** How many observations (scenes) stand behind the pixel at timestep t: null when the store has no coverage variable or it cannot be read. */
  async #observedAt(lod, row, col, t, x, y) {
    const { store } = this;
    if (!store.hasCoverage) return null;
    try {
      const coverage = await store.getCoverage(lod, row, col, t);
      return coverage ? coverage[y * store.levels[lod].chunkWidth + x] : null;
    } catch (error) {
      console.error(`coverage load failed (lod ${lod}, row ${row}, col ${col}, timestep ${t}):`, error);
      return null;
    }
  }

  // ---- chart of the clicked pixel over time ----

  /** A new click: forget the old chart, read what is cached, then fetch the rest of the cell's timesteps. */
  #startChart({ lod, row, col, x, y }) {
    this.#clearChart();
    const { width, height } = this.store.cellExtent(lod, row, col);
    const chart = {
      cell: { lod, row, col },
      pixel: { x, y },
      window: windowPixels(x, y, width, height, 1),
      readings: new Array(this.store.times.length).fill(undefined),
      // Observations behind the pixel per timestep (undefined = not loaded, null = unreadable); null without a coverage variable.
      coverage: this.store.hasCoverage ? new Array(this.store.times.length).fill(undefined) : null,
      controller: new AbortController(),
      failed: 0,
      done: false,
      hover: null,
    };
    this.#chart = chart;
    this.#renderChart();
    this.#fillChart(chart);
  }

  #clearChart() {
    this.#chart?.controller.abort();
    this.#chart = null;
    clearTimeout(this.#chartTimer);
    this.#chartTimer = 0;
  }

  async #fillChart(chart) {
    const { store } = this;
    const { lod, row, col } = chart.cell;
    const level = store.levels[lod];
    const sample = (anchor, delta) => ({ pixels: chart.window.map(([x, y]) => samplePixelFrom(anchor, delta, level, x, y)) });
    const coverageOffset = chart.pixel.y * level.chunkWidth + chart.pixel.x;
    for (let t = 0; t < chart.readings.length; t++) {
      const anchor = store.peekRaw(lod, row, col, store.anchorOf(t));
      const delta = store.isAnchor(t) ? null : store.peekRaw(lod, row, col, t);
      if (anchor && (store.isAnchor(t) || delta)) chart.readings[t] = sample(anchor, delta);
      if (chart.coverage) {
        const coverage = store.peekCoverage(lod, row, col, t);
        if (coverage) chart.coverage[t] = coverage[coverageOffset];
      }
    }
    this.#renderChart();
    // Timesteps still missing their values or (with a coverage variable) their coverage, nearest to the one on screen first.
    const missing = chart.readings
      .flatMap((reading, t) => (reading && (!chart.coverage || chart.coverage[t] !== undefined) ? [] : [t]))
      .sort((a, b) => Math.abs(a - this.t) - Math.abs(b - this.t));
    const { signal } = chart.controller;
    for (let i = 0; i < missing.length; i += CHART_BATCH) {
      await Promise.all(
        missing.slice(i, i + CHART_BATCH).map(async (t) => {
          const anchorT = store.anchorOf(t);
          const observed = chart.coverage && chart.coverage[t] === undefined ? this.#loadChartCoverage(chart, t, coverageOffset) : null;
          if (!chart.readings[t]) {
            try {
              const [anchor, delta] = await Promise.all([store.getRaw(lod, row, col, anchorT, { signal }), anchorT === t ? null : store.getRaw(lod, row, col, t, { signal })]);
              chart.readings[t] = sample(anchor, delta);
            } catch (error) {
              if (error.name === 'AbortError') return;
              chart.failed++;
              console.error(`chart: chunk load failed (lod ${lod}, row ${row}, col ${col}, timestep ${t}):`, error);
            }
          }
          await observed;
        }),
      );
      if (signal.aborted) return;
      this.#scheduleChartRender();
    }
    chart.done = true;
    this.#renderChart();
  }

  /** The coverage of the charted pixel at timestep t, into chart.coverage (null when it cannot be read). */
  async #loadChartCoverage(chart, t, offset) {
    const { lod, row, col } = chart.cell;
    try {
      const coverage = await this.store.getCoverage(lod, row, col, t, { signal: chart.controller.signal });
      chart.coverage[t] = coverage ? coverage[offset] : null;
    } catch (error) {
      if (error.name === 'AbortError') return;
      chart.coverage[t] = null;
      console.error(`chart: coverage load failed (lod ${lod}, row ${row}, col ${col}, timestep ${t}):`, error);
    }
  }

  #scheduleChartRender() {
    if (this.#chartTimer) return;
    this.#chartTimer = setTimeout(() => {
      this.#chartTimer = 0;
      this.#renderChart();
    }, CHART_RENDER_MS);
  }

  /** Draw the chart for the current product from the readings loaded so far. */
  #renderChart() {
    const target = $('chart');
    const chart = this.#chart;
    if (!chart || !target) return;
    const { store } = this;
    const specs = seriesSpecs(this.products[this.productIndex], this.bands, this.bandChoice);
    chart.series = buildSeries(specs, chart.readings, store.nodata);
    const [lo, hi] = chartRange(chart.series);
    const n = store.times.length;
    const xOf = (t) => xFromTime(t, n, CHART.left, CHART.right);
    const yOf = (value) => CHART.bottom - ((value - lo) / (hi - lo)) * (CHART.bottom - CHART.top);
    const fmt = (value) => (Math.abs(hi - lo) >= 2 ? value.toFixed(1) : value.toFixed(2));
    const grid = [hi, (lo + hi) / 2, lo]
      .map((value) => `<line x1="${CHART.left}" x2="${CHART.right}" y1="${yOf(value)}" y2="${yOf(value)}" class="chart-grid"/><text x="${CHART.left - 4}" y="${yOf(value) + 3}" text-anchor="end" class="chart-axis">${fmt(value)}</text>`)
      .join('');
    const lines = chart.series.map((s) => `<path d="${seriesPath(s.values, xOf, yOf)}" fill="none" stroke="${s.color}" stroke-width="1.5" stroke-linejoin="round"/>`).join('');
    // Hollow points mark timesteps where nothing was observed at the pixel and the value was filled in from another month.
    const gaps = chart.series
      .flatMap((s) => gapFilledTimes(s.values, chart.coverage).map((t) => `<circle cx="${xOf(t).toFixed(1)}" cy="${yOf(s.values[t]).toFixed(1)}" r="2.6" fill="var(--surface)" stroke="${s.color}" stroke-width="1.3"/>`))
      .join('');
    const anyGap = chart.coverage?.some((c) => c === 0) ?? false;
    target.innerHTML = `
      <svg class="chart-svg" viewBox="0 0 ${CHART.width} ${CHART.height}" role="img" aria-label="${chart.series.map((s) => s.label).join(', ')} over time">
        ${grid}${lines}${gaps}
        <line id="chart-marker" y1="${CHART.top}" y2="${CHART.bottom}" class="chart-marker"/>
        <line id="chart-hover" y1="${CHART.top}" y2="${CHART.bottom}" class="chart-hover" visibility="hidden"/>
        <text x="${CHART.left}" y="${CHART.height - 4}" class="chart-axis">${this.#formatTime(0)}</text>
        <text x="${CHART.right}" y="${CHART.height - 4}" text-anchor="end" class="chart-axis">${this.#formatTime(n - 1)}</text>
      </svg>`;
    $('chart-legend').innerHTML =
      chart.series.map((s) => `<span style="color:${s.color}">${s.label}</span>`).join('') +
      (anyGap ? '<span class="chart-gap-key" title="No scene observed this pixel this month; its value is carried over from another month"><i></i>gap-filled</span>' : '');
    this.#updateChartMarker();
    this.#updateChartStatus();
  }

  #updateChartMarker() {
    const marker = $('chart-marker');
    if (!marker || !this.#chart) return;
    const x = xFromTime(this.t, this.store.times.length, CHART.left, CHART.right);
    marker.setAttribute('x1', x);
    marker.setAttribute('x2', x);
  }

  /** Under the chart: what the pointer is on, or how much of the series has loaded. */
  #updateChartStatus() {
    const chart = this.#chart;
    const status = $('chart-status');
    if (!chart || !status) return;
    const n = chart.readings.length;
    if (chart.hover !== null) {
      const t = chart.hover;
      const values = chart.series.map((s) => (typeof s.values[t] === 'number' ? formatValue(s.values[t]) : s.values[t] === null ? 'no data' : '…'));
      const gap = chart.coverage?.[t] === 0 ? ' · gap-filled' : '';
      status.textContent = `${this.#formatTime(t)} · ${values.join(' · ')}${gap}`;
      return;
    }
    const loaded = chart.readings.filter(Boolean).length;
    const failed = chart.failed > 0 ? ` · ${chart.failed} failed` : '';
    const gapCount = chart.coverage?.filter((c) => c === 0).length ?? 0;
    const gapNote = gapCount > 0 ? ` · ${gapCount} gap-filled` : '';
    status.textContent = chart.done ? `${loaded} of ${n} timesteps${failed}${gapNote} · click to jump` : `loading ${loaded} of ${n} timesteps…`;
  }

  #bindChart() {
    const content = $('sidebar-content');
    const timeAt = (e) => {
      const svg = e.target.closest('.chart-svg');
      if (!svg || !this.#chart) return null;
      const rect = svg.getBoundingClientRect();
      return timeFromX(((e.clientX - rect.left) * CHART.width) / rect.width, this.store.times.length, CHART.left, CHART.right);
    };
    content.addEventListener('click', (e) => {
      const t = timeAt(e);
      if (t !== null) this.goToTime(t);
    });
    content.addEventListener('pointermove', (e) => {
      const chart = this.#chart;
      const t = timeAt(e);
      if (!chart) return;
      if (t === null) {
        if (chart.hover !== null) {
          chart.hover = null;
          $('chart-hover')?.setAttribute('visibility', 'hidden');
          this.#updateChartStatus();
        }
        return;
      }
      chart.hover = t;
      const x = xFromTime(t, this.store.times.length, CHART.left, CHART.right);
      const line = $('chart-hover');
      line.setAttribute('x1', x);
      line.setAttribute('x2', x);
      line.setAttribute('visibility', 'visible');
      this.#updateChartStatus();
    });
    content.addEventListener('pointerleave', () => {
      if (!this.#chart) return;
      this.#chart.hover = null;
      $('chart-hover')?.setAttribute('visibility', 'hidden');
      this.#updateChartStatus();
    });
  }

  // ---- performance overlay (key "d") ----

  /** Show or hide the performance overlay: cache, requests, transfer, time to frame and slow frames, refreshed 4 times a second while visible. */
  togglePerfOverlay() {
    const panel = $('perf-overlay');
    const show = panel.hidden;
    panel.hidden = !show;
    const perf = this.#perf;
    clearInterval(perf.timer);
    perf.timer = 0;
    if (!show) {
      perf.monitor.stop();
      return;
    }
    perf.monitor.reset();
    perf.monitor.start();
    this.#updatePerfOverlay();
    perf.timer = setInterval(() => this.#updatePerfOverlay(), PERF_UPDATE_MS);
  }

  #updatePerfOverlay() {
    if (!this.store) return;
    const stats = readStats(this.store);
    const timing = this.viewTiming;
    const frames = this.#perf.monitor.snapshot();
    const count = (value) => (value === null ? '–' : value.toLocaleString('en-US'));
    const rows = [
      ['cache', `hit ${count(stats.cacheHits)} / miss ${count(stats.cacheMisses)} (${hitRate(stats.cacheHits, stats.cacheMisses)})`],
      ['decoded', `${formatBytes(stats.decodedBytes)} · compressed ${formatBytes(stats.compressedBytes)} · speculative ${formatBytes(stats.speculativeBytes)}`],
      ['requests', `in flight ${count(stats.inflight)} · deduped ${count(stats.dedupedRequests)} · total ${count(stats.requests)}`],
      ['transferred', `${formatBytes(stats.transferredBytes)} · ${formatRate(stats.bandwidth)}`],
      ['last view', timing ? `${timing.kind}: coarse ${formatMs(timing.coarseMs)} · full ${formatMs(timing.fullMs)}` : '–'],
      ['frames', `> 16.7 ms: ${frames.over16_7ms} · > 33 ms: ${frames.over33ms} of ${frames.frames}`],
    ];
    $('perf-overlay').textContent = rows.map(([label, value]) => `${label.padEnd(12)}${value}`).join('\n');
  }

  // ---- gap-filled pixels on the map ----

  /** Hatch the pixels that no scene observed in this timestep (coverage 0) over the map, or stop. */
  toggleGaps() {
    this.#gaps.visible = !this.#gaps.visible;
    this.#updateGapToggle();
    this.#drawGapOverlay();
  }

  #updateGapToggle() {
    const button = $('gap-toggle');
    button.hidden = !this.store?.hasCoverage;
    button.classList.toggle('active', this.#gaps.visible);
    button.setAttribute('aria-pressed', String(this.#gaps.visible));
  }

  /** Redraw the hatching for the cells on screen: each cell's coverage-0 pixels as a mask, scaled like the map, filled with stripes. */
  #drawGapOverlay() {
    const overlay = $('gap-canvas');
    const { width, height } = this.canvas;
    if (overlay.width !== width || overlay.height !== height) {
      overlay.width = width;
      overlay.height = height;
    }
    const ctx = overlay.getContext('2d');
    ctx.clearRect(0, 0, width, height);
    const { store } = this;
    if (!this.#gaps.visible || !store?.hasCoverage) return;
    const { lod } = this.#view;
    const level = store.levels[lod];
    const factor = 2 ** lod;
    const { cx, cy, scale } = this.camera;
    ctx.imageSmoothingEnabled = false;
    for (const key of this.#view.cells) {
      const [row, col] = key.split('/').map(Number);
      const mask = this.#gapMask(lod, row, col);
      if (!mask) continue;
      const x = (col * level.chunkWidth * factor - cx) * scale + width / 2;
      const y = (row * level.chunkHeight * factor - cy) * scale + height / 2;
      ctx.drawImage(mask, 0, 0, mask.width, mask.height, x, y, mask.width * factor * scale, mask.height * factor * scale);
    }
    ctx.globalCompositeOperation = 'source-in';
    ctx.fillStyle = this.#hatchPattern(ctx);
    ctx.fillRect(0, 0, width, height);
    ctx.globalCompositeOperation = 'source-over';
  }

  /** A canvas the size of the cell's valid pixels, opaque where coverage is 0; null while the coverage chunk loads (the overlay redraws when it has). */
  #gapMask(lod, row, col) {
    const { store } = this;
    const { t } = this;
    const key = `${lod}/${row}/${col}/${t}`;
    const { masks, requested } = this.#gaps;
    const cached = masks.get(key);
    if (cached) return cached;
    const coverage = store.peekCoverage(lod, row, col, t);
    if (!coverage) {
      if (coverage === undefined && !requested.has(key)) {
        requested.add(key);
        store.getCoverage(lod, row, col, t).then(
          () => {
            requested.delete(key);
            this.#drawGapOverlay();
          },
          (error) => console.error(`gap overlay: coverage load failed (lod ${lod}, row ${row}, col ${col}, timestep ${t}):`, error),
        );
      }
      return null;
    }
    const { width, height } = store.cellExtent(lod, row, col);
    const stride = store.levels[lod].chunkWidth;
    const mask = document.createElement('canvas');
    mask.width = width;
    mask.height = height;
    const maskContext = mask.getContext('2d');
    const image = maskContext.createImageData(width, height);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) if (coverage[y * stride + x] === 0) image.data[(y * width + x) * 4 + 3] = 255;
    }
    maskContext.putImageData(image, 0, 0);
    masks.set(key, mask);
    if (masks.size > GAP_MASK_CACHE) masks.delete(masks.keys().next().value);
    return mask;
  }

  /** Diagonal white stripes, GAP_HATCH_PX apart on screen whatever the pixel ratio. */
  #hatchPattern(ctx) {
    const size = Math.max(4, Math.round(GAP_HATCH_PX * (window.devicePixelRatio || 1)));
    if (this.#hatch?.size !== size) {
      const tile = document.createElement('canvas');
      tile.width = size;
      tile.height = size;
      const tileContext = tile.getContext('2d');
      tileContext.strokeStyle = 'rgba(255, 255, 255, 0.8)';
      tileContext.lineWidth = Math.max(1, size / 6);
      tileContext.beginPath();
      for (const shift of [-size, 0, size]) {
        tileContext.moveTo(shift, size);
        tileContext.lineTo(shift + size, 0);
      }
      tileContext.stroke();
      this.#hatch = { size, tile };
    }
    return ctx.createPattern(this.#hatch.tile, 'repeat');
  }

  // ---- input ----

  #bindInput() {
    const canvas = this.canvas;
    let drag = null;
    canvas.addEventListener('pointerdown', (e) => {
      drag = { x: e.clientX, y: e.clientY, moved: 0 };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!drag || !this.store) return;
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      drag.moved += Math.abs(dx) + Math.abs(dy);
      drag.x = e.clientX;
      drag.y = e.clientY;
      const scaleToCanvas = canvas.width / canvas.getBoundingClientRect().width;
      this.camera.cx -= (dx * scaleToCanvas) / this.camera.scale;
      this.camera.cy -= (dy * scaleToCanvas) / this.camera.scale;
      this.#beginView('camera');
      this.#paintPartial = true;
      this.requestRender();
      this.#scheduleUrlSync();
    });
    canvas.addEventListener('pointerup', (e) => {
      const wasClick = drag && drag.moved < CLICK_SLOP_PX;
      drag = null;
      if (wasClick) this.#inspect(e.clientX, e.clientY);
    });
    canvas.addEventListener('wheel', (e) => {
      if (!this.store) return;
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const px = ((e.clientX - rect.left) * canvas.width) / rect.width;
      const py = ((e.clientY - rect.top) * canvas.height) / rect.height;
      const { cx, cy, scale } = this.camera;
      const next = clamp(scale * Math.exp(-e.deltaY * 0.0015), this.fitScale * 0.5, MAX_SCALE);
      const worldX = cx + (px - canvas.width / 2) / scale;
      const worldY = cy + (py - canvas.height / 2) / scale;
      this.camera = { cx: worldX - (px - canvas.width / 2) / next, cy: worldY - (py - canvas.height / 2) / next, scale: next };
      this.#beginView('camera');
      this.#paintPartial = true;
      this.requestRender();
      this.#scheduleUrlSync();
    }, { passive: false });
    canvas.addEventListener('dblclick', () => this.store && this.fit());

    $('play-btn').addEventListener('click', () => this.togglePlay());
    $('speed').min = '0';
    $('speed').max = String(SPEEDS.length - 1);
    $('speed').addEventListener('input', (e) => this.setSpeed(SPEEDS[Number(e.target.value)]));
    this.#updateSpeedUi();
    $('prev-btn').addEventListener('click', () => this.goToTime(this.t - 1));
    $('next-btn').addEventListener('click', () => this.goToTime(this.t + 1));
    $('band-select').addEventListener('change', (e) => this.setBandChoice(Number(e.target.value)));
    const applyStretch = () => this.setStretch(Number($('stretch-min').value), Number($('stretch-max').value));
    $('stretch-min').addEventListener('change', applyStretch);
    $('stretch-max').addEventListener('change', applyStretch);
    $('stretch-auto').addEventListener('click', () => this.autoStretch());
    $('gap-toggle').addEventListener('click', () => this.toggleGaps());

    const track = $('timeline-track');
    let scrubbing = false;
    const timeFromEvent = (e) => {
      const rect = track.getBoundingClientRect();
      const pad = 8;
      const frac = clamp((e.clientX - rect.left - pad) / (rect.width - pad * 2), 0, 1);
      return Math.round(frac * (this.store.times.length - 1));
    };
    track.addEventListener('pointerdown', (e) => {
      if (!this.store) return;
      scrubbing = true;
      this.goToTime(timeFromEvent(e));
    });
    window.addEventListener('pointermove', (e) => scrubbing && this.goToTime(timeFromEvent(e)));
    window.addEventListener('pointerup', () => {
      scrubbing = false;
    });

    document.addEventListener('keydown', (e) => {
      if (!this.store) return;
      const typing = e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT';
      if (e.key === ' ') {
        if (e.target.tagName === 'SELECT' || (typing && e.target.type !== 'range')) return;
        e.preventDefault();
        if (!e.repeat) this.togglePlay();
        return;
      }
      if (typing) return;
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        this.goToTime(this.t - 1);
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        this.goToTime(this.t + 1);
      } else if (/^[1-9]$/.test(e.key)) {
        this.setProduct(Number(e.key) - 1);
      } else if ((e.key === 'd' || e.key === 'D') && !e.metaKey && !e.ctrlKey && !e.altKey) {
        this.togglePerfOverlay();
      }
    });
    // A focused button would also click on Space; the keydown above has already toggled playback.
    document.addEventListener('keyup', (e) => {
      if (e.key === ' ' && e.target.tagName === 'BUTTON') e.preventDefault();
    });
  }

  // ---- UI ----

  #buildProducts() {
    const container = $('products');
    container.replaceChildren();
    this.products.forEach((product, index) => {
      const button = document.createElement('button');
      button.textContent = product.name;
      button.dataset.index = index;
      button.disabled = !product.available;
      if (!product.available) button.title = `needs bands: ${product.missing.join(', ')}`;
      button.addEventListener('click', () => this.setProduct(index));
      container.appendChild(button);
    });
    const select = $('band-select');
    select.replaceChildren(...this.bands.map((band, i) => new Option(band.name, i)));
    select.value = String(this.bandChoice);
    this.#updateProductUi();
  }

  #updateProductUi() {
    for (const button of $('products').children) button.classList.toggle('active', Number(button.dataset.index) === this.productIndex);
    $('band-select').hidden = this.products[this.productIndex].id !== 'band';
    this.#updateStretchUi();
  }

  /** The min/max stretch controls show only while a band is displayed with an adjustable linear stretch. */
  #updateStretchUi() {
    const visible = Boolean(this.store) && this.#usesLinearRange();
    $('stretch').hidden = !visible;
    if (!visible) return;
    const [lo, hi] = this.#linear.range ?? [0, 1];
    for (const [id, value] of [['stretch-min', lo], ['stretch-max', hi]]) {
      const input = $(id);
      if (document.activeElement !== input) input.value = String(Number(value.toPrecision(5)));
    }
  }

  #buildTimeline() {
    const track = $('timeline-track');
    track.querySelectorAll('.timeline-tick').forEach((tick) => tick.remove());
    const n = this.store.times.length;
    this.#tickElements = this.store.times.map((_, i) => {
      const tick = document.createElement('div');
      tick.className = 'timeline-tick';
      tick.style.left = n > 1 ? `calc(8px + (100% - 16px) * ${i / (n - 1)})` : '50%';
      tick.title = this.#formatTime(i);
      track.appendChild(tick);
      return tick;
    });
  }

  #updatePlayUi() {
    const playing = this.#playback?.playing ?? false;
    if (this.#wasPlaying && !playing) {
      this.requestRender();
      this.#scheduleUrlSync();
    }
    if (playing !== this.#wasPlaying) this.#playEpoch++;
    this.#wasPlaying = playing;
    const button = $('play-btn');
    button.classList.toggle('playing', playing);
    button.title = playing ? 'Pause (Space)' : 'Play (Space)';
    button.setAttribute('aria-label', button.title);
    button.disabled = !this.store;
    $('export-btn').disabled = !this.store;
    if (!playing) {
      this.#lookahead?.abort();
      this.#lookahead = null;
    }
    this.#updateSpeedUi();
  }

  /** The slider shows the requested speed; when the display cannot keep up the label adds what is delivered. */
  #updateSpeedUi() {
    $('speed').value = String(SPEEDS.indexOf(this.#speed));
    const effective = this.#playback?.playing ? Math.round(this.#playback.effectiveStepsPerSecond) : this.#speed;
    $('speed-label').textContent = effective < this.#speed ? `${this.#speed} /s → ${effective}` : `${this.#speed} /s`;
  }

  /** "playing at 1/2 res · fits memory" (or "· link") while the movie level is coarser than the normal one; the tooltip has the numbers. */
  #updateResHint() {
    const { baseLod, lod, reason, detail } = this.#movie;
    const coarser = this.#playing && lod > baseLod;
    const text = coarser ? `playing at 1/${2 ** (lod - baseLod)} res · ${describeReason(reason)}` : '';
    const hint = $('res-hint');
    if (hint.textContent !== text) {
      hint.textContent = text;
      hint.title = coarser ? (detail ?? '') : '';
    }
  }

  #updateTimeUi() {
    this.#updateChartMarker();
    $('time-label').textContent = this.#formatTime(this.t);
    this.#tickElements.forEach((tick, i) => tick.classList.toggle('active', i === this.t));
  }

  #updateMeta() {
    const level = this.store.levels[0];
    const name = new URL(this.store.url, location.href).pathname.replace(/\/$/, '').split('/').pop();
    $('nav-meta').textContent = `${name} · ${this.bands.map((band) => band.name).join(' ')} · ${this.store.times.length} steps · ${level.width}×${level.height}`;
  }

  #updateCacheStats() {
    if (!this.store) return;
    const stats = readStats(this.store);
    $('cache-stats').textContent = `cache ${stats.cacheHits} hit / ${stats.cacheMisses} miss · ${((stats.decodedBytes ?? 0) / 1048576).toFixed(0)} MB · GPU ${this.renderer.slots} slots`;
  }

  #setProgress(frac) {
    const fill = $('progress-fill');
    if (frac <= 0) {
      fill.style.opacity = '0';
      fill.style.width = '0';
    } else if (frac >= 1) {
      fill.style.width = '100%';
      setTimeout(() => {
        fill.style.opacity = '0';
      }, 200);
    } else {
      fill.style.width = `${frac * 100}%`;
      fill.style.opacity = '1';
    }
  }

  /** A toast is a non-blocking notice that fades on its own; otherwise the box stays until the next load. */
  #showError(title, message, { toast = false } = {}) {
    clearTimeout(this.#toastTimer);
    $('error-title').textContent = title;
    $('error-message').textContent = message;
    const overlay = $('error-overlay');
    overlay.classList.add('visible');
    overlay.classList.toggle('toast', toast);
    if (toast) this.#toastTimer = setTimeout(() => this.#hideError(), TOAST_MS);
  }

  #hideError() {
    $('error-overlay').classList.remove('visible', 'toast');
  }

  #updateSidebar(info) {
    const empty = $('sidebar-empty');
    const content = $('sidebar-content');
    empty.style.display = info ? 'none' : 'flex';
    content.style.display = info ? 'block' : 'none';
    if (!info) return;
    content.innerHTML = sidebarHtml(info, this.#formatTime(info.t));
    this.#renderChart();
  }
}

const ndviColor = (v) => (v === null ? 'var(--text-3)' : v > 0.3 ? 'var(--green)' : v > 0 ? 'var(--amber)' : 'var(--red)');
const ndwiColor = (v) => (v === null ? 'var(--text-3)' : v > 0 ? 'var(--accent)' : 'var(--text-2)');

function metricHtml(name, value, color) {
  const frac = value === null ? 0 : ((value + 1) / 2) * 100;
  return `
    <div class="metric">
      <div class="metric-header">
        <span class="metric-name">${name}</span>
        <span class="metric-value" style="color:${color}">${value === null ? '—' : value.toFixed(3)}</span>
      </div>
      <div class="metric-bar"><div class="metric-fill" style="width:${frac}%;background:${color}"></div></div>
    </div>`;
}

/** A value for display: whole numbers as they are, others with 3 significant-ish digits (more decimals for small values). */
function formatValue(value) {
  if (!Number.isFinite(value) || Number.isInteger(value)) return String(value);
  const magnitude = Math.abs(value);
  return value.toFixed(magnitude >= 1000 ? 1 : magnitude >= 10 ? 2 : 3);
}

function sidebarHtml(info, timeLabel) {
  const indices = [];
  if (info.hasNdvi) indices.push(metricHtml('NDVI', info.ndvi, ndviColor(info.ndvi)));
  if (info.hasNdwi) {
    indices.push(metricHtml('NDWI', info.ndwi, ndwiColor(info.ndwi)));
    indices.push(`
      <div class="metric">
        <div class="metric-header">
          <span class="metric-name">Water</span>
          <span class="water-badge ${info.isWater ? 'yes' : 'no'}">
            <span class="water-dot" style="background:${info.isWater ? 'var(--water)' : 'var(--text-3)'}"></span>
            ${info.isWater ? 'Detected' : 'None'}
          </span>
        </div>
      </div>`);
  }
  const row = (label, value) => `<div class="meta-row"><span class="label">${label}</span><span class="value mono">${value}</span></div>`;
  const reflectance = info.bands.every((b) => b.reflectance);
  const physical = (b) => (reflectance ? b.value.toFixed(4) : `${formatValue(b.value)}${b.units && b.units !== 'reflectance' ? ` ${b.units}` : ''}`);
  const scaled = info.bands.some((b) => b.value !== b.stored);
  const observed = info.observed === null || info.observed === undefined ? '' : row('Observed by', info.observed === 0 ? 'no scene (gap-filled)' : `${info.observed} scene${info.observed === 1 ? '' : 's'}`);
  return `
    <div class="sidebar-section">
      <div class="section-label">Location</div>
      <div class="meta-row"><span class="label">Time</span><span class="value">${timeLabel}</span></div>
      ${row('Pixel (x, y)', `${info.pixel.x}, ${info.pixel.y}`)}
      ${row('Level', info.lod === 0 ? '0 (full resolution)' : `${info.lod} (${2 ** info.lod}× coarser)`)}
      ${observed}
    </div>
    ${indices.length ? `<div class="sidebar-section"><div class="section-label">Indices</div>${indices.join('')}</div>` : ''}
    <div class="sidebar-section">
      <div class="section-label">Over time</div>
      <div id="chart-legend" class="chart-legend"></div>
      <div id="chart" class="chart"></div>
      <div id="chart-status" class="chart-status"></div>
    </div>
    <div class="sidebar-section">
      <div class="section-label">${reflectance ? 'Reflectance' : 'Value'}</div>
      ${info.bands.map((b) => row(b.name, physical(b))).join('')}
    </div>
    ${
      scaled
        ? `<div class="sidebar-section">
      <div class="section-label">Stored value</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 16px;">
        ${info.bands.map((b) => row(b.name, b.stored)).join('')}
      </div>
    </div>`
        : ''
    }`;
}

async function loadCatalog() {
  const url = new URL('catalog.json', location.href);
  const response = await fetch(url);
  if (response.status === 404) return [];
  if (!response.ok) throw new Error(`catalog.json: HTTP ${response.status}`);
  const entries = await response.json();
  return entries.map(({ name, url: storeUrl }) => ({ name, url: new URL(storeUrl, url).href }));
}

async function main() {
  const viewer = new Viewer();
  window.tileripper = {
    viewer,
    bench: () => import('./bench.js').then((m) => m.runBenchmarks(viewer)),
    scrubBench: (options) => import('./bench.js').then((m) => m.runScrubBenchmarks(viewer, options)),
    playBench: (options) => import('./bench.js').then((m) => m.playBench(viewer, options)),
    interactionBench: (options) => import('./bench.js').then((m) => m.interactionBench(viewer, options)),
  };

  $('export-btn').addEventListener('click', () => toggleExportPanel(viewer));

  const catalog = await loadCatalog().catch((error) => {
    console.warn('catalog.json not usable:', error);
    return [];
  });
  const select = $('catalog-select');
  const inCatalog = (url) => catalog.some((entry) => entry.url === url);
  const initialSearch = location.search;
  const open = (url, { fallback = true, viewSearch } = {}) => {
    // A catalog store is not pinned in the URL, so an open tab follows catalog changes on reload;
    // only an external store is shareable via ?store=. The view (t, p, z, c) in the URL is restored
    // when the page first opens; opening another store starts from its default view.
    viewer.pinnedStore = inCatalog(url) ? null : url;
    history.replaceState(null, '', viewer.pinnedStore ? `?store=${encodeURIComponent(url)}` : location.pathname);
    select.value = url;
    // An external store earns a dropdown entry only once it has loaded, so a dead URL from an old
    // permalink never lingers as an option.
    const optionFor = (value) => [...select.options].find((option) => option.value === value);
    window.tileripper.ready = viewer
      .loadStore(url, { viewSearch })
      .then((result) => {
        if (!inCatalog(url) && catalog.length > 0 && !optionFor(url)) {
          select.add(new Option(url.replace(/^https?:\/\//, ''), url));
          select.value = url;
        }
        return result;
      })
      .catch((error) => {
        console.error(`loadStore(${url}) failed:`, error);
        optionFor(url)?.remove();
        if (fallback && catalog.length > 0 && catalog[0].url !== url) {
          console.warn(`falling back to the catalog store ${catalog[0].url}`);
          open(catalog[0].url, { fallback: false });
        }
      });
  };
  if (catalog.length > 0) {
    select.replaceChildren(...catalog.map((entry) => new Option(entry.name, entry.url)));
    select.hidden = false;
    select.addEventListener('change', () => open(select.value));
  }

  const requested = new URLSearchParams(location.search).get('store');
  if (requested) {
    open(new URL(requested, location.href).href, { viewSearch: initialSearch });
  } else if (catalog.length > 0) open(catalog[0].url, { viewSearch: initialSearch });
  else {
    $('error-title').textContent = 'No store selected';
    $('error-message').textContent = 'Open this page with ?store=<base URL of a chronozarr store>.';
    $('error-overlay').classList.add('visible');
  }
}

main();
