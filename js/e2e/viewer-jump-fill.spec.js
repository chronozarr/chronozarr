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

/** Resolves once the decoded cache holds every chunk of every level, so that the fill has the whole store to upload from and nothing is left to load. */
const waitForStoreInMemory = (page) =>
  page.waitForFunction(
    () => {
      const { store } = window.chronozarr.viewer;
      return store.levels.every((level, lod) => {
        for (let row = 0; row < level.gridRows; row++)
          for (let col = 0; col < level.gridCols; col++)
            for (let t = 0; t < store.times.length; t++) if ((store.peekCompact(lod, row, col, t) ?? store.peekRaw(lod, row, col, t)) === null) return false;
        return true;
      });
    },
    undefined,
    { timeout: 20_000 },
  );

/**
 * Make the animation frames of the page the test's own: a frame runs when `window.runFrame()` is called and not before, and
 * `runFrame` returns after the tasks that the frame queued have run. The page is left with `window.frames` (frames run so
 * far) and `window.fillSlices`, the slices of the GPU fill (tasks that uploaded to the GPU in the background) counted per
 * frame: index n holds those that ran after n frames and before frame n + 1. Each background upload takes 1.5 ms more,
 * as on a slow GPU, so that the window is many slices of 4 ms and not one.
 */
const controlFrames = (page) =>
  page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const { renderer } = viewer;
    window.frames = 0;
    window.fillSlices = [0];
    let inSlice = false;
    const upload = renderer.upload;
    renderer.upload = function (...args) {
      if (args[3]?.background) {
        if (!inSlice) {
          inSlice = true;
          window.fillSlices[window.frames]++;
          // The microtask runs when the task that made the upload ends: the next upload in a task of its own is another slice.
          queueMicrotask(() => {
            inSlice = false;
          });
        }
        for (const start = performance.now(); performance.now() - start < 1.5; );
      }
      return upload.apply(this, args);
    };

    // The fill that the opening of the viewer started goes on until the window is on the GPU, and a frame that the viewer
    // asked for already would run on its own and stand in for the ones the test runs: wait until neither is left.
    viewer.endScrub();
    for (let quiet = 0; quiet < 3; ) {
      const before = window.fillSlices[0];
      await new Promise((resolve) => setTimeout(resolve, 50));
      quiet = window.fillSlices[0] === before ? quiet + 1 : 0;
    }
    const nextFrame = () => new Promise((resolve) => requestAnimationFrame(resolve));
    await nextFrame();
    await nextFrame();
    window.fillSlices[0] = 0;

    const queued = new Map();
    let nextId = 1;
    window.requestAnimationFrame = (callback) => {
      queued.set(nextId, callback);
      return nextId++;
    };
    window.cancelAnimationFrame = (id) => {
      queued.delete(id);
    };
    window.runFrame = async () => {
      const callbacks = [...queued.values()];
      queued.clear();
      window.fillSlices[++window.frames] = 0;
      for (const callback of callbacks) callback(performance.now());
      await new Promise((resolve) => setTimeout(resolve, 30));
    };
  });

test('behind a drawn frame the GPU window fills: the next timesteps are on the GPU before anyone steps to them', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('link_plain'));
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

const FRAMES = 8;

for (const interaction of ['a movie plays', 'the user scrubs']) {
  test(`while ${interaction} the GPU fill runs at most one slice per frame, after the frame, and none between frames`, async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('link_plain'));
    await waitForStoreInMemory(page);
    await controlFrames(page);

    const { perFrame, between } = await page.evaluate(
      async ({ interaction, frames }) => {
        const { viewer } = window.chronozarr;
        // With nothing on the GPU the fill has the window to upload.
        viewer.renderer.clearResident();
        if (interaction === 'a movie plays') viewer.play();
        for (let i = 0; i < frames; i++) {
          // Steps of one timestep, each within the scrub window of the one before, so the scrub goes on.
          if (interaction === 'the user scrubs') viewer.goToTime(1 + (i % 2));
          await window.runFrame();
        }
        const during = window.fillSlices.reduce((sum, n) => sum + n, 0);
        // No frame runs now, and the scrub window (300 ms) is not over.
        await new Promise((resolve) => setTimeout(resolve, 150));
        return { perFrame: [...window.fillSlices], between: window.fillSlices.reduce((sum, n) => sum + n, 0) - during };
      },
      { interaction, frames: FRAMES },
    );
    expect(perFrame[0], 'no slice ran before the first frame').toBe(0);
    expect(Math.max(...perFrame), `slices per frame: ${perFrame}`).toBeLessThanOrEqual(1);
    expect(perFrame.reduce((sum, n) => sum + n, 0), `the fill went on, a slice after the frames: ${perFrame}`).toBeGreaterThanOrEqual(FRAMES / 2);
    expect(between, 'no slice ran while no frame did').toBe(0);
  });
}

test('when nothing moves the GPU fill runs its slices one after the other, without waiting for a frame', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('link_plain'));
  await waitForStoreInMemory(page);
  await controlFrames(page);

  const { frames, slices, resident } = await page.evaluate(
    async (cells) => {
      const { viewer } = window.chronozarr;
      const { renderer } = viewer;
      renderer.clearResident();
      viewer.renderNow();
      const isFilled = () => cells.every(([row, col]) => [1, 2, 3].every((t) => renderer.isResident(`0/${row}/${col}/${t}`)));
      for (const started = performance.now(); !isFilled() && performance.now() - started < 10_000; ) await new Promise((resolve) => setTimeout(resolve, 10));
      return { frames: window.frames, slices: window.fillSlices[0], resident: isFilled() };
    },
    LEVEL_0_CELLS,
  );
  expect(frames, 'no animation frame ran').toBe(0);
  expect(resident, 'the next timesteps are on the GPU').toBe(true);
  expect(slices, 'in more than one slice').toBeGreaterThan(1);
});

for (const [change, act] of [
  ['a jump', (viewer) => viewer.goToTime(8)],
  ['a camera move', (viewer) => viewer.setView({ center: { row: 100, col: 100 } })],
]) {
  test(`${change} drops the GPU fill that was waiting for the old window`, async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('link_plain'));
    await waitForStoreInMemory(page);
    await controlFrames(page);

    const slices = await page.evaluate(
      async ({ act }) => {
        const { viewer } = window.chronozarr;
        viewer.renderer.clearResident();
        // The frame schedules the fill: it waits for its task.
        viewer.renderNow();
        // eslint-disable-next-line no-new-func -- the test passes the change as source: a function cannot cross into the page
        new Function('viewer', `return (${act})(viewer);`)(viewer);
        // The frame of the new view is held, and it is what would schedule the fill again.
        await new Promise((resolve) => setTimeout(resolve, 100));
        return window.fillSlices[0];
      },
      { act: act.toString() },
    );
    expect(slices, 'the fill of the old window did not run').toBe(0);
  });
}
