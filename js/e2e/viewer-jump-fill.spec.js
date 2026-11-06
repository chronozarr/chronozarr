// The GPU fill: what a jump in time has on the GPU before its frame is drawn, how the slices of the fill are paced against animation frames, and what a jump to a frame in memory asks for.

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

/** Load the decoded fixture for the pacing tests, independent of what idle prefetch can warm. */
const loadStoreIntoMemory = (page) =>
  page.evaluate(async () => {
    const { store } = window.chronozarr.viewer;
    const chunks = [];
    for (const [lod, level] of store.levels.entries()) {
      for (let row = 0; row < level.gridRows; row++) {
        for (let col = 0; col < level.gridCols; col++) {
          for (let t = 0; t < store.times.length; t++) chunks.push([lod, row, col, t]);
        }
      }
    }
    await Promise.all(chunks.map((chunk) => store.ensureRaw(...chunk)));
    const missing = chunks.find((chunk) => !store.hasCachedRaw(...chunk));
    if (missing) throw new Error(`pacing fixture did not retain chunk ${missing.join('/')}`);
  });

/** Open with the GPU fill messages held, then control frames and upload costs independently of the runner's speed. */
async function openControlledViewer(page, servers, url) {
  await page.clock.install();
  await page.addInitScript(() => {
    const messages = [];
    const NativeMessageChannel = window.MessageChannel;
    window.MessageChannel = class extends NativeMessageChannel {
      constructor() {
        super();
        this.port2.postMessage = (data) => messages.push(() => this.port1.onmessage?.(new MessageEvent('message', { data })));
      }
    };
    window.runFillTask = async () => {
      // Let the preceding frame's or slice's microtasks finish before delivering another message.
      await Promise.resolve();
      const message = messages.shift();
      if (!message) return false;
      message();
      return true;
    };
    window.drainFillTasks = async () => {
      for (let count = 0; await window.runFillTask(); count++) {
        if (count === 100) throw new Error('the GPU fill did not yield to a frame or finish');
      }
    };
  });
  await openViewer(page, servers, url);
  await loadStoreIntoMemory(page);
  // Let the initial camera settle and freeze wall-clock timers, including the scrub timer.
  await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
  await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const { renderer } = viewer;
    const frameMs = 1000 / 60;
    let now = performance.now();
    let frameStart = now;
    Object.defineProperty(performance, 'now', { value: () => now });
    window.advanceUploadClock = (ms) => { now += ms; };
    window.resetCounts = () => {
      window.frames = 0;
      window.fillSlices = [0];
      window.fillSliceStarts = [];
      window.frameStarts = [0];
      window.frameBudgets = [0];
    };
    window.resetCounts();
    let inSlice = false;
    const upload = renderer.upload;
    renderer.upload = function (...args) {
      if (args[3]?.background) {
        if (!inSlice) {
          inSlice = true;
          window.fillSlices[window.frames]++;
          window.fillSliceStarts.push({ frame: window.frames, at: performance.now() });
          // The microtask runs when the task that made the upload ends: the next upload in a task of its own is another slice.
          queueMicrotask(() => {
            inSlice = false;
          });
        }
        now += 1.5;
      }
      return upload.apply(this, args);
    };

    const queued = new Map();
    let nextId = -1;
    const realCancel = window.cancelAnimationFrame.bind(window);
    window.requestAnimationFrame = (callback) => {
      queued.set(nextId, callback);
      return nextId--;
    };
    // A frame that was asked for before this point is a real one, which the viewer may still cancel.
    window.cancelAnimationFrame = (id) => {
      if (!queued.delete(id)) realCancel(id);
    };
    window.runFrameCallbacks = () => {
      const callbacks = [...queued.keys()];
      frameStart = Math.max(now, frameStart + frameMs);
      now = frameStart;
      window.fillSlices[++window.frames] = 0;
      window.frameStarts[window.frames] = frameStart;
      for (const id of callbacks) {
        const callback = queued.get(id);
        queued.delete(id);
        callback?.(frameStart);
      }
      window.frameBudgets[window.frames] = viewer.playback.frameMs / 2;
    };
    window.runFrame = async () => {
      window.runFrameCallbacks();
      await window.drainFillTasks();
    };

    // Finish the idle fill from opening the store before the test starts counting slices.
    viewer.renderNow();
    await window.drainFillTasks();
    window.resetCounts();
  });
}

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

/** A movie at 30 steps/s, once it has the frames it starts with: until then it buffers, and the fill is not paced. */
const PLAY = async (viewer) => {
  viewer.setSpeed(30);
  viewer.play();
  for (let frames = 0; viewer.playback.buffering && frames < 20; frames++) await window.runFrame();
};

