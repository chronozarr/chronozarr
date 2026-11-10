// The chart of a clicked pixel reads one chunk per timestep of its cell. It takes timesteps from the cache while a movie
// plays or a scrub runs, and reads one missing timestep in the background if the cache stops providing them.
//
// A single-cell store of 60 timesteps with every chunk answered after 120 ms, so the chart's batches of four take a good
// second to cover it and a scrub of a second and a half falls inside that time.

import { existsSync } from 'node:fs';
import path from 'node:path';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { expect, test } from './fixtures.js';
import { writeTree } from './make-node-fixtures.mjs';

const N_TIME = 60;
const CHART_BATCH = 4;
const CHUNK_DELAY_MS = 120;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SPEC = { nTime: N_TIME, nBand: 1, height: 128, width: 128, chunk: 128, sharded: false, nLevels: 1, consolidated: true, specVersion: '0.3.0', nodata: 0 };

async function writeChartStore(dir) {
  const store = path.join(dir, 'chart-yield');
  if (!existsSync(store)) await writeTree(store, buildSyntheticStore(SPEC).files);
}

/** How many timesteps the chart has loaded: the number in its status line. */
const chartLoaded = (page) => page.evaluate(() => Number(/(\d+) of \d+ timesteps/.exec(document.getElementById('chart-status').textContent)?.[1] ?? NaN));

/**
 * Record requests from the chart and the visible frame. Prefetch does not call either of these methods.
 */
const watchReads = (page) =>
  page.evaluate(() => {
    const { store } = window.chronozarr.viewer;
    window.__asked = [];
    for (const method of ['ensureRaw', 'readPixels']) {
      const read = store[method].bind(store);
      store[method] = (...args) => {
        window.__asked.push(args[3]);
        return read(...args);
      };
    }
  });

/** How far ahead of the timestep on screen the farthest timestep asked for lies. */
const farthestAhead = (page) => page.evaluate(() => Math.max(...window.__asked) - window.chronozarr.viewer.t);

test.describe('chart over a slow single-cell store', () => {
  // The chunks of the last timestep arrive when a test lets them, if it holds them: the frame of that timestep keeps loading.
  let lastTimestep;

  // A small canvas: software GL draws it in a few ms, so that the page keeps up with a scrub on a busy runner.
  test.use({ viewport: { width: 480, height: 360 } });

  test.beforeEach(async ({ page, servers, stores }) => {
    lastTimestep = null;
    await writeChartStore(stores.dir);
    await page.route(`${servers.dataUrl}/**/c/**`, async (route) => {
      await sleep(CHUNK_DELAY_MS);
      if (lastTimestep && Number(/\/c\/(\d+)\//.exec(new URL(route.request().url()).pathname)?.[1]) === N_TIME - 1) await lastTimestep.promise;
      await route.continue();
    });
    await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(`${servers.dataUrl}/chart-yield/`)}`);
    await page.waitForFunction(() => window.chronozarr?.viewer?.paintedT === 0, null, { timeout: 30_000 });
    const box = await page.locator('#gl-canvas').boundingBox();
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await expect(page.locator('#chart-status')).toContainText('loading');
  });

  test('the chart takes what is in the cache and finishes while the scrub goes on', async ({ page }) => {
    await watchReads(page);
    // Arrow keys 90 ms apart, pressed by a timer in the page so that a busy runner cannot stretch the gaps past the 300 ms of a scrub.
    await page.evaluate(() => {
      window.__keys = setInterval(() => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })), 90);
    });
    // The reader's idle prefetch brings the other timesteps into the cache while the chart waits; the chart's own reads would have asked for them.
    await expect(page.locator('#chart-status')).toHaveText(`${N_TIME} of ${N_TIME} timesteps · click to jump`, { timeout: 4000 });
    const ahead = await farthestAhead(page);
    await page.evaluate(() => clearInterval(window.__keys));
    expect(ahead, 'timesteps asked for beyond the one on screen while the scrub went on').toBeLessThan(CHART_BATCH * 3);
  });

  test('the chart takes what is in the cache and finishes while the movie plays', async ({ page }) => {
    await watchReads(page);
    await page.keyboard.press('Space');
    await page.waitForFunction(() => window.chronozarr.viewer.playback.playing);
    await expect(page.locator('#chart-status')).toHaveText(`${N_TIME} of ${N_TIME} timesteps · click to jump`, { timeout: 10_000 });
    expect(await page.evaluate(() => window.chronozarr.viewer.playback.playing), 'the movie is still playing').toBe(true);
    // The movie asks for its next frames (a buffer of a few seconds at 4 steps a second).
    expect(await farthestAhead(page), 'timesteps asked for beyond the one on screen while the movie played').toBeLessThan(N_TIME / 2);
  });

  test('the chart reads one timestep at a time while the frame the user stopped on loads, and in batches again once it is in', async ({ page }) => {
    lastTimestep = {};
    lastTimestep.promise = new Promise((resolve) => {
      lastTimestep.release = resolve;
    });
    // Count only the chart's reads in flight.
    await page.evaluate(() => {
      const { store } = window.chronozarr.viewer;
      const readPixels = store.readPixels.bind(store);
      const reads = { inflight: 0, max: 0 };
      store.readPixels = (...args) => {
        reads.max = Math.max(reads.max, ++reads.inflight);
        return readPixels(...args).finally(() => reads.inflight--);
      };
      window.__reads = reads;
    });
    // A jump to the last timestep: its frame cannot be drawn until its chunk arrives. The scrub of the jump is over 300 ms later,
    // and the batch of four that was in flight lands within a chunk delay.
    await page.evaluate((t) => window.chronozarr.viewer.goToTime(t), N_TIME - 1);
    await sleep(700);
    await page.evaluate(() => {
      window.__reads.max = window.__reads.inflight;
    });
    const during = await chartLoaded(page);
    await sleep(1000);
    const after = await chartLoaded(page);
    expect(await page.evaluate(() => window.__reads.max), 'chart reads in flight while the frame was loading').toBeLessThanOrEqual(1);
    expect(after - during, 'the chart goes on, a timestep at a time').toBeGreaterThanOrEqual(3);
    expect(after, 'the chart is not done yet').toBeLessThan(N_TIME);

    lastTimestep.release();
    await expect(page.locator('#chart-status')).toContainText(`${N_TIME} of ${N_TIME} timesteps`, { timeout: 30_000 });
    expect(await page.evaluate(() => window.__reads.max), 'chart reads in flight once the frame was in').toBeGreaterThan(1);
  });
});

test('a chart with warm imagery reads cold coverage and finishes while playback continues', async ({ page, servers, stores }) => {
  const nTime = 4;
  const dir = path.join(stores.dir, 'chart-coverage');
  await writeTree(dir, buildSyntheticStore({ ...SPEC, nTime, coverage: true }).files);
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(`${servers.dataUrl}/chart-coverage/`)}`);
  await page.waitForFunction(() => window.chronozarr?.viewer?.paintedT === 0);
  const state = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const { store } = viewer;
    await Promise.all(store.times.map((_, t) => store.ensureRaw(0, 0, 0, t)));
    const coverage = store.getCoverage.bind(store);
    const reads = { count: 0, inflight: 0, max: 0 };
    store.getCoverage = (lod, row, col, t, options) => {
      if (!options?.background) return coverage(lod, row, col, t, options);
      reads.count++;
      reads.max = Math.max(reads.max, ++reads.inflight);
      return coverage(lod, row, col, t, options).finally(() => reads.inflight--);
    };
    window.__coverageReads = reads;
    viewer.play();
    return { playing: viewer.playback.playing, warmCoverage: store.times.some((_, t) => store.peekCoverage(0, 0, 0, t)) };
  });
  expect(state).toEqual({ playing: true, warmCoverage: false });
  const box = await page.locator('#gl-canvas').boundingBox();
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
  await expect(page.locator('#chart-status')).toHaveText(/^4 of 4 timesteps( · \d+ gap-filled)? · click to jump$/, { timeout: 15_000 });
  expect(await page.evaluate(() => window.chronozarr.viewer.playback.playing)).toBe(true);
  expect(await page.evaluate(() => window.__coverageReads)).toEqual({ count: nTime - 1, inflight: 0, max: 1 });
});

