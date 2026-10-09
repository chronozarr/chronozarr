// Pointer, timeline, keyboard and control bindings for the browser viewer.
import { bindSpeedText, bindTimelineSlider } from './a11y.js';
import { SPEEDS } from './playback.js';

const $ = (id) => document.getElementById(id);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
export const MAX_SCALE = 16;
const CLICK_SLOP_PX = 4;

/** Bind UI input; the viewer owns rendering, inspection and asynchronous data work. */
export function bindViewerInput(viewer, callbacks) {
  bindMapInput(viewer, callbacks);
  bindControls(viewer);
  bindTimelineInput(viewer);
  bindTimelineSlider(viewer);
  bindSpeedText();
  bindKeyboardInput(viewer);
}

/** Keep drag state local to the map and preserve cursor-centered zoom. */
function bindMapInput(viewer, { inspect, cameraChanged }) {
  const canvas = viewer.canvas;
  let drag = null;
  canvas.addEventListener('pointerdown', (e) => {
    // No text selection or native drag from a press on the map.
    e.preventDefault();
    drag = { x: e.clientX, y: e.clientY, moved: 0 };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag || !viewer.store) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    drag.moved += Math.abs(dx) + Math.abs(dy);
    drag.x = e.clientX;
    drag.y = e.clientY;
    const scaleToCanvas = canvas.width / canvas.getBoundingClientRect().width;
    viewer.camera.cx -= (dx * scaleToCanvas) / viewer.camera.scale;
    viewer.camera.cy -= (dy * scaleToCanvas) / viewer.camera.scale;
    cameraChanged();
  });
  canvas.addEventListener('pointerup', (e) => {
    const wasClick = drag && drag.moved < CLICK_SLOP_PX;
    drag = null;
    if (wasClick) inspect(e.clientX, e.clientY);
  });
  canvas.addEventListener('pointercancel', () => {
    drag = null;
  });
  canvas.addEventListener('wheel', (e) => {
    if (!viewer.store) return;
    e.preventDefault();
    const rect = canvas.getBoundingClientRect();
    const px = ((e.clientX - rect.left) * canvas.width) / rect.width;
    const py = ((e.clientY - rect.top) * canvas.height) / rect.height;
    const { cx, cy, scale } = viewer.camera;
    const next = clamp(scale * Math.exp(-e.deltaY * 0.0015), viewer.fitScale * 0.5, MAX_SCALE);
    const worldX = cx + (px - canvas.width / 2) / scale;
    const worldY = cy + (py - canvas.height / 2) / scale;
    viewer.camera = { cx: worldX - (px - canvas.width / 2) / next, cy: worldY - (py - canvas.height / 2) / next, scale: next };
    cameraChanged();
  }, { passive: false });
  canvas.addEventListener('dblclick', () => viewer.store && viewer.fit());
}

/** Bind product, playback, inspector and display controls to the viewer API. */
function bindControls(viewer) {
  $('play-btn').addEventListener('click', () => viewer.togglePlay());
  $('speed').min = '0';
  $('speed').max = String(SPEEDS.length - 1);
  $('speed').addEventListener('input', (e) => viewer.setSpeed(SPEEDS[Number(e.target.value)]));
  $('prev-btn').addEventListener('click', () => viewer.goToTime(viewer.t - 1));
  $('next-btn').addEventListener('click', () => viewer.goToTime(viewer.t + 1));
  $('band-select').addEventListener('change', (e) => viewer.setBandChoice(Number(e.target.value)));
  $('product-select').addEventListener('change', (e) => viewer.setProduct(Number(e.target.value)));
  $('inspector-close').addEventListener('click', () => viewer.closeInspector());
  const applyStretch = () => viewer.setStretch(Number($('stretch-min').value), Number($('stretch-max').value));
  $('stretch-min').addEventListener('change', applyStretch);
  $('stretch-max').addEventListener('change', applyStretch);
  $('stretch-auto').addEventListener('click', () => viewer.autoStretch());
  $('gap-toggle').addEventListener('click', () => viewer.toggleGaps());
}

/** A timeline gesture owns its scrub state until the pointer is released or cancelled. */
function bindTimelineInput(viewer) {
  const track = $('timeline-track');
  let scrubbing = false;
  const timeFromEvent = (e) => {
    const rect = track.getBoundingClientRect();
    const pad = 8;
    const frac = clamp((e.clientX - rect.left - pad) / (rect.width - pad * 2), 0, 1);
    return Math.round(frac * (viewer.store.times.length - 1));
  };
  track.addEventListener('pointerdown', (e) => {
    // Without these a press on the track starts a text selection that the pointer, drifting off the control, extends over the page.
    e.preventDefault();
    if (!viewer.store) return;
    track.setPointerCapture(e.pointerId);
    scrubbing = true;
    viewer.goToTime(timeFromEvent(e));
  });
  const endScrub = () => {
    if (scrubbing) viewer.endScrub();
    scrubbing = false;
  };
  window.addEventListener('pointermove', (e) => scrubbing && viewer.goToTime(timeFromEvent(e)));
  window.addEventListener('pointerup', endScrub);
  window.addEventListener('pointercancel', endScrub);
}

/** Keyboard shortcuts respect form focus: a focused select, field or button keeps its own Space (a button clicks on it). */
function bindKeyboardInput(viewer) {
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && viewer.inspectorOpen) {
      viewer.closeInspector();
      return;
    }
    if (!viewer.store) return;
    const typing = e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT';
    if (e.key === ' ') {
      if (e.target.tagName === 'SELECT' || e.target.tagName === 'BUTTON' || (typing && e.target.type !== 'range')) return;
      e.preventDefault();
      if (!e.repeat) viewer.togglePlay();
      return;
    }
    if (typing) return;
    dispatchNavigationShortcut(viewer, e);
  });
}

/** Dispatch navigation and product shortcuts after focus and playback policy are handled. */
function dispatchNavigationShortcut(viewer, e) {
  if (e.key === 'ArrowLeft') {
    e.preventDefault();
    viewer.goToTime(viewer.t - 1);
  } else if (e.key === 'ArrowRight') {
    e.preventDefault();
    viewer.goToTime(viewer.t + 1);
  } else if (/^[1-9]$/.test(e.key)) {
    viewer.setProduct(Number(e.key) - 1);
  } else if ((e.key === 'd' || e.key === 'D') && !e.metaKey && !e.ctrlKey && !e.altKey) {
    viewer.togglePerfOverlay();
  }
}
