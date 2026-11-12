// The viewer's status: one value that says whether a store is open, whether the frame asked for is on the canvas, how
// playback goes, whether an export holds the cache and what notice is on screen (viewer.status), one promise to wait on
// (viewer.whenSettled()), the data-store, data-frame and data-playback attributes of <html>, and the `status` hook.

import { expect, test as base } from './fixtures.js';
import { openViewer } from './viewer-helpers.js';

// The fixtures fail a test on any console error. A store that is not there, or a chunk that answers 503, is reported by
// the browser and the reader as errors, which the tests about it list.
const test = base.extend({
  expectedErrors: [null, { option: true }],
  consoleErrors: [
    async ({ page, expectedErrors }, use) => {
      const errors = [];
      page.on('console', (message) => {
        if (message.type() === 'error') errors.push(`console.error: ${message.text()}`);
      });
      page.on('pageerror', (error) => errors.push(`uncaught: ${error.stack || error.message}`));
      await use(errors);
      expect(
        errors.filter((error) => !expectedErrors?.test(error)),
        'the page logged errors',
      ).toEqual([]);
    },
    { auto: true },
  ],
});

/** Hold the requests that match `pattern` unanswered; `release` lets them go (the page may have given up on some meanwhile). */
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

const readStatus = (page) => page.evaluate(() => window.chronozarr.viewer.status);

/** The data-* attributes of <html> that publish the status. */
const readAttributes = (page) =>
  page.evaluate(() => {
    const { store, frame, playback } = document.documentElement.dataset;
    return { store, frame, playback };
  });

/** Call whenSettled() and keep its outcome in window.settled: 'pending' until it resolves, then the status it resolved with. */
const startSettledWait = (page) =>
  page.evaluate(() => {
    window.settled = 'pending';
    window.chronozarr.viewer.whenSettled().then((status) => {
      window.settled = status;
    });
  });

/** Whether whenSettled() resolves before a timer of 0 ms fires: at once, when nothing is pending. */
const settlesAtOnce = (page) =>
  page.evaluate(() => Promise.race([window.chronozarr.viewer.whenSettled().then(() => 'resolved'), new Promise((resolve) => setTimeout(() => resolve('pending'), 0))]));

/** Record the frame value of the status each time the `status` hook is called. */
const recordStatusHook = (page) =>
  page.evaluate(() => {
    window.statusCalls = [];
    const { viewer } = window.chronozarr;
    viewer.hooks.status = () => window.statusCalls.push(viewer.status);
  });

/** Wait for `count` animation frames of the page. */
const frames = (page, count) =>
  page.evaluate(async (n) => {
    for (let i = 0; i < n; i++) await new Promise((resolve) => requestAnimationFrame(resolve));
  }, count);

test('after a store opens it is open, settled and paused, the attributes say so, and whenSettled resolves at once', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  await page.evaluate(() => window.chronozarr.viewer.whenSettled());
  const status = await readStatus(page);
  expect(status).toEqual({ store: 'open', frame: 'settled', playback: 'paused', exporting: false, t: 0, shownT: 0, level: 0, error: null });
  expect(await readAttributes(page)).toEqual({ store: 'open', frame: 'settled', playback: 'paused' });
  expect(await settlesAtOnce(page)).toBe('resolved');
});

test('a step to a timestep whose chunks are held loads until they arrive, and whenSettled waits for it', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('u16_plain');
  const chunks = await holdRequests(page, /\/u16_plain\/\d+\/data\/c\/3\//);
  await openViewer(page, servers, url);
  const right = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    viewer.goToTime(3);
    return viewer.status.frame;
  });
  expect(right, 'the frame right after the call').toBe('loading');
  await startSettledWait(page);
  // Past the end of the scrub that a manual step starts (300 ms) and several frames.
  await page.waitForTimeout(500);
  expect((await readStatus(page)).frame, 'the frame while the chunks are held').toBe('loading');
  expect((await readAttributes(page)).frame).toBe('loading');
  expect(await page.evaluate(() => window.settled), 'whenSettled while the chunks are held').toBe('pending');

  await chunks.release();
  await page.waitForFunction(() => window.settled !== 'pending');
  const settled = await page.evaluate(() => window.settled);
  expect(settled).toEqual(expect.objectContaining({ store: 'open', frame: 'settled', t: 3, shownT: 3 }));
  expect((await readAttributes(page)).frame).toBe('settled');
});

test('a zoom to a finer level whose chunks are held loads while paintedT still equals t', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('beyond_idle');
  const level0 = await holdRequests(page, /\/beyond_idle\/0\/data\/c\//);
  await openViewer(page, servers, url);
  await page.evaluate(() => window.chronozarr.viewer.whenSettled());
  expect((await readStatus(page)).level, 'the fitted view draws the coarser level').toBe(1);

  await page.evaluate(() => window.chronozarr.viewer.setView({ zoom: 2 }));
  await startSettledWait(page);
  await frames(page, 5);
  const during = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    return { frame: viewer.status.frame, paintedAtT: viewer.paintedT === viewer.t, settled: window.settled };
  });
  expect(during, 'the finer level is held: paintedT still says the timestep is painted, the status does not').toEqual({ frame: 'loading', paintedAtT: true, settled: 'pending' });

  await level0.release();
  await page.waitForFunction(() => window.settled !== 'pending', null, { timeout: 30_000 });
  expect(await page.evaluate(() => window.settled)).toEqual(expect.objectContaining({ frame: 'settled', level: 0, shownT: 0 }));
});

