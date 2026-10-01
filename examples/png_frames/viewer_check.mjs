// Drive the TileRipper viewer on the store converted from PNG frames with headless Chromium (software WebGL) and
// check what the reader and the GPU show: an 8-bit RGB store with a mask opens, True color is enabled and the
// reflectance products (False color, NDVI, NDWI, Water) are not, masked pixels are drawn as the background colour,
// a click returns the three uint8 values the reader holds, and playback runs.
//
// Run from js/ (Playwright lives in js/node_modules) with the repo served with byte ranges:
//   uv run --with rangehttpserver python -m RangeHTTPServer 8000      # repo root, port 8000
//   cd js && node ../examples/png_frames/viewer_check.mjs
// Screenshots go to data/reports/viewer_ucayali_png_{truecolor,edge}.png.

import path from 'node:path';
import { chromium } from '../../js/node_modules/playwright/index.mjs';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, arg, i, all) => (arg.startsWith('--') ? [...pairs, [arg.slice(2), all[i + 1]]] : pairs), []),
);
const base = args.base ?? 'http://localhost:8000';
const root = path.resolve(import.meta.dirname, '../..');
const reports = path.join(root, 'data/reports');
const storeUrl = `${base}/${args.store ?? 'data/stores/ucayali_santa_maria/png-1'}`;
const viewerUrl = (query) => `${base}/js/tileripper/index.html?store=${encodeURIComponent(storeUrl)}&${query}`;
const BACKGROUND = [9, 12, 18]; // js/e2e/cpu-render.js: round([0.035, 0.047, 0.071] * 255)
const EDGE_GAP = 6; // pixels between a masked pixel and the valid pixel to its right
const MARGIN = 40; // pixels kept between the edge searched for and any cell border

const failures = [];
const check = (ok, label) => {
  console.log(`[${ok ? 'ok' : 'FAIL'}] ${label}`);
  if (!ok) failures.push(label);
};

const browser = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'] });
const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1, serviceWorkers: 'block' });
const page = await context.newPage();
const problems = [];
page.on('console', (m) => m.type() === 'error' && problems.push(`console error: ${m.text()}`));
page.on('pageerror', (e) => problems.push(`page error: ${e.message}`));

async function open(query) {
  await page.goto(viewerUrl(query));
  await page.waitForFunction(() => window.tileripper?.ready, null, { timeout: 120_000 });
  const timings = await page.evaluate(() => window.tileripper.ready);
  if (!timings) throw new Error(`the viewer did not open ${storeUrl}: ${await page.locator('#error-message').innerText()}`);
  return timings;
}

/** Repaint the current frame and wait until it is complete at pyramid level `lod` for timestep t. */
async function settle(t, lod = 0) {
  await page.waitForFunction(
    ([want, wantLod]) => {
      const v = window.tileripper.viewer;
      if (v.paintedT !== want) return false;
      const result = v.renderNow();
      return result.complete && result.lod === wantLod;
    },
    [t, lod],
    { timeout: 60_000 },
  );
}

const canvasPixel = (x, y) =>
  page.evaluate(
    ([x, y]) => {
      const { renderer, canvas } = window.tileripper.viewer;
      window.tileripper.viewer.renderNow();
      const rgba = renderer.readFrame(canvas.width, canvas.height);
      const i = ((canvas.height - 1 - y) * canvas.width + x) * 4;
      return [rgba[i], rgba[i + 1], rgba[i + 2]];
    },
    [x, y],
  );

/** Level-0 pixel (x, y) of timestep t as the reader holds it: its [red, green, blue] uint8 values and mask. */
const readerPixel = (t, x, y) =>
  page.evaluate(
    async ([t, x, y]) => {
      const store = window.tileripper.viewer.store;
      const level = store.levels[0];
      const stride = level.chunkHeight * level.chunkWidth;
      const [row, col] = [Math.floor(y / level.chunkHeight), Math.floor(x / level.chunkWidth)];
      const cell = await store.getCell(0, row, col, t);
      const mask = await store.getMask(0, row, col, t);
      const i = (y - row * level.chunkHeight) * level.chunkWidth + (x - col * level.chunkWidth);
      return { rgb: [cell.data[i], cell.data[stride + i], cell.data[2 * stride + i]], mask: mask[i] };
    },
    [t, x, y],
  );

/** Fraction of masked pixels of every timestep, from the one cell of pyramid level 1. */
const maskedFractions = () =>
  page.evaluate(async () => {
    const store = window.tileripper.viewer.store;
    const fractions = [];
    for (let t = 0; t < store.times.length; t++) {
      const mask = await store.getMask(1, 0, 0, t);
      fractions.push(mask.reduce((zeros, v) => zeros + (v === 0 ? 1 : 0), 0) / mask.length);
    }
    return fractions;
  });

