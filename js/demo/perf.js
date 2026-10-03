// Performance numbers shared by the overlay (key "d") and the interaction benchmark: the store's counters in
// one flat shape whichever reader version supplies them, and a frame-gap monitor.
//
// The reader exposes `store.stats()` with `network {requests, bytes, deduped, inflight}` and
// `cache {hits, misses, joins, evictions, speculativeBytes}`, `store.cacheInfo()` for the decoded and compressed
// cache sizes and `store.bandwidthEstimate()`. A counter the reader leaves out is null here, never guessed.

export const MB = 1024 * 1024;

// Gaps between animation frames above these count as a missed 60 Hz / 30 Hz frame (16.7 ms and 33.3 ms plus timestamp noise).
export const SLOW_FRAME_MS = 17;
export const VERY_SLOW_FRAME_MS = 33;

/** The store's counters as one flat object; a counter the reader leaves out is null. */
export function readStats(store) {
  const { cache = {}, network = {} } = store.stats();
  const info = store.cacheInfo();
  return {
    cacheHits: cache.hits ?? null,
    cacheMisses: cache.misses ?? null,
    evictions: cache.evictions ?? null,
    dedupedRequests: network.deduped ?? null,
    requests: network.requests ?? null,
    inflight: network.inflight ?? null,
    transferredBytes: network.bytes ?? null,
    decodedBytes: info.bytes ?? null,
    compressedBytes: info.compressedBytes ?? null,
    speculativeBytes: cache.speculativeBytes ?? null,
    bandwidth: store.bandwidthEstimate() ?? null,
  };
}

/** Counters that only grow; the rest (cache sizes, in flight, bandwidth) are levels, not counts. */
const CUMULATIVE = ['cacheHits', 'cacheMisses', 'evictions', 'dedupedRequests', 'requests', 'transferredBytes'];

/** `after` minus `before` for the cumulative counters (null when either side lacks one); levels come from `after`. */
export function statsDelta(before, after) {
  const out = { ...after };
  for (const key of CUMULATIVE) out[key] = before[key] === null || after[key] === null ? null : after[key] - before[key];
  return out;
}

/** Counters of a store that has just been opened: zero for the cumulative ones, unknown for the levels. */
export function emptyStats() {
  return {
    cacheHits: 0,
    cacheMisses: 0,
    evictions: 0,
    dedupedRequests: 0,
    requests: 0,
    inflight: null,
    transferredBytes: 0,
    decodedBytes: null,
    compressedBytes: null,
    speculativeBytes: null,
    bandwidth: null,
  };
}

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return '–';
  if (bytes >= 1024 * MB) return `${(bytes / (1024 * MB)).toFixed(2)} GB`;
  if (bytes >= MB) return `${(bytes / MB).toFixed(bytes >= 100 * MB ? 0 : 1)} MB`;
  return `${Math.round(bytes / 1024)} kB`;
}

/** "38.2 MB/s (306 Mbit/s)" for bytes per second; "–" when unknown. */
export function formatRate(bytesPerSecond) {
  if (bytesPerSecond === null || bytesPerSecond === undefined) return '–';
  return `${(bytesPerSecond / MB).toFixed(1)} MB/s (${Math.round((bytesPerSecond * 8) / 1e6)} Mbit/s)`;
}

