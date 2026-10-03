// Export the current view's loop as a WebM video (canvas.captureStream + MediaRecorder) or a GIF (vendored
// gifenc). Frames are drawn offscreen at the canvas size by the viewer's export session, so the file matches
// what is on screen; a timestep whose data is not in memory yet is waited for, like playback holds.

import { applyPalette, GIFEncoder, quantize } from '../vendor/gifenc.esm.js';
import { Playback } from './playback.js';

const PALETTE_SAMPLES = 6;
const PALETTE_PIXEL_STRIDE = 4;
const PRELOAD_BATCH = 4;
const LOAD_ATTEMPTS = 3;
const WEBM_TYPES = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];

const abortError = () => new DOMException('Aborted', 'AbortError');

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

/** GIF frame delays are whole centiseconds, and browsers show anything under 20 ms as 100 ms: 50 steps/s at most. */
export function gifDelayMs(stepsPerSecond) {
  return Math.max(20, Math.round(1000 / stepsPerSecond / 10) * 10);
}

export function pickWebmMimeType(isTypeSupported) {
  return WEBM_TYPES.find((type) => isTypeSupported(type)) ?? null;
}

export function webmBitrate(width, height, stepsPerSecond) {
  return Math.round(Math.min(24e6, Math.max(2e6, width * height * stepsPerSecond * 0.15)));
}

/** Up to `count` distinct timesteps spread evenly over from..to, ends included. */
export function paletteSampleTimes(from, to, count = PALETTE_SAMPLES) {
  const times = new Set();
  for (let i = 0; i < count; i++) times.add(from + Math.round((i * (to - from)) / Math.max(1, count - 1)));
  return [...times];
}

/** RGBA rows in the opposite order (GL reads bottom-up, images are top-down). */
export function flipRows(rgba, width, height) {
  const out = new Uint8Array(rgba.length);
  const stride = width * 4;
  for (let row = 0; row < height; row++) out.set(rgba.subarray(row * stride, (row + 1) * stride), (height - 1 - row) * stride);
  return out;
}

/** Every `stride`-th pixel of an RGBA array, for choosing a palette without looking at every pixel. */
export function subsamplePixels(rgba, stride) {
  const out = new Uint8Array(Math.ceil(rgba.length / 4 / stride) * 4);
  for (let i = 0, o = 0; i < rgba.length; i += 4 * stride, o += 4) out.set(rgba.subarray(i, i + 4), o);
  return out;
}

export function exportFilename(storeName, format, from, to, labels) {
  const slug = (text) => text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return `chronozarr-${slug(storeName)}-${slug(labels(from))}-to-${slug(labels(to))}.${format === 'gif' ? 'gif' : 'webm'}`;
}

/** Load a timestep for every cell, retrying if the cache dropped it again; fails rather than waiting forever. */
async function ensureLoaded(session, t, signal) {
  for (let attempt = 0; attempt < LOAD_ATTEMPTS && !session.isReady(t); attempt++) await session.prepare(t, signal);
  if (!session.isReady(t)) {
    throw new Error(`timestep ${t} did not stay in the chunk cache after ${LOAD_ATTEMPTS} loads; try a smaller size or a shorter range`);
  }
}

/** Fetch the whole range first (when it fits the cache) so recording runs without waiting on the network. */
async function preload(session, from, to, onProgress, signal) {
  const total = to - from + 1;
  if (!session.fits(total)) return;
  for (let t = from, done = 0; t <= to; t += PRELOAD_BATCH) {
    const batch = Array.from({ length: Math.min(PRELOAD_BATCH, to - t + 1) }, (_, i) => t + i);
    await Promise.all(batch.map((step) => (session.isReady(step) ? null : session.prepare(step, signal))));
    done += batch.length;
    onProgress({ stage: 'loading', done, total });
  }
}

