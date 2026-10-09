// Do the three systems show the same values? Two checks, both against a reference read with no browser and no chronozarr
// code (reference.py: zarr-python on the store, rasterio on the COG of that date).
//
// 1. Reader level. PIXELS at TIMES, level 0, all four bands, read through each system's own reader:
//      A  the chronozarr JS reader (js/chronozarr/decoder.js getCell, the chunks the viewer draws from)
//      B  zarr-layer's queryData at its finest level
//      C  TiTiler's /cog/point on that date's COG (the values behind its tiles)
// 2. Displayed. The zoom view at TIMES[0], the first DISPLAYED pixels of PIXELS, each at the screen position the system's own
//    projection gives for the pixel's centre (display.mjs reads the canvas back there and finds the uniform block of the pixel):
//      A  the viewer's own click-to-inspect at that position reports the stored values of the pixel it is over, and
//         its canvas has a block centred there (the viewer's colours are tone-mapped with a measured stretch, so they are not compared)
//      B  the block's colour equals what zarr-layer's shader makes of the pixel's values (clamp(v / 3000), gamma 1/2.2;
//         bench/zarr-layer/page.js RGB_FRAG), within 2 of 255, and the block is centred there
//      C  the block's colour equals TiTiler's rendering of the pixel's values (rescale 0..3000, 8 bit), within 1 of 255,
//         and the block is centred there (within half a level-0 pixel)
//    A pixel whose neighbours have different colours (chosen so, see PIXELS) shows a misplacement of a pixel or more.
//
// A pixel is nonzero in all four bands at all three dates: 0 is the store's nodata, which zarr-layer leaves out as NaN.
//
//   node bench/v03/values.mjs        (needs the same data and Docker image as run.mjs; prints the result)

import { execFile } from 'node:child_process';
import path from 'node:path';
import { promisify } from 'node:util';
import { launchBrowser, newPage } from '../lib/harness.mjs';
import { REPO_ROOT, STORE_DIR, TITILER_TILE_QUERY, VIEWPORT, VIEWS } from './config.mjs';
import { PATCH_HALF, blockAround, placedRight, readPatchInPage } from './display.mjs';
import { footprintLonLat, lonLatOfUtm, resolveView, utmOfPixel, viewerSearch } from './view.mjs';

const run = promisify(execFile);

/**
 * [row, col] of level-0 pixels. The first three lie in the zoom view, one in each of cells (2,2), (3,3) and (2,3); at t=10
 * each has B04/B03/B02 inside 300..2700 and every 4-neighbour differs from it by at least 5 of 255 in the stretched colour.
 * The fourth is the corner shared by the four cells of the zoom view (a chunk boundary); the last two are far from it.
 */
export const PIXELS = [
  [1304, 1298],
  [1762, 1761],
  [1295, 1805],
  [1535, 1536],
  [700, 310],
  [2701, 2688],
];
export const DISPLAYED = 3;
export const TIMES = [10, 40, 60];
export const BANDS = ['B02', 'B03', 'B04', 'B08'];

/** The colour a system draws for the reflectance DNs [B02, B03, B04] (red = B04, green = B03, blue = B02), as 0..255 [r, g, b]. */
export const EXPECTED_COLOUR = {
  B: { tolerance: 2, of: ([b02, b03, b04]) => [b04, b03, b02].map((v) => Math.round(255 * Math.min(1, Math.max(0, v / 3000)) ** (1 / 2.2))) },
  C: { tolerance: 1, of: ([b02, b03, b04]) => [b04, b03, b02].map((v) => Math.round(255 * Math.min(1, Math.max(0, v / 3000)))) },
};

const sameValues = (a, b) => Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => v === b[i]);
const withinColour = (a, b, tolerance) => Array.isArray(a) && Array.isArray(b) && a.every((v, i) => Math.abs(v - b[i]) <= tolerance);

async function referenceValues(cogDir) {
  const { stdout } = await run('uv', ['run', '--extra', 'geo', 'python', path.join(REPO_ROOT, 'bench/v03/reference.py'), STORE_DIR, cogDir, JSON.stringify({ pixels: PIXELS, times: TIMES })], { cwd: REPO_ROOT, maxBuffer: 16 * 1024 * 1024 });
  return JSON.parse(stdout);
}

const blankPage = (servers) => `${servers.app.url}/bench/v03/page-c.html`;

