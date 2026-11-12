// The Export panel: it offers MP4 where the browser records it, writes the loop, and hands it over through the
// Download link, in the full viewer and in an embed.

import { readFile } from 'node:fs/promises';
import { expect, test } from './fixtures.js';

async function open(page, url) {
  await page.goto(url);
  await page.waitForFunction(() => window.chronozarr?.ready);
  await page.evaluate(() => window.chronozarr.ready);
}

/** Run an export from the panel and return the file the Download link saves. */
async function exportAndDownload(page, format, { from = 0, to = 2 } = {}) {
  await page.locator('#export-format').selectOption(format);
  await page.locator('#export-from').selectOption(String(from));
  await page.locator('#export-to').selectOption(String(to));
  await page.locator('#export-start').click();
  const link = page.locator('#export-download');
  await expect(link).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('#export-status')).toContainText('Done');
  const [download] = await Promise.all([page.waitForEvent('download'), link.click()]);
  return { name: download.suggestedFilename(), bytes: await readFile(await download.path()) };
}

test('an export keeps its starting product even when the viewer changes products', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('u16_plain');
  await open(page, `${servers.appUrl}/demo/index.html?store=${encodeURIComponent(store)}`);
  const result = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const session = viewer.createExportSession({ scale: 0.25, timesteps: 1 });
    try {
      await session.prepare(0);
      const beforeComplete = session.render(0);
      const before = session.readPixels();
      viewer.setProduct(viewer.products.findIndex(({ id }) => id === 'ndvi'));
      const afterComplete = session.render(0);
      const after = session.readPixels();
      return { beforeComplete, afterComplete, unchanged: before.every((byte, i) => byte === after[i]) };
    } finally {
      session.close();
    }
  });
  expect(result).toEqual({ beforeComplete: true, afterComplete: true, unchanged: true });
});

test('switching datasets during recording fails the export with a visible explanation', async ({ page, servers, storeUrl, consoleErrors }) => {
  const first = await storeUrl('u16_plain');
  const second = await storeUrl('link_plain');
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [{ name: 'First', url: first }, { name: 'Second', url: second }] }));
  await open(page, `${servers.appUrl}/demo/index.html`);
  await page.evaluate(() => window.chronozarr.viewer.setSpeed(1));
  await page.locator('#export-btn').click();
  await page.locator('#export-size').selectOption('0.25');
  await page.locator('#export-start').click();
  const status = page.locator('#export-status');
  await expect(status).toContainText('Recording: 1 of 4');
  await page.locator('#catalog-select').selectOption(second);
  await expect(status).toContainText('The dataset changed during export.');
  await expect(page.locator('#export-download')).toBeHidden();
  const error = consoleErrors.findIndex((message) => message.includes('export failed: Error: The dataset changed during export.'));
  expect(error).toBeGreaterThanOrEqual(0);
  consoleErrors.splice(error, 1);
});

test('video export loads uncached frames in order when only one frame fits the cache', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('link_plain');
  await open(page, `${servers.appUrl}/demo/index.html?store=${encodeURIComponent(store)}`);
  await page.evaluate(async () => {
    const viewer = window.chronozarr.viewer;
    viewer.pause();
    // Level 1 is one 256 x 256 uint16 chunk with four bands. Leave the cache's window
    // headroom, but less room than two decoded frames, so buffering must advance one at a time.
    await viewer.loadStore(viewer.store.url, { lod: 1, maxCacheBytes: 1.5 * 256 * 256 * 4 * 2 });
    const createSession = viewer.createExportSession.bind(viewer);
    viewer.createExportSession = (options) => {
      const session = createSession(options);
      const probe = window.exportProbe = { fitsOne: session.fits(1), fitsTwo: session.fits(2), prepared: [], frames: [] };
      const prepare = session.prepare;
      session.prepare = (t, signal) => {
        probe.prepared.push(t);
        return prepare(t, signal);
      };
      const render = session.render;
      session.render = (t) => {
        const complete = render(t);
        probe.frames.push({ t, complete });
        return complete;
      };
      return session;
    };
    viewer.store.clearCache();
    viewer.renderer.clearResident();
  });
  await page.locator('#export-btn').click();
  await page.locator('#export-size').selectOption('0.25');
  const mp4 = await page.evaluate(() => ['video/mp4;codecs=avc1', 'video/mp4'].some((type) => MediaRecorder.isTypeSupported(type)));
  const format = mp4 ? 'mp4' : 'webm';
  const movie = await exportAndDownload(page, format, { from: 3, to: 10 });
  expect(movie.bytes.length).toBeGreaterThan(100);
  if (mp4) expect(movie.bytes.subarray(4, 8).toString('latin1')).toBe('ftyp');
  else expect([...movie.bytes.subarray(0, 4)]).toEqual([0x1a, 0x45, 0xdf, 0xa3]);
  const probe = await page.evaluate(() => window.exportProbe);
  expect(probe.fitsOne).toBe(true);
  expect(probe.fitsTwo).toBe(false);
  expect(probe.prepared.length).toBeGreaterThanOrEqual(8);
  expect(probe.prepared.every((t) => Number.isInteger(t) && t >= 3 && t <= 10)).toBe(true);
  expect(probe.frames).toEqual(Array.from({ length: 8 }, (_, i) => ({ t: 3 + i, complete: true })));
});

