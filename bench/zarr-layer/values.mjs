// Do the values zarr-layer reads equal the values in the store? Point query at the finest level for one pixel at some
// timesteps, compared with the reference read straight from the Zarr arrays (results/pixel-reference-*.json).
//
//   node zarr-layer/values.mjs [--times 0,2,3,40,41,60,80,100,101,102,116]

import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { launchBrowser, newPage, startServer } from '../lib/harness.mjs';

const args = process.argv.slice(2);
const flag = (name, fallback) => {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
};
const times = flag('times', '0,2,3,40,41,60,80,100,101,102,116').split(',').map(Number);
const store = 'https://data.tileripper.com/ucayali_santa_maria/chronozarr-3';
const bounds = [485650, 9142230, 513240, 9169880];
const crs = 'EPSG:32718';
const reference = JSON.parse(await readFile(path.join(import.meta.dirname, '../results/pixel-reference-L0-r1388-c1380.json'), 'utf8'));
// Centre of pixel (row 1388, col 1380) of level 0, 10 m pixels from the corner (485650, 9169880).
const pixelXY = [485650 + 1380.5 * 10, 9169880 - 1388.5 * 10];

const server = await startServer();
const browser = await launchBrowser();
try {
  const { page } = await newPage(browser, { width: 1500, height: 1500 });
  await page.goto(`${server.url}/bench/zarr-layer/page.html`);
  await page.waitForFunction(() => window.zl);
  const result = await page.evaluate(
    async ({ store, bounds, crs, pixelXY, times }) => {
      const view = zl.aoiView({ bounds, crs, zoom: 12 });
      await zl.createMap({ center: view.center, zoom: 12 });
      const layer = await zl.addLayer({ source: store, extra: { crs, bounds }, selector: { band: ['B02', 'B03', 'B04', 'B08'], time: { selected: 40, type: 'index' } } });
      const lonLat = zl.proj4(crs, 'EPSG:4326', pixelXY);
      const answer = await layer.queryData({ type: 'Point', coordinates: lonLat }, { band: { selected: [0, 1, 2, 3], type: 'index' }, time: { selected: times, type: 'index' } }, { level: 'finest' });
      return { lonLat, answer };
    },
    { store, bounds, crs, pixelXY, times },
  );
  // zarr-layer leaves a nodata pixel (the store's 0) out of the answer: null here.
  let compared = 0;
  let nodataFiltered = 0;
  const mismatches = [];
  for (const t of times) {
    for (let band = 0; band < 4; band++) {
      const got = result.answer.data[t]?.[band]?.[0];
      const expected = reference.values[t][band];
      compared++;
      if ((got === null || Number.isNaN(got)) && expected === 0) nodataFiltered++;
      else if (got !== expected) mismatches.push({ t, band, got, expected });
    }
  }
  const report = { store, level: 'finest', pixel: reference.row + ',' + reference.col, timesteps: times, valuesCompared: compared, equal: compared - mismatches.length, nodataLeftOutByTheLayer: nodataFiltered, mismatches };
  console.log(JSON.stringify(report));
  await writeFile(path.join(import.meta.dirname, '../results/zarr-layer-values.json'), JSON.stringify(report, null, 1) + '\n');
} finally {
  await browser.close();
  await server.close();
}
