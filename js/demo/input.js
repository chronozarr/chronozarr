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

/** Pan with one pointer and zoom about the midpoint of two captured pointers. */
function bindMapInput(viewer, { inspect, cameraChanged }) {
  const canvas = viewer.canvas;
  const points = new Map();
  let moved = 0;
  let pinched = false;
  const moveCamera = (from, to, factor = 1) => {
    const rect = canvas.getBoundingClientRect();
    const offset = ({ x, y }) => ({
      x: ((x - rect.left) / rect.width - 0.5) * canvas.width,
      y: ((y - rect.top) / rect.height - 0.5) * canvas.height,
    });
    const before = offset(from);
    const after = offset(to);
    const { cx, cy, scale } = viewer.camera;
    const next = clamp(scale * factor, viewer.fitScale * 0.5, MAX_SCALE);
    viewer.camera = { cx: cx + before.x / scale - after.x / next, cy: cy + before.y / scale - after.y / next, scale: next };
    cameraChanged();
  };
  const midpoint = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
  const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
  const position = (e) => ({ x: e.clientX, y: e.clientY });
  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 || points.size >= 2) return;
    // No text selection or native drag from a press on the map.
    e.preventDefault();
    if (!points.size) {
      moved = 0;
      pinched = false;
    } else {
      pinched = true;
    }
    points.set(e.pointerId, position(e));
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', (e) => {
    const before = points.get(e.pointerId);
    if (!before || !viewer.store) return;
    const after = position(e);
    points.set(e.pointerId, after);
    if (points.size === 2) {
      const other = [...points.values()].find((p) => p !== after);
      const previousDistance = distance(before, other);
      const nextDistance = distance(after, other);
      const factor = previousDistance > 0 && nextDistance > 0 ? nextDistance / previousDistance : 1;
      moveCamera(midpoint(before, other), midpoint(after, other), factor);
    } else {
      moved += Math.abs(after.x - before.x) + Math.abs(after.y - before.y);
      moveCamera(before, after);
    }
  });
  canvas.addEventListener('pointerup', (e) => {
    if (points.delete(e.pointerId) && !pinched && moved < CLICK_SLOP_PX) inspect(e.clientX, e.clientY);
  });
  canvas.addEventListener('lostpointercapture', (e) => points.delete(e.pointerId));
  canvas.addEventListener('wheel', (e) => {
    if (!viewer.store) return;
    e.preventDefault();
    const point = position(e);
    moveCamera(point, point, Math.exp(-e.deltaY * 0.0015));
  }, { passive: false });
  canvas.addEventListener('dblclick', () => viewer.store && viewer.fit());
  canvas.addEventListener('keydown', (e) => {
    if (!viewer.store || !viewer.inspectorUi || e.metaKey || e.ctrlKey || e.altKey) return;
    const rect = canvas.getBoundingClientRect();
    const center = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    if (e.key === 'Enter') inspect(center.x, center.y);
    else if (e.key === '+' || e.key === '=') moveCamera(center, center, 1.5);
    else if (e.key === '-') moveCamera(center, center, 1 / 1.5);
    else if (e.key === '0') viewer.fit();
    else if (e.key === 'ArrowLeft') moveCamera(center, { ...center, x: center.x + 32 });
    else if (e.key === 'ArrowRight') moveCamera(center, { ...center, x: center.x - 32 });
    else if (e.key === 'ArrowUp') moveCamera(center, { ...center, y: center.y + 32 });
    else if (e.key === 'ArrowDown') moveCamera(center, { ...center, y: center.y - 32 });
    else return;
    e.preventDefault();
    e.stopPropagation();
  });
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
  $('colormap-select').addEventListener('change', (e) => viewer.setColormap(Number(e.target.value)));
  $('product-select').addEventListener('change', (e) => viewer.setProduct(Number(e.target.value)));
  $('inspector-close').addEventListener('click', () => viewer.closeInspector());
  const applyStretch = () => viewer.setStretch(Number($('stretch-min').value), Number($('stretch-max').value));
  $('stretch-min').addEventListener('change', applyStretch);
  $('stretch-max').addEventListener('change', applyStretch);
  $('auto-exposure').addEventListener('click', () => viewer.setAutoExposure(!viewer.autoExposure));
  $('stretch-auto').addEventListener('click', () => viewer.autoStretch());
  $('gap-toggle').addEventListener('click', () => viewer.toggleGaps());
}

/**
 * A timeline gesture owns its scrub state until its pointer is released or cancelled. The pointer that last pressed the
 * track is the scrub's: the moves, release and cancel of any other pointer (a second finger panning the map while the
 * first drags the timeline) are not, wherever that pointer is.
 */
export function bindTimelineInput(viewer) {
  const track = $('timeline-track');
  let scrubPointer = null;
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
    scrubPointer = e.pointerId;
    viewer.goToTime(timeFromEvent(e));
  });
  const endScrub = (e) => {
    if (e.pointerId !== scrubPointer) return;
    scrubPointer = null;
    viewer.endScrub();
  };
  window.addEventListener('pointermove', (e) => {
    if (e.pointerId === scrubPointer) viewer.goToTime(timeFromEvent(e));
  });
  window.addEventListener('pointerup', endScrub);
  window.addEventListener('pointercancel', endScrub);
}

/** Keyboard shortcuts respect form focus: a focused select, field or button keeps its own Space (a button clicks on it). */
function bindKeyboardInput(viewer) {
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('dialog[open]')) return;
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
