// A click reads its pixel in the turn that the chunk lands in. The mask and the coverage of the pixel arrive later, and the
// chunk can leave the cache while they do; the sidebar must still show the values that were read.
//
// A single-cell store whose cache holds one decoded chunk, and a coverage read that the test holds back: while it waits, a
// read of another timestep pushes the clicked chunk out.

import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { buildSyntheticStore, defaultValues } from '../support/synthetic-store.js';
import { formatValue } from '../demo/inspector.js';
import { expect, test } from './fixtures.js';
import { clickStorePixel, openViewer, readSidebar } from './viewer-helpers.js';

const CHUNK_BYTES = 128 * 128;
const SPEC = {
  dtype: 'uint8',
  nTime: 4,
  nBand: 1,
  height: 128,
  width: 128,
  chunk: 128,
  sharded: false,
  nLevels: 1,
  consolidated: true,
  specVersion: '0.3.0',
  nodata: null,
  coverage: true,
  bandObjects: [{ name: 'depth', units: 'm', scale: 1 }],
};

test('the sidebar shows the pixel that was read although its chunk left the cache while the coverage was loading', async ({ page, servers, stores }) => {
  for (const [key, bytes] of buildSyntheticStore(SPEC).files) {
    const file = path.join(stores.dir, 'inspect-eviction', key);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, bytes);
  }
  const url = `${servers.dataUrl}/inspect-eviction/`;
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  let requested;
  const coverageRequested = new Promise((resolve) => {
    requested = resolve;
  });
  await page.route(`${servers.dataUrl}/**/coverage/**`, async (route) => {
    requested();
    await held;
    await route.continue();
  });
  await openViewer(page, servers, url);
  // Reopen it with a cache that holds one chunk.
  await page.evaluate(({ url, maxCacheBytes }) => window.chronozarr.viewer.loadStore(url, { maxCacheBytes }), { url, maxCacheBytes: CHUNK_BYTES + 4096 });

  await clickStorePixel(page, 50, 40);
  await coverageRequested;
  const evicted = await page.evaluate(async () => {
    const { store } = window.chronozarr.viewer;
    await store.ensureRaw(0, 0, 0, 1);
    return !store.hasCachedRaw(0, 0, 0, 0);
  });
  expect(evicted, 'the clicked chunk is out of the cache while the coverage is still on its way').toBe(true);
  release();

  await expect(page.locator('#sidebar-content')).toContainText('50, 40');
  const expected = defaultValues('uint8')(0, 0, 40, 50, 0);
  expect((await readSidebar(page)).Value).toEqual([['depth', `${formatValue(expected)} m`]]);
});
