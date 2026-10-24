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