for (const embed of [false, true]) {
  test(`${embed ? 'embed' : 'full viewer'}: Export saves the loop as a video and as a GIF through the Download link`, async ({ page, servers, storeUrl }) => {
    const store = await storeUrl('u16_plain');
    await open(page, `${servers.appUrl}/demo/index.html?${embed ? 'embed=1&' : ''}store=${encodeURIComponent(store)}`);
    await page.locator('#export-btn').click();
    await expect(page.locator('#export-panel')).toBeVisible();

    // MP4 is offered exactly where MediaRecorder records it (Chrome and Safari; not Firefox or a Chromium without H.264).
    const mp4 = await page.evaluate(() => ['video/mp4;codecs=avc1', 'video/mp4'].some((type) => MediaRecorder.isTypeSupported(type)));
    expect(await page.locator('#export-format option[value="mp4"]').isDisabled()).toBe(!mp4);
    expect(await page.locator('#export-format').inputValue()).toBe(mp4 ? 'mp4' : 'webm');

    const video = mp4 ? 'mp4' : 'webm';
    const movie = await exportAndDownload(page, video);
    expect(movie.name).toMatch(new RegExp(`^chronozarr-.*\\.${video}$`));
    // An MP4 starts with an ftyp box; a WebM with the EBML magic number.
    if (video === 'mp4') expect(movie.bytes.subarray(4, 8).toString('latin1')).toBe('ftyp');
    else expect([...movie.bytes.subarray(0, 4)]).toEqual([0x1a, 0x45, 0xdf, 0xa3]);

    const gif = await exportAndDownload(page, 'gif');
    expect(gif.name).toMatch(/^chronozarr-.*\.gif$/);
    expect(gif.bytes.subarray(0, 6).toString('latin1')).toBe('GIF89a');
  });
}

test('a second export does not show the first one\'s result while it prepares', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('u16_plain');
  await open(page, `${servers.appUrl}/demo/index.html?store=${encodeURIComponent(store)}`);
  await page.locator('#export-btn').click();
  await page.locator('#export-size').selectOption('0.25');
  await exportAndDownload(page, 'gif');
  const status = page.locator('#export-status');
  await expect(status).toContainText('Done in');

  // With the cache empty, the second export has to wait for chunks: hold every chunk request.
  const held = [];
  await page.route(/\/data\/c\/\d+\/0\/\d+\/\d+$/, (route) => held.push(route));
  await page.evaluate(() => {
    window.chronozarr.viewer.store.clearCache();
    window.chronozarr.viewer.renderer.clearResident();
  });
  try {
    await page.locator('#export-start').click();
    await expect.poll(() => held.length).toBeGreaterThan(0);
    await expect(page.locator('#export-cancel')).toBeVisible();
    await expect(page.locator('#export-download')).toBeHidden();
    await expect(status).toHaveText('Preparing…');
    await expect(status).not.toContainText('Done in');
    expect(await page.locator('#export-fill').evaluate((fill) => fill.style.width)).toBe('0%');
  } finally {
    await page.unroute(/\/data\/c\/\d+\/0\/\d+\/\d+$/);
    await Promise.all(held.map((route) => route.continue().catch(() => {})));
  }
  await expect(status).toContainText('Done in', { timeout: 30_000 });
});