// What the user does in each frame while the fill runs, for 8 frames of the test's own making.
const GESTURES = {
  'a movie plays': { before: PLAY, each: () => {} },
  'the user scrubs in steps of one': { each: (viewer, i) => viewer.goToTime(1 + (i % 2)) },
  'the user drags the timeline in jumps': { each: (viewer, i) => viewer.goToTime(i % 2 === 0 ? 1 : 8) },
  'the camera moves in every frame': { each: (viewer, i) => viewer.setView({ center: { row: 100 + (i % 2) * 20, col: 100 } }) },
  'the camera moves in every frame while a movie plays': { before: PLAY, each: (viewer, i) => viewer.setView({ center: { row: 100 + (i % 2) * 20, col: 100 } }) },
};

for (const [gesture, { before, each }] of Object.entries(GESTURES)) {
  test(`while ${gesture} the GPU fill runs after the paint of a frame, for no longer than its share of it, and never between frames`, async ({ page, servers, storeUrl }) => {
    await openControlledViewer(page, servers, await storeUrl('link_plain'));

    const { perFrame, late, between } = await page.evaluate(
      async ({ before, each, frames }) => {
        const { viewer } = window.chronozarr;
        const run = (source) => new Function('viewer', 'i', `return (${source})(viewer, i);`); // eslint-disable-line no-new-func -- a function cannot cross into the page
        if (before) await run(before)(viewer);
        // With nothing on the GPU the fill has the window to upload.
        viewer.renderer.clearResident();
        window.resetCounts();
        for (let i = 0; i < frames; i++) {
          // Every frame leaves the whole window to upload, so that the fill has work in each of them.
          viewer.renderer.clearResident();
          run(each)(viewer, i);
          await window.runFrame();
        }
        const during = window.fillSlices.reduce((sum, n) => sum + n, 0);
        // No frame runs now, and neither the scrub window (300 ms) nor the camera's (250 ms) is over.
        window.advanceUploadClock(100);
        await window.drainFillTasks();
        // The first slice after a frame may start at any time; the ones after it only inside the budget.
        const seen = new Set();
        const late = window.fillSliceStarts.filter(({ frame, at }) => {
          const first = !seen.has(frame);
          seen.add(frame);
          return !first && at - window.frameStarts[frame] >= window.frameBudgets[frame];
        });
        return { perFrame: [...window.fillSlices], late, between: window.fillSlices.reduce((sum, n) => sum + n, 0) - during };
      },
      { before: before?.toString(), each: each.toString(), frames: FRAMES },
    );
    expect(perFrame[0], 'no slice ran before the first frame').toBe(0);
    expect(late, 'slices that began after half of their frame, other than the first').toEqual([]);
    // The frame that first asks for the fill is the first; from the second on there is a slice after every one.
    expect(perFrame.slice(2).every((slices) => slices >= 1), `the fill kept running, slices after each frame: ${perFrame}`).toBe(true);
    expect(between, 'no slice ran while no frame did').toBe(0);
  });
}

test('when nothing moves the GPU fill runs its slices one after the other, without waiting for a frame', async ({ page, servers, storeUrl }) => {
  await openControlledViewer(page, servers, await storeUrl('link_plain'));

  const { frames, slices, resident } = await page.evaluate(
    async (cells) => {
      const { viewer } = window.chronozarr;
      const { renderer } = viewer;
      renderer.clearResident();
      viewer.renderNow();
      await window.drainFillTasks();
      const resident = cells.every(([row, col]) => [1, 2, 3].every((t) => renderer.isResident(`0/${row}/${col}/${t}`)));
      return { frames: window.frames, slices: window.fillSlices[0], resident };
    },
    LEVEL_0_CELLS,
  );
  expect(frames, 'no animation frame ran').toBe(0);
  expect(resident, 'the next timesteps are on the GPU').toBe(true);
  expect(slices, 'in more than one slice').toBeGreaterThan(1);
});

test('after a camera move the GPU fill is paced: it waits for the frames and runs after them', async ({ page, servers, storeUrl }) => {
  await openControlledViewer(page, servers, await storeUrl('link_plain'));

  const { beforeFrames, afterFrames } = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    viewer.renderer.clearResident();
    viewer.setView({ center: { row: 100, col: 100 } });
    // The frame of the new view, drawn now: it asks for the fill, and the camera has just moved.
    viewer.renderNow();
    // The camera's window (250 ms) is not over, and no animation frame runs.
    window.advanceUploadClock(100);
    await window.drainFillTasks();
    const beforeFrames = window.fillSlices[0];
    for (let i = 0; i < 3; i++) await window.runFrame();
    return { beforeFrames, afterFrames: window.fillSlices.slice(1) };
  });
  expect(beforeFrames, 'no slice ran with no frame').toBe(0);
  expect(afterFrames.every((slices) => slices >= 1), `a slice after each of the frames: ${afterFrames}`).toBe(true);
});

