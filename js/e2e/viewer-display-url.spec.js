// The initial product, band and display limits as URL parameters: a shared link opens with the same view, the address
// bar keeps the limits a user sets, and the notebook player passes its traits on in the iframe URL.

import { copyFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { expect, test } from './fixtures.js';
import { captureFrame, expectFrameMatchesStore } from './viewer-helpers.js';

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

test('cm= colors the single band; the GPU frame equals the CPU render, and the choice reaches the address bar', async ({ page, servers, storeUrl }) => {
  // A linear band and a band toned as reflectance: the colormap applies after either.
  for (const [store, colormap, index] of [['i16_band', 'viridis', 1], ['u16_sharded', 'magma', 2]]) {
    await openAt(page, servers, await storeUrl(store), `p=band&cm=${colormap}`);
    const capture = await captureFrame(page);
    expect(capture.frame.colormap).toBe(index);
    expectFrameMatchesStore(capture, store, 0);
    await expect(page.locator('#colormap-select')).toBeVisible();
    await expect(page.locator('#colormap-select')).toHaveValue(String(index));
    await expect.poll(() => new URL(page.url()).searchParams.get('cm')).toBe(colormap);
  }
  await page.locator('#colormap-select').selectOption({ label: 'Gray' });
  await expect.poll(() => new URL(page.url()).searchParams.get('cm')).toBeNull();
  expect((await captureFrame(page)).frame.colormap).toBe(0);
});

test('the colormap control shows only for the single band, and other products ignore cm=', async ({ page, servers, storeUrl }) => {
  await openAt(page, servers, await storeUrl('u16_sharded'), 'cm=turbo');
  await expect(page.locator('#colormap-select')).toBeHidden();
  const capture = await captureFrame(page);
  expect(capture.frame.shader).not.toBe(5);
  expectFrameMatchesStore(capture, 'u16_sharded', 0);
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

test('the notebook player puts product, band, limits and colormap in the iframe URL and links to the same view', async ({ page, servers, stores, storeUrl }) => {
  const store = await storeUrl('i16_band');
  const hostDir = path.join(stores.dir, 'player-host');
  await mkdir(hostDir, { recursive: true });
  await copyFile(PLAYER, path.join(hostDir, 'player.js'));
  await writeFile(
    path.join(hostDir, 'host.html'),
    `<!doctype html><meta charset="utf-8"><body></body>
<script type="module">
  const values = {
    store_url: ${JSON.stringify(store)}, viewer_url: ${JSON.stringify(`${servers.appUrl}/demo/index.html?theme=dark&controls=1`)},
    controls: false, height: 400, theme: 'light', t: 0, product: 'band', band: 'elev', range: [-50, 300], colormap: 'viridis',
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
  window.model = model;
  const widget = (await import('./player.js')).default;
  widget.render({ model, el: document.body });
</script>`,
  );
  await page.goto(`${servers.dataUrl}/player-host/host.html`);
  await page.waitForSelector('iframe');
  const params = await page.evaluate(() => Object.fromEntries(new URL(document.querySelector('iframe').src).searchParams));
  expect([params.p, params.b, params.r, params.cm]).toEqual(['band', 'elev', '-50,300', 'viridis']);
  await page.waitForFunction(() => window.values.ready);
  expect(await page.evaluate(() => window.values.state.range)).toEqual([-50, 300]);
  expect(await page.evaluate(() => window.values.state.colormap)).toBe('viridis');
  const viewer = page.frameLocator('iframe');
  await expect.poll(() => viewer.locator('html').evaluate(() => window.chronozarr.viewer.colormap)).toBe(1);

  // A colormap set from Python goes to the viewer and comes back in the acknowledged state.
  await page.evaluate(() => window.model.set('colormap', 'magma'));
  await page.waitForFunction(() => window.values.state.colormap === 'magma');
  expect(await viewer.locator('html').evaluate(() => window.chronozarr.viewer.colormap)).toBe(2);
  // The select shows with the other single-band controls, and a choice there reaches the viewer the same way.
  const select = page.locator('select[aria-label=Colormap]');
  await expect(select).toBeHidden();
  await page.evaluate(() => window.model.set('controls', true));
  await expect(select).toHaveValue('magma');
  await select.selectOption('turbo');
  await page.waitForFunction(() => window.values.state.colormap === 'turbo');
  expect(await viewer.locator('html').evaluate(() => window.chronozarr.viewer.colormap)).toBe(3);

  // The link opens the same view in the full viewer (which has Export), without the embed parameters or the query
  // of viewer_url. A fitted camera is left out, so the full viewer fits the store to its own window.
  const link = page.getByRole('link', { name: 'Open in viewer' });
  await expect(link).toHaveAttribute('target', '_blank');
  const linkParams = async () => Object.fromEntries(new URL(await link.getAttribute('href')).searchParams);
  const fitted = await linkParams();
  expect(fitted).toEqual({ store, t: '0', p: 'band', b: 'elev', r: '-50,300', cm: 'turbo' });
  expect(await page.evaluate(() => window.values.state.fitted)).toBe(true);

  // Once the camera moves, the link carries it.
  const zoom = await viewer.locator('html').evaluate(() => {
    const zoomed = window.chronozarr.viewer.zoom * 2;
    window.chronozarr.viewer.setView({ zoom: zoomed });
    return window.chronozarr.viewer.zoom;
  });
  await page.waitForFunction(() => window.values.state.fitted === false);
  const moved = await linkParams();
  expect(Number(moved.z)).toBe(zoom);
  const center = await page.evaluate(() => window.values.state.center);
  expect(moved.c).toBe(`${center.x},${center.y}`);
  const href = new URL(await link.getAttribute('href'));
  expect(href.origin + href.pathname).toBe(`${servers.appUrl}/demo/index.html`);
  await page.goto(href.href);
  await page.waitForFunction(() => window.chronozarr?.ready);
  await page.evaluate(() => window.chronozarr.ready);
  const opened = await page.evaluate(() => ({ colormap: window.chronozarr.viewer.colormap, range: window.chronozarr.viewer.stretchRange, zoom: window.chronozarr.viewer.zoom }));
  expect(opened).toEqual({ colormap: 3, range: [-50, 300], zoom });
});

test('the notebook player does not send an empty initial band', async ({ page, servers, stores, storeUrl }) => {
  const store = await storeUrl('i16_band');
  const hostDir = path.join(stores.dir, 'player-empty-band');
  await mkdir(hostDir, { recursive: true });
  await copyFile(PLAYER, path.join(hostDir, 'player.js'));
  await writeFile(
    path.join(hostDir, 'host.html'),
    `<!doctype html><meta charset="utf-8"><body></body>
<script type="module">
  const values = {
    store_url: ${JSON.stringify(store)}, viewer_url: ${JSON.stringify(`${servers.appUrl}/demo/index.html`)},
    controls: false, height: 400, theme: 'light', t: 0, product: '', band: '', range: null, colormap: 'gray',
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
  await page.goto(`${servers.dataUrl}/player-empty-band/host.html`);
  await page.waitForFunction(() => window.values.ready);
  await page.waitForTimeout(100);
  expect(await page.evaluate(() => window.values.error)).toEqual({});
});
