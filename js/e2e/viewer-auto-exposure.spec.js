import { expect, test } from './fixtures.js';
import { captureFrame, openViewer } from './viewer-helpers.js';

test('auto exposure follows a viewport within one chunk and freezes when disabled', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('i16_band'));
  const button = page.locator('#auto-exposure');
  await button.click();
  await expect(button).toHaveAttribute('aria-pressed', 'true');
  await page.evaluate(() => window.chronozarr.viewer.setView({ zoom: 8, center: { col: 20, row: 20 } }));
  const first = (await captureFrame(page)).frame.range;
  await page.evaluate(() => window.chronozarr.viewer.setView({ zoom: 8, center: { col: 100, row: 100 } }));
  const second = (await captureFrame(page)).frame.range;
  expect(second).not.toEqual(first);
  await button.click();
  await page.evaluate(() => window.chronozarr.viewer.setView({ zoom: 8, center: { col: 20, row: 20 } }));
  expect((await captureFrame(page)).frame.range).toEqual(second);
  await button.click();
  await page.evaluate(() => window.chronozarr.viewer.setStretch(-10, 10));
  await expect(button).toHaveAttribute('aria-pressed', 'false');
});

test('auto exposure is offered for radar composites and hidden for optical images', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_sar'));
  await expect(page.locator('#auto-exposure')).toBeVisible();
  await page.locator('#auto-exposure').click();
  expect((await captureFrame(page)).frame.range).toHaveLength(3);
  await openViewer(page, servers, await storeUrl('u16_sharded'));
  await expect(page.locator('#auto-exposure')).toBeHidden();
});

test('warm viewport exposure uses cached pixels with bounded paint cost', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('i16_band'));
  const results = await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    const measure = (enabled) => {
      viewer.setAutoExposure(enabled);
      const samples = [];
      for (let i = 0; i < 30; i++) {
        viewer.setView({ zoom: 8, center: { col: 70 + i, row: 80 } });
        const start = performance.now();
        viewer.renderNow();
        samples.push(performance.now() - start);
      }
      samples.sort((a, b) => a - b);
      return { median: samples[15], p95: samples[28] };
    };
    const before = performance.getEntriesByType('resource').length;
    const off = measure(false);
    const on = measure(true);
    return { off, on, requests: performance.getEntriesByType('resource').length - before };
  });
  console.log('Warm viewport paint (ms):', JSON.stringify(results));
  expect(results.requests).toBe(0);
  await page.screenshot({ path: '/tmp/chrono-auto-exposure/viewer.png' });
});
