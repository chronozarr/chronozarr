// In compact mode a chunk is kept packed, and reading it into an array unpacks the whole 2 MiB. The coarse stage of a
// cold open, the click on a pixel, the chart behind it and an export read a few pixels or only need the chunk loaded, so
// none of them may do that.
//
// The `coarse` store is two timesteps of 8 cells; its level-0 frame is big enough that the viewer stages a coarser frame
// first. A cap of 12 MiB (less than the 32 MiB loop) turns compact mode on. The chunks of timestep 1 are held back until
// the chart has started, so that the chart has to read one chunk over the network as well as one from the cache.

import { expect, test } from './fixtures.js';
import { clickStorePixel, openViewer } from './viewer-helpers.js';

const CAP_BYTES = 12 * 1024 * 1024;

test.use({ viewport: { width: 1920, height: 1080 } });

test('compact mode: the coarse stage, the inspector click, the chart and the export expand no packed chunk', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('coarse');
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  await page.route(`${servers.dataUrl}/**/c/1/**`, async (route) => {
    await held;
    await route.continue();
  });
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
  await page.waitForFunction(() => window.chronozarr?.ready);

  // Count the reads that expand a chunk (getRaw and peekRaw of a packed one) and the coarse-stage loads, on every store the viewer opens.
  const facts = await page.evaluate(
    async ({ url, totalBytes }) => {
      const { viewer } = window.chronozarr;
      const counts = { expanded: 0, coarseFrames: 0 };
      let current = viewer.store;
      const instrument = (store) => {
        const { getRaw, peekRaw, getCoarseFrame } = Object.fromEntries(['getRaw', 'peekRaw', 'getCoarseFrame'].map((name) => [name, store[name].bind(store)]));
        store.getRaw = async (lod, row, col, t, options) => {
          const data = await getRaw(lod, row, col, t, options);
          if (store.peekCompact(lod, row, col, t)) counts.expanded++;
          return data;
        };
        store.peekRaw = (lod, row, col, t) => {
          if (store.peekCompact(lod, row, col, t)) counts.expanded++;
          return peekRaw(lod, row, col, t);
        };
        store.getCoarseFrame = (...args) => {
          counts.coarseFrames++;
          return getCoarseFrame(...args);
        };
      };
      Object.defineProperty(viewer, 'store', {
        configurable: true,
        get: () => current,
        set: (store) => {
          current = store;
          if (store) instrument(store);
        },
      });
      window.__counts = counts;
      const timings = await viewer.loadStore(url, { totalBytes });
      return { compact: viewer.store.compactCacheEnabled, coarseMs: timings.coarseMs, firstPaintMs: timings.firstPaintMs, ...counts };
    },
    { url, totalBytes: CAP_BYTES },
  );
  expect(facts.compact, 'the cap is smaller than the loop, so the cache packs').toBe(true);
  expect(facts.coarseFrames, 'the cold open loaded a coarse stage').toBeGreaterThan(0);
  expect(facts.coarseMs, 'and painted it before the full frame').toBeLessThan(facts.firstPaintMs);
  expect.soft(facts.expanded, 'chunks expanded by the cold open').toBe(0);

  const box = await page.locator('#gl-canvas').boundingBox();
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator('#chart-status'), 'timestep 0 came from the cache, timestep 1 is held back').toContainText('loading 1 of 2 timesteps');
  release();
  await expect(page.locator('#chart-status')).toContainText('2 of 2 timesteps · click to jump');
  expect(await page.locator('.chart-svg path[stroke]').count(), 'a line per series is drawn').toBeGreaterThan(0);
  expect.soft(await page.evaluate(() => window.__counts.expanded), 'chunks expanded by the click and the chart').toBe(0);

  // An export loads the chunks of the timestep it draws next, and has no use for them as arrays.
  const exported = await page.evaluate(async () => {
    const session = window.chronozarr.viewer.createExportSession({ scale: 0.25 });
    try {
      await session.prepare(1, new AbortController().signal);
      return { ready: session.isReady(1), expanded: window.__counts.expanded };
    } finally {
      session.close();
    }
  });
  expect(exported, 'the export has the timestep in the cache and expanded no chunk').toEqual({ ready: true, expanded: 0 });
});

test('compact mode: the chart of a masked store draws what the unpacked chunks give', async ({ page, context, servers, storeUrl }) => {
  const url = await storeUrl('u16_mask');
  const charted = async (tab) => {
    await clickStorePixel(tab, 10, 10);
    await expect(tab.locator('#chart-status')).toContainText('6 of 6 timesteps · click to jump');
    return tab.locator('#chart path[stroke]').evaluateAll((paths) => paths.map((path) => path.getAttribute('d')));
  };
  // The store as it first opens (it fits its cache): the chart reads ordinary decoded arrays.
  await openViewer(page, servers, url);
  const unpacked = await charted(page);
  // Pixel (10, 10) is masked at t = 0 and 4 and valid at 1, 2, 3 and 5: two segments per series.
  expect(unpacked.map((d) => (d.match(/M/g) ?? []).length), 'path segments of each charted series').toEqual([2, 2, 2]);

  const tab = await context.newPage();
  await openViewer(tab, servers, url);
  const compact = await tab.evaluate(async ({ url, totalBytes }) => {
    await window.chronozarr.viewer.loadStore(url, { lod: 0, totalBytes });
    return window.chronozarr.viewer.store.compactCacheEnabled;
  }, { url, totalBytes: 2 * 1024 * 1024 });
  expect(compact, 'a cap of 2 MiB is less than the 3 MiB loop: the cache packs').toBe(true);
  expect(await charted(tab), 'the same lines from packed chunks').toEqual(unpacked);
});
