// Drive the TileRipper viewer on a water stack with headless Chromium (software WebGL) and check what the
// reader and the GPU show: the store opens, the single-band product offers ndwi and water, a masked pixel is
// drawn as background, a click returns scaled physical values ("water 1 fraction" on a river pixel and "0 fraction"
// on land at level 0, a fraction between 0 and 1 on a coarser level), and the chart of a river pixel is the water series.
//
// --month YYYY-MM picks the month of the river view (default: the wettest one with 90 % valid pixels).
// Run from js/ (Playwright lives in js/node_modules) with the repo served with byte ranges:
//   uv run --with rangehttpserver python -m RangeHTTPServer 8000      # repo root, port 8000
//   cd js && node ../examples/water_masks/viewer_check.mjs --aoi ucayali_santa_maria --month 2019-03
// Screenshots go to data/reports/viewer_<aoi>_{ndwi,water_chart,water_coarse}.png.

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { chromium } from '../../js/node_modules/playwright/index.mjs';

const args = Object.fromEntries(
  process.argv.slice(2).reduce((pairs, arg, i, all) => (arg.startsWith('--') ? [...pairs, [arg.slice(2), all[i + 1]]] : pairs), []),
);
const aoi = args.aoi ?? 'ucayali_santa_maria';
const base = args.base ?? 'http://localhost:8000';
const root = path.resolve(import.meta.dirname, '../..');
const reports = path.join(root, 'data/reports');
const storeUrl = `${base}/data/stores/${aoi}/water-1`;
const viewerUrl = (query) => `${base}/js/tileripper/index.html?store=${encodeURIComponent(storeUrl)}&${query}`;
const BACKGROUND = [9, 12, 18]; // js/e2e/cpu-render.js: round([0.035, 0.047, 0.071] * 255)

const failures = [];
const check = (ok, label) => {
  console.log(`[${ok ? 'ok' : 'FAIL'}] ${label}`);
  if (!ok) failures.push(label);
};

// The months of the store are the written rows of the build's CSV, in order.
const [header, ...lines] = readFileSync(path.join(root, `data/stores/${aoi}/water-1.months.csv`), 'utf8').trim().split(/\r?\n/);
const columns = header.split(',');
const months = lines.map((line) => Object.fromEntries(line.split(',').map((value, i) => [columns[i], value]))).filter((row) => row.status === 'written');
const indexOf = (row) => months.indexOf(row);
const wettest = months.filter((m) => Number(m.valid_frac) >= 0.9).reduce((best, m) => (Number(m.water_frac) > Number(best.water_frac) ? m : best));
const riverMonth = args.month ? months.find((m) => m.month === args.month) : wettest;
if (!riverMonth) throw new Error(`--month ${args.month} is not a written month of ${aoi}`);
const gapMonth = months.reduce((best, m) => (Math.abs(Number(m.valid_frac) - 0.6) < Math.abs(Number(best.valid_frac) - 0.6) ? m : best));
console.log(`${months.length} months; river view ${riverMonth.month} (t=${indexOf(riverMonth)}), gap view ${gapMonth.month} (t=${indexOf(gapMonth)})`);

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

/**
 * A level-0 pixel of timestep t, found in the cells the reader holds: kind "river" is the centre of a 21 x 21 window
 * that is all water (stored 10000) and valid; kind "land" the centre of a 5 x 5 window that is all land (stored 0)
 * and valid; kind "edge" the centre of a 5 x 5 window that is all masked with a valid pixel EDGE_GAP pixels to its
 * right in the same row.
 */
