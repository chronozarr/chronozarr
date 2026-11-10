// A click reads its pixel in the turn that the chunk lands in. The mask and the coverage of the pixel arrive later, and the
// chunk can leave the cache while they do; the sidebar must still show the values that were read. A chart also samples chunks
// that lose cache admission without requesting them again.
//
// A single-cell store whose cache holds one decoded chunk.

import { existsSync } from 'node:fs';
import path from 'node:path';
import { buildSyntheticStore, defaultValues } from '../support/synthetic-store.js';
import { formatValue } from '../demo/inspector.js';
import { expect, test } from './fixtures.js';
import { writeTree } from './make-node-fixtures.mjs';
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

/** Write the store, open it in the viewer and reopen it with a cache that holds one chunk. */
async function openWithOneChunk({ page, servers, stores }) {
  const dir = path.join(stores.dir, 'inspect-eviction');
  if (!existsSync(dir)) await writeTree(dir, buildSyntheticStore(SPEC).files);
  const url = `${servers.dataUrl}/inspect-eviction/`;
  await openViewer(page, servers, url);
  await page.evaluate(({ url, maxCacheBytes }) => window.chronozarr.viewer.loadStore(url, { maxCacheBytes }), { url, maxCacheBytes: CHUNK_BYTES + 4096 });
}

const expectedValue = () => `${formatValue(defaultValues('uint8')(0, 0, 40, 50, 0))} m`;

test('the sidebar shows the pixel that was read although its chunk left the cache while the coverage was loading', async ({ page, servers, stores }) => {
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
  await openWithOneChunk({ page, servers, stores });

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
  expect((await readSidebar(page)).Value).toEqual([['depth', expectedValue()]]);
});

test('the chart fetches each cold timestep once when the cache keeps the displayed chunk', async ({ page, servers, stores }) => {
  const requested = [];
  await page.route(`${servers.dataUrl}/**/data/c/**`, async (route) => {
    requested.push(Number(/\/data\/c\/(\d+)\//.exec(route.request().url())[1]));
    await route.continue().catch(() => {});
  });
  await openWithOneChunk({ page, servers, stores });
  const cached = () => page.evaluate(() => {
    const { store } = window.chronozarr.viewer;
    return store.times.flatMap((_, t) => store.hasCachedRaw(0, 0, 0, t) ? [t] : []);
  });
  await page.evaluate((chunkBytes) => window.chronozarr.viewer.store.setBudgets({ totalBytes: chunkBytes, decodedBytes: chunkBytes, compressedBytes: 0 }), CHUNK_BYTES);
  expect(await cached()).toEqual([0]);
  requested.length = 0;

  await clickStorePixel(page, 50, 40);
  await expect(page.locator('#sidebar-content')).toContainText('50, 40');
  expect((await readSidebar(page)).Value, 'the pixel the click read').toEqual([['depth', expectedValue()]]);
  await expect(page.locator('#chart-status'), 'every timestep charted, none failed').toHaveText(/^4 of 4 timesteps( · \d+ gap-filled)? · click to jump$/);
  expect(requested.sort((a, b) => a - b)).toEqual([1, 2, 3]);
  expect(await cached(), 'cold chart chunks did not evict the displayed chunk').toEqual([0]);
});
