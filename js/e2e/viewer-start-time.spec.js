// Where the demo starts in time: a bare visit starts at the latest well-covered timestep; a link to a view keeps the
// timestep it names, and t=0 when it names none, as links always have.

import { expect, test } from './fixtures.js';

async function openDemo(page, url) {
  await page.goto(url);
  await page.waitForFunction(() => window.chronozarr?.ready);
  await page.evaluate(() => window.chronozarr.ready);
  return page.evaluate(() => window.chronozarr.viewer.t);
}

test('a bare visit starts at the latest timestep, and the address bar then names it', async ({ page, servers, storeUrl }) => {
  const plain = await storeUrl('u16_plain'); // 4 timesteps, no mask or coverage: the last one
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [{ name: 'Demo', url: plain }] }));
  expect(await openDemo(page, `${servers.appUrl}/demo/index.html`)).toBe(3);
  await expect.poll(() => new URL(page.url()).searchParams.get('t'), { message: 'the view is pinned once shown' }).toBe('3');
});

test('a bare visit to a store with a mask starts at the latest timestep that is at least half valid', async ({ page, servers, storeUrl }) => {
  const masked = await storeUrl('u16_mask'); // 6 timesteps, three pixels in four valid at each
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [{ name: 'Demo', url: masked }] }));
  expect(await openDemo(page, `${servers.appUrl}/demo/index.html`)).toBe(5);
});

test('links to a view keep their timestep, and t=0 when they name none', async ({ page, servers, storeUrl }) => {
  const plain = await storeUrl('u16_plain');
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [{ name: 'Demo', url: plain }] }));
  const demo = `${servers.appUrl}/demo/index.html`;
  expect(await openDemo(page, `${demo}?t=1`), 'an explicit timestep').toBe(1);
  expect(await openDemo(page, `${demo}?p=false_color`), 'a view link without t').toBe(0);
  expect(await openDemo(page, `${demo}?store=${encodeURIComponent(plain)}`), 'a copied link names the store').toBe(0);
});

test('choosing another dataset in the menu starts it at its latest timestep', async ({ page, servers, storeUrl }) => {
  const first = await storeUrl('u16_plain');
  const second = await storeUrl('u16_mask');
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [{ name: 'First', url: first }, { name: 'Second', url: second }] }));
  await openDemo(page, `${servers.appUrl}/demo/index.html?t=1`);
  await page.locator('#catalog-select').selectOption(second);
  await page.waitForFunction((url) => window.chronozarr.viewer.store?.url === url, second);
  await page.evaluate(() => window.chronozarr.ready);
  expect(await page.evaluate(() => window.chronozarr.viewer.t)).toBe(5);
});
