import { expect, test } from './fixtures.js';
import { DAYMET_LCC } from '../support/crs-fixtures.js';
import { clickStorePixel, openViewer } from './viewer-helpers.js';

for (const clipboardWorks of [true, false]) {
  test(`pixel coordinates ${clipboardWorks ? 'copy to the clipboard' : 'remain selectable when clipboard access fails'}`, async ({ page, servers, storeUrl }) => {
    await page.addInitScript((works) => {
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async (text) => {
          if (!works) throw new Error('Clipboard blocked');
          window.copiedCoordinates = text;
        },
      } });
    }, clipboardWorks);
    await openViewer(page, servers, await storeUrl('u16_lcc'));
    const [x, y, lon, lat] = DAYMET_LCC.grid.centres[1];
    await clickStorePixel(page, x, y);
    const expected = `(${lat.toFixed(6)}, ${lon.toFixed(6)})`;
    await expect(page.locator('#coordinate-tuple')).toHaveText(expected);
    await page.getByRole('button', { name: 'Copy latitude and longitude' }).click();
    if (clipboardWorks) {
      await expect(page.locator('#coordinate-copy-status')).toHaveText('Copied');
      expect(await page.evaluate(() => window.copiedCoordinates)).toBe(expected);
    } else {
      await expect(page.locator('#coordinate-copy-status')).toHaveText('Select and copy the coordinates manually.');
      expect(await page.evaluate(() => window.getSelection().toString())).toBe(expected);
    }
  });
}

test('a delayed coordinate lookup cannot overwrite a newer pixel selection', async ({ page, servers, storeUrl }) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route('**/projection.js', async (route) => {
    await held;
    await route.continue();
  });
  await openViewer(page, servers, await storeUrl('u16_lcc'));
  await clickStorePixel(page, 50, 40);
  await expect(page.locator('#coordinate-tuple')).toHaveText('Loading coordinates…');
  const [x, y, lon, lat] = DAYMET_LCC.grid.centres[2];
  await clickStorePixel(page, x, y);
  release();
  await expect(page.locator('#coordinate-tuple')).toHaveText(`(${lat.toFixed(6)}, ${lon.toFixed(6)})`);
});
