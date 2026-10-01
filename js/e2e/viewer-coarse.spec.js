import { expect, test } from './fixtures.js';

const DELAY_MS = 300;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test.use({ viewport: { width: 1920, height: 1080 } });

test('coarse-first: on a slow store the coarse frame paints before the full one', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('coarse');
  // Every response from the store takes DELAY_MS longer, like a store a round trip away. The site itself is not delayed.
  await page.route(`${servers.dataUrl}/**`, async (route) => {
    await sleep(DELAY_MS);
    await route.continue();
  });
  await page.goto(`${servers.appUrl}/tileripper/index.html?store=${encodeURIComponent(url)}`);

  // The moment the coarse frame has painted the full frame is still in flight: what is on the canvas is the coarse level.
  await page.waitForFunction(() => window.tileripper?.viewer?.viewTiming?.coarseMs != null, null, { polling: 'raf', timeout: 30_000 });
  const fullMsAtCoarse = await page.evaluate(() => window.tileripper.viewer.viewTiming.fullMs);
  expect(fullMsAtCoarse, 'the full frame is not there yet when the coarse frame has painted').toBeNull();

  const timings = await page.evaluate(() => window.tileripper.ready);
  expect(timings, 'loadStore resolved with its timings').toBeTruthy();
  expect(timings.coarseMs, 'time to a frame that covers the view').not.toBeNull();
  expect(timings.coarseAfterMetadataMs, 'the delayed store really took a round trip per request').toBeGreaterThanOrEqual(DELAY_MS * 0.8);
  expect(timings.coarseMs, 'coarse frame before the full one').toBeLessThan(timings.firstPaintMs);
  expect(timings.firstPaintMs - timings.coarseMs, 'the full frame is clearly later (it starts a stage lead after the coarse request and needs its own round trip)').toBeGreaterThan(100);
  expect(await page.evaluate(() => window.tileripper.viewer.paintedT)).toBe(0);
});
