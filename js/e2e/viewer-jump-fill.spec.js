// What a jump in time does before the frame is drawn: when it asks for its chunks, and what has reached the GPU by the
// time the last chunk lands.

import { expect, test } from './fixtures.js';
import { openViewer, waitForPaintedTime } from './viewer-helpers.js';

test.use({ viewport: { width: 1280, height: 720 } });

/** Stop animation frames from running: the page keeps them queued until `releaseFrames`. Whatever happens in between happens in the calls of the test. */
const holdFrames = (page) =>
  page.evaluate(() => {
    window.heldFrames = [];
    window.realRequestAnimationFrame = window.requestAnimationFrame;
    window.requestAnimationFrame = (callback) => window.heldFrames.push(callback);
  });

const releaseFrames = (page) =>
  page.evaluate(() => {
    window.requestAnimationFrame = window.realRequestAnimationFrame;
    for (const callback of window.heldFrames.splice(0)) window.requestAnimationFrame(callback);
  });

test('a jump in time asks for its chunks in the call, not on the next animation frame', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('beyond_idle');
  // The last timestep is not answered until the end of the test, so it cannot be in memory however long the idle prefetch has run.
  const held = [];
  await page.route(`${servers.dataUrl}/beyond_idle/**/c/9/**`, (route) => {
    held.push(route);
  });
  await openViewer(page, servers, url);

  expect(held.length, 'nothing has asked for the last timestep yet').toBe(0);

  await holdFrames(page);
  const before = await page.evaluate(() => window.chronozarr.viewer.frameStats.painted);
  await page.evaluate(() => window.chronozarr.viewer.goToTime(9));

  await expect.poll(() => held.length, { message: 'the chunks of the target timestep were requested while no animation frame ran' }).toBeGreaterThan(0);
  const after = await page.evaluate(() => ({ painted: window.chronozarr.viewer.frameStats.painted, shown: window.chronozarr.viewer.shownFrame }));
  expect(after.painted, 'nothing was drawn in the meantime').toBe(before);
  expect(after.shown.t, 'the canvas still shows the first timestep').toBe(0);
  await releaseFrames(page);
  await Promise.all(held.map((route) => route.continue()));
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
