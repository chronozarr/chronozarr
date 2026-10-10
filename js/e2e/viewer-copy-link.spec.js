// The full-viewer copy control: it must use the existing permalink state, not leak store credentials, and report
// the actual clipboard result. The embedded viewer deliberately has no sharing controls.

import { expect, test } from './fixtures.js';

async function open(page, url) {
  await page.goto(url);
  await page.waitForFunction(() => window.chronozarr?.ready);
  await page.evaluate(() => window.chronozarr.ready);
}

test('copies the current redacted view and that link restores it', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('i16_band');
  await open(page, `${servers.appUrl}/demo/index.html?store=${encodeURIComponent(store)}&t=1&p=band&r=-50,300`);
  await page.evaluate((url) => {
    window.chronozarr.viewer.pinnedStore = `${url}?token=SECRET`;
  }, store);
  await page.evaluate(() => window.chronozarr.viewer.setView({ zoom: 2, center: { col: 60, row: 70 } }));
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (text) => (document.documentElement.dataset.copiedLink = text) },
    });
  });

  const button = page.locator('#copy-link');
  await expect(button).toBeEnabled();
  await button.press('Enter');
  await expect(button).toHaveText('Link copied');
  await expect(page.locator('#copy-link-status')).toHaveText('Link copied.');
  const copied = await page.evaluate(() => document.documentElement.dataset.copiedLink);
  expect(copied).not.toContain('SECRET');
  const params = new URL(copied).searchParams;
  expect(Object.fromEntries(['t', 'p', 'r', 'z', 'c'].map((key) => [key, params.get(key)]))).toEqual({ t: '1', p: null, r: '-50,300', z: '2', c: '600,-700' });

  await open(page, copied);
  expect(await page.evaluate(() => ({ t: window.chronozarr.viewer.t, range: window.chronozarr.viewer.stretchRange, zoom: window.chronozarr.viewer.zoom, center: { col: window.chronozarr.viewer.camera.cx, row: window.chronozarr.viewer.camera.cy } }))).toEqual({ t: 1, range: [-50, 300], zoom: 2, center: { col: 60, row: 70 } });
});

test('copies the selected catalog dataset instead of the catalog default', async ({ page, servers, storeUrl }) => {
  const first = await storeUrl('u16_plain');
  const second = await storeUrl('i16_band');
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [{ name: 'First', url: first }, { name: 'Second', url: second }] }));
  await open(page, `${servers.appUrl}/demo/index.html`);
  await page.locator('#catalog-select').selectOption(second);
  await page.waitForFunction((url) => window.chronozarr.viewer.store.url === url, second);
  await page.evaluate(() => window.chronozarr.ready);
  expect(new URL(page.url()).searchParams.has('store'), 'the address bar keeps its existing catalog behavior').toBe(false);
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (text) => (document.documentElement.dataset.copiedLink = text) },
    });
  });

  await page.locator('#copy-link').click();
  const copied = await page.evaluate(() => document.documentElement.dataset.copiedLink);
  expect(new URL(copied).searchParams.get('store')).toBe(second);

  await open(page, copied);
  expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(second);
});

test('says to copy the address bar if clipboard access fails', async ({ page, servers, storeUrl }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async () => Promise.reject(new Error('blocked')) },
    });
  });
  const store = await storeUrl('u16_plain');
  await open(page, `${servers.appUrl}/demo/index.html?store=${encodeURIComponent(store)}`);
  await page.getByRole('button', { name: 'Copy link', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Copy address bar', exact: true })).toBeVisible();
  await expect(page.locator('#copy-link-status')).toHaveText('Could not copy the link. Copy the address bar.');
});

test('is hidden in the compact embed and remains usable at phone width', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('u16_plain');
  await open(page, `${servers.appUrl}/demo/index.html?embed=1&store=${encodeURIComponent(store)}`);
  await expect(page.locator('#copy-link')).toBeHidden();

  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, `${servers.appUrl}/demo/index.html?store=${encodeURIComponent(store)}`);
  const button = page.getByRole('button', { name: 'Copy link', exact: true });
  await expect(button).toBeVisible();
  const box = await button.boundingBox();
  expect(box.height).toBeGreaterThanOrEqual(30);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
});
