import { expect, test } from './fixtures.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { openViewer, waitForSettled } from './viewer-helpers.js';

test.use({ viewport: { width: 880, height: 680 } });

test.beforeEach(async ({ page, webgl }) => {
  test.skip(webgl.maxArrayTextureLayers < 2048, 'the 600-band regression needs at least 2048 array texture layers');
  await page.addInitScript(() => {
    const getParameter = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function (name) {
      const value = getParameter.call(this, name);
      return name === this.MAX_ARRAY_TEXTURE_LAYERS ? Math.min(2048, value) : value;
    };
  });
});

async function manyBandStore(page, servers, overrides = {}) {
  const { files } = buildSyntheticStore({
    nTime: 2,
    nBand: 600,
    height: 4,
    width: 4,
    chunk: 2,
    nLevels: 1,
    nodata: null,
    sharded: false,
    consolidated: true,
    ...overrides,
  });
  const url = `${servers.appUrl}/capacity-store/`;
  await page.route(`${url}**`, async (route) => {
    const key = new URL(route.request().url()).pathname.slice('/capacity-store'.length);
    const bytes = files.get(key);
    await route.fulfill({
      status: bytes ? 200 : 404,
      contentType: key.endsWith('.json') ? 'application/json' : 'application/octet-stream',
      body: bytes ? Buffer.from(bytes) : '',
    });
  });
  return url;
}

test('a fitted 600-band store settles when four cells exceed its three GPU slots and another store can open', async ({ page, servers, storeUrl }) => {
  const url = await manyBandStore(page, servers);
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
  await expect.poll(() => page.evaluate(() => window.chronozarr?.viewer.status.frame)).toBe('unavailable');
  expect(await page.evaluate(() => window.chronozarr.ready)).toMatchObject({ firstPaintMs: null });
  expect(await waitForSettled(page)).toMatchObject({ store: 'open', frame: 'unavailable', shownT: null, error: { code: 'gpu_capacity' } });
  expect(await page.evaluate(() => window.chronozarr.viewer.renderer.slots)).toBe(3);
  await expect(page.locator('#error-message')).toContainText('needs 4 GPU chunk slots');
  await expect(page.locator('#error-message')).toContainText('can hold 3');
  await expect(page.locator('#frame-date-status')).toContainText('Unavailable');
  await expect(page.locator('#timeline-track')).not.toHaveClass(/loading/);
  await expect(page.locator('#progress-fill')).toHaveCSS('opacity', '0');

  const good = await storeUrl('u16_plain');
  await page.evaluate((url) => window.chronozarr.viewer.loadStore(url), good);
  expect(await waitForSettled(page)).toMatchObject({ store: 'open', frame: 'settled', error: null });
  await expect(page.locator('#error-overlay')).not.toHaveClass(/visible/);
});

test('a valid crop remains usable, fitting the oversized view pauses playback, and zooming back recovers', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  const url = await manyBandStore(page, servers);
  const initial = await page.evaluate(async (url) => {
    const { viewer } = window.chronozarr;
    const camera = { cx: 1, cy: 1, scale: Math.max(viewer.canvas.width, viewer.canvas.height) };
    await viewer.loadStore(url, { camera });
    window.capacityCamera = camera;
    await viewer.prefetchNow();
    return { status: viewer.status, slots: viewer.renderer.slots, complete: viewer.renderNow().complete };
  }, url);
  expect(initial).toMatchObject({ status: { frame: 'settled' }, slots: 3, complete: true });

  const fitted = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    viewer.play();
    const playing = viewer.playback.playing;
    viewer.fit();
    const rendered = viewer.renderNow();
    return { playing, complete: rendered.complete, status: await viewer.whenSettled() };
  });
  expect(fitted).toMatchObject({ playing: true, complete: false, status: { frame: 'unavailable', playback: 'paused', error: { code: 'gpu_capacity' } } });

  await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    viewer.camera = window.capacityCamera;
    viewer.renderNow();
  });
  expect(await waitForSettled(page)).toMatchObject({ frame: 'settled', error: null });
  expect(await page.evaluate(() => window.chronozarr.viewer.canvas.getContext('webgl2').getError())).toBe(0);
});

test('a three-cell frame and its validity mask fit exactly three GPU slots', async ({ page, servers }) => {
  const url = await manyBandStore(page, servers, { width: 6, height: 2, mask: true });
  const timings = await openViewer(page, servers, url);
  expect(timings.firstPaintMs).not.toBeNull();
  const result = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const session = viewer.createExportSession({ timesteps: 1, scale: 0.25 });
    try {
      await session.prepare(0);
      return { slots: viewer.renderer.slots, complete: viewer.renderNow().complete, cells: session.cellCount, exportComplete: session.render(0) };
    } finally {
      session.close();
    }
  });
  expect(result).toEqual({ slots: 3, complete: true, cells: 3, exportComplete: true });
  expect(await waitForSettled(page)).toMatchObject({ frame: 'settled', exporting: false });
});