async function encodeGif(session, { from, to, stepsPerSecond, onProgress, signal }) {
  const total = to - from + 1;
  const { width, height } = session.canvas;
  const samples = [];
  for (const t of paletteSampleTimes(from, to)) {
    await ensureLoaded(session, t, signal);
    session.render(t);
    samples.push(subsamplePixels(session.readPixels(), PALETTE_PIXEL_STRIDE));
  }
  const sampled = new Uint8Array(samples.reduce((n, s) => n + s.length, 0));
  let at = 0;
  for (const s of samples) {
    sampled.set(s, at);
    at += s.length;
  }
  const palette = quantize(sampled, 256);
  const gif = GIFEncoder();
  const delay = gifDelayMs(stepsPerSecond);
  for (let i = 0; i < total; i++) {
    signal?.throwIfAborted();
    await ensureLoaded(session, from + i, signal);
    session.render(from + i);
    const index = applyPalette(flipRows(session.readPixels(), width, height), palette);
    gif.writeFrame(index, width, height, i === 0 ? { palette, delay, repeat: 0 } : { delay });
    onProgress({ stage: 'encoding', done: i + 1, total });
    await sleep(0, signal);
  }
  gif.finish();
  return new Blob([gif.bytesView()], { type: 'image/gif' });
}

async function recordWebm(session, { from, to, stepsPerSecond, onProgress, signal }) {
  const mimeType = pickWebmMimeType((type) => MediaRecorder.isTypeSupported(type));
  if (!mimeType) throw new Error('This browser cannot record WebM: MediaRecorder with a WebM codec is not available.');
  const total = to - from + 1;
  const stream = session.canvas.captureStream(0);
  const [track] = stream.getVideoTracks();
  const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: webmBitrate(session.canvas.width, session.canvas.height, stepsPerSecond) });
  const chunks = [];
  recorder.ondataavailable = (event) => event.data.size > 0 && chunks.push(event.data);
  const stopped = new Promise((resolve, reject) => {
    recorder.onstop = resolve;
    recorder.onerror = (event) => reject(event.error ?? new Error('MediaRecorder failed'));
  });
  const frame = (t) => {
    session.render(t);
    track.requestFrame();
  };
  try {
    await ensureLoaded(session, from, signal);
    recorder.start();
    frame(from);
    onProgress({ stage: 'recording', done: 1, total });
    await sleep(1000 / stepsPerSecond, signal);
    if (total > 1) {
      // The same frame-driven player as playback, so holds and pacing behave the same; it stops at the last frame.
      await new Promise((resolve, reject) => {
        let index = 0;
        const player = new Playback({
          count: total,
          stepsPerSecond,
          getIndex: () => index,
          goTo: (i) => {
            index = i;
            frame(from + i);
            onProgress({ stage: 'recording', done: i + 1, total });
            if (i === total - 1) {
              player.pause();
              resolve();
            }
          },
          isReady: (i) => session.isReady(from + i),
          prepare: (i) =>
            ensureLoaded(session, from + i, signal).catch((error) => {
              player.pause();
              reject(error);
            }),
        });
        signal?.addEventListener('abort', () => {
          player.pause();
          reject(abortError());
        }, { once: true });
        player.play();
      });
      await sleep(1000 / stepsPerSecond, signal);
    }
    recorder.stop();
    await stopped;
  } finally {
    if (recorder.state !== 'inactive') recorder.stop();
    for (const t of stream.getTracks()) t.stop();
  }
  return new Blob(chunks, { type: mimeType });
}

/**
 * Render timesteps from..to of the current view at `stepsPerSecond` and encode them.
 * @param {object} viewer  the Viewer (pauses it, then uses viewer.createExportSession)
 * @param {{format: 'webm'|'gif', from: number, to: number, stepsPerSecond: number, scale?: number,
 *   onProgress?: (p: {stage: 'loading'|'recording'|'encoding', done: number, total: number}) => void, signal?: AbortSignal}} options
 * @returns {Promise<{blob: Blob, filename: string, frames: number, width: number, height: number, ms: number, bytes: number}>}
 */