async function viaChronozarr(servers, browser) {
  const { context, page } = await newPage(browser, { width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: VIEWPORT.deviceScaleFactor });
  try {
    await page.goto(blankPage(servers));
    return await page.evaluate(
      async ({ storeUrl, pixels, times }) => {
        const { openStore, samplePixelFrom } = await import('/js/chronozarr/decoder.js');
        const store = await openStore(storeUrl, { workers: 0 });
        const level = store.level(0);
        const out = [];
        for (const t of times) {
          for (const [row, col] of pixels) {
            const cell = await store.getCell(0, Math.floor(row / level.chunkHeight), Math.floor(col / level.chunkWidth), t);
            out.push({ t, row, col, values: Array.from(samplePixelFrom(cell.data, level, col % level.chunkWidth, row % level.chunkHeight)) });
          }
        }
        store.close();
        return out;
      },
      { storeUrl: servers.store, pixels: PIXELS, times: TIMES },
    );
  } finally {
    await context.close();
  }
}

/** Open zarr-layer on the zoom view at TIMES[0]; resolves with the page, which then answers reader and canvas questions. */
async function openZarrLayer(servers, browser) {
  const view = resolveView(VIEWS.zoom);
  const { context, page } = await newPage(browser, { width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: VIEWPORT.deviceScaleFactor });
  await page.goto(`${servers.app.url}/bench/zarr-layer/page.html`);
  await page.waitForFunction(() => window.zl);
  await page.evaluate(
    async ({ storeUrl, view, t }) => {
      await zl.createMap({ center: view.centerLonLat, zoom: view.mapZoom });
      window.valuesLayer = await zl.addLayer({ source: storeUrl, extra: { zarrVersion: 3 }, selector: { band: ['B02', 'B03', 'B04', 'B08'], time: { selected: t, type: 'index' } } });
      await zl.whenComplete(window.valuesLayer, { timeoutMs: 120000 });
    },
    { storeUrl: servers.store, view, t: TIMES[0] },
  );
  return { context, page };
}

const pointsOf = () => PIXELS.map(([row, col]) => ({ row, col, lonLat: lonLatOfUtm(...utmOfPixel(col, row, { centre: true })) }));

/** For each point: the screen position MapLibre projects its lon/lat to, and the block of its pixel on the page's map canvas. */
async function blocksOnMap(page, points) {
  const out = [];
  for (const { lonLat } of points) {
    const screen = await page.evaluate((ll) => {
      const { x, y } = window.map.project(ll);
      return [x, y];
    }, lonLat);
    const patch = await page.evaluate(readPatchInPage, { canvasKind: 'map', x: screen[0], y: screen[1], half: PATCH_HALF });
    out.push({ screen, ...blockAround(patch) });
  }
  return out;
}

async function viaZarrLayer(servers, browser) {
  const { context, page } = await openZarrLayer(servers, browser);
  try {
    const points = pointsOf();
    const values = await page.evaluate(
      async ({ points, times }) => {
        const out = [];
        for (const { row, col, lonLat } of points) {
          const answer = await window.valuesLayer.queryData({ type: 'Point', coordinates: lonLat }, { band: { selected: [0, 1, 2, 3], type: 'index' }, time: { selected: times, type: 'index' } }, { level: 'finest' });
          for (const t of times) out.push({ t, row, col, values: [0, 1, 2, 3].map((b) => answer.data[t]?.[b]?.[0] ?? null) });
        }
        return out;
      },
      { points, times: TIMES },
    );
    const blocks = await blocksOnMap(page, points.slice(0, DISPLAYED));
    return { values, blocks };
  } finally {
    await context.close();
  }
}

