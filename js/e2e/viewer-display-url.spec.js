// The initial product, band and display limits as URL parameters: a shared link opens with the same view, the address
// bar keeps the limits a user sets, and the notebook player passes its traits on in the iframe URL.

import { copyFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { expect, test } from './fixtures.js';
import { captureFrame } from './viewer-helpers.js';

const PLAYER = path.resolve(import.meta.dirname, '../../src/chronozarr/player.js');

async function openAt(page, servers, storeUrl, query) {
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(storeUrl)}&${query}`);
  await page.waitForFunction(() => window.chronozarr?.ready);
  await page.evaluate(() => window.chronozarr.ready);
}

const addressRange = (page) => new URL(page.url()).searchParams.get('r');

test('r= in the link sets the display limits of an adjustable single band, in physical units', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('i16_band'), 'p=band&r=-50,300');
  expect(await page.evaluate(() => window.chronozarr.viewer.stretchRange)).toEqual([-50, 300]);
  const { frame } = await captureFrame(page);
  expect(frame.display).toBe('linear');
  expect(frame.range).toEqual([-50, 300]);
  expect(addressRange(page)).toBe('-50,300');
  await expect(page.locator('#stretch-min')).toHaveValue('-50');
  await expect(page.locator('#stretch-max')).toHaveValue('300');
});

test('limits set in the page reach the address bar and auto limits remove them', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('i16_band'), 'p=band');
  expect(addressRange(page)).toBeNull();
  await page.evaluate(() => window.chronozarr.viewer.setStretch(0, 10));
  await expect.poll(() => addressRange(page)).toBe('0,10');
  await page.evaluate(() => window.chronozarr.viewer.autoStretch());
  await expect.poll(() => addressRange(page)).toBeNull();
});

test('r= is ignored where the viewer has no adjustable limits', async ({ page, servers, storeUrl }) => {
  // reflectance bands are toned with fixed limits
  await openAt(page, servers, await storeUrl('u16_sharded'), 'p=band&r=0,1');
  expect(await page.evaluate(() => window.chronozarr.viewer.stretchRange)).toBeNull();
  expect(addressRange(page)).toBeNull();
  // a product other than the single band
  await openAt(page, servers, await storeUrl('u8_rgb'), 'r=0,1');
  expect(await page.evaluate(() => window.chronozarr.viewer.stretchRange)).toBeNull();
});

test('the notebook player puts product, band and limits in the iframe URL', async ({ page, servers, stores, storeUrl }) => {
  const store = await storeUrl('i16_band');
  const hostDir = path.join(stores.dir, 'player-host');
  await mkdir(hostDir, { recursive: true });
  await copyFile(PLAYER, path.join(hostDir, 'player.js'));
  await writeFile(
    path.join(hostDir, 'host.html'),
    `<!doctype html><meta charset="utf-8"><body></body>
<script type="module">
  const values = {
    store_url: ${JSON.stringify(store)}, viewer_url: ${JSON.stringify(`${servers.appUrl}/demo/index.html`)},
    controls: false, height: 400, theme: 'light', t: 0, product: 'band', band: 'elev', range: [-50, 300],
    speed: 4, playing: false, times: [], products: [], bands: [], ready: false, state: {}, click: {}, error: {},
  };
  window.values = values;
  const listeners = new Map();
  const model = {
    get: (key) => values[key],
    set(key, value) { values[key] = value; for (const callback of listeners.get('change:' + key) ?? []) callback(); },
    save_changes() {},
    on(key, callback) { if (!listeners.has(key)) listeners.set(key, new Set()); listeners.get(key).add(callback); },
    off(key, callback) { listeners.get(key)?.delete(callback); },
  };
  const widget = (await import('./player.js')).default;
  widget.render({ model, el: document.body });
</script>`,
  );
  await page.goto(`${servers.dataUrl}/player-host/host.html`);
  await page.waitForSelector('iframe');
  const params = await page.evaluate(() => Object.fromEntries(new URL(document.querySelector('iframe').src).searchParams));
  expect([params.p, params.b, params.r]).toEqual(['band', 'elev', '-50,300']);
  await page.waitForFunction(() => window.values.ready);
  expect(await page.evaluate(() => window.values.state.range)).toEqual([-50, 300]);
});