test('a coarser level can fit while a pinned level settles as unavailable and rejects an incomplete export', async ({ page, servers }) => {
  const url = await manyBandStore(page, servers, { nLevels: 2 });
  await openViewer(page, servers, url);
  expect(await waitForSettled(page)).toMatchObject({ frame: 'settled', level: 1 });
  const result = await page.evaluate(async () => {
    const { viewer } = window.chronozarr;
    const getContext = HTMLCanvasElement.prototype.getContext;
    const contexts = [];
    HTMLCanvasElement.prototype.getContext = function (kind, ...options) {
      const context = getContext.call(this, kind, ...options);
      if (kind === 'webgl2' && this !== viewer.canvas) contexts.push(context);
      return context;
    };
    viewer.lodOverride = 0;
    let message;
    try {
      const session = viewer.createExportSession({ timesteps: 1 });
      session.close();
    } catch (error) {
      message = error.message;
    } finally {
      HTMLCanvasElement.prototype.getContext = getContext;
    }
    viewer.renderNow();
    const unavailable = await viewer.whenSettled();
    viewer.lodOverride = null;
    viewer.renderNow();
    const prefetch = await viewer.prefetchNow();
    return { message, disposed: contexts.map((context) => context.isContextLost()), unavailable, prefetch: prefetch !== null, recovered: await viewer.whenSettled() };
  });
  expect(result.message).toContain('export needs 4 GPU chunk slots');
  expect(result.disposed).toEqual([true]);
  expect(result.unavailable).toMatchObject({ frame: 'unavailable', exporting: false, error: { code: 'gpu_capacity' } });
  expect(result).toMatchObject({ prefetch: true, recovered: { frame: 'settled', exporting: false, level: 1, error: null } });
});

test('a status hook replacing the store while capacity failure pauses playback keeps the replacement outcome', async ({ page, servers, storeUrl }) => {
  const good = await storeUrl('u16_plain');
  await openViewer(page, servers, good);
  const url = await manyBandStore(page, servers);
  const result = await page.evaluate(async ({ url, good }) => {
    const { viewer } = window.chronozarr;
    await viewer.loadStore(url, { camera: { cx: 1, cy: 1, scale: Math.max(viewer.canvas.width, viewer.canvas.height) } });
    await viewer.prefetchNow();
    const errors = [];
    viewer.hooks.error = ({ code }) => errors.push(code);
    let sawPlaying = false;
    let switched = false;
    let replacement;
    viewer.hooks.status = () => {
      if (viewer.status.playback !== 'paused') sawPlaying = true;
      if (sawPlaying && !switched && viewer.status.playback === 'paused') {
        switched = true;
        replacement = viewer.loadStore(good);
      }
    };
    viewer.play();
    viewer.fit();
    viewer.renderNow();
    await replacement;
    return { switched, errors, url: viewer.store.url, status: await viewer.whenSettled() };
  }, { url, good });
  expect(result).toMatchObject({ switched: true, errors: [], url: good, status: { store: 'open', frame: 'settled', error: null } });
});

test('a pause status hook selecting a fitting crop keeps its queued render and outcome', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  const url = await manyBandStore(page, servers, { width: 64, height: 64, chunk: 32 });
  const result = await page.evaluate(async (url) => {
    const { viewer } = window.chronozarr;
    await viewer.loadStore(url, { camera: { cx: 0, cy: 0, scale: 16 } });
    await viewer.prefetchNow();
    const errors = [];
    viewer.hooks.error = ({ code }) => errors.push(code);
    let sawPlaying = false;
    let moved = false;
    viewer.hooks.status = () => {
      if (viewer.status.playback !== 'paused') sawPlaying = true;
      if (sawPlaying && !moved && viewer.status.playback === 'paused') {
        moved = true;
        viewer.setView({ zoom: 16, center: { col: 0, row: 0 } });
      }
    };
    viewer.play();
    viewer.fit();
    viewer.renderNow();
    return { moved, errors, status: await viewer.whenSettled(), camera: viewer.camera };
  }, url);
  expect(result).toMatchObject({ moved: true, errors: [], status: { store: 'open', frame: 'settled', playback: 'paused', error: null }, camera: { cx: 0, cy: 0, scale: 16 } });
});
