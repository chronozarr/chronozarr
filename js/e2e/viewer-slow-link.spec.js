import { expect, test } from './fixtures.js';
import { openViewer } from './viewer-helpers.js';

test.use({ viewport: { width: 1280, height: 720 } });

/** Limit the page's network (CDP) to `bytesPerSecond` down and up with `latencyMs` added to each request. */
async function limitLink(page, { bytesPerSecond, latencyMs }) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', { offline: false, latency: latencyMs, downloadThroughput: bytesPerSecond, uploadThroughput: bytesPerSecond });
}

const play = (page) =>
  page.evaluate(() => {
    const { viewer } = window.chronozarr;
    viewer.setSpeed(4);
    viewer.play();
  });

test('playing on a link that cannot carry the speed: the viewer lowers it, says so, and does not pause again and again', async ({ page, servers, storeUrl }) => {
  test.setTimeout(120_000);
  // 1 MB/s with 50 ms per request from the first request on, so the link is measured as it is: one 524 KB chunk is half a second of it,
  // and a step of the coarsest level is one chunk, 4 steps a second asked for.
  await limitLink(page, { bytesPerSecond: 1_000_000, latencyMs: 50 });
  await openViewer(page, servers, await storeUrl('link_plain'));
  await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null, null, { timeout: 60_000 });
  await play(page);

  // The link rule shows as soon as the first frames are due.
  await page.waitForFunction(() => window.chronozarr.viewer.playback.linkLimited === true, null, { timeout: 60_000 });
  const slowed = await page.evaluate(() => ({
    effective: window.chronozarr.viewer.playback.effectiveStepsPerSecond,
    hint: document.getElementById('res-hint').textContent,
    label: document.getElementById('speed-label').textContent,
  }));
  expect(slowed.effective, 'the speed that is delivered is below the one asked for').toBeLessThan(4);
  expect(slowed.effective, 'but a movie, not a slide show').toBeGreaterThan(0.4);
  expect(slowed.hint, 'the page says why').toMatch(/step\/s: link too slow for 4/);
  expect(slowed.label, 'and the speed label shows what is delivered next to what was asked').toMatch(/^4 \/s → [\d.]+$/);

  // Steady: frames keep coming, and it does not run dry over and over (the first frames may, before the link is measured over a second).
  await page.waitForTimeout(12_000);
  const after = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    return { steps: viewer.playback.stats.steps, pauses: viewer.playback.stats.bufferingPauses, playing: viewer.playback.playing };
  });
  expect(after.playing).toBe(true);
  expect(after.steps, 'it kept stepping').toBeGreaterThanOrEqual(6);
  expect(after.pauses, 'with a speed the link feeds, it does not run dry over and over').toBeLessThanOrEqual(1);
});

test('on a link that carries the speed nothing changes: no hint, the speed asked for', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('link_plain'));
  await page.waitForFunction(() => window.chronozarr.viewer.viewTiming?.fullMs != null);
  await play(page);
  await page.waitForFunction(() => window.chronozarr.viewer.playback.stats.steps >= 6, null, { timeout: 30_000 });
  const state = await page.evaluate(() => ({ limited: window.chronozarr.viewer.playback.linkLimited, hint: document.getElementById('res-hint').textContent }));
  expect(state.limited).toBe(false);
  expect(state.hint).not.toMatch(/link too slow/);
});