test.describe('chart reads and the reader\'s idle prefetch', () => {
  test.use({ viewport: { width: 480, height: 360 } });

  test('the reader starts speculative requests while the chart\'s reads are on the wire, and the chart still finishes', async ({ page, servers, stores }) => {
    await writeChartStore(stores.dir);
    // The chunks of the chart's first batch, the timesteps after the 12 that the idle prefetch has loaded, stay on the wire until the test lets them arrive.
    const held = { first: 13, last: 16 };
    let release;
    const arrive = new Promise((resolve) => {
      release = resolve;
    });
    const requested = [];
    await page.route(`${servers.dataUrl}/**/c/**`, async (route) => {
      const t = Number(/\/c\/(\d+)\//.exec(new URL(route.request().url()).pathname)?.[1]);
      requested.push(t);
      await sleep(CHUNK_DELAY_MS);
      if (t >= held.first && t <= held.last) await arrive;
      await route.continue().catch(() => {});
    });
    await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(`${servers.dataUrl}/chart-yield/`)}`);
    await page.waitForFunction(() => window.chronozarr?.viewer?.paintedT === 0, null, { timeout: 30_000 });
    await page.waitForFunction((t) => window.chronozarr.viewer.store.hasCachedRaw(0, 0, 0, t), held.first - 1, { timeout: 30_000 });

    // The chart reads timesteps 13 to 16, which do not arrive. A jump to timestep 40 gives the prefetch a window of its own, 28 to 52:
    // it only starts requests there if the chart's reads on the wire do not hold it back.
    const box = await page.locator('#gl-canvas').boundingBox();
    await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
    await expect(page.locator('#chart-status')).toContainText('loading');
    await page.evaluate(() => window.chronozarr.viewer.goToTime(40));
    await sleep(1500);
    const beside = requested.filter((t) => t >= 28 && t <= 52 && t !== 40);
    release();
    expect(beside, 'timesteps around the one jumped to that the prefetch asked for while the chart\'s reads were on the wire').not.toEqual([]);
    await expect(page.locator('#chart-status')).toContainText(`${N_TIME} of ${N_TIME} timesteps`, { timeout: 30_000 });
  });
});