/**
 * A level-0 pixel of timestep t at the centre of a 5 x 5 window that is all masked, with a valid pixel EDGE_GAP pixels
 * to its right in the same row whose red, green and blue are three different values strictly between 0 and 255 (so a
 * click and a drawn colour cannot agree by being clipped), searched in the cells the reader holds from the middle of
 * the grid outwards and at least MARGIN pixels from the border of every cell, so the edge is inside the image.
 */
const findMaskedEdge = (t) =>
  page.evaluate(
    async ([t, gap, margin]) => {
      const store = window.tileripper.viewer.store;
      const level = store.levels[0];
      const cells = [];
      for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) cells.push([row, col]);
      const distance = ([row, col]) => Math.hypot(row - (level.gridRows - 1) / 2, col - (level.gridCols - 1) / 2);
      cells.sort((a, b) => distance(a) - distance(b));
      for (const [row, col] of cells) {
        const mask = await store.getMask(0, row, col, t);
        const cell = await store.getCell(0, row, col, t);
        const stride = level.chunkHeight * level.chunkWidth;
        const extent = store.cellExtent(0, row, col);
        const distinct = (i) => {
          const rgb = [cell.data[i], cell.data[stride + i], cell.data[2 * stride + i]];
          return rgb.every((v) => v > 0 && v < 255) && new Set(rgb).size === 3;
        };
        for (let y = margin; y < Math.min(extent.height, level.chunkHeight) - margin; y += 3) {
          for (let x = margin; x < Math.min(extent.width, level.chunkWidth) - gap - margin; x += 3) {
            let allMasked = true;
            for (let dy = -2; dy <= 2 && allMasked; dy++) for (let dx = -2; dx <= 2; dx++) if (mask[(y + dy) * level.chunkWidth + x + dx] !== 0) allMasked = false;
            if (allMasked && mask[y * level.chunkWidth + x + gap] === 1 && distinct(y * level.chunkWidth + x + gap)) return { x: col * level.chunkWidth + x, y: row * level.chunkHeight + y, transform: level.transform };
          }
        }
      }
      return null;
    },
    [t, EDGE_GAP, MARGIN],
  );

const toProjected = ([a, , c, , e, f], x, y) => `${a * (x + 0.5) + c},${e * (y + 0.5) + f}`;

/** Click the centre of the canvas and read the inspector: sections as { label: { row: value } } (the "Value" section lists the bands). */
async function clickCentre(waitFor) {
  const rect = await page.locator('#gl-canvas').boundingBox();
  await page.mouse.click(rect.x + rect.width / 2, rect.y + rect.height / 2);
  await page.waitForFunction((text) => document.getElementById('sidebar-content').innerText.includes(text), waitFor, { timeout: 30_000 });
  const rows = await page.locator('#sidebar-content .sidebar-section').evaluateAll((sections) =>
    Object.fromEntries(
      sections.map((s) => [s.querySelector('.section-label').textContent.trim(), Object.fromEntries([...s.querySelectorAll('.meta-row')].map((r) => [r.querySelector('.label').textContent.trim(), r.querySelector('.value').textContent.trim()]))]),
    ),
  );
  console.log(`  sidebar: ${JSON.stringify(rows)}`);
  return rows;
}

// 1. Open on a month with a large masked area. The store is uint8 RGB with a mask, and its bands are named by colour.
await open('t=0');
const fractions = await maskedFractions();
const gapT = fractions.reduce((best, f, t) => (Math.abs(f - 0.4) < Math.abs(fractions[best] - 0.4) ? t : best), 0);
const times = await page.evaluate(() => window.tileripper.viewer.store.times);
console.log(`${fractions.length} timesteps; view t=${gapT} (${times[gapT]}), ${(fractions[gapT] * 100).toFixed(1)} % masked at level 1`);
const timings = await open(`t=${gapT}`);
console.log(`opened in ${Math.round(timings.openMs)} ms, first paint ${Math.round(timings.firstPaintMs)} ms`);
const info = await page.evaluate(() => {
  const v = window.tileripper.viewer;
  return { dtype: v.dtype, hasMask: v.store.hasMask, times: v.store.times.length, bands: v.bands.map((b) => [b.name, b.common_name, b.scale, b.offset]) };
});
check(info.dtype === 'uint8' && info.hasMask && info.times === 36, `uint8 store with a mask, ${info.times} timesteps`);
check(JSON.stringify(info.bands) === JSON.stringify([['red', 'red', 1, 0], ['green', 'green', 1, 0], ['blue', 'blue', 1, 0]]), `bands ${JSON.stringify(info.bands)}`);
check(fractions[gapT] > 0.2 && fractions[gapT] < 0.8, `the view month is ${(fractions[gapT] * 100).toFixed(1)} % masked, enough to see the mask`);

