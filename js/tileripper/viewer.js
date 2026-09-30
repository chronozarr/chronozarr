// TileRipper viewer: renders a chronozarr store with WebGL2, scrubs through time, switches
// products on the GPU and shows decoded values on click. Opens ?store=<base url>.

import { chunkKey, openStore, scrubCost } from '../chronozarr/decoder.js';
import { Playback } from './playback.js';
import { Renderer } from './renderer.js';
import { computeStretchLo, describePixel, inputIndices, makeTimeFormatter, resolveProducts } from './products.js';

const PREFETCH_SETTLE_MS = 30;
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
const MIN_SPEED = 1;
const MAX_SPEED = 15;
const DEFAULT_SPEED = 4;
const SPEED_KEY = 'tileripper.stepsPerSecond';
const STRETCH_SAMPLES_PER_CELL = 300;

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** The last playback speed, if localStorage has a valid one (it can be missing or blocked). */
function loadSpeed() {
  try {
    const stored = Number(localStorage.getItem(SPEED_KEY));
    if (Number.isInteger(stored) && stored >= MIN_SPEED && stored <= MAX_SPEED) return stored;
  } catch (error) {
    console.warn('could not read the saved playback speed:', error);
  }
  return DEFAULT_SPEED;
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
  #paintPartial = true;
  #paintedLod = 0;
  #view = { lod: 0, cells: new Set() };
  #maxVisibleCells = 0;
  #rafId = 0;
  #prefetchTimer = 0;
  #gpuFillTimer = 0;
  #prefetchAbort = null;
  #painted = [];
  #storeGeneration = 0;
  #tickElements = [];
  #wave = null;
  #toastTimer = 0;
  #playback = null;
  #lookahead = null;
  #speed = loadSpeed();

  constructor() {
    this.renderer = new Renderer(this.canvas);
    this.renderer.evictionScore = (meta) => this.#chunkScore(meta);
    this.#resizeCanvas();
    new ResizeObserver(() => {
      this.#resizeCanvas();
      this.#paintPartial = true;
      this.requestRender();
    }).observe(this.canvas.parentElement);
    this.#bindInput();
    setInterval(() => this.#updateCacheStats(), 500);
  }

  /**
   * Open a store and resolve after the first complete frame is painted.
   * @param {string} url
   * @param {{lod?:number, fetch?:typeof fetch, maxCacheBytes?:number, workers?:number, camera?:{cx:number,cy:number,scale:number}}} [options]
   */
  async loadStore(url, options = {}) {
    this.#playback?.pause();
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

    this.store = store;
    store.evictionScore = (entry) => this.#chunkScore(entry);
    this.#configurePool();
    this.#playback = this.#createPlayback();
    this.products = resolveProducts(store.bands);
    this.productIndex = this.products.findIndex((p) => p.available);
    this.bandChoice = 0;
    this.t = 0;
    this.paintedT = -1;
    this.#direction = 1;
    this.#stretchLo = null;
    this.lodOverride = options.lod ?? null;
    this.#formatTime = makeTimeFormatter(store.times);
    this.#paintPartial = true;
    if (options.camera) this.camera = { ...options.camera };
    else this.fit();
    this.#buildProducts();
    this.#buildTimeline();
    this.#updateTimeUi();
    this.#updatePlayUi();
    this.#updateMeta();
    this.#updateSidebar(null);
    $('click-hint').classList.remove('hidden');

    const painted = this.whenPainted();
    this.renderNow();
    await painted;
    this.#setProgress(1);
    return { openMs, firstPaintMs: performance.now() - started };
  }

  fit() {
    const level = this.store.levels[0];
    const { width, height } = this.canvas;
    const scale = Math.min(width / level.width, height / level.height) * 0.94;
    this.camera = { cx: level.width / 2, cy: level.height / 2, scale };
    this.#paintPartial = true;
    this.requestRender();
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
    this.#direction = direction ?? (next > this.t ? 1 : -1);
    this.t = next;
    this.#emit({ type: 'input', t: next });
    this.#updateTimeUi();
    this.requestRender();
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
    this.#speed = clamp(Math.round(stepsPerSecond), MIN_SPEED, MAX_SPEED);
    saveSpeed(this.#speed);
    this.#playback?.setSpeed(this.#speed);
    this.#updateSpeedUi();
  }

  /** Whether every visible cell has the chunks for timestep t in memory, so showing it needs no fetch. */
  isTimestepReady(t) {
    const { lod, cells } = this.#view;
    const anchorT = this.store.anchorOf(t);
    for (const key of cells) {
      const [row, col] = key.split('/').map(Number);
      for (const ct of new Set([anchorT, t])) {
        if (!this.renderer.isResident(chunkKey(lod, row, col, ct)) && !this.store.peekRaw(lod, row, col, ct)) return false;
      }
    }
    return true;
  }

  setProduct(index) {
    if (!this.products[index]?.available) return;
    this.productIndex = index;
    this.#updateProductUi();
    this.#paintPartial = true;
    this.requestRender();
  }

  setBandChoice(index) {
    this.bandChoice = index;
    this.#paintPartial = true;
    this.requestRender();
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
    const painted = clear || drawable.length > 0;

    if (complete && this.#stretchLo === null) this.#stretchLo = this.#computeStretch(lod, cells, t);
    if (painted) {
      renderer.beginPaint(
        {
          width: canvas.width,
          height: canvas.height,
          ...this.camera,
          ...this.#productUniforms(),
          stretchLo: this.#stretchLo ?? 0,
          nodata: store.nodata,
        },
        { clear },
      );
      if (!complete) this.#drawCoarser(lod, t);
      for (const { row, col, slots } of drawable) this.#drawCell(lod, row, col, slots);
      this.#paintedLod = lod;
      if (complete) this.paintedT = t;
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
    if (painted) this.#emit({ type: 'paint', t, lod, complete, cells: cells.length, ready: drawable.length, uploadMs, renderMs: ms - uploadMs });
    if (complete) {
      $('status').innerHTML = `<span class="fast">${Math.round(ms)}ms</span>`;
      for (const resolve of this.#painted.splice(0)) resolve();
    }
    return { complete, ms, lod, cells: cells.length };
  }

  /** Start background prefetch now and resolve with its result when it finishes or is aborted. */
  prefetchNow() {
    clearTimeout(this.#prefetchTimer);
    this.#prefetchAbort?.abort();
    const abort = new AbortController();
    this.#prefetchAbort = abort;
    const lod = this.#view.lod;
    const cells = [...this.#view.cells].map((k) => k.split('/').map(Number));
    return this.store
      .prefetch({
        lod,
        cells,
        t: this.t,
        direction: this.#direction,
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
    const { lod, cells } = this.#view;
    const anchorT = this.store.anchorOf(t);
    for (const key of cells) {
      const [row, col] = key.split('/').map(Number);
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
    const slots = renderer.planSlots(first.nBand, first.chunkWidth, first.chunkHeight, POOL_BUDGET_BYTES, wanted);
    renderer.configure({ nBand: first.nBand, chunkWidth: first.chunkWidth, chunkHeight: first.chunkHeight, slots });
    this.#maxVisibleCells = Math.floor(slots / 2);
  }

  #resizeCanvas() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.parentElement.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
  }

  #targetLod() {
    if (this.lodOverride !== null) return this.lodOverride;
    const maxLod = this.store.levels.length - 1;
    let lod = clamp(Math.floor(Math.log2(1 / this.camera.scale) + LOD_BIAS + 1e-9), 0, maxLod);
    while (lod < maxLod && this.#visibleCells(lod).length > this.#maxVisibleCells) lod++;
    return lod;
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
  #slotsFor(lod, row, col, t) {
    const anchorT = this.store.anchorOf(t);
    const anchor = this.#slotForChunk(lod, row, col, anchorT);
    if (anchor < 0) return null;
    if (anchorT === t) return { anchor, delta: -1 };
    const delta = this.#slotForChunk(lod, row, col, t);
    return delta < 0 ? null : { anchor, delta };
  }

  #slotForChunk(lod, row, col, t) {
    const key = chunkKey(lod, row, col, t);
    const resident = this.renderer.slotOf(key);
    if (resident >= 0) return resident;
    const data = this.store.peekRaw(lod, row, col, t);
    return data ? this.renderer.upload(key, { lod, row, col, t }, data) : -1;
  }

  #scheduleGpuFill() {
    clearTimeout(this.#gpuFillTimer);
    this.#gpuFillTimer = setTimeout(() => this.#fillGpuWindow(), 0);
  }

  /**
   * Keep a window of timesteps around t resident in the texture pool, as wide as the pool holds for the
   * visible cells and reaching further in the scrub direction, so the next steps are uniform changes
   * instead of uploads. Uploads come from the decoded cache in slices of GPU_UPLOAD_SLICE_MS.
   */
  #fillGpuWindow() {
    const { lod, cells } = this.#view;
    const { store, renderer, t } = this;
    if (cells.size === 0) return;
    const anchorShare = store.anchorIndices.length / store.times.length;
    const steps = Math.max(1, Math.floor((renderer.slots * GPU_FILL_FRACTION) / (cells.size * (1 + anchorShare))));
    const timesteps = Array.from({ length: store.times.length }, (_, i) => i)
      .sort((a, b) => scrubCost(a - t, this.#direction) - scrubCost(b - t, this.#direction))
      .slice(0, steps);
    const started = performance.now();
    for (const tt of timesteps) {
      for (const ct of new Set([store.anchorOf(tt), tt])) {
        for (const key of cells) {
          const [row, col] = key.split('/').map(Number);
          const chunk = chunkKey(lod, row, col, ct);
          const data = renderer.isResident(chunk) ? null : store.peekRaw(lod, row, col, ct);
          if (!data) continue;
          renderer.upload(chunk, { lod, row, col, t: ct }, data, { background: true });
          if (performance.now() - started > GPU_UPLOAD_SLICE_MS) {
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
    const cost = scrubCost(meta.t - this.t, this.#direction);
    return (visible ? 0 : 1e6) + (this.store.isAnchor(meta.t) ? cost / 2 : cost);
  }

  #drawCell(lod, row, col, slots) {
    const level = this.store.levels[lod];
    const factor = 2 ** lod;
    const { width, height } = this.store.cellExtent(lod, row, col);
    this.renderer.drawCell(
      { x: col * level.chunkWidth * factor, y: row * level.chunkHeight * factor, w: width * factor, h: height * factor },
      { w: width, h: height },
      slots.anchor,
      slots.delta,
    );
  }

  /** Progressive LOD: under a partial frame, paint any already-cached coarser cells, coarsest first. */
  #drawCoarser(lod, t) {
    for (let coarse = this.store.levels.length - 1; coarse > lod; coarse--) {
      for (const [row, col] of this.#visibleCells(coarse)) {
        const slots = this.#slotsFor(coarse, row, col, t);
        if (slots) this.#drawCell(coarse, row, col, slots);
      }
    }
  }

  #productUniforms() {
    const product = this.products[this.productIndex];
    return { shader: product.shader, inputs: inputIndices(product, this.store.bands, this.bandChoice) };
  }

  /** 2nd percentile of tone-mapped true-color samples, kept fixed while scrubbing. 0 without B02/B03/B04. */
  #computeStretch(lod, cells, t) {
    const { store } = this;
    const idx = ['B04', 'B03', 'B02'].map((name) => store.bands.indexOf(name));
    if (idx.some((i) => i < 0)) return 0;
    const level = store.levels[lod];
    const samples = [];
    for (const [row, col] of cells) {
      const { width, height } = store.cellExtent(lod, row, col);
      const stride = Math.max(1, Math.floor((width * height) / STRETCH_SAMPLES_PER_CELL));
      for (let i = 0; i < width * height; i += stride) {
        const values = store.samplePixel(lod, row, col, t, i % width, Math.floor(i / width));
        if (values) samples.push(idx.map((b) => values[b]));
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
      let done = 0;
      const { signal } = wave.controller;
      const anchorT = this.store.anchorOf(t);
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
    previous?.controller.abort();
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

  #schedulePrefetch() {
    clearTimeout(this.#prefetchTimer);
    this.#prefetchTimer = setTimeout(() => this.prefetchNow(), PREFETCH_SETTLE_MS);
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
      this.#updateSidebar(null);
      return;
    }
    const row = Math.floor(y / level.chunkHeight);
    const col = Math.floor(x / level.chunkWidth);
    const t = this.t;
    const anchorT = this.store.anchorOf(t);
    try {
      await Promise.all([this.store.getRaw(lod, row, col, anchorT), anchorT === t ? null : this.store.getRaw(lod, row, col, t)]);
    } catch (error) {
      this.#showError('Chunk load failed', error.message);
      return;
    }
    const values = this.store.samplePixel(lod, row, col, t, x - col * level.chunkWidth, y - row * level.chunkHeight);
    this.#updateSidebar({ t, lod, pixel: { x: Math.floor(worldX), y: Math.floor(worldY) }, ...describePixel(values, this.store.bands) });
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
      this.#paintPartial = true;
      this.requestRender();
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
      this.#paintPartial = true;
      this.requestRender();
    }, { passive: false });
    canvas.addEventListener('dblclick', () => this.store && this.fit());

    $('play-btn').addEventListener('click', () => this.togglePlay());
    $('speed').min = String(MIN_SPEED);
    $('speed').max = String(MAX_SPEED);
    $('speed').addEventListener('input', (e) => this.setSpeed(Number(e.target.value)));
    this.#updateSpeedUi();
    $('prev-btn').addEventListener('click', () => this.goToTime(this.t - 1));
    $('next-btn').addEventListener('click', () => this.goToTime(this.t + 1));
    $('band-select').addEventListener('change', (e) => this.setBandChoice(Number(e.target.value)));

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
    select.replaceChildren(...this.store.bands.map((name, i) => new Option(name, i)));
    this.#updateProductUi();
  }

  #updateProductUi() {
    for (const button of $('products').children) button.classList.toggle('active', Number(button.dataset.index) === this.productIndex);
    $('band-select').hidden = this.products[this.productIndex].id !== 'band';
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
    const button = $('play-btn');
    button.classList.toggle('playing', playing);
    button.title = playing ? 'Pause (Space)' : 'Play (Space)';
    button.setAttribute('aria-label', button.title);
    button.disabled = !this.store;
    if (!playing) {
      this.#lookahead?.abort();
      this.#lookahead = null;
    }
    this.#updateSpeedUi();
  }

  #updateSpeedUi() {
    $('speed').value = String(this.#speed);
    $('speed-label').textContent = `${this.#speed} /s`;
  }

  #updateTimeUi() {
    $('time-label').textContent = this.#formatTime(this.t);
    this.#tickElements.forEach((tick, i) => tick.classList.toggle('active', i === this.t));
  }

  #updateMeta() {
    const level = this.store.levels[0];
    const name = new URL(this.store.url, location.href).pathname.replace(/\/$/, '').split('/').pop();
    $('nav-meta').textContent = `${name} · ${this.store.bands.join(' ')} · ${this.store.times.length} steps · ${level.width}×${level.height}`;
  }

  #updateCacheStats() {
    if (!this.store) return;
    const { cache } = this.store.stats;
    const info = this.store.cacheInfo();
    $('cache-stats').textContent = `cache ${cache.hits} hit / ${cache.misses} miss · ${(info.bytes / 1048576).toFixed(0)} MB · GPU ${this.renderer.slots} slots`;
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
  return `
    <div class="sidebar-section">
      <div class="section-label">Location</div>
      <div class="meta-row"><span class="label">Time</span><span class="value">${timeLabel}</span></div>
      ${row('Pixel (x, y)', `${info.pixel.x}, ${info.pixel.y}`)}
      ${row('Level', info.lod === 0 ? '0 (full resolution)' : `${info.lod} (${2 ** info.lod}× coarser)`)}
    </div>
    ${indices.length ? `<div class="sidebar-section"><div class="section-label">Indices</div>${indices.join('')}</div>` : ''}
    <div class="sidebar-section">
      <div class="section-label">Reflectance</div>
      ${info.bands.map((b) => row(b.name, b.reflectance.toFixed(4))).join('')}
    </div>
    <div class="sidebar-section">
      <div class="section-label">Raw DN</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 16px;">
        ${info.bands.map((b) => row(b.name, b.dn)).join('')}
      </div>
    </div>`;
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
  };

  const catalog = await loadCatalog().catch((error) => {
    console.warn('catalog.json not usable:', error);
    return [];
  });
  const select = $('catalog-select');
  const inCatalog = (url) => catalog.some((entry) => entry.url === url);
  const open = (url, { fallback = true } = {}) => {
    // A catalog store is not pinned in the URL, so an open tab follows catalog changes on reload;
    // only an external store is shareable via ?store=.
    history.replaceState(null, '', inCatalog(url) ? location.pathname : `?store=${encodeURIComponent(url)}`);
    select.value = url;
    window.tileripper.ready = viewer.loadStore(url).catch((error) => {
      console.error(`loadStore(${url}) failed:`, error);
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
    const url = new URL(requested, location.href).href;
    if (catalog.length > 0 && !catalog.some((entry) => entry.url === url)) {
      select.add(new Option(url.replace(/^https?:\/\//, ''), url));
    }
    open(url);
  }
  else if (catalog.length > 0) open(catalog[0].url);
  else {
    $('error-title').textContent = 'No store selected';
    $('error-message').textContent = 'Open this page with ?store=<base URL of a chronozarr store>.';
    $('error-overlay').classList.add('visible');
  }
}

main();