for (const [change, act] of [
  ['a jump', (viewer) => viewer.goToTime(8)],
  ['a camera move', (viewer) => viewer.setView({ center: { row: 100, col: 100 } })],
]) {
  test(`${change} drops the GPU fill that waits for a message: it could run before the frame of the new view`, async ({ page, servers, storeUrl }) => {
    await openControlledViewer(page, servers, await storeUrl('link_plain'));

    const slices = await page.evaluate(
      async ({ act }) => {
        const { viewer } = window.chronozarr;
        viewer.renderer.clearResident();
        // The frame schedules the fill: it waits for its task.
        viewer.renderNow();
        // eslint-disable-next-line no-new-func -- the test passes the change as source: a function cannot cross into the page
        new Function('viewer', `return (${act})(viewer);`)(viewer);
        // The frame of the new view is held, and it is what would schedule the fill again.
        await window.drainFillTasks();
        return window.fillSlices[0];
      },
      { act: act.toString() },
    );
    expect(slices, 'the fill of the old window did not run').toBe(0);
  });
}

test('a camera move between paced slices drops the queued slice and fills after the new paint', async ({ page, servers, storeUrl }) => {
  await openControlledViewer(page, servers, await storeUrl('link_plain'));
  const { first, beforePaint, afterPaint } = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    viewer.renderer.clearResident();
    viewer.setView({ zoom: 8, center: { row: 100, col: 100 } });
    viewer.renderNow();
    window.runFrameCallbacks();
    await window.runFillTask();
    const first = window.fillSlices[1];

    viewer.setView({ center: { row: 400, col: 400 } });
    await window.drainFillTasks();
    const beforePaint = window.fillSlices[1];
    await window.runFrame();
    await window.runFrame();
    return { first, beforePaint, afterPaint: window.fillSlices[3] };
  });
  expect(first, 'one slice ran for the old view').toBe(1);
  expect(beforePaint, 'the queued continuation did not run before the new paint').toBe(first);
  expect(afterPaint, 'the new view still fills').toBeGreaterThan(0);
});

test('the order of the GPU window is worked out once for all the slices of a fill, and again when t changes', async ({ page, servers, storeUrl }) => {
  await openControlledViewer(page, servers, await storeUrl('link_plain'));

  const fills = await page.evaluate(
    async (cells) => {
      const { viewer } = window.chronozarr;
      const { renderer } = viewer;
      // windowOrder sorts an array with one entry per timestep. Its sorts count when the task that made one also uploads for the
      // fill: the prefetch plans with windowOrder too, in tasks of its own.
      const nTime = viewer.store.times.length;
      let sortsInTask = 0;
      let sortsOfFill = 0;
      let taskOpen = false;
      const openTask = () => {
        if (taskOpen) return;
        taskOpen = true;
        queueMicrotask(() => {
          taskOpen = false;
          sortsInTask = 0;
        });
      };
      const sort = Array.prototype.sort;
      Array.prototype.sort = function (...args) {
        if (this.length === nTime) {
          openTask();
          sortsInTask++;
        }
        return sort.apply(this, args);
      };
      const upload = renderer.upload;
      renderer.upload = function (...args) {
        if (args[3]?.background) {
          sortsOfFill += sortsInTask;
          sortsInTask = 0;
        }
        return upload.apply(this, args);
      };

      // The fill of the window ahead of timestep t, started from an empty GPU with t drawn and no scrub going on.
      const fill = async (t) => {
        const slicesBefore = window.fillSlices[0];
        sortsOfFill = 0;
        viewer.goToTime(t);
        viewer.endScrub();
        renderer.clearResident();
        viewer.renderNow();
        await window.drainFillTasks();
        const filled = cells.every(([row, col]) => [-1, 1, 2, 3].every((step) => renderer.isResident(`0/${row}/${col}/${t + step}`)));
        return { filled, sorts: sortsOfFill, slices: window.fillSlices[0] - slicesBefore };
      };
      const result = [await fill(7), await fill(3)];
      Array.prototype.sort = sort;
      return result;
    },
    LEVEL_0_CELLS,
  );
  for (const { filled, slices } of fills) {
    expect(filled, 'the window around t was filled').toBe(true);
    expect(slices, 'in several slices').toBeGreaterThanOrEqual(4);
  }
  expect(
    fills.map(({ sorts }) => sorts),
    'one sort of the time axis for each window, not one for each slice',
  ).toEqual([1, 1]);
});