// 2. True color is the only colour product: the others need near infrared, which a rendered PNG does not have.
const buttons = await page.locator('#products button').evaluateAll((bs) => bs.map((b) => [b.textContent.trim(), !b.disabled, b.classList.contains('active')]));
check(JSON.stringify(buttons.filter(([, enabled]) => enabled).map(([name]) => name)) === JSON.stringify(['True color', 'Single band']), `enabled products: ${JSON.stringify(buttons.filter(([, e]) => e).map(([n]) => n))}`);
const disabled = buttons.filter(([, enabled]) => !enabled).map(([name]) => name);
check(['False color', 'NDVI', 'NDWI', 'Water'].every((name) => disabled.includes(name)), `disabled products: ${JSON.stringify(disabled)}`);
check(buttons.find(([name]) => name === 'True color')?.[2] === true, 'True color is the active product');
await page.waitForFunction((t) => window.tileripper.viewer.paintedT === t, gapT);
await page.waitForTimeout(1500);
await page.screenshot({ path: path.join(reports, 'viewer_ucayali_png_truecolor.png') });
console.log(`screenshot data/reports/viewer_ucayali_png_truecolor.png (True color, t=${gapT} ${times[gapT]}, ${(fractions[gapT] * 100).toFixed(0)} % masked)`);

// 3. A masked pixel at level 0 is the background colour; the valid pixel EDGE_GAP pixels to its right shows its stored colour.
const edge = await findMaskedEdge(gapT);
check(Boolean(edge), `found a masked pixel with a distinct valid colour ${EDGE_GAP} pixels to its right: ${JSON.stringify(edge)}`);
if (edge) {
  const valid = { x: edge.x + EDGE_GAP, y: edge.y };
  const stored = await readerPixel(gapT, valid.x, valid.y);
  const masked = await readerPixel(gapT, edge.x, edge.y);
  check(masked.mask === 0 && stored.mask === 1, `reader mask: ${masked.mask} at (${edge.x}, ${edge.y}), ${stored.mask} at (${valid.x}, ${valid.y})`);
  await open(`t=${gapT}&z=8&c=${toProjected(edge.transform, edge.x, edge.y)}`);
  await settle(gapT);
  const [cx, cy, scale] = await page.evaluate(() => {
    const { canvas, camera } = window.tileripper.viewer;
    return [Math.floor(canvas.width / 2), Math.floor(canvas.height / 2), camera.scale];
  });
  const drawnMasked = await canvasPixel(cx, cy);
  const drawnValid = await canvasPixel(cx + Math.round(EDGE_GAP * scale), cy);
  const isBackground = (rgb) => rgb.every((c, i) => Math.abs(c - BACKGROUND[i]) <= 1);
  check(isBackground(drawnMasked), `masked pixel (${edge.x}, ${edge.y}) draws ${JSON.stringify(drawnMasked)} (background ${JSON.stringify(BACKGROUND)})`);
  check(drawnValid.every((c, i) => Math.abs(c - stored.rgb[i]) <= 1), `valid pixel (${valid.x}, ${valid.y}) draws ${JSON.stringify(drawnValid)}, the reader holds ${JSON.stringify(stored.rgb)}`);

  // 4. A click on the valid pixel reads the three uint8 values the reader holds, at level 0.
  await open(`t=${gapT}&z=8&c=${toProjected(edge.transform, valid.x, valid.y)}`);
  await settle(gapT);
  const rows = await clickCentre(`${valid.x}, ${valid.y}`);
  const shown = [rows.Value.red, rows.Value.green, rows.Value.blue].map(Number);
  check(rows.Location.Level.startsWith('0'), `the inspector read level ${rows.Location.Level}`);
  check(JSON.stringify(shown) === JSON.stringify(stored.rgb), `click on (${valid.x}, ${valid.y}) reads red, green, blue ${JSON.stringify(shown)}; the reader holds ${JSON.stringify(stored.rgb)}`);
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(reports, 'viewer_ucayali_png_edge.png') });
  console.log('screenshot data/reports/viewer_ucayali_png_edge.png (masked edge at 8x, inspector open on the first valid pixel)');
}

// 5. Playback runs: it buffers, steps through timesteps and stops again.
await open('t=0');
await page.waitForFunction(() => window.tileripper.viewer.paintedT === 0);
await page.keyboard.press('Space');
await page.waitForFunction(() => window.tileripper.viewer.playback.stats.steps >= 6, null, { timeout: 90_000 });
const playing = await page.evaluate(() => ({ t: window.tileripper.viewer.t, playing: window.tileripper.viewer.playback.playing, steps: window.tileripper.viewer.playback.stats.steps }));
await page.keyboard.press('Space');
await page.waitForFunction(() => !window.tileripper.viewer.playback.playing, null, { timeout: 10_000 });
check(playing.playing && playing.steps >= 6 && playing.t > 0, `playback advanced to t=${playing.t} after ${playing.steps} steps, then stopped`);

check(problems.length === 0, `no console errors or page errors${problems.length ? `: ${problems.join(' | ')}` : ''}`);
await browser.close();
if (failures.length) {
  console.error(`${failures.length} check(s) failed`);
  process.exit(1);
}
console.log('all checks passed');
