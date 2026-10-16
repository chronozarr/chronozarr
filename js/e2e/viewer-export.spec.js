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
async function exportAndDownload(page, format) {
  await page.locator('#export-format').selectOption(format);
  await page.locator('#export-from').selectOption('0');
  await page.locator('#export-to').selectOption('2');
  await page.locator('#export-start').click();
  const link = page.locator('#export-download');
  await expect(link).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('#export-status')).toContainText('Done');
  const [download] = await Promise.all([page.waitForEvent('download'), link.click()]);
  return { name: download.suggestedFilename(), bytes: await readFile(await download.path()) };
}

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
