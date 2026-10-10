// The "Open store URL" field of the demo page: a store pasted into it is opened without ?store= being written by hand,
// the address bar changes only when it opened, and a store that does not open leaves the view on screen as it was.

import { expect, test as base } from './fixtures.js';
import { openViewer } from './viewer-helpers.js';

// The fixtures fail a test on any console error. A store that is not there is reported by the browser as errors, which
// the tests about it list; the same pattern as viewer-embed.spec.js.
const test = base.extend({
  expectedErrors: [null, { option: true }],
  consoleErrors: [
    async ({ page, expectedErrors }, use) => {
      const errors = [];
      page.on('console', (message) => {
        if (message.type() === 'error') errors.push(`console.error: ${message.text()}`);
      });
      page.on('pageerror', (error) => errors.push(`uncaught: ${error.stack || error.message}`));
      await use(errors);
      expect(
        errors.filter((error) => !expectedErrors?.test(error)),
        'the page logged errors',
      ).toEqual([]);
    },
    { auto: true },
  ],
});

const withoutSlash = (url) => url.replace(/\/$/, '');

/** Remember the store and address of the page as they are now, to compare after a failed open. */
const snapshot = (page) =>
  page.evaluate(() => {
    window.openUrlFirst = window.chronozarr.viewer.store;
    return { search: location.search, t: window.chronozarr.viewer.t, paintedT: window.chronozarr.viewer.paintedT };
  });

test('a store pasted into the field opens, and its address (without a trailing slash) goes to the address bar and back into the field', async ({ page, servers, storeUrl }) => {
  const first = await storeUrl('u16_sharded');
  const second = await storeUrl('u16_plain');
  await openViewer(page, servers, first);
  await snapshot(page);

  const field = page.getByLabel('Open store URL');
  await field.fill(second);
  await field.press('Enter');
  await page.waitForFunction(() => window.chronozarr.viewer.store !== window.openUrlFirst && window.chronozarr.viewer.paintedT >= 0);

  expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(withoutSlash(second));
  expect(new URL(page.url()).searchParams.get('store')).toBe(withoutSlash(second));
  await expect(field).toHaveValue(withoutSlash(second));
  await expect(page.locator('#open-store-error')).toBeHidden();
});

test('the button opens it too, from the keyboard order: the field, then the button', async ({ page, servers, storeUrl }) => {
  const first = await storeUrl('u16_sharded');
  const second = await storeUrl('u8_rgb');
  await openViewer(page, servers, first);
  await snapshot(page);

  await page.getByLabel('Open store URL').fill(second);
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: 'Open', exact: true })).toBeFocused();
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => window.chronozarr.viewer.store !== window.openUrlFirst && window.chronozarr.viewer.paintedT >= 0);
  expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(withoutSlash(second));
});

test.describe('a store that does not open', () => {
  // The static server's 404 carries no CORS header, so the browser reports the failed read as a CORS error, and the reader its give-up.
  test.use({ expectedErrors: /Failed to load resource|blocked by CORS policy|request failed, giving up/ });

  test('says why next to the field and leaves the store, the view and the address bar as they were', async ({ page, servers, storeUrl }) => {
    const first = await storeUrl('u16_sharded');
    await openViewer(page, servers, first);
    const before = await snapshot(page);

    const field = page.getByLabel('Open store URL');
    await field.fill(`${servers.dataUrl}/no_such_store/`);
    await field.press('Enter');

    const error = page.getByRole('alert').filter({ hasText: 'Blocked by the browser' });
    await expect(error).toBeVisible();
    await expect(error).toContainText('Access-Control-Allow-Origin');
    await expect(field).toHaveAttribute('aria-invalid', 'true');
    await expect(field).toHaveValue(`${servers.dataUrl}/no_such_store/`);

    const after = await page.evaluate(() => ({ same: window.chronozarr.viewer.store === window.openUrlFirst, search: location.search, t: window.chronozarr.viewer.t, paintedT: window.chronozarr.viewer.paintedT, overlay: document.getElementById('error-overlay').classList.contains('visible') }));
    expect(after).toEqual({ same: true, search: before.search, t: before.t, paintedT: before.paintedT, overlay: false });
  });
});

test('a URL with a login is refused without a request, and nothing of it is shown', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_sharded'));
  await snapshot(page);
  servers.data.requests.length = 0;

  const field = page.getByLabel('Open store URL');
  await field.fill(`http://alice:hunter2@${new URL(servers.dataUrl).host}/u16_plain`);
  await field.press('Enter');
  const error = page.getByRole('alert').filter({ hasText: 'Login in the URL' });
  await expect(error).toBeVisible();
  await expect(error).not.toContainText(/alice|hunter2/);
  expect(servers.data.requests.filter((request) => request.path.startsWith('/u16_plain')), 'no request for the refused store').toEqual([]);
  expect(await page.evaluate(() => window.chronozarr.viewer.store === window.openUrlFirst)).toBe(true);
});

test('a signed query is dropped from the address bar and the field once the store is open', async ({ page, servers, storeUrl }) => {
  const first = await storeUrl('u16_sharded');
  const second = await storeUrl('u16_plain');
  await openViewer(page, servers, first);
  await snapshot(page);

  const field = page.getByLabel('Open store URL');
  await field.fill(`${withoutSlash(second)}?X-Amz-Signature=SECRET-SIGNATURE`);
  await field.press('Enter');
  await page.waitForFunction(() => window.chronozarr.viewer.store !== window.openUrlFirst && window.chronozarr.viewer.paintedT >= 0);

  expect(await page.evaluate(() => window.chronozarr.viewer.store.url), "the reads still carry the signature").toBe(`${withoutSlash(second)}?X-Amz-Signature=SECRET-SIGNATURE`);
  expect(page.url()).not.toContain('SECRET');
  await expect(field).toHaveValue(withoutSlash(second));
});

test('an existing ?store= link still opens, and the field is not part of an embed', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('u16_plain');
  await openViewer(page, servers, url);
  expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(url);
  await expect(page.getByLabel('Open store URL')).toBeVisible();

  await page.goto(`${servers.appUrl}/demo/index.html?embed=1&store=${encodeURIComponent(url)}`);
  await page.waitForFunction(() => window.chronozarr?.viewer.paintedT >= 0);
  await expect(page.locator('#open-store')).toBeHidden();
});
