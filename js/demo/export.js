// Export the current view's loop as an MP4 or WebM video (canvas.captureStream + MediaRecorder) or a GIF (vendored
// gifenc). Frames are drawn offscreen at the canvas size by the viewer's export session, so the file matches
// what is on screen; a timestep whose data is not in memory yet is waited for, like playback holds. MP4 (H.264) is
// what slides, chat apps and phones play; Chrome and Safari record it, Firefox records only WebM.

import { Machine } from '../chronozarr/machine.js';
import { redactText } from '../chronozarr/redact.js';
import { applyPalette, GIFEncoder, quantize } from '../vendor/gifenc.esm.js';
import { Playback } from './playback.js';

const PALETTE_SAMPLES = 6;
const PALETTE_PIXEL_STRIDE = 4;
const PRELOAD_BATCH = 4;
const LOAD_ATTEMPTS = 3;
/** MediaRecorder types per video format, in the order they are tried. A bare video/mp4 lets the browser pick H.264. */
const VIDEO_TYPES = {
  mp4: ['video/mp4;codecs=avc1', 'video/mp4'],
  webm: ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'],
};
export const EXPORT_FORMATS = ['mp4', 'webm', 'gif'];

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

/** The first MediaRecorder type of a video format ('mp4' or 'webm') that the browser records, or null. */
export function pickVideoMimeType(format, isTypeSupported) {
  return VIDEO_TYPES[format].find((type) => isTypeSupported(type)) ?? null;
}

/** Whether this browser can write `format`: GIF always, a video format when MediaRecorder records one of its types. */
export function canExport(format, recorder = globalThis.MediaRecorder) {
  if (format === 'gif') return true;
  return typeof recorder?.isTypeSupported === 'function' && pickVideoMimeType(format, (type) => recorder.isTypeSupported(type)) !== null;
}

