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
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);

  // The moment the coarse frame has painted the full frame is still in flight: what is on the canvas is the coarse level.
  await page.waitForFunction(() => window.chronozarr?.viewer?.viewTiming?.coarseMs != null, null, { polling: 'raf', timeout: 30_000 });
  const fullMsAtCoarse = await page.evaluate(() => window.chronozarr.viewer.viewTiming.fullMs);
  expect(fullMsAtCoarse, 'the full frame is not there yet when the coarse frame has painted').toBeNull();

  const timings = await page.evaluate(() => window.chronozarr.ready);
  expect(timings, 'loadStore resolved with its timings').toBeTruthy();
  expect(timings.coarseMs, 'time to a frame that covers the view').not.toBeNull();
  expect(timings.coarseAfterMetadataMs, 'the delayed store really took a round trip per request').toBeGreaterThanOrEqual(DELAY_MS * 0.8);
  expect(timings.coarseMs, 'coarse frame before the full one').toBeLessThan(timings.firstPaintMs);
  expect(timings.firstPaintMs - timings.coarseMs, 'the full frame is clearly later (it starts a stage lead after the coarse request and needs its own round trip)').toBeGreaterThan(100);
  expect(await page.evaluate(() => window.chronozarr.viewer.paintedT)).toBe(0);
});

for (const stallInBody of [false, true]) {
  const stall = stallInBody ? 'partway through the body' : 'before the response';
  test(`coarse-first: a preview stalled ${stall} gives way to the full frame`, async ({ page, servers, storeUrl }) => {
    const url = await storeUrl('coarse');
    await page.clock.install();
    await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
    await page.waitForFunction(() => !!window.chronozarr?.ready);
    await page.evaluate(() => window.chronozarr.ready);
    await page.clock.pauseAt(new Date(Date.now() + 1000));

    await page.evaluate(async ({ url, stallInBody }) => {
      const { viewer } = window.chronozarr;
      const previewUrl = new URL('2/data/c/1/0/0/0', url).href;
      const bytes = stallInBody ? new Uint8Array(await (await fetch(previewUrl)).arrayBuffer()) : null;
      const { promise: resume, resolve: release } = Promise.withResolvers();
      const { promise: bodyRead, resolve: read } = Promise.withResolvers();
      const levels = [];
      let body;
      let sent = 0;
      const sendByte = () => {
        body.enqueue(bytes.subarray(sent, sent + 1));
        sent++;
      };
      const fetchChunk = async (request) => {
        const chunk = /\/([012])\/data\/c\/1\//.exec(new URL(request.url).pathname);
        if (chunk) levels.push(Number(chunk[1]));
        if (request.url !== previewUrl) return fetch(request);
        if (!stallInBody) {
          await resume;
          return fetch(request);
        }
        return new Response(new ReadableStream({
          start(controller) {
            body = controller;
            sendByte();
            resume.then(() => {
              controller.enqueue(bytes.subarray(sent));
              controller.close();
            });
          },
          pull() { read(); },
        }));
      };
      // A fresh store and no idle prefetch keep timestep 1 cold. A 2 MiB preview at this rate would get the full 2 s lead.
      await viewer.loadStore(url, { fetch: fetchChunk, workers: 0 });
      viewer.store.prefetch = async () => ({ errors: [] });
      viewer.store.bandwidthEstimate = () => 1_000_000;
      window.previewProbe = { levels, release, bodyRead, sendByte };
      viewer.goToTime(1);
      viewer.renderNow();
    }, { url, stallInBody });

    const levels = () => page.evaluate(() => window.previewProbe.levels);
    expect(await levels()).toEqual([2]);
    if (stallInBody) {
      await page.evaluate(() => window.previewProbe.bodyRead);
      await page.clock.runFor(250);
      expect(await levels(), 'the preview can use the link while bytes are arriving').toEqual([2]);
      await page.evaluate(() => window.previewProbe.sendByte());
      await page.clock.runFor(250);
      expect(await levels(), 'new bytes extend the lead').toEqual([2]);
    }
    await page.clock.runFor(stallInBody ? 200 : 410);
    expect(await levels(), 'the full-resolution requests start while the preview is still held').toContain(0);
    await expect.poll(() => page.evaluate(() => window.chronozarr.viewer.paintedT)).toBe(1);
    const view = await page.evaluate(() => window.chronozarr.viewer.viewTiming);
    expect(view.fullMs).not.toBeNull();
    expect(view.coarseMs, 'the full frame was the first to paint for timestep 1').toBe(view.fullMs);
    await page.evaluate(() => window.previewProbe.release());
  });
}
