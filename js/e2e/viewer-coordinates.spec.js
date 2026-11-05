import { expect, test } from './fixtures.js';
import { DAYMET_LCC } from '../support/crs-fixtures.js';
import { clickStorePixel, openViewer, readSidebar } from './viewer-helpers.js';
import { storedValue } from './stores.js';

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

test('the inspector and chart treat store-supplied band metadata as text', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('i16_band');
  const name = 'sample"><img src=x onerror="window.bandCodeRan=true">';
  const units = '<img src=x onerror="window.unitsCodeRan=true">';
  await page.route(`${store}zarr.json`, async (route) => {
    const response = await route.fetch();
    const json = await response.json();
    Object.assign(json.attributes.chronozarr.bands[0], { name, units });
    json.attributes.chronozarr.band_names[0] = name;
    await route.fulfill({ response, json });
  });
  await openViewer(page, servers, store);
  await clickStorePixel(page, 50, 40);
  await expect(page.locator('.chart-svg')).toHaveAttribute('aria-label', `${name} (${units}) over time`);
  await expect(page.locator('#sidebar-content')).toContainText(units);
  await expect(page.locator('#chart-legend')).toContainText(name);
  await expect(page.locator('#sidebar-content img')).toHaveCount(0);
  expect(await page.evaluate(() => Boolean(window.bandCodeRan || window.unitsCodeRan))).toBe(false);
});

test('a delayed sample cannot replace a newer pixel selection', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  await page.evaluate(() => {
    const { store } = window.chronozarr.viewer;
    const original = store.ensureRaw.bind(store);
    const gate = new Promise((resolve) => { window.releaseFirstSample = resolve; });
    store.ensureRaw = async (...args) => {
      store.ensureRaw = original;
      window.firstSampleWaiting = true;
      await original(...args);
      await gate;
    };
  });
  await clickStorePixel(page, 50, 40);
  await page.waitForFunction(() => window.firstSampleWaiting);
  await clickStorePixel(page, 80, 90);
  const content = page.locator('#sidebar-content');
  await expect(content).toContainText('80, 90');
  await page.evaluate(async () => {
    window.releaseFirstSample();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  await expect(content).toContainText('80, 90');
  await expect(content).not.toContainText('50, 40');
});

test('a nodata band remains missing when another band is valid at the same pixel', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_partial'));
  await clickStorePixel(page, 50, 40);
  await expect(page.locator('#sidebar-content')).toContainText('no data');
  const sidebar = await readSidebar(page);
  const nir = storedValue('u16_partial', 0, 3, 40, 50);
  expect(sidebar.Reflectance).toEqual(expect.arrayContaining([['B04', 'no data'], ['B08', (nir / 10000).toFixed(4)]]));
  expect(sidebar['Stored value']).toEqual(expect.arrayContaining([['B04', 'no data'], ['B08', String(nir)]]));
});