const discover = (t, kind, edgeGap) =>
  page.evaluate(
    async ([t, kind, edgeGap]) => {
      const store = window.tileripper.viewer.store;
      const level = store.levels[0];
      const stride = level.chunkHeight * level.chunkWidth;
      const windowIs = (half, test, x, y) => {
        for (let dy = -half; dy <= half; dy += 2) for (let dx = -half; dx <= half; dx += 2) if (!test((y + dy) * level.chunkWidth + (x + dx))) return false;
        return true;
      };
      // Cells from the middle of the grid outwards, so the pixel found is away from the edge of the store.
      const cells = [];
      for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) cells.push([row, col]);
      const distance = ([row, col]) => Math.hypot(row - (level.gridRows - 1) / 2, col - (level.gridCols - 1) / 2);
      cells.sort((a, b) => distance(a) - distance(b));
      {
        for (const [row, col] of cells) {
          const cell = await store.getCell(0, row, col, t);
          const mask = await store.getMask(0, row, col, t);
          const water = cell.data.subarray(stride, 2 * stride);
          const extent = store.cellExtent(0, row, col);
          const half = kind === 'river' ? 10 : 2;
          for (let y = half + 1; y < Math.min(extent.height, level.chunkHeight) - half - 1; y += 3) {
            for (let x = half + 1; x < Math.min(extent.width, level.chunkWidth) - half - edgeGap - 1; x += 3) {
              const hit =
                kind === 'river'
                  ? windowIs(half, (i) => water[i] === 10_000 && mask[i] === 1, x, y)
                  : kind === 'land'
                    ? windowIs(half, (i) => water[i] === 0 && mask[i] === 1, x, y)
                    : windowIs(half, (i) => mask[i] === 0, x, y) && mask[y * level.chunkWidth + x + edgeGap] === 1;
              if (hit) return { x: col * level.chunkWidth + x, y: row * level.chunkHeight + y, transform: level.transform };
            }
          }
        }
      }
      return null;
    },
    [t, kind, edgeGap],
  );

/**
 * A pixel of pyramid level `lod` at timestep t whose water value is a fraction strictly between 0.25 and 0.75 (stored
 * 2500 to 7500) and valid, searched from the middle of the level's cell grid and of each cell outwards; its stored
 * value is returned.
 */
const discoverFraction = (t, lod) =>
  page.evaluate(
    async ([t, lod]) => {
      const store = window.tileripper.viewer.store;
      const level = store.levels[lod];
      const stride = level.chunkHeight * level.chunkWidth;
      const cells = [];
      for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) cells.push([row, col]);
      const distance = ([row, col]) => Math.hypot(row - (level.gridRows - 1) / 2, col - (level.gridCols - 1) / 2);
      cells.sort((a, b) => distance(a) - distance(b));
      for (const [row, col] of cells) {
        const cell = await store.getCell(lod, row, col, t);
        const mask = await store.getMask(lod, row, col, t);
        const water = cell.data.subarray(stride, 2 * stride);
        const extent = store.cellExtent(lod, row, col);
        const fromMiddle = (size) => Array.from({ length: Math.max(0, size - 4) }, (_, k) => k + 2).sort((a, b) => Math.abs(a - size / 2) - Math.abs(b - size / 2));
        for (const y of fromMiddle(Math.min(extent.height, level.chunkHeight))) {
          for (const x of fromMiddle(Math.min(extent.width, level.chunkWidth))) {
            const i = y * level.chunkWidth + x;
            if (mask[i] === 1 && water[i] >= 2500 && water[i] <= 7500) return { x: col * level.chunkWidth + x, y: row * level.chunkHeight + y, stored: water[i], lod, transform: store.levels[0].transform };
          }
        }
      }
      return null;
    },
    [t, lod],
  );

const toProjected = ([a, , c, , e, f], x, y) => `${a * (x + 0.5) + c},${e * (y + 0.5) + f}`;

/** Click the centre of the canvas and read the inspector: sections as { label: { row: value } }. */
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

/** The chart on screen, once every timestep is in: its label, legend and the values of the charted months. */
async function readChart() {
  await page.waitForFunction((n) => document.getElementById('chart-status')?.innerText.includes(`${n} of ${n} timesteps`), months.length, { timeout: 90_000 });
  const chart = await page.evaluate(() => {
    const svg = document.querySelector('#chart svg');
    const d = svg.querySelector('path[stroke]').getAttribute('d');
    const ticks = [...svg.querySelectorAll('text.chart-axis')].map((t) => t.textContent).filter((t) => /^-?[\d.]+$/.test(t)).map(Number);
    const ys = [...d.matchAll(/[ML]([\d.]+),([\d.]+)/g)].map((m) => Number(m[2]));
    const gridYs = [...svg.querySelectorAll('line.chart-grid')].map((l) => Number(l.getAttribute('y1')));
    return { label: svg.getAttribute('aria-label'), legend: document.getElementById('chart-legend').innerText.replace(/\s+/g, ' '), ticks, ys, gridYs, status: document.getElementById('chart-status').innerText };
  });
  const [top, , bottom] = chart.gridYs;
  const [vTop, , vBottom] = chart.ticks;
  const values = chart.ys.map((y) => vBottom + ((bottom - y) / (bottom - top)) * (vTop - vBottom));
  console.log(`  chart: ${chart.label}; ${chart.legend}; axis ${chart.ticks}; ${chart.status}`);
  return { ...chart, values };
}