export async function exportLoop(viewer, { format, from, to, stepsPerSecond, scale = 1, onProgress = () => {}, signal }) {
  if (!['webm', 'gif'].includes(format)) throw new RangeError(`unknown export format "${format}"`);
  if (!(from >= 0 && to >= from && to < viewer.store.times.length)) throw new RangeError(`export range ${from}..${to} is outside 0..${viewer.store.times.length - 1}`);
  viewer.pause();
  const started = performance.now();
  const session = viewer.createExportSession({ scale, timesteps: to - from + 1 });
  try {
    await preload(session, from, to, onProgress, signal);
    const run = format === 'gif' ? encodeGif : recordWebm;
    const blob = await run(session, { from, to, stepsPerSecond, onProgress, signal });
    const storeName = new URL(viewer.store.url, location.href).pathname.replace(/\/$/, '').split('/').slice(-2).join(' ');
    return {
      blob,
      filename: exportFilename(storeName, format, from, to, (t) => viewer.timeLabel(t)),
      frames: to - from + 1,
      width: session.canvas.width,
      height: session.canvas.height,
      ms: performance.now() - started,
      bytes: blob.size,
    };
  } finally {
    session.close();
  }
}

// ---- the Export panel ----

const $ = (id) => document.getElementById(id);
let panelReady = false;
let running = null;
let downloadUrl = null;
let listedStore = null;

const megabytes = (bytes) => `${(bytes / 1048576).toFixed(1)} MB`;

function describeProgress({ stage, done, total }, format) {
  const what = stage === 'loading' ? 'Loading' : stage === 'recording' ? 'Recording' : `Encoding ${format.toUpperCase()}`;
  return { fraction: done / total, text: `${what}: ${done} of ${total}` };
}

function fillTimeSelects(viewer) {
  const options = () => viewer.store.times.map((_, t) => new Option(viewer.timeLabel(t), t));
  const from = $('export-from');
  const to = $('export-to');
  const last = viewer.store.times.length - 1;
  if (listedStore !== viewer.store) {
    listedStore = viewer.store;
    from.replaceChildren(...options());
    to.replaceChildren(...options());
    from.value = '0';
    to.value = String(last);
  }
  $('export-rate').textContent = `${viewer.speed} steps/s (from the speed control)`;
}

async function startExport(viewer) {
  const format = $('export-format').value;
  const from = Number($('export-from').value);
  const to = Number($('export-to').value);
  const controller = new AbortController();
  running = controller;
  $('export-start').hidden = true;
  $('export-cancel').hidden = false;
  $('export-download').hidden = true;
  if (downloadUrl) URL.revokeObjectURL(downloadUrl);
  const status = $('export-status');
  const fill = $('export-fill');
  try {
    const result = await exportLoop(viewer, {
      format,
      from,
      to,
      stepsPerSecond: viewer.speed,
      scale: Number($('export-size').value),
      signal: controller.signal,
      onProgress: (progress) => {
        const { fraction, text } = describeProgress(progress, format);
        fill.style.width = `${fraction * 100}%`;
        status.textContent = text;
      },
    });
    downloadUrl = URL.createObjectURL(result.blob);
    const link = $('export-download');
    link.href = downloadUrl;
    link.download = result.filename;
    link.textContent = `Download ${result.filename}`;
    link.hidden = false;
    fill.style.width = '100%';
    status.textContent = `Done in ${(result.ms / 1000).toFixed(1)} s · ${megabytes(result.bytes)} · ${result.width}×${result.height} · ${result.frames} frames`;
  } catch (error) {
    fill.style.width = '0';
    status.textContent = error.name === 'AbortError' ? 'Cancelled.' : `Export failed: ${error.name}: ${error.message}`;
    if (error.name !== 'AbortError') console.error('export failed:', error);
  } finally {
    running = null;
    $('export-start').hidden = false;
    $('export-cancel').hidden = true;
  }
}

/** Open or close the Export panel; wires it up on first use. */
export function toggleExportPanel(viewer) {
  const panel = $('export-panel');
  if (!panelReady) {
    panelReady = true;
    $('export-start').addEventListener('click', () => startExport(viewer));
    $('export-cancel').addEventListener('click', () => running?.abort());
    $('export-close').addEventListener('click', () => {
      panel.hidden = true;
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !panel.hidden) panel.hidden = true;
    });
  }
  panel.hidden = !panel.hidden;
  if (!panel.hidden && viewer.store) fillTimeSelects(viewer);
}
