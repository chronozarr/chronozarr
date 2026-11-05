// A click reads its pixel in the turn that the chunk lands in. The mask and the coverage of the pixel arrive later, and the
// chunk can leave the cache while they do; the sidebar must still show the values that were read. And when a cache too small
// for what loads at once has dropped the chunk before the pixel is read, the chunk is read again.
//
// A single-cell store whose cache holds one decoded chunk.

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

/** Write the store, open it in the viewer and reopen it with a cache that holds one chunk. */
async function openWithOneChunk({ page, servers, stores }) {
  for (const [key, bytes] of buildSyntheticStore(SPEC).files) {
    const file = path.join(stores.dir, 'inspect-eviction', key);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, bytes);
  }
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

test('a chunk that a cache of one chunk pushes out the moment it lands is read again for the click and the chart', async ({ page, servers, stores }) => {
  await openWithOneChunk({ page, servers, stores });
  await page.evaluate(() => {
    const { store } = window.chronozarr.viewer;
    const ensureRaw = store.ensureRaw.bind(store);
    const getRaw = store.getRaw.bind(store);
    // The viewer reads its pixel after getRaw only when the chunk is gone: count those reads.
    window.__rereads = [];
    store.getRaw = (lod, row, col, t, options) => {
      window.__rereads.push(t);
      return getRaw(lod, row, col, t, options);
    };
    // The click's read of timestep 0 resolves after timestep 1 has landed, which pushes timestep 0 out.
    let pushed = false;
    store.ensureRaw = async (lod, row, col, t, options) => {
      await ensureRaw(lod, row, col, t, options);
      if (t === 0 && !pushed) {
        pushed = true;
        await ensureRaw(lod, row, col, 1, options);
      }
    };
  });

  await clickStorePixel(page, 50, 40);
  await expect(page.locator('#sidebar-content')).toContainText('50, 40');
  expect((await readSidebar(page)).Value, 'the pixel the click read').toEqual([['depth', expectedValue()]]);
  expect(await page.evaluate(() => window.__rereads), 'the chunk was read again for the pixel').toContain(0);
  await expect(page.locator('#chart-status'), 'every timestep charted, none failed').toHaveText(/^4 of 4 timesteps( · \d+ gap-filled)? · click to jump$/);
});