// 1. Open, fit view, gap month: the single-band product offers ndwi and water; masked pixels are background.
const timings = await open(`t=${indexOf(gapMonth)}`);
console.log(`opened in ${Math.round(timings.openMs)} ms, first paint ${Math.round(timings.firstPaintMs)} ms`);
const info = await page.evaluate(() => {
  const v = window.tileripper.viewer;
  return { dtype: v.dtype, hasMask: v.store.hasMask, bands: v.bands.map((b) => b.name ?? b), times: v.store.times.length, units: v.bands.map((b) => b.units) };
});
check(info.dtype === 'int16' && info.hasMask && info.times === months.length, `int16 store with a mask, ${info.times} timesteps (CSV: ${months.length})`);
const productButtons = await page.locator('#products button').evaluateAll((bs) => bs.map((b) => [b.textContent.trim(), !b.disabled, b.classList.contains('active')]));
check(JSON.stringify(productButtons.filter(([, enabled]) => enabled)) === JSON.stringify([['Single band', true, true]]), `enabled products: ${JSON.stringify(productButtons.filter(([, e]) => e).map(([n]) => n))}`);
const bandOptions = await page.locator('#band-select option').allInnerTexts();
check(JSON.stringify(bandOptions) === JSON.stringify(['ndwi', 'water']), `band selector offers ${JSON.stringify(bandOptions)}`);
await page.waitForFunction((t) => window.tileripper.viewer.paintedT === t, indexOf(gapMonth));
await page.waitForTimeout(1500);
await page.screenshot({ path: path.join(reports, `viewer_${aoi}_ndwi.png`) });
console.log(`screenshot data/reports/viewer_${aoi}_ndwi.png (single band ndwi, ${gapMonth.month}, ${Math.round((1 - Number(gapMonth.valid_frac)) * 100)} % masked)`);

// 2. A masked pixel at level 0 is the background colour; a valid pixel EDGE_GAP pixels to its right is not.
const EDGE_GAP = 6;
const gapT = indexOf(gapMonth);
const edge = await discover(gapT, 'edge', EDGE_GAP);
check(Boolean(edge), `found a masked pixel with valid data ${EDGE_GAP} pixels to its right: ${JSON.stringify(edge)} in ${gapMonth.month}`);
if (edge) {
  await open(`t=${gapT}&b=ndwi&z=8&c=${toProjected(edge.transform, edge.x, edge.y)}`);
  await settle(gapT);
  const [cx, cy, scale] = await page.evaluate(() => {
    const { canvas, camera } = window.tileripper.viewer;
    return [Math.floor(canvas.width / 2), Math.floor(canvas.height / 2), camera.scale];
  });
  const masked = await canvasPixel(cx, cy);
  const valid = await canvasPixel(cx + Math.round(EDGE_GAP * scale), cy);
  const isBackground = (rgb) => rgb.every((c, i) => Math.abs(c - BACKGROUND[i]) <= 1);
  check(isBackground(masked), `masked pixel (${edge.x}, ${edge.y}) draws ${JSON.stringify(masked)} (background ${JSON.stringify(BACKGROUND)})`);
  check(!isBackground(valid), `valid pixel (${edge.x + EDGE_GAP}, ${edge.y}) draws ${JSON.stringify(valid)}`);
}

