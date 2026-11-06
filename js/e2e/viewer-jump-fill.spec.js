// What a jump in time puts on the GPU before the frame is drawn, and what a jump to a frame in memory asks for.

import { expect, test } from './fixtures.js';
import { openViewer, waitForPaintedTime } from './viewer-helpers.js';

test.use({ viewport: { width: 1280, height: 720 } });

/**
 * Hold the requests whose URL matches `pattern`, unanswered, so that what they carry cannot be in memory whatever the prefetch does.
 * `release` lets the held ones go to the server and stops holding. The page may have given up on one meanwhile (a jump cancels
 * the prefetch): that one cannot be continued.
 */
async function holdRequests(page, pattern) {
  const held = [];
  const hold = (route) => {
    held.push(route);
  };
  await page.route(pattern, hold);
  return {
    held,
    release: async () => {
      await page.unroute(pattern, hold);
      await Promise.all(held.map((route) => route.continue().catch(() => {})));
    },
  };
}

const LEVEL_0_CELLS = [[0, 0], [0, 1], [1, 0], [1, 1]];

// The fill yields with scheduler.postTask where the browser has it and with a message elsewhere (Safari, Firefox before 142): both are run.
for (const yielding of ['scheduler.postTask', 'MessageChannel']) {
  test.describe(`the GPU fill yields with ${yielding}`, () => {
    test.beforeEach(async ({ page }) => {
      if (yielding === 'MessageChannel') await page.addInitScript(() => Object.defineProperty(globalThis.Scheduler.prototype, 'postTask', { value: undefined }));
    });

    test('behind a drawn frame the GPU window fills: the next timesteps are on the GPU before anyone steps to them', async ({ page, servers, storeUrl }) => {
      await openViewer(page, servers, await storeUrl('link_plain'));
      expect(await page.evaluate(() => typeof globalThis.scheduler?.postTask === 'function'), 'the page has scheduler.postTask only in the run that yields with it').toBe(yielding === 'scheduler.postTask');
      await page.waitForFunction(
        (cells) => {
          const { viewer } = window.chronozarr;
          return cells.every(([row, col]) => [1, 2].every((t) => viewer.renderer.isResident(`0/${row}/${col}/${t}`)));
        },
        LEVEL_0_CELLS,
        { timeout: 15_000 },
      );
    });

    test('while a jump waits for its last cell, the cells that have arrived are on the GPU already and the frame is not drawn', async ({ page, servers, storeUrl }) => {
      // The cell (1, 1) of timestep 8 is not answered until the cells around it are on the GPU.
      const late = await holdRequests(page, `${servers.dataUrl}/link_plain/0/data/c/8/0/1/1`);
      await openViewer(page, servers, await storeUrl('link_plain'));

      await page.evaluate(() => {
        const { viewer } = window.chronozarr;
        const { renderer } = viewer;
        // The uploads the paint of timestep 8 at level 0 makes itself, from its start (newFrame) to its end (the paint event).
        window.paintUploads = [];
        let atStart = 0;
        const newFrame = renderer.newFrame;
        renderer.newFrame = function () {
          atStart = this.stats.uploads;
          return newFrame.call(this);
        };
        viewer.probe = (event) => {
          if (event.type === 'paint' && event.t === 8 && event.lod === 0) window.paintUploads.push(renderer.stats.uploads - atStart);
        };
        viewer.goToTime(8);
      });

      await page.waitForFunction(
        (cells) => cells.every(([row, col]) => window.chronozarr.viewer.renderer.isResident(`0/${row}/${col}/8`)),
        LEVEL_0_CELLS.filter(([row, col]) => row !== 1 || col !== 1),
        { timeout: 15_000 },
      );
      const waiting = await page.evaluate(() => ({ shown: window.chronozarr.viewer.shownFrame, paintedT: window.chronozarr.viewer.paintedT, paints: window.paintUploads }));
      expect(late.held.length, 'the last cell was asked for and is still on its way').toBeGreaterThan(0);
      expect(waiting.paints, 'no frame of level 0 at timestep 8 has been drawn').toEqual([]);
      expect(waiting.shown.lod === 0 && waiting.shown.t === 8, 'the canvas does not show a frame with a cell missing').toBe(false);
      expect(waiting.paintedT).toBe(0);

      await late.release();
      await waitForPaintedTime(page, 8);
      expect(await page.evaluate(() => window.paintUploads), 'the paint that completed the frame uploaded only the cell that was late').toEqual([1]);
    });

    test('opening a store cancels the fill that was waiting for the one it replaces', async ({ page, servers, storeUrl }) => {
      const url = await storeUrl('link_plain');
      await openViewer(page, servers, url);
      const reads = await page.evaluate(async (storeUrl) => {
        const { viewer } = window.chronozarr;
        const { renderer, store } = viewer;
        // With nothing on the GPU the fill that the next frame schedules has a whole window to read from the store.
        renderer.clearResident();
        viewer.renderNow();
        let count = 0;
        for (const name of ['peekCompact', 'peekRaw']) {
          const read = store[name];
          store[name] = function (...args) {
            count++;
            return read.apply(this, args);
          };
        }
        await viewer.loadStore(storeUrl);
        return count;
      }, url);
      expect(reads, 'the fill of the old store did not run').toBe(0);
    });
  });
}

test('a far jump to a frame that is in memory makes no request', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('link_plain'));
  // Visit the target and come back, so that its chunks are in memory when the jump is made again.
  await page.evaluate(() => window.chronozarr.viewer.goToTime(8));
  await waitForPaintedTime(page, 8);
  await page.evaluate(() => window.chronozarr.viewer.goToTime(0));
  await waitForPaintedTime(page, 0);

  servers.data.requests.length = 0;
  await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    window.loadStarts = [];
    viewer.probe = (event) => {
      if (event.type === 'load-start') window.loadStarts.push(event.t);
    };
    viewer.goToTime(8);
  });
  await waitForPaintedTime(page, 8);
  // What a switch starts leaves after it, so, as the gate runner does, wait and count again.
  await page.waitForTimeout(1000);
  expect(await page.evaluate(() => window.loadStarts), 'no cell was missing').toEqual([]);
  // Level 0 is the level of the frame; the coarse loop of the prefetch may still be fetching timesteps of the level above it.
  expect(servers.data.requests.filter(({ path }) => path.includes('/0/data/c/8/')), 'no chunk of the frame reached the server').toEqual([]);
});
