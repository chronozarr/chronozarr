// What a jump in time does before the frame is drawn: when it asks for its chunks, and what has reached the GPU by the
// time the last chunk lands.

import { expect, test } from './fixtures.js';
import { openViewer, waitForPaintedTime } from './viewer-helpers.js';

test.use({ viewport: { width: 1280, height: 720 } });

/**
 * Stop animation frames from running: the page keeps them queued until `releaseFrames`. Whatever happens in between happens
 * in the calls of the test. A frame that the viewer had asked for already would still run, and would stand in for the one a
 * jump asks for (requestRender asks for one at a time), so the scrub timer is ended and two frames are let through first.
 */
const holdFrames = (page) =>
  page.evaluate(async () => {
    window.chronozarr.viewer.endScrub();
    const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve));
    await nextFrame();
    await nextFrame();
    window.heldFrames = [];
    window.realRequestAnimationFrame = window.requestAnimationFrame;
    window.requestAnimationFrame = (callback) => window.heldFrames.push(callback);
  });

const releaseFrames = (page) =>
  page.evaluate(() => {
    window.requestAnimationFrame = window.realRequestAnimationFrame;
    for (const callback of window.heldFrames.splice(0)) window.requestAnimationFrame(callback);
  });

/** Let the requests that a test held go to the server. The page may have given up on one meanwhile (a jump cancels the prefetch): that one cannot be continued. */
const releaseRoutes = (routes) => Promise.all(routes.map((route) => route.continue().catch(() => {})));

test('a jump in time asks for its chunks in the call, not on the next animation frame', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('beyond_idle');
  // The last timestep is not answered until the end of the test, so it cannot be in memory whatever the prefetch does.
  const held = [];
  await page.route(`${servers.dataUrl}/beyond_idle/**/c/9/**`, (route) => {
    held.push(route);
  });
  await openViewer(page, servers, url);

  // Nothing else in the page runs between the call and the read of what it did, so no animation frame comes in between.
  const afterCall = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    const events = [];
    viewer.probe = (event) => events.push(event);
    const painted = viewer.frameStats.painted;
    viewer.goToTime(9);
    return { loads: events.filter((event) => event.type === 'load-start').map((event) => event.t), painted: viewer.frameStats.painted - painted, shown: viewer.shownFrame.t };
  });
  expect(afterCall.loads, 'the call asked for the cells of timestep 9').toEqual([9]);
  expect(afterCall.painted, 'and drew nothing').toBe(0);
  expect(afterCall.shown, 'the canvas still shows the first timestep').toBe(0);

  await releaseRoutes(held);
  await waitForPaintedTime(page, 9);
});

test('a jump to a frame that is in memory is drawn on the next animation frame, not in the call', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('link_plain'));
  // Visit the target and come back, so that its chunks are in memory when the jump is made again.
  await page.evaluate(() => window.chronozarr.viewer.goToTime(8));
  await waitForPaintedTime(page, 8);
  await page.evaluate(() => window.chronozarr.viewer.goToTime(0));
  await waitForPaintedTime(page, 0);

  await holdFrames(page);
  const during = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    viewer.goToTime(8);
    return { shown: viewer.shownFrame, frames: window.heldFrames.length };
  });
  expect(during.shown.t, 'the canvas has not changed in the call').toBe(0);
  expect(during.frames, 'the draw waits for an animation frame').toBeGreaterThan(0);
  await releaseFrames(page);
  await waitForPaintedTime(page, 8);
});

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
      const held = [];
      await page.route(`${servers.dataUrl}/link_plain/0/data/c/8/0/1/1`, (route) => {
        held.push(route);
      });
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
      expect(held.length, 'the last cell was asked for and is still on its way').toBeGreaterThan(0);
      expect(waiting.paints, 'no frame of level 0 at timestep 8 has been drawn').toEqual([]);
      expect(waiting.shown.lod === 0 && waiting.shown.t === 8, 'the canvas does not show a frame with a cell missing').toBe(false);
      expect(waiting.paintedT).toBe(0);

      await releaseRoutes(held);
      await waitForPaintedTime(page, 8);
      expect(await page.evaluate(() => window.paintUploads), 'the paint that completed the frame uploaded only the cell that was late').toEqual([1]);
    });
  });
}

test('a warm step uploads nothing, requests nothing and draws a complete frame', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('link_plain'));
  await page.waitForFunction(
    (cells) => cells.every(([row, col]) => window.chronozarr.viewer.renderer.isResident(`0/${row}/${col}/1`)),
    LEVEL_0_CELLS,
    { timeout: 15_000 },
  );
  const step = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    const { renderer, store } = viewer;
    const before = { uploads: renderer.stats.uploads, requests: store.stats.network.requests };
    viewer.goToTime(1);
    const frame = viewer.renderNow();
    return { complete: frame.complete, uploads: renderer.stats.uploads - before.uploads, requests: store.stats.network.requests - before.requests };
  });
  expect(step).toEqual({ complete: true, uploads: 0, requests: 0 });
});