/** System A in the zoom view: click each displayed pixel at its screen position and read what the viewer's inspector reports. */
async function viaViewerInspector(servers, browser) {
  const view = resolveView(VIEWS.zoom);
  const { context, page } = await newPage(browser, { width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: VIEWPORT.deviceScaleFactor, blockCatalog: true });
  try {
    await page.goto(`${servers.app.url}/js/demo/index.html`);
    await page.waitForFunction(() => Boolean(window.chronozarr?.viewer));
    await page.addStyleTag({ content: 'nav, .timeline-bar, .sidebar, .click-hint, .perf-overlay { display: none !important; }' });
    await page.evaluate(({ storeUrl, search }) => window.chronozarr.viewer.loadStore(storeUrl, { viewSearch: search }), { storeUrl: servers.store, search: viewerSearch(view, TIMES[0]) });
    const out = [];
    for (const [row, col] of PIXELS.slice(0, DISPLAYED)) {
      const target = await page.evaluate(
        ({ row, col }) => {
          const v = window.chronozarr.viewer;
          const rect = v.canvas.getBoundingClientRect();
          const { cx, cy, scale } = v.camera;
          const px = (col + 0.5 - cx) * scale + v.canvas.width / 2;
          const py = (row + 0.5 - cy) * scale + v.canvas.height / 2;
          window.__clicked = null;
          v.hooks.click = (payload) => {
            window.__clicked = payload;
          };
          return { x: rect.left + (px * rect.width) / v.canvas.width, y: rect.top + (py * rect.height) / v.canvas.height };
        },
        { row, col },
      );
      const block = blockAround(await page.evaluate(readPatchInPage, { canvasKind: 'viewer', x: target.x, y: target.y, half: PATCH_HALF }));
      await page.mouse.click(target.x, target.y);
      await page.waitForFunction(() => window.__clicked !== null, null, { timeout: 30000 });
      const clicked = await page.evaluate(() => ({ t: window.__clicked.t, lod: window.__clicked.lod, pixel: window.__clicked.pixel, values: window.__clicked.info.bands.map((b) => b.stored) }));
      out.push({ t: clicked.t, row, col, screen: [target.x, target.y], lod: clicked.lod, viewerPixel: clicked.pixel, values: clicked.values, block });
      await page.evaluate(() => window.chronozarr.viewer.closeInspector());
    }
    return out;
  } finally {
    await context.close();
  }
}

async function viaTitiler(servers, cogName) {
  const out = [];
  for (const t of TIMES) {
    for (const [row, col] of PIXELS) {
      const [lon, lat] = lonLatOfUtm(...utmOfPixel(col, row, { centre: true }));
      const response = await fetch(`${servers.titiler.url}/cog/point/${lon},${lat}?url=${encodeURIComponent(servers.cogUrl(t))}`);
      if (!response.ok) throw new Error(`TiTiler /cog/point failed for t=${t} row=${row} col=${col}: HTTP ${response.status} ${(await response.text()).slice(0, 200)}`);
      const body = await response.json();
      out.push({ t, row, col, values: body.values, cog: cogName(t) });
    }
  }
  return out;
}

/** System C in the zoom view: MapLibre raster tiles of TIMES[0] from TiTiler, then the colour at each displayed pixel. */
async function viaTitilerTiles(servers, browser) {
  const view = resolveView(VIEWS.zoom);
  const { context, page } = await newPage(browser, { width: VIEWPORT.width, height: VIEWPORT.height, deviceScaleFactor: VIEWPORT.deviceScaleFactor });
  try {
    await page.goto(blankPage(servers));
    await page.waitForFunction(() => window.maplibregl);
    await page.addScriptTag({ path: new URL('common.js', import.meta.url).pathname });
    await page.addScriptTag({ path: new URL('driver-c.js', import.meta.url).pathname });
    const cfg = { cogUrls: Array.from({ length: servers.timesteps }, (_, t) => servers.cogUrl(t)), titilerUrl: servers.titiler.url, tileQuery: TITILER_TILE_QUERY, bounds: footprintLonLat(), latitude: view.centerLonLat[1] };
    await page.evaluate((args) => window.__driver.open(args), { t: TIMES[0], view, cfg, timeoutMs: 120000 });
    await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    return await blocksOnMap(page, pointsOf().slice(0, DISPLAYED));
  } finally {
    await context.close();
  }
}

