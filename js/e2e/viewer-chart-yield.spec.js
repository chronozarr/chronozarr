// The chart of a clicked pixel reads one chunk per timestep of its cell. Those reads wait while the user scrubs or plays, and
// while the frame they stopped on loads: they would queue next to the chunks of the frames the user is waiting for. The chart
// carries on when the scrub or the movie is over and the frame is in.
//
// A single-cell store of 60 timesteps with every chunk answered after 120 ms, so the chart's batches of four take a good
// second to cover it and a scrub of a second and a half falls inside that time.

import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { expect, test } from './fixtures.js';

const N_TIME = 60;
const CHART_BATCH = 4;
const CHUNK_DELAY_MS = 120;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SPEC = { nTime: N_TIME, nBand: 1, height: 128, width: 128, chunk: 128, sharded: false, nLevels: 1, consolidated: true, specVersion: '0.3.0', nodata: 0 };

async function writeChartStore(dir) {
  for (const [key, bytes] of buildSyntheticStore(SPEC).files) {
    const file = path.join(dir, 'chart-yield', key);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, bytes);
  }
}

/** How many timesteps the chart has loaded: the number in its status line. */
const chartLoaded = (page) => page.evaluate(() => Number(/(\d+) of \d+ timesteps/.exec(document.getElementById('chart-status').textContent)?.[1] ?? NaN));

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

  test('the chart does not read while the user scrubs, and finishes once the scrub is over', async ({ page }) => {
    // Arrow keys 90 ms apart, pressed by a timer in the page so that a busy runner cannot stretch the gaps past the 300 ms of a scrub.
    await page.evaluate(() => {
      window.__keys = setInterval(() => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })), 90);
    });
    await sleep(500);
    const during = await chartLoaded(page);
    await sleep(1000);
    const after = await chartLoaded(page);
    await page.evaluate(() => clearInterval(window.__keys));
    expect(after - during, 'timesteps the chart loaded while the scrub went on (at most the batch that was already in flight)').toBeLessThanOrEqual(CHART_BATCH);
    expect(after, 'the chart is not done yet').toBeLessThan(N_TIME);

    await expect(page.locator('#chart-status')).toContainText(`${N_TIME} of ${N_TIME} timesteps`, { timeout: 30_000 });
  });

  test('the chart does not read while a movie plays, and finishes once it is paused', async ({ page }) => {
    await page.keyboard.press('Space');
    await page.waitForFunction(() => window.chronozarr.viewer.playback.playing);
    await sleep(700);
    const during = await chartLoaded(page);
    await sleep(1200);
    const after = await chartLoaded(page);
    expect(after - during, 'timesteps the chart loaded while the movie played (at most the batch that was already in flight)').toBeLessThanOrEqual(CHART_BATCH);
    expect(after, 'the chart is not done yet').toBeLessThan(N_TIME);

    await page.keyboard.press('Space');
    await expect(page.locator('#chart-status')).toContainText(`${N_TIME} of ${N_TIME} timesteps`, { timeout: 30_000 });
  });

  test('the chart does not read while the frame the user stopped on loads, and finishes once it is in', async ({ page }) => {
    lastTimestep = {};
    lastTimestep.promise = new Promise((resolve) => {
      lastTimestep.release = resolve;
    });
    // A jump to the last timestep: its frame cannot be drawn until its chunk arrives. The scrub of the jump is over 300 ms later.
    await page.evaluate((t) => window.chronozarr.viewer.goToTime(t), N_TIME - 1);
    await sleep(600);
    const during = await chartLoaded(page);
    await sleep(900);
    const after = await chartLoaded(page);
    expect(after - during, 'timesteps the chart loaded while the frame was loading (at most the batch that was already in flight)').toBeLessThanOrEqual(CHART_BATCH);
    expect(after, 'the chart is not done yet').toBeLessThan(N_TIME);

    lastTimestep.release();
    await expect(page.locator('#chart-status')).toContainText(`${N_TIME} of ${N_TIME} timesteps`, { timeout: 30_000 });
  });
});