export function videoBitrate(width, height, stepsPerSecond) {
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
  return `chronozarr-${slug(storeName)}-${slug(labels(from))}-to-${slug(labels(to))}.${format}`;
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

async function recordVideo(session, { format, from, to, stepsPerSecond, onProgress, signal }) {
  const mimeType = typeof MediaRecorder === 'undefined' ? null : pickVideoMimeType(format, (type) => MediaRecorder.isTypeSupported(type));
  if (!mimeType) throw new Error(`This browser cannot record ${format.toUpperCase()}: MediaRecorder with a ${format.toUpperCase()} codec is not available. Choose another format.`);
  const total = to - from + 1;
  const stream = session.canvas.captureStream(0);
  const [track] = stream.getVideoTracks();
  const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: videoBitrate(session.canvas.width, session.canvas.height, stepsPerSecond) });
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
          wrap: false,
          // The loop may exceed the cache: keep only the next frame ready, not two seconds of frames.
          startSeconds: 1 / stepsPerSecond,
          lowSeconds: 1 / stepsPerSecond,
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
          prepare: (indices) =>
            Promise.all(indices.map((i) => ensureLoaded(session, from + i, signal))).catch((error) => {
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
  return new Blob(chunks, { type: recorder.mimeType || mimeType });
}

/**
 * Render timesteps from..to of the current view at `stepsPerSecond` and encode them.
 * @param {object} viewer  the Viewer (pauses it, then uses viewer.createExportSession)
 * @param {{format: 'mp4'|'webm'|'gif', from: number, to: number, stepsPerSecond: number, scale?: number,
 *   onProgress?: (p: {stage: 'loading'|'recording'|'encoding', done: number, total: number}) => void, signal?: AbortSignal}} options
 * @returns {Promise<{blob: Blob, filename: string, frames: number, width: number, height: number, ms: number, bytes: number}>}
 */
export async function exportLoop(viewer, { format, from, to, stepsPerSecond, scale = 1, onProgress = () => {}, signal }) {
  if (!EXPORT_FORMATS.includes(format)) throw new RangeError(`unknown export format "${format}"; formats: ${EXPORT_FORMATS.join(', ')}`);
  if (!(from >= 0 && to >= from && to < viewer.store.times.length)) throw new RangeError(`export range ${from}..${to} is outside 0..${viewer.store.times.length - 1}`);
  const storeName = new URL(viewer.store.url, location.href).pathname.replace(/\/$/, '').split('/').slice(-2).join(' ');
  const filename = exportFilename(storeName, format, from, to, (t) => viewer.timeLabel(t));
  viewer.pause();
  const started = performance.now();
  const session = viewer.createExportSession({ scale, timesteps: to - from + 1 });
  try {
    await preload(session, from, to, onProgress, signal);
    const run = format === 'gif' ? encodeGif : recordVideo;
    const blob = await run(session, { format, from, to, stepsPerSecond, onProgress, signal });
    return {
      blob,
      filename,
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

/**
 * What the panel is doing. idle: nothing was exported yet. running: an export is under way. done: the last export
 * finished and can be downloaded. cancelled: the last export was cancelled. failed: the last export failed.
 */
const EXPORT_MOVES = {
  idle: { start: 'running' },
  running: { finish: 'done', cancel: 'cancelled', fail: 'failed' },
  done: { start: 'running' },
  cancelled: { start: 'running' },
  failed: { start: 'running' },
};

const $ = (id) => document.getElementById(id);
const exportState = new Machine('export panel', EXPORT_MOVES, 'idle');
/**
 * What the current state carries: running {stage, done, total, format} with stage 'preparing' until the first
 * progress report; done {text, href, filename}; failed {message}.
 */
let exportRecord = {};
let panelReady = false;
let running = null;
let listedStore = null;

const megabytes = (bytes) => `${(bytes / 1048576).toFixed(1)} MB`;

function describeProgress({ stage, done, total, format }) {
  const what = stage === 'loading' ? 'Loading' : stage === 'recording' ? 'Recording' : `Encoding ${format.toUpperCase()}`;
  return { fraction: done / total, text: `${what}: ${done} of ${total}` };
}

/** What the panel shows in `state` with its `record`: which of Start and Cancel are hidden, the download link, the status line and the bar. */
export function exportPanelView(state, record) {
  const view = { startHidden: false, cancelHidden: true, download: null, status: '', fraction: 0 };
  if (state === 'running') {
    view.startHidden = true;
    view.cancelHidden = false;
    if (record.stage === 'preparing') {
      view.status = 'Preparing…';
    } else {
      const { fraction, text } = describeProgress(record);
      view.status = text;
      view.fraction = fraction;
    }
  } else if (state === 'done') {
    view.download = { href: record.href, filename: record.filename, text: `Download ${record.filename}` };
    view.status = record.text;
    view.fraction = 1;
  } else if (state === 'cancelled') {
    view.status = 'Cancelled.';
  } else if (state === 'failed') {
    view.status = `Export failed: ${record.message}`;
  }
  return view;
}

function drawPanel() {
  const view = exportPanelView(exportState.state, exportRecord);
  $('export-start').hidden = view.startHidden;
  $('export-cancel').hidden = view.cancelHidden;
  const link = $('export-download');
  link.hidden = view.download === null;
  if (view.download) {
    link.href = view.download.href;
    link.download = view.download.filename;
    link.textContent = view.download.text;
  }
  $('export-status').textContent = view.status;
  $('export-fill').style.width = `${view.fraction * 100}%`;
}

/** Take the move `event`, release what the state being left holds, keep `record` for the new state and draw it. */
function moveTo(event, record) {
  if (exportState.is('done')) URL.revokeObjectURL(exportRecord.href);
  exportState.send(event);
  exportRecord = record;
  drawPanel();
}

/** Disable the formats this browser cannot write; a disabled choice falls to the first one that works (GIF always does). */
function fillFormats() {
  const select = $('export-format');
  for (const option of select.options) {
    option.disabled = !canExport(option.value);
    option.title = option.disabled ? 'This browser cannot record this format' : '';
  }
  if (select.selectedOptions[0]?.disabled) select.value = [...select.options].find((option) => !option.disabled).value;
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
  if (!exportState.can('start')) return;
  const format = $('export-format').value;
  const from = Number($('export-from').value);
  const to = Number($('export-to').value);
  const controller = new AbortController();
  running = controller;
  moveTo('start', { stage: 'preparing', done: 0, total: 0, format });
  try {
    const result = await exportLoop(viewer, {
      format,
      from,
      to,
      stepsPerSecond: viewer.speed,
      scale: Number($('export-size').value),
      signal: controller.signal,
      onProgress: ({ stage, done, total }) => {
        exportRecord = { ...exportRecord, stage, done, total };
        drawPanel();
      },
    });
    moveTo('finish', {
      href: URL.createObjectURL(result.blob),
      filename: result.filename,
      text: `Done in ${(result.ms / 1000).toFixed(1)} s · ${megabytes(result.bytes)} · ${result.width}×${result.height} · ${result.frames} frames`,
    });
  } catch (error) {
    if (error.name === 'AbortError') {
      moveTo('cancel', {});
    } else {
      moveTo('fail', { message: redactText(error) });
      console.error('export failed:', redactText(error));
    }
  } finally {
    running = null;
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
    drawPanel();
  }
  panel.hidden = !panel.hidden;
  if (panel.hidden) return;
  fillFormats();
  if (viewer.store) fillTimeSelects(viewer);
}
