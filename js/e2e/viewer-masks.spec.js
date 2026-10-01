import { maskValue } from '../support/synthetic-store.js';
import { expect, test } from './fixtures.js';
import { SAMPLE_PIXELS, captureFrame, clickStorePixel, expectFrameMatchesStore, openViewer, waitForPaintedTime } from './viewer-helpers.js';

// A store with a mask variable: 1 = valid, 0 = invalid, one pixel in four at any timestep (see maskValue). A pixel the
// mask marks invalid must be drawn as background, as nodata is, and must be a gap in the chart of the pixel.
test('masks: masked pixels render as background and the chart of a masked pixel shows gaps', async ({ page, servers, storeUrl }) => {
  const storeName = 'u16_mask';
  await openViewer(page, servers, await storeUrl(storeName));

  for (const t of [0, 1, 2, 3]) {
    if (t > 0) {
      await page.keyboard.press('ArrowRight');
      await waitForPaintedTime(page, t);
    }
    const masked = SAMPLE_PIXELS.filter(([X, Y]) => maskValue(t, Y, X, 0) === 0).length;
    expect(masked, `the samples at t=${t} include masked and valid pixels`).toBeGreaterThan(0);
    expect(masked).toBeLessThan(SAMPLE_PIXELS.length);
    expectFrameMatchesStore(await captureFrame(page), storeName, t);
  }

  // Pixel (10, 10) is masked at t = 0 and 4 and valid at 1, 2, 3 and 5: two segments per series.
  await clickStorePixel(page, 10, 10);
  await expect
    .poll(() => page.locator('#chart path[stroke]').evaluateAll((paths) => paths.map((path) => (path.getAttribute('d').match(/M/g) ?? []).length)), { message: 'path segments of each charted series' })
    .toEqual([2, 2, 2]);
});