test('a fast scrub that draws a coarser level is a preview, and settles once the scrub ends without a call from the test', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('link_plain');
  // The finer level of every timestep but the first is held, so the scrub can only draw the coarser one.
  const level0 = await holdRequests(page, /\/link_plain\/0\/data\/c\/([1-9]|1[01])\//);
  await openViewer(page, servers, url);
  await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const { store } = viewer;
    await Promise.all(store.times.map((_, t) => store.ensureRaw(1, 0, 0, t)));
    await viewer.whenSettled();
  });
  await recordStatusHook(page);
  // Two steps less than 300 ms apart are a scrub (a single step is a tap).
  await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    viewer.goToTime(1);
    viewer.goToTime(2);
  });
  await page.waitForFunction(() => window.statusCalls.some((status) => status.frame === 'preview'));
  const preview = await page.evaluate(() => window.statusCalls.find((status) => status.frame === 'preview'));
  expect(preview).toEqual(expect.objectContaining({ t: 2, shownT: 2, level: 1 }));
  await startSettledWait(page);
  expect(await page.evaluate(() => window.settled), 'a preview is not an end state').toBe('pending');

  await level0.release();
  await page.waitForFunction(() => window.settled !== 'pending', null, { timeout: 30_000 });
  expect(await page.evaluate(() => window.settled)).toEqual(expect.objectContaining({ frame: 'settled', t: 2, shownT: 2, level: 0 }));
});

test.describe('a frame whose cells fail for good', () => {
  test.use({ expectedErrors: /Failed to load resource: .*503|request failed, giving up|chunk load failed|prefetch failed for chunk/ });

  test('is unavailable, and whenSettled resolves with it', async ({ page, servers, storeUrl }) => {
    test.setTimeout(120_000);
    const url = await storeUrl('u16_plain');
    await page.route(/\/u16_plain\/\d+\/data\/c\/3\//, (route) => route.fulfill({ status: 503, headers: { 'Access-Control-Allow-Origin': '*' } }));
    await page.clock.install();
    await openViewer(page, servers, url);
    await page.evaluate(() => window.chronozarr.viewer.goToTime(3));
    await startSettledWait(page);
    // The retries of a cell wait 4, 8 and 12 s, and the reader retries each request too: run the clock past them.
    await expect
      .poll(
        async () => {
          await page.clock.runFor(2000);
          return page.evaluate(() => window.settled === 'pending');
        },
        { timeout: 60_000, intervals: [100] },
      )
      .toBe(false);
    expect(await page.evaluate(() => window.settled)).toEqual(expect.objectContaining({ store: 'open', frame: 'unavailable', t: 3 }));
    expect((await readAttributes(page)).frame).toBe('unavailable');
  });
});

test.describe('an open that fails', () => {
  test.use({ expectedErrors: /Failed to load resource|blocked by CORS policy|request failed, giving up/ });

  test('leaves the store failed with no frame and the error of the open, and whenSettled resolves', async ({ page, servers, storeUrl }) => {
    await openViewer(page, servers, await storeUrl('u16_plain'));
    const gone = `${servers.dataUrl}/no_such_store/`;
    await page.route(`${gone}zarr.json`, (route) => route.fulfill({ status: 404, headers: { 'Access-Control-Allow-Origin': '*' } }));
    expect(await page.evaluate((url) => window.chronozarr.viewer.loadStore(url).then(() => 'resolved', () => 'rejected'), gone)).toBe('rejected');
    const status = await readStatus(page);
    expect(status).toEqual(expect.objectContaining({ store: 'failed', frame: 'none', playback: 'paused', error: { code: 'store_open_failed', toast: false } }));
    expect(await readAttributes(page)).toEqual({ store: 'failed', frame: 'none', playback: 'paused' });
    expect(await settlesAtOnce(page)).toBe('resolved');
  });
});

test('playback is starting or playing after play and paused after pause, and data-playback follows', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  const started = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    viewer.play();
    return [viewer.status.playback, document.documentElement.dataset.playback];
  });
  expect(['starting', 'playing']).toContain(started[0]);
  expect(started[1]).toBe(started[0]);
  await page.waitForFunction(() => document.documentElement.dataset.playback === 'playing');
  expect((await readStatus(page)).playback).toBe('playing');

  await page.evaluate(() => window.chronozarr.viewer.pause());
  expect((await readStatus(page)).playback).toBe('paused');
  expect((await readAttributes(page)).playback).toBe('paused');
});

test('the status hook is called when a frame starts loading and settles, when playback starts, and after a product change', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('u16_plain');
  const chunks = await holdRequests(page, /\/u16_plain\/\d+\/data\/c\/3\//);
  await openViewer(page, servers, url);
  await page.evaluate(() => window.chronozarr.viewer.whenSettled());
  await recordStatusHook(page);

  await page.evaluate(() => window.chronozarr.viewer.goToTime(3));
  await page.waitForFunction(() => window.statusCalls.some((status) => status.frame === 'loading'));
  await chunks.release();
  await page.waitForFunction(() => window.statusCalls.some((status) => status.frame === 'settled' && status.shownT === 3));

  await page.evaluate(() => {
    window.statusCalls = [];
    window.chronozarr.viewer.play();
  });
  await page.waitForFunction(() => window.statusCalls.some((status) => status.playback !== 'paused'));
  await page.evaluate(() => window.chronozarr.viewer.pause());

  const afterProduct = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    const before = window.statusCalls.length;
    const other = viewer.products.findIndex((product, index) => product.available && index !== viewer.productIndex);
    viewer.setProduct(other);
    return window.statusCalls.length - before;
  });
  expect(afterProduct, 'calls of the hook by setProduct').toBeGreaterThan(0);
});