/** Compare everything; `servers.titiler` may be null (system C not selected). */
export async function checkValues({ servers, cogName, cogDir }) {
  const reference = await referenceValues(cogDir);
  const browser = await launchBrowser({ channel: 'chromium' });
  let a;
  let b;
  let inspector;
  let c = null;
  let cBlocks = null;
  try {
    a = await viaChronozarr(servers, browser);
    b = await viaZarrLayer(servers, browser);
    inspector = await viaViewerInspector(servers, browser);
    if (servers.titiler) {
      c = await viaTitiler(servers, cogName);
      cBlocks = await viaTitilerTiles(servers, browser);
    }
  } finally {
    await browser.close();
  }
  const find = (rows, row) => rows?.find((r) => r.t === row.t && r.row === row.row && r.col === row.col)?.values ?? null;
  const rows = reference.map((ref) => {
    const sources = { store: ref.store, cog: ref.cog, A: find(a, ref), B: find(b.values, ref), C: find(c, ref) };
    const mismatches = Object.entries(sources).filter(([, values]) => values !== null && !sameValues(values, ref.store)).map(([name]) => name);
    return { t: ref.t, date: ref.date, row: ref.row, col: ref.col, bands: BANDS, ...sources, mismatches, storeHasNodata: ref.store.some((v) => v === 0) };
  });
  const devicePxPerPixel = VIEWS.zoom.cssPxPerL0 * VIEWPORT.deviceScaleFactor;
  const shownBlock = (block) => ({ colour: block.colour, widthPx: block.widthPx, heightPx: block.heightPx, offsetX: block.offsetX, offsetY: block.offsetY });
  const displayed = PIXELS.slice(0, DISPLAYED).map(([row, col], i) => {
    const truth = reference.find((r) => r.t === TIMES[0] && r.row === row && r.col === col).store;
    const rgb = truth.slice(0, 3);
    const inspected = inspector[i];
    const entry = { t: TIMES[0], row, col, store: truth };
    entry.A = { viewerInspector: inspected.values, screen: inspected.screen, viewerPixel: inspected.viewerPixel, lod: inspected.lod, block: shownBlock(inspected.block) };
    entry.A.matches = sameValues(inspected.values, truth) && placedRight(inspected.block, devicePxPerPixel);
    entry.B = { expected: EXPECTED_COLOUR.B.of(rgb), screen: b.blocks[i].screen, block: shownBlock(b.blocks[i]) };
    entry.B.matches = withinColour(b.blocks[i].colour, entry.B.expected, EXPECTED_COLOUR.B.tolerance) && placedRight(b.blocks[i], devicePxPerPixel);
    if (cBlocks) {
      entry.C = { expected: EXPECTED_COLOUR.C.of(rgb), screen: cBlocks[i].screen, block: shownBlock(cBlocks[i]) };
      entry.C.matches = withinColour(cBlocks[i].colour, entry.C.expected, EXPECTED_COLOUR.C.tolerance) && placedRight(cBlocks[i], devicePxPerPixel);
    }
    return entry;
  });
  const readerBad = rows.reduce((n, r) => n + r.mismatches.length, 0);
  const displayBad = displayed.reduce((n, d) => n + ['A', 'B', 'C'].filter((s) => d[s] && !d[s].matches).length, 0);
  const sources = c ? 5 : 4;
  const invalid = rows.filter((r) => r.storeHasNodata).length;
  return {
    pixels: PIXELS,
    times: TIMES,
    rows,
    displayed,
    summary: `reader level: ${rows.length} pixel-date samples x ${sources} sources (store, COG, A, B${c ? ', C' : ''}), ${readerBad} mismatches of ${rows.length * sources}${invalid ? `; ${invalid} samples hit nodata, pick other pixels` : ''}; displayed: ${displayed.length} pixels x ${cBlocks ? 3 : 2} systems, ${displayBad} mismatches of ${displayed.length * (cBlocks ? 3 : 2)}`,
    mismatches: rows.filter((r) => r.mismatches.length),
    displayMismatches: displayed.filter((d) => ['A', 'B', 'C'].some((s) => d[s] && !d[s].matches)),
  };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { startAppServer, startDataServer, startTitiler, ensureTitilerImage } = await import('./servers.mjs');
  const { readFile } = await import('node:fs/promises');
  const { STORE_REL, COG_REL, COG_DIR } = await import('./config.mjs');
  const store = JSON.parse(await readFile(path.join(STORE_DIR, 'zarr.json'), 'utf8'));
  const times = store.attributes.chronozarr.times;
  const cogName = (t) => `L0_${times[t].slice(0, 10)}.tif`;
  await ensureTitilerImage();
  const app = await startAppServer();
  const data = await startDataServer();
  const port = new URL(data.url).port;
  const servers = { app, data, store: `${data.url}/${STORE_REL}`, timesteps: times.length, cogUrl: (t) => `http://host.docker.internal:${port}/${COG_REL}/${cogName(t)}`, titiler: null };
  try {
    servers.titiler = await startTitiler({ cogUrl: servers.cogUrl });
    const result = await checkValues({ servers, cogName, cogDir: COG_DIR });
    console.log(JSON.stringify(result, null, 1));
  } finally {
    await servers.titiler?.stop();
    await data.close();
    await app.close();
  }
}
