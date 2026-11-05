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

test('a jump in time asks for its chunks in the call, not on the next animation frame', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('beyond_idle');
  const last = await holdRequests(page, `${servers.dataUrl}/beyond_idle/**/c/9/**`);
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

  await last.release();
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

const nextFrame = (page) => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));

test('in a drag only the press asks for its cells in the call: the moves, each further than a few timesteps, wait for the frame', async ({ page, servers, storeUrl }) => {
  const steps = await holdRequests(page, `${servers.dataUrl}/link_plain/**/c/{5,6,11}/**`);
  await openViewer(page, servers, await storeUrl('link_plain'));

  const inTheCalls = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    window.loadStarts = [];
    viewer.probe = (event) => {
      if (event.type === 'load-start') window.loadStarts.push(event.t);
    };
    // A press on the timeline, then two moves of the drag, each more than SEEK_DISTANCE timesteps from the one before.
    viewer.goToTime(6);
    viewer.goToTime(11);
    viewer.goToTime(5);
    return [...window.loadStarts];
  });
  expect(inTheCalls, 'the press asked for its cells in its call, the moves did not').toEqual([6]);
  await nextFrame(page);
  // The scrub may ask for a timestep at more than one level, so what counts is which timesteps were asked for.
  expect(await page.evaluate(() => [...new Set(window.loadStarts)]), 'the frame asked for the timestep the drag is at, and for none that it passed').toEqual([6, 5]);

  await steps.release();
  await waitForPaintedTime(page, 5);
});

test('two jumps before the next frame: the request of the first is cancelled and its frame is never drawn', async ({ page, servers, storeUrl }) => {
  const jumps = await holdRequests(page, `${servers.dataUrl}/beyond_idle/**/c/{9,4}/**`);
  const cancelled = [];
  page.on('requestfailed', (request) => {
    if (request.failure()?.errorText === 'net::ERR_ABORTED') cancelled.push(request.url());
  });
  await openViewer(page, servers, await storeUrl('beyond_idle'));

  await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    window.events = [];
    viewer.probe = (event) => {
      if (event.type === 'load-start' || event.type === 'paint') window.events.push([event.type, event.t]);
    };
    viewer.goToTime(9);
    viewer.goToTime(4);
  });
  await nextFrame(page);
  expect(await page.evaluate(() => window.events.filter(([type]) => type === 'load-start')), 'both were asked for, the second at the frame').toEqual([['load-start', 9], ['load-start', 4]]);
  // The first request was held, so the only way it ends is that the page gives it up.
  await expect.poll(() => cancelled.some((url) => url.includes('/c/9/')), { message: 'the page cancelled the request of the first jump' }).toBe(true);

  await jumps.release();
  await waitForPaintedTime(page, 4);
  const painted = await page.evaluate(() => window.events.filter(([type]) => type === 'paint').map(([, t]) => t));
  expect(painted, 'no frame of the first jump was drawn').not.toContain(9);
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

test('a far jump to a frame that is in memory asks for nothing, in its call or a second later', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('link_plain'));
  // Visit the target and come back, so that its chunks are in memory when the jump is made again.
  await page.evaluate(() => window.chronozarr.viewer.goToTime(8));
  await waitForPaintedTime(page, 8);
  await page.evaluate(() => window.chronozarr.viewer.goToTime(0));
  await waitForPaintedTime(page, 0);

  servers.data.requests.length = 0;
  await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    // The scrub of the two jumps above is over, so this one stands alone and asks in its call.
    viewer.endScrub();
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
