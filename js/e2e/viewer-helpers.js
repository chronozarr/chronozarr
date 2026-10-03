// Driving the viewer page from a test and reading back what it drew.

import { expect } from './fixtures.js';
import { maskValue } from '../support/synthetic-store.js';
import { BACKGROUND, shadeTexel } from './cpu-render.js';
import { STORES, storedValue } from './stores.js';

/** Open the viewer on a store and wait for its first complete frame; returns the timings `chronozarr.ready` resolves to. */
export async function openViewer(page, servers, storeUrl) {
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(storeUrl)}`);
  await page.waitForFunction(() => window.chronozarr?.ready);
  const timings = await page.evaluate(() => window.chronozarr.ready);
  expect(timings, 'loadStore resolved with its timings (it resolves with nothing when the store failed to open)').toBeTruthy();
  return timings;
}

export const waitForPaintedTime = (page, t) => page.waitForFunction((want) => window.chronozarr.viewer.paintedT === want, t);

/** Level-0 pixels of the 200 x 200 stores that the GPU/CPU comparison reads: the nodata columns, cell edges and corners. */
export const SAMPLE_PIXELS = [
  [2, 2],
  [5, 100],
  [6, 0],
  [7, 100],
  [50, 40],
  [127, 127],
  [128, 128],
  [150, 120],
  [190, 25],
  [199, 199],
  [100, 199],
];

/**
 * Repaint the current frame synchronously and read the canvas back from the GPU. Wrapping Renderer.beginPaint for
 * that one repaint records the exact uniforms the viewer gave the shader (stretch, range, nodata, unit conversion),
 * so the CPU side can reproduce the frame without knowing how the viewer chose them. Returns the frame, the level it
 * was drawn at, the GL error flag, the share of the canvas that is not background, and the RGB bytes under the
 * centre of each requested level-0 pixel.
 */
export async function captureFrame(page, pixels = SAMPLE_PIXELS) {
  return page.evaluate(
    ({ pixels, background }) => {
      const { viewer } = window.chronozarr;
      const { renderer, canvas } = viewer;
      let frame = null;
      const original = renderer.beginPaint;
      renderer.beginPaint = function (f, options) {
        frame = JSON.parse(JSON.stringify(f));
        return original.call(this, f, options);
      };
      let result;
      try {
        result = viewer.renderNow();
      } finally {
        renderer.beginPaint = original;
      }
      const { width, height } = canvas;
      const rgba = renderer.readFrame(width, height);
      const at = (x, y) => {
        const i = ((height - 1 - y) * width + x) * 4;
        return [rgba[i], rgba[i + 1], rgba[i + 2]];
      };
      const isBackground = ([r, g, b]) => Math.abs(r - background[0]) <= 1 && Math.abs(g - background[1]) <= 1 && Math.abs(b - background[2]) <= 1;
      let seen = 0;
      let other = 0;
      for (let y = 0; y < height; y += 4) {
        for (let x = 0; x < width; x += 4) {
          seen++;
          if (!isBackground(at(x, y))) other++;
        }
      }
      const samples = frame
        ? pixels.map(([X, Y]) => {
            const x = Math.floor((X + 0.5 - frame.cx) * frame.scale + frame.width / 2);
            const y = Math.floor((Y + 0.5 - frame.cy) * frame.scale + frame.height / 2);
            return { X, Y, canvas: { x, y }, rgb: x >= 0 && y >= 0 && x < width && y < height ? at(x, y) : null };
          })
        : [];
      return { frame, complete: result.complete, lod: result.lod, t: viewer.t, glError: canvas.getContext('webgl2').getError(), nonBackground: other / seen, samples };
    },
    { pixels, background: BACKGROUND },
  );
}

const TOLERANCE = 2;

/**
 * Compare the GPU pixels of a capture with the CPU render of the values the store holds (the `values` function that
 * built it), and check the frame is complete, drawn at level 0 and free of GL errors. A texel outside the store, or
 * one the store's mask marks invalid, is background.
 */
export function expectFrameMatchesStore(capture, storeName, t) {
  expect(capture.frame, 'the viewer painted a frame').not.toBeNull();
  expect(capture.complete, 'the frame is complete').toBe(true);
  expect(capture.lod, 'the stores are small enough to be drawn at level 0').toBe(0);
  expect(capture.glError, 'gl.getError() after the frame (0 = NO_ERROR)').toBe(0);
  expect(capture.nonBackground, 'share of the canvas that is not background').toBeGreaterThan(0.05);
  const { spec } = STORES[storeName];
  const mismatches = [];
  for (const { X, Y, canvas, rgb } of capture.samples) {
    const inside = X < spec.width && Y < spec.height;
    const stored = Array.from({ length: spec.nBand }, (_, band) => storedValue(storeName, t, band, Y, X));
    const masked = spec.mask && maskValue(t, Y, X, 0) === 0;
    const expected = inside && !masked ? shadeTexel(capture.frame, stored) : [...BACKGROUND];
    if (rgb === null || rgb.some((c, i) => Math.abs(c - expected[i]) > TOLERANCE)) {
      mismatches.push(`pixel (${X}, ${Y}) at canvas (${canvas.x}, ${canvas.y}), t=${t}, stored [${stored}]${masked ? ', masked' : ''}: GPU ${JSON.stringify(rgb)}, CPU ${JSON.stringify(expected.map((c) => Math.round(c)))}`);
    }
  }
  expect(mismatches, `pixels where the GPU frame differs from the CPU render (tolerance ${TOLERANCE})`).toEqual([]);
}

/** Click the centre of a level-0 pixel on the canvas. */
export async function clickStorePixel(page, X, Y) {
  const { x, y } = await page.evaluate(
    ([X, Y]) => {
      const { canvas, camera } = window.chronozarr.viewer;
      const rect = canvas.getBoundingClientRect();
      return {
        x: rect.left + (((X + 0.5 - camera.cx) * camera.scale + canvas.width / 2) * rect.width) / canvas.width,
        y: rect.top + (((Y + 0.5 - camera.cy) * camera.scale + canvas.height / 2) * rect.height) / canvas.height,
      };
    },
    [X, Y],
  );
  await page.mouse.click(x, y);
}

/** The sidebar as { sectionLabel: [[label, value], ...] }. */
export function readSidebar(page) {
  return page.locator('#sidebar-content').evaluate((root) =>
    Object.fromEntries(
      [...root.querySelectorAll('.sidebar-section')].map((section) => [
        section.querySelector('.section-label').textContent.trim(),
        [...section.querySelectorAll('.meta-row, .metric')].map((row) => [
          row.querySelector('.label, .metric-name').textContent.trim(),
          row.querySelector('.value, .metric-value, .water-badge').textContent.trim(),
        ]),
      ]),
    ),
  );
}

/** The product buttons as [name, enabled, active] triples, in button order. */
export function readProductButtons(page) {
  return page.locator('#products button').evaluateAll((buttons) => buttons.map((button) => [button.textContent.trim(), !button.disabled, button.classList.contains('active')]));
}
