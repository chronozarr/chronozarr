import { expect, test } from './fixtures.js';
import { storedValue } from './stores.js';
import { captureFrame, expectFrameMatchesStore, openViewer, waitForPaintedTime } from './viewer-helpers.js';

for (const split of [false, true]) test(`viewer decodes profile metadata deviations${split ? ' with time/band splits and nonsquare cells' : ''}`, async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('u16_plain');
  const shapes = new Map();
  await page.route(`${store}zarr.json`, async (route) => {
    const root = await (await route.fetch()).json();
    const cz = root.attributes.chronozarr;
    cz.spec_version = '0.3.7';
    delete cz.levels;
    delete cz.band_names;
    delete cz.crs;
    delete cz.variable;
    delete root.attributes.zarr_conventions;
    root.attributes.multiscales.resampling_method = 'nearest';
    for (const [path, meta] of Object.entries(root.consolidated_metadata.metadata)) {
      if (!path.endsWith('/data')) continue;
      delete meta.attributes._ARRAY_DIMENSIONS;
      delete meta.attributes.zarr_conventions;
      shapes.set(path.split('/')[0], meta.shape);
      if (split) meta.chunk_grid.configuration.chunk_shape = path.startsWith('0/') ? [2, 1, 64, 96] : [2, 1, 32, 48];
    }
    await route.fulfill({ json: root });
  });
  if (split) await page.route(`${store}**/data/c/**`, async (route) => {
    const [, lod, ct, cb, cy, cx] = /\/(\d+)\/data\/c\/(\d+)\/(\d+)\/(\d+)\/(\d+)$/.exec(new URL(route.request().url()).pathname);
    const shape = shapes.get(lod);
    const h = lod === '0' ? 64 : 32, w = lod === '0' ? 96 : 48;
    const data = new Uint16Array(2 * h * w);
    for (let t = 0; t < 2; t++) for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const time = +ct * 2 + t, row = +cy * h + y, col = +cx * w + x;
      if (time < shape[0] && row < shape[2] && col < shape[3]) data[t * h * w + y * w + x] = storedValue('u16_plain', time, +cb, row, col, +lod);
    }
    await route.fulfill({ body: Buffer.from(data.buffer), contentType: 'application/octet-stream' });
  });
  await openViewer(page, servers, store);
  expectFrameMatchesStore(await captureFrame(page), 'u16_plain', 0);
  const notes = await page.evaluate(() => window.chronozarr.viewer.store.notes);
  expect(notes.join('\n')).toMatch(/_ARRAY_DIMENSIONS/);
  expect(notes.join('\n')).toMatch(/unknown patch/);
  await page.locator('#next-btn').click();
  await waitForPaintedTime(page, 1);
  expectFrameMatchesStore(await captureFrame(page), 'u16_plain', 1);
  if (split) {
    await page.evaluate(() => { const { viewer } = window.chronozarr; viewer.lodOverride = 1; viewer.renderNow(); });
    await page.waitForFunction(() => window.chronozarr.viewer.renderNow().complete && window.chronozarr.viewer.shownFrame.lod === 1);
    expectFrameMatchesStore(await captureFrame(page), 'u16_plain', 1, 1);
  }
});