export function formatMs(ms) {
  if (ms === null || ms === undefined) return '–';
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`;
}

/** hits / (hits + misses) as a percentage string; "–" without lookups. */
export function hitRate(hits, misses) {
  if (hits === null || misses === null || hits + misses === 0) return '–';
  return `${Math.round((hits / (hits + misses)) * 100)} %`;
}

/**
 * Counts animation frames that took longer than 17 ms and 33 ms (missed 60 Hz and 30 Hz frames) between
 * start() and stop(). `requestFrame`/`cancelFrame` are injectable for tests.
 */
export class FrameMonitor {
  #requestFrame;
  #cancelFrame;
  #handle = null;
  #last = null;
  #running = false;
  frames = 0;
  over17 = 0;
  over33 = 0;
  maxGapMs = 0;

  constructor({ requestFrame = (callback) => requestAnimationFrame(callback), cancelFrame = (handle) => cancelAnimationFrame(handle) } = {}) {
    this.#requestFrame = requestFrame;
    this.#cancelFrame = cancelFrame;
  }

  get running() {
    return this.#running;
  }

  start() {
    if (this.#running) return;
    this.#running = true;
    this.#last = null;
    this.#handle = this.#requestFrame((time) => this.#onFrame(time));
  }

  stop() {
    if (!this.#running) return;
    this.#running = false;
    if (this.#handle !== null) this.#cancelFrame(this.#handle);
    this.#handle = null;
  }

  reset() {
    this.frames = 0;
    this.over17 = 0;
    this.over33 = 0;
    this.maxGapMs = 0;
    this.#last = null;
  }

  snapshot() {
    return { frames: this.frames, over16_7ms: this.over17, over33ms: this.over33, maxGapMs: this.maxGapMs };
  }

  #onFrame(time) {
    if (!this.#running) return;
    if (this.#last !== null) {
      const gap = time - this.#last;
      this.frames++;
      if (gap > SLOW_FRAME_MS) this.over17++;
      if (gap > VERY_SLOW_FRAME_MS) this.over33++;
      this.maxGapMs = Math.max(this.maxGapMs, gap);
    }
    this.#last = time;
    this.#handle = this.#requestFrame((next) => this.#onFrame(next));
  }
}

// ---- latency from probe events ----

/**
 * Time from each input to the first paint that reached it, from the viewer's probe events (`{type:'paint', at, t,
 * complete, ...}`). `inputs` is `[{at, t}]`; `reached(paint, input)` says whether a paint shows what the input
 * asked for (for a step in the scrub direction: that timestep or a later one; for a gesture: any paint after it).
 * `coarseMs` is the first whole frame at any level (see isWholeFrame; null when the viewer does not report whether
 * its frames are whole), `fullMs` the first complete paint at the target level.
 */
export function analyzeLatency(events, inputs, reached) {
  const paints = events.filter((e) => e.type === 'paint');
  const rows = inputs.map((input) => {
    const candidates = paints.filter((p) => p.at >= input.at && reached(p, input));
    const whole = candidates.find(isWholeFrame);
    const full = candidates.find((p) => p.complete);
    const reportsFrames = paints.some((p) => p.covered !== undefined || p.partial !== undefined);
    return {
      coarseMs: reportsFrames && whole ? whole.at - input.at : null,
      fullMs: full ? full.at - input.at : null,
    };
  });
  const pick = (key) => rows.map((row) => row[key]).filter((value) => value !== null);
  return { inputs: rows.length, coarse: distribution(pick('coarseMs')), full: distribution(pick('fullMs')), neverCompleted: rows.filter((r) => r.fullMs === null).length };
}

/** {n, median, p95, max} of a list of numbers (nulls when empty), rounded to 0.1. */
export function distribution(values) {
  const sorted = [...values].sort((a, b) => a - b);
  if (sorted.length === 0) return { n: 0, median: null, p95: null, max: null };
  const at = (q) => Math.round(sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))] * 10) / 10;
  return { n: sorted.length, median: at(0.5), p95: at(0.95), max: at(1) };
}

const cell = (value, format) => (value === null || value === undefined ? '–' : format(value));
const medMax = (d) => (d.n === 0 ? '–' : `${formatMs(d.median)} / ${formatMs(d.max)}`);

/**
 * Markdown table of interaction-benchmark phases. Each phase: `{name, coarse, full, frameCompleteness, stats (a statsDelta),
 * frames, peakInflight}`.
 */
export function formatPhaseTable(phases) {
  const header = ['phase', 'coarse med / max', 'full med / max', 'partial / fallback / kept frames', 'requests', 'transferred', 'hits / misses', 'deduped', 'peak in-flight', 'frames >16.7 / >33 ms', 'decoded / compressed cache', 'bandwidth'];
  const rows = phases.map((p) => [
    p.name,
    p.coarse ? medMax(p.coarse) : '–',
    p.full ? medMax(p.full) : '–',
    p.frameCompleteness ? `${p.frameCompleteness.partial} / ${cell(p.frameCompleteness.levelFallbacks, String)} / ${cell(p.frameCompleteness.keptFrames, String)} of ${p.frameCompleteness.painted}` : '–',
    cell(p.stats.requests, String),
    cell(p.stats.transferredBytes, formatBytes),
    `${cell(p.stats.cacheHits, String)} / ${cell(p.stats.cacheMisses, String)}`,
    cell(p.stats.dedupedRequests, String),
    cell(p.peakInflight, String),
    `${p.frames.over16_7ms} / ${p.frames.over33ms} of ${p.frames.frames}`,
    `${formatBytes(p.stats.decodedBytes)} / ${formatBytes(p.stats.compressedBytes)}`,
    cell(p.stats.bandwidth, formatRate),
  ]);
  return [header, header.map(() => '---'), ...rows].map((row) => `| ${row.join(' | ')} |`).join('\n');
}

// ---- frame completeness from probe events ----

/**
 * Whether a paint event is one whole frame. A viewer that draws only whole frames says so with `partial: false`. For
 * one that painted cell by cell, a frame is whole when every cell of the target level was drawn, or when only a
 * coarser level was drawn and under no target cell at all; a target cell drawn on top of a coarser level or of the
 * previous timestep is a partial frame.
 */
export function isWholeFrame(paint) {
  if (paint.partial !== undefined) return !paint.partial;
  return Boolean(paint.complete) || (paint.ready === 0 && paint.covered === true);
}

/**
 * What the frames painted in a run were like: how many, how many were partial (the viewer should never paint one),
 * how many showed a coarser level than the one it wanted (`levelFallbacks`), and how often it kept the canvas as it
 * was because no whole frame was ready (`keptFrames`). The last two are null for a viewer that does not report them.
 */
export function frameCompleteness(events) {
  const paints = events.filter((e) => e.type === 'paint');
  const reports = paints.some((p) => p.partial !== undefined) || events.some((e) => e.type === 'kept');
  const partial = paints.filter((p) => !isWholeFrame(p)).length;
  return {
    painted: paints.length,
    partial,
    partialFraction: paints.length === 0 ? 0 : partial / paints.length,
    levelFallbacks: reports ? paints.filter((p) => p.fallback === true).length : null,
    keptFrames: reports ? events.filter((e) => e.type === 'kept').length : null,
  };
}