// 3. River pixel at level 0: a click returns scaled values ("water 1 fraction"); the chart is the water series.
const riverT = indexOf(riverMonth);
await open(`t=${riverT}`);
await page.waitForFunction((t) => window.tileripper.viewer.paintedT === t, riverT);
const river = await discover(riverT, 'river', 0);
check(Boolean(river), `found a river window at pixel ${JSON.stringify(river)} in ${riverMonth.month}`);
if (river) {
  await open(`t=${riverT}&b=ndwi&z=3&c=${toProjected(river.transform, river.x, river.y)}`);
  await settle(riverT);
  const rows = await clickCentre(`${river.x}, ${river.y}`);
  const { Value: value, 'Stored value': stored } = rows;
  const physicalNdwi = parseFloat(value.ndwi);
  check(rows.Location.Level.startsWith('0'), `the inspector read level ${rows.Location.Level}`);
  check(Math.abs(physicalNdwi) <= 1 && Math.abs(Number(stored.ndwi)) <= 10_000, `click on (${river.x}, ${river.y}): ndwi ${value.ndwi} (stored ${stored.ndwi}), water ${value.water} (stored ${stored.water})`);
  check(Math.abs(Number(stored.ndwi) * 1e-4 - physicalNdwi) < 6e-4, `physical = stored x 1e-4 (${Number(stored.ndwi) * 1e-4} vs ${physicalNdwi})`);
  check(value.water === '1 fraction' && stored.water === '10000', `the river pixel reads "water ${value.water}", stored ${stored.water}`);

  // The chart follows the band on screen: switch to water, click again, wait for every timestep.
  await page.locator('#band-select').selectOption({ label: 'water' });
  await page.waitForFunction(() => new URLSearchParams(location.search).get('b') === 'water');
  await settle(riverT);
  const rect = await page.locator('#gl-canvas').boundingBox();
  await page.mouse.click(rect.x + rect.width / 2, rect.y + rect.height / 2);
  const chart = await readChart();
  const ones = chart.values.filter((v) => v > 0.99).length;
  check(chart.label.startsWith('water (fraction)'), `chart is the water series (${chart.label})`);
  check(chart.values.length >= 20 && chart.values.every((v) => v < 0.01 || v > 0.99) && ones / chart.values.length > 0.5, `${chart.values.length} charted months, ${ones} at water = 1 (pixel ${river.x}, ${river.y})`);
  await page.screenshot({ path: path.join(reports, `viewer_${aoi}_water_chart.png`) });
  console.log(`screenshot data/reports/viewer_${aoi}_water_chart.png`);
}

// 4. Land pixel at level 0 reads "water 0 fraction".
const land = await discover(riverT, 'land', 0);
check(Boolean(land), `found a land window at pixel ${JSON.stringify(land)} in ${riverMonth.month}`);
if (land) {
  await open(`t=${riverT}&b=water&z=3&c=${toProjected(land.transform, land.x, land.y)}`);
  await settle(riverT);
  const rows = await clickCentre(`${land.x}, ${land.y}`);
  check(rows.Value.water === '0 fraction' && rows['Stored value'].water === '0', `the land pixel reads "water ${rows.Value.water}", stored ${rows['Stored value'].water}`);
}

// 5. A click on a coarser level reads a fraction: the inspector shows the level it read, and the chart plots fractions.
const COARSE = 2;
await open(`t=${riverT}`);
await page.waitForFunction((t) => window.tileripper.viewer.paintedT === t, riverT);
const blended = await discoverFraction(riverT, COARSE);
check(Boolean(blended), `found a level ${COARSE} pixel with a water fraction of 0.25 to 0.75: ${JSON.stringify(blended)} in ${riverMonth.month}`);
if (blended) {
  const factor = 2 ** COARSE;
  const centre = (n) => (n + 0.5) * factor - 0.5; // level-0 coordinate of the middle of a level pixel
  await open(`t=${riverT}&b=water&z=${1 / factor}&c=${toProjected(blended.transform, centre(blended.x), centre(blended.y))}`);
  await settle(riverT, COARSE);
  const rows = await clickCentre(`${factor}× coarser`);
  const { Value: value, 'Stored value': stored } = rows;
  const fraction = parseFloat(value.water);
  check(rows.Location.Level.startsWith(String(COARSE)), `the inspector read level ${rows.Location.Level}`);
  check(Number(stored.water) === blended.stored, `stored water ${stored.water} equals the reader's value ${blended.stored} at level ${COARSE} pixel (${blended.x}, ${blended.y})`);
  check(fraction > 0 && fraction < 1 && value.water.endsWith(' fraction') && Math.abs(fraction - blended.stored * 1e-4) < 6e-4, `level ${COARSE} click reads "water ${value.water}" (stored ${stored.water})`);
  const chart = await readChart();
  const between = chart.values.filter((v) => v > 0.01 && v < 0.99).length;
  check(chart.label.startsWith('water (fraction)') && chart.values.length >= 20 && chart.values.every((v) => v > -0.01 && v < 1.01) && between > 0, `${chart.values.length} charted months at level ${COARSE}, ${between} strictly between 0 and 1`);
  await page.screenshot({ path: path.join(reports, `viewer_${aoi}_water_coarse.png`) });
  console.log(`screenshot data/reports/viewer_${aoi}_water_coarse.png`);
}

check(problems.length === 0, `no console errors or page errors${problems.length ? `: ${problems.join(' | ')}` : ''}`);
await browser.close();
if (failures.length) {
  console.error(`${failures.length} check(s) failed`);
  process.exit(1);
}
console.log('all checks passed');
