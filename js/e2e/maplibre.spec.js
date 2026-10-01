import { expect, test } from './fixtures.js';
import { storedValue } from './stores.js';

// The demo page loads MapLibre GL JS from jsdelivr (pinned in its import map); that is the one request allowed to leave
// the machine, and the basemap style is replaced by an empty one so nothing else is needed.
const EMPTY_STYLE = { version: 8, sources: {}, layers: [{ id: 'background', type: 'background', paint: { 'background-color': '#1b2233' } }] };

test('MapLibre demo: the layer opens a synthetic store, reports ready, follows time and product, and reads values under a click', async ({ page, servers, storeUrl }) => {
  await page.route(/^https:\/\/cdn\.jsdelivr\.net\//, (route) => route.continue());
  await page.route('https://demotiles.maplibre.org/style.json', (route) => route.fulfill({ json: EMPTY_STYLE }));
  const storeName = 'u16_stardelta';
  await page.goto(`${servers.appUrl}/maplibre/index.html?store=${encodeURIComponent(await storeUrl(storeName))}`);

  const status = page.locator('#status');
  await expect(status).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  await expect(page.locator('#error')).toBeEmpty();
  const opened = await page.evaluate(() => {
    const { layer, map } = window.chronozarrDemo;
    return { times: layer.times.length, products: layer.products.filter((p) => p.available).map((p) => p.id), zoom: map.getZoom() };
  });
  expect(opened.times).toBe(6);
  expect(opened.products).toEqual(['true_color', 'false_color', 'ndvi', 'ndwi', 'water', 'band']);
  await expect(page.locator('#products button')).toHaveCount(5);
  await expect(page.locator('#time')).toHaveAttribute('max', '5');

  // A product and a timestep each make the layer load and settle again.
  await page.locator('#products button', { hasText: 'NDVI' }).click();
  await expect(page.locator('#products button[aria-pressed="true"]')).toHaveText('NDVI');
  await expect(status).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  await page.locator('#time').fill('3');
  await expect(page.locator('#time-label')).toHaveText('2024-01-04');
  await expect(status).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
  expect(await page.evaluate(() => window.chronozarrDemo.layer.t)).toBe(3);

  // The map was fitted to the store, so the middle of the map is inside it: the readout is the store's values there.
  const box = await page.locator('#map').boundingBox();
  await page.mouse.click(box.x + box.width / 2 + 150, box.y + box.height / 2);
  const readout = page.locator('#readout table');
  await expect(readout).toBeVisible();
  const rows = Object.fromEntries(await readout.locator('tr').evaluateAll((trs) => trs.map((tr) => [tr.cells[0].textContent.trim(), tr.cells[1].textContent.trim()])));
  const [, col, row] = /col (\d+), row (\d+)/.exec(rows.pixel);
  expect(rows.time).toBe('2024-01-04');
  for (const [band, name] of ['B02', 'B03', 'B04', 'B08'].entries()) {
    expect(rows[name], `${name} under the click at col ${col}, row ${row}`).toMatch(new RegExp(`^${storedValue(storeName, 3, band, Number(row), Number(col))}\\s`));
  }
});
