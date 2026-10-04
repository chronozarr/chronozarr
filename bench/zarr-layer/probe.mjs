// Does @carbonplan/zarr-layer open a chronozarr store as published?
//
//   node zarr-layer/probe.mjs [storeUrl] [--zoom 12] [--time 60] [--timeout 90000] [--shot out.jpg] [--out result.json]
//        [--patch no-pixels-per-tile] [--extra '{"crs":"EPSG:32718","bounds":[...]}']
//
// Opens the store in a headless MapLibre map at a fixed view and timestep and prints what zarr-layer made of the
// store's metadata (`describe()`), whether every visible region loaded, the console warnings and errors, and where the
// drawn pixels are against where the store's own bounding box says they should be. `--patch` applies a named edit of the
// root attributes in the browser (the published store is not touched); `--extra` passes ZarrLayer constructor options.

import { writeFile } from 'node:fs/promises';
import { applyProfile, attachNet, launchBrowser, newPage, startServer } from '../lib/harness.mjs';

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const store = args.find((a) => /^https?:/.test(a)) ?? 'https://data.chronozarr.org/ucayali_santa_maria/chronozarr-3';
const zoom = Number(flag('zoom', 12));
const time = Number(flag('time', 60));
const timeoutMs = Number(flag('timeout', 90000));
const shot = flag('shot', null);
const patch = flag('patch', null);
const outFile = flag('out', null);
const extra = JSON.parse(flag('extra', '{}'));
// Level 0 spatial:bbox of the Ucayali store (x min, y min, x max, y max) in EPSG:32718.
const bounds = [485650, 9142230, 513240, 9169880];
const crs = 'EPSG:32718';

const server = await startServer();
const browser = await launchBrowser();
try {
  const { context, page } = await newPage(browser, { width: 1500, height: 1500 });
  const messages = [];
  page.on('console', (m) => messages.push({ type: m.type(), text: m.text().slice(0, 600) }));
  page.on('pageerror', (e) => messages.push({ type: 'pageerror', text: String(e.message).slice(0, 600) }));
  const { cdp, counter } = await attachNet(context, page, [new URL(store).origin]);
  await applyProfile(cdp, null);
  await page.goto(`${server.url}/bench/zarr-layer/page.html`);
  await page.waitForFunction(() => window.zl);
  const result = await page.evaluate(
    async ({ store, patch, extra, bounds, crs, zoom, time, timeoutMs }) => {
      const view = zl.aoiView({ bounds, crs, zoom });
      await zl.createMap({ center: view.center, zoom });
      const started = performance.now();
      const layer = await zl.addLayer({ source: store, patch, extra, selector: { band: ['B04', 'B03', 'B02'], time: { selected: time, type: 'index' } } });
      let outcome = 'complete';
      let state;
      try {
        state = await zl.whenComplete(layer, { timeoutMs });
      } catch (error) {
        outcome = `not complete: ${error.message}`;
        state = zl.layerState(layer);
      }
      await new Promise((r) => setTimeout(r, 300));
      const describe = layer.regionRenderer?.zarrStore?.describe?.() ?? layer.zarrStore?.describe?.() ?? null;
      const canvas = document.querySelector('canvas.maplibregl-canvas');
      const g = document.createElement('canvas');
      g.width = canvas.width;
      g.height = canvas.height;
      const ctx = g.getContext('2d');
      ctx.drawImage(canvas, 0, 0);
      const { data, width, height } = ctx.getImageData(0, 0, g.width, g.height);
      let minX = width, minY = height, maxX = -1, maxY = -1, lit = 0;
      for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
          const i = (y * width + x) * 4;
          if (data[i] + data[i + 1] + data[i + 2] > 12) {
            lit++;
            if (x < minX) minX = x;
            if (x > maxX) maxX = x;
            if (y < minY) minY = y;
            if (y > maxY) maxY = y;
          }
        }
      }
      return {
        outcome,
        ms: performance.now() - started,
        view,
        state,
        describe: describe && {
          crs: describe.crs,
          proj4: describe.proj4,
          levelAssets: describe.levelAssets,
          xyLimits: describe.xyLimits,
          latIsAscending: describe.latIsAscending,
          dimensions: describe.dimensions,
          shape: describe.shape,
          chunks: describe.chunks,
          fill_value: describe.fill_value,
          dtype: describe.dtype,
          resolutionLevels: describe.resolutionLevels?.map((l) => ({ asset: l.asset, shape: l.shape, xyLimits: l.xyLimits })),
        },
        canvas: { width, height },
        lit,
        litBox: lit ? { minX, minY, maxX, maxY } : null,
        mapCenter: window.map.getCenter(),
        mapZoom: window.map.getZoom(),
      };
    },
    { store, patch, extra, bounds, crs, zoom, time, timeoutMs },
  );
  if (shot) await page.screenshot({ path: shot, type: 'jpeg', quality: 70 });
  const net = counter.snapshot();
  const requests = counter.entries().map((e) => ({ method: e.method, path: new URL(e.url).pathname.replace(/^.*chronozarr-3/, ''), range: e.range, status: e.status, bytes: e.wire ?? e.body }));
  const report = { store, patch, extra, ...result, net, requests, messages: messages.filter((m) => m.type !== 'log' && m.type !== 'debug').slice(0, 30) };
  console.log(JSON.stringify(report, null, 1));
  if (outFile) await writeFile(outFile, `${JSON.stringify(report, null, 1)}\n`);
  const failures = counter.entries().filter((e) => e.status && e.status >= 400);
  if (failures.length) console.log('HTTP errors:', JSON.stringify(failures.slice(0, 10), null, 1));
  await context.close();
} finally {
  await browser.close();
  await server.close();
}
