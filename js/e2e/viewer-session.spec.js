// The viewer's store session. While a store opens, and after an open fails, no store is open: nothing that the
// previous store left behind (a step, a zoom, a click, a queued render, a playing movie, an export, the sidebar) can act
// on the viewer, and every open settles, whether its first frame comes, cannot be had, or a newer open takes over.

import { existsSync } from 'node:fs';
import path from 'node:path';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { expect, test as base } from './fixtures.js';
import { writeTree } from './make-node-fixtures.mjs';
import { clickStorePixel, waitForSettled } from './viewer-helpers.js';

// The fixtures fail a test on any console error. A store that is not there, or a chunk that answers 503, is reported by
// the browser and the reader as errors, which the tests about it list (the pattern of viewer-open-url.spec.js).
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

const MISSING_STORE_ERRORS = /Failed to load resource|blocked by CORS policy|request failed, giving up/;

/** A store address whose root answers 404 with CORS headers, so that the reader reports "Store not found". */
async function missingStore(page, servers) {
  const url = `${servers.dataUrl}/no_such_store/`;
  await page.route(`${url}zarr.json`, (route) => route.fulfill({ status: 404, headers: { 'Access-Control-Allow-Origin': '*' } }));
  return url;
}

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

/** Open the viewer on `url` (with the extra query `query`) and wait for its first frame. */
async function openAt(page, servers, url, query = '') {
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}${query}`);
  await page.waitForFunction(() => window.chronozarr?.ready);
  expect(await page.evaluate(() => window.chronozarr.ready), 'the first store opened').toBeTruthy();
}

/** Record every `error` hook call of the viewer from now on, as its code. */
const recordErrorHooks = (page) =>
  page.evaluate(() => {
    window.errorHooks = [];
    window.chronozarr.viewer.hooks.error = ({ code }) => window.errorHooks.push(code);
  });

/** Console errors from now on, read back with `since()`. */
function watchConsole(page) {
  const errors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  const start = errors.length;
  return { since: () => errors.slice(start) };
}

/** Press ArrowRight, zoom in with the wheel over the canvas and click its centre. */
async function stepZoomAndClick(page) {
  await page.keyboard.press('ArrowRight');
  const box = await page.locator('#gl-canvas').boundingBox();
  const center = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  await page.mouse.move(center.x, center.y);
  await page.mouse.wheel(0, -200);
  await page.mouse.click(center.x, center.y);
}

const openingStore = (page, url, options = {}) =>
  page.evaluate(([url, options]) => {
    window.opening = window.chronozarr.viewer.loadStore(url, options);
    // The test reads the outcome later; an unhandled rejection must not reach the console first.
    window.opening.catch(() => {});
  }, [url, options]);

test.describe('an open that fails', () => {
  test.use({ expectedErrors: MISSING_STORE_ERRORS });

  test('leaves no store open: play and export are off, and a step, a zoom and a click do nothing', async ({ page, servers, storeUrl }) => {
    await openAt(page, servers, await storeUrl('u16_sharded'), '&t=2');
    const gone = await missingStore(page, servers);
    await recordErrorHooks(page);
    const failure = await page.evaluate(async (url) => {
      const { viewer } = window.chronozarr;
      // What the page does before an open (open() in viewer.js): the address bar names the store about to open, with no view.
      viewer.pinnedStore = url;
      viewer.resetUrl();
      return viewer.loadStore(url).then(() => 'resolved', () => 'rejected');
    }, gone);
    expect(failure).toBe('rejected');

    const consoleErrors = watchConsole(page);
    await stepZoomAndClick(page);
    // A render is one animation frame away and the address bar sync 300 ms: give both time to happen if they were going to.
    await page.waitForTimeout(1000);

    const state = await page.evaluate(() => {
      const { viewer } = window.chronozarr;
      const params = new URLSearchParams(location.search);
      const overlay = document.getElementById('error-overlay');
      return {
        storeOpen: viewer.store !== null,
        playDisabled: document.getElementById('play-btn').disabled,
        exportDisabled: document.getElementById('export-btn').disabled,
        errorHooks: window.errorHooks,
        box: { visible: overlay.classList.contains('visible'), toast: overlay.classList.contains('toast'), title: document.getElementById('error-title').textContent },
        view: { t: params.get('t'), z: params.get('z') },
      };
    });
    expect(state).toEqual({
      storeOpen: false,
      playDisabled: true,
      exportDisabled: true,
      errorHooks: ['store_open_failed'],
      box: { visible: true, toast: false, title: 'Could not open store' },
      view: { t: null, z: null },
    });
    expect(consoleErrors.since(), 'console errors after the failed open').toEqual([]);
  });

  test('leaves the sidebar empty after a pixel of the previous store was clicked', async ({ page, servers, storeUrl }) => {
    await openAt(page, servers, await storeUrl('u16_sharded'));
    await clickStorePixel(page, 50, 40);
    await expect(page.locator('#sidebar-content')).toBeVisible();
    const gone = await missingStore(page, servers);
    expect(await page.evaluate((url) => window.chronozarr.viewer.loadStore(url).then(() => 'resolved', () => 'rejected'), gone)).toBe('rejected');
    await expect(page.locator('#sidebar-content')).toBeHidden();
    await expect(page.locator('#sidebar-content')).toBeEmpty();
    await expect(page.locator('#sidebar-empty')).toBeVisible();
  });
});

/** The URLs of the requests the page sends from now on whose URL matches `pattern`, each once. */
function recordRequests(page, pattern) {
  const urls = new Set();
  page.on('request', (request) => {
    if (pattern.test(request.url())) urls.add(request.url());
  });
  return urls;
}

test('a step, a zoom and a click while a store opens reach no store, and the new store asks for its first frame at once', async ({ page, servers, storeUrl }) => {
  await page.clock.install();
  await openAt(page, servers, await storeUrl('u16_sharded'), '&t=2');
  const next = await storeUrl('u16_plain');
  await recordErrorHooks(page);
  const root = await holdRequests(page, `${next}zarr.json`);
  // The new store opens at the timestep the step goes to (3), at the same level: a wave of requests that the step had
  // left behind for the previous store would be taken for the new store's own.
  await openingStore(page, next, { viewSearch: '?t=3' });
  await expect.poll(() => root.held.length).toBe(1);
  // The previous store's prefetch may have sent a request just before the open: its event can still be on its way.
  await page.waitForTimeout(200);
  const toPrevious = recordRequests(page, /\/u16_sharded\//);

  await stepZoomAndClick(page);
  // The step is a scrub, which ends 300 ms after it: by then the view has settled on the normal level.
  await page.waitForTimeout(600);
  const during = await page.evaluate(() => ({ storeOpen: window.chronozarr.viewer.store !== null, errorHooks: window.errorHooks, toast: document.getElementById('error-overlay').classList.contains('toast') }));
  expect.soft(during).toEqual({ storeOpen: false, errorHooks: [], toast: false });
  expect.soft([...toPrevious], 'requests to the previous store').toEqual([]);

  // From here no timer fires: the first frame of the new store has to be asked for by the open itself, not by a retry
  // timer or a prefetch timer.
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 10));
  const firstFrame = recordRequests(page, /\/u16_plain\/0\/data\/c\/3\//);
  await root.release();
  await expect.poll(() => firstFrame.size, { message: 'requests for the four cells of the first frame', timeout: 5000 }).toBe(4);
  await page.clock.resume();
  const result = await page.evaluate(() => window.opening);
  expect(result?.firstPaintMs, 'the new store painted its first frame').toEqual(expect.any(Number));
  expect(await page.evaluate(() => window.chronozarr.viewer.paintedT)).toBe(3);
});

test('fit, prefetchNow and isTimestepReady do nothing while a store opens, and createExportSession says that none is open', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_sharded'));
  const next = await storeUrl('u16_plain');
  const root = await holdRequests(page, `${next}zarr.json`);
  await openingStore(page, next);
  await expect.poll(() => root.held.length).toBe(1);

  const outcomes = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const attempt = async (call) => {
      try {
        return { value: (await call()) ?? null };
      } catch (error) {
        return { error: `${error.name}: ${error.message}` };
      }
    };
    return {
      fit: await attempt(() => viewer.fit()),
      prefetchNow: await attempt(() => viewer.prefetchNow()),
      isTimestepReady: await attempt(() => viewer.isTimestepReady(0)),
      createExportSession: await attempt(() => viewer.createExportSession()),
    };
  });
  expect(outcomes).toEqual({
    fit: { value: null },
    prefetchNow: { value: null },
    isTimestepReady: { value: false },
    createExportSession: { error: 'Error: createExportSession needs an open store: none is open' },
  });

  await root.release();
  expect(await page.evaluate(() => window.opening)).toBeTruthy();
});

test('Space while a store opens starts no movie', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_sharded'), '&t=2');
  const next = await storeUrl('u16_plain');
  await recordErrorHooks(page);
  const root = await holdRequests(page, `${next}zarr.json`);
  await openingStore(page, next);
  await expect.poll(() => root.held.length).toBe(1);

  await page.keyboard.press('Space');
  await page.waitForTimeout(300);
  const during = await page.evaluate(() => ({ playing: window.chronozarr.viewer.playback.playing, errorHooks: window.errorHooks }));
  expect(during).toEqual({ playing: false, errorHooks: [] });

  await root.release();
  expect(await page.evaluate(() => window.opening)).toBeTruthy();
  const after = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const seen = new Set();
    for (let frame = 0; frame < 90; frame++) {
      seen.add(viewer.t);
      await new Promise((resolve) => requestAnimationFrame(resolve));
    }
    return { playing: viewer.playback.playing, timesteps: [...seen] };
  });
  expect(after).toEqual({ playing: false, timesteps: [0] });
});

test('a switch while a movie plays keeps the progress bar of the open, and the new store opens paused', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_sharded'));
  const next = await storeUrl('u16_plain');
  await page.evaluate(() => window.chronozarr.viewer.play());
  await page.waitForFunction(() => window.chronozarr.viewer.playback.stats.steps >= 2);
  const root = await holdRequests(page, `${next}zarr.json`);
  await openingStore(page, next);
  await expect.poll(() => root.held.length).toBe(1);

  // Longer than the progress bar's fade (200 ms) and a few animation frames.
  await page.waitForTimeout(500);
  expect(await page.evaluate(() => document.getElementById('progress-fill').style.opacity), 'the progress bar while the store opens').toBe('1');

  await root.release();
  expect(await page.evaluate(() => window.opening)).toBeTruthy();
  expect(await page.evaluate(() => window.chronozarr.viewer.playback.playing)).toBe(false);
});

test('a switch while a movie plays shows no store from the first status that is not open', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_sharded'));
  const next = await storeUrl('u16_plain');
  await page.evaluate(() => window.chronozarr.viewer.play());
  await page.waitForFunction(() => window.chronozarr.viewer.playback.stats.steps >= 2);
  const records = await page.evaluate(async (url) => {
    const { viewer } = window.chronozarr;
    const log = [];
    viewer.hooks = { status: () => log.push([viewer.status.store, viewer.store === null]) };
    await viewer.loadStore(url);
    return log;
  }, next);
  for (const [store, noStore] of records) expect(noStore, `viewer.store is null exactly when the status store is not open (status store ${store})`).toBe(store !== 'open');
  expect(records.map(([store]) => store)).toContain('opening');
});

test('a timeline drag held while a store opens reaches no store', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_sharded'));
  const next = await storeUrl('u16_plain');
  const root = await holdRequests(page, `${next}zarr.json`);
  const box = await page.locator('#timeline-track').boundingBox();
  const y = box.y + box.height / 2;
  await page.mouse.move(box.x + box.width * 0.25, y);
  await page.mouse.down();
  await openingStore(page, next);
  await expect.poll(() => root.held.length).toBe(1);
  expect(await page.evaluate(() => window.chronozarr.viewer.status.store)).toBe('opening');
  await page.mouse.move(box.x + box.width * 0.5, y);
  await page.mouse.move(box.x + box.width * 0.75, y);
  await page.mouse.up();
  await root.release();
  expect(await page.evaluate(() => window.opening)).toBeTruthy();
});

test('the progress bar of a load during playback fades while the movie goes on', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_plain'));
  const level1 = await holdRequests(page, /\/u16_plain\/1\/data\/c\//);
  await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    viewer.setSpeed(15);
    viewer.play();
    viewer.lodOverride = 1;
    viewer.renderNow();
  });
  await expect.poll(() => level1.held.length).toBeGreaterThan(0);
  await level1.release();

  // Every frame the movie shows says the load is complete: the bar fades 200 ms after the first of them, not after the last.
  await expect.poll(() => page.evaluate(() => document.getElementById('progress-fill').style.opacity), { timeout: 3000 }).toBe('0');
  const steps = await page.evaluate(() => window.chronozarr.viewer.playback.stats.steps);
  await page.waitForFunction((before) => window.chronozarr.viewer.playback.stats.steps > before + 3, steps);
  expect(await page.evaluate(() => window.chronozarr.viewer.playback.playing), 'the movie still plays').toBe(true);
});

test.describe('two opens that overlap', () => {
  test.use({ expectedErrors: MISSING_STORE_ERRORS });

  test('the first resolves null and the second with its own timings', async ({ page, servers, storeUrl }) => {
    await openAt(page, servers, await storeUrl('u16_sharded'));
    const second = await storeUrl('u16_plain');
    const third = await storeUrl('u8_rgb');
    const chunks = await holdRequests(page, /\/u16_plain\/\d+\/data\/c\//);
    await openingStore(page, second);
    await expect.poll(() => chunks.held.length).toBeGreaterThan(0);

    const [first, latest] = await page.evaluate((url) => Promise.all([window.opening, window.chronozarr.viewer.loadStore(url)]), third);
    expect(first, 'the open that was superseded').toBeNull();
    expect(latest?.firstPaintMs, 'the open that took over').toEqual(expect.any(Number));
    expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(third);
    await chunks.release();
  });

  test('the first settles when the second fails to open', async ({ page, servers, storeUrl }) => {
    await openAt(page, servers, await storeUrl('u16_sharded'));
    const second = await storeUrl('u16_plain');
    const gone = await missingStore(page, servers);
    const chunks = await holdRequests(page, /\/u16_plain\/\d+\/data\/c\//);
    await openingStore(page, second);
    await expect.poll(() => chunks.held.length).toBeGreaterThan(0);

    const outcome = await page.evaluate(async (url) => {
      const latest = await window.chronozarr.viewer.loadStore(url).then(() => 'resolved', () => 'rejected');
      const first = await Promise.race([window.opening, new Promise((resolve) => setTimeout(() => resolve('pending'), 2000))]);
      return { first, latest };
    }, gone);
    expect(outcome).toEqual({ first: null, latest: 'rejected' });
    await chunks.release();
  });
});

test.describe('a store whose first frame cannot be had', () => {
  test.use({ expectedErrors: /Failed to load resource: .*503|request failed, giving up|chunk load failed|prefetch failed for chunk/ });

  test('opens, and its open settles without a first frame', async ({ page, servers, storeUrl }) => {
    test.setTimeout(120_000);
    const url = await storeUrl('u16_plain');
    await page.route(/\/u16_plain\/\d+\/data\/c\//, (route) => route.fulfill({ status: 503, headers: { 'Access-Control-Allow-Origin': '*' } }));
    await page.clock.install();
    await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
    await page.waitForFunction(() => Boolean(window.chronozarr?.ready));
    await page.evaluate(() => {
      window.openOutcome = 'pending';
      window.chronozarr.ready.then((result) => {
        window.openOutcome = result;
      });
    });
    // The retries of a cell wait 4, 8 and 12 s, and the reader retries each request too: run the clock past them.
    await expect
      .poll(
        async () => {
          await page.clock.runFor(2000);
          return page.evaluate(() => window.openOutcome === 'pending');
        },
        { timeout: 60_000, intervals: [100] },
      )
      .toBe(false);
    const { outcome, store } = await page.evaluate(() => ({ outcome: window.openOutcome, store: window.chronozarr.viewer.store?.url ?? null }));
    expect(outcome, 'loadStore resolved with its timings').toEqual(expect.objectContaining({ firstPaintMs: null, openMs: expect.any(Number) }));
    expect(store, 'the store is open').toBe(url);
  });
});

test('a coverage read of the gap overlay that a store switch ends logs no error', async ({ page, servers, stores, storeUrl }) => {
  // One cell of one band, with a coverage variable: the gap overlay reads the coverage of the cell on screen.
  const dir = path.join(stores.dir, 'session-coverage');
  if (!existsSync(dir)) {
    const spec = { dtype: 'uint8', nTime: 4, nBand: 1, height: 128, width: 128, chunk: 128, sharded: false, nLevels: 1, consolidated: true, specVersion: '0.3.0', nodata: null, coverage: true, bandObjects: [{ name: 'depth', units: 'm', scale: 1 }] };
    await writeTree(dir, buildSyntheticStore(spec).files);
  }
  const coverage = await holdRequests(page, /\/session-coverage\/.*coverage\//);
  await openAt(page, servers, `${servers.dataUrl}/session-coverage/`);
  await page.evaluate(() => window.chronozarr.viewer.toggleGaps());
  await expect.poll(() => coverage.held.length, 'the gap overlay asks for the coverage of the cell').toBeGreaterThan(0);

  // The switch closes the store, which ends the coverage read in flight.
  expect(await page.evaluate((url) => window.chronozarr.viewer.loadStore(url), await storeUrl('u16_plain'))).toBeTruthy();
  await coverage.release();
  expect(await waitForSettled(page)).toEqual(expect.objectContaining({ store: 'open', frame: 'settled' }));
});

test('an export of the previous store does not keep the prefetch of the next one off', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_sharded'));
  const next = await storeUrl('u16_plain');
  // An export session that is never closed holds the prefetch off while it lasts.
  await page.evaluate(() => {
    window.exportSession = window.chronozarr.viewer.createExportSession();
  });
  const before = servers.data.requests.length;
  expect(await page.evaluate((url) => window.chronozarr.viewer.loadStore(url), next)).toBeTruthy();
  // The first frame is timestep 0: a request for any other timestep of the new store is the prefetch's.
  await expect
    .poll(() => servers.data.requests.slice(before).some((request) => /^\/u16_plain\/\d+\/data\/c\/[1-3]\//.test(request.path)), { timeout: 5000 })
    .toBe(true);
});
