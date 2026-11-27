// A superseded open releases its metadata reads, and callbacks cannot publish old-session notices.
import { expect, test } from './fixtures.js';

async function openViewer(page, servers, url) {
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
  await page.waitForFunction(() => window.chronozarr?.ready);
  expect(await page.evaluate(() => window.chronozarr.ready)).toBeTruthy();
}

test('replacing the initial open cancels its adopted early root without a second root request', async ({ page, servers, storeUrl }) => {
  const slow = await storeUrl('u16_plain');
  const next = await storeUrl('u8_rgb');
  let held;
  let requests = 0;
  let aborted = false;
  page.on('requestfailed', (request) => { if (request.url() === `${slow}zarr.json`) aborted = true; });
  await page.route(`${slow}zarr.json`, (route) => { requests++; held = route; });
  await page.goto(`${servers.appUrl}/demo/index.html?embed=1&store=${encodeURIComponent(slow)}`);
  await page.waitForFunction(() => window.chronozarr?.viewer.status.store === 'opening' && Boolean(window.chronozarr.ready));
  await expect.poll(() => Boolean(held)).toBe(true);
  await page.evaluate(() => {
    window.initialOutcome = 'pending';
    window.chronozarr.ready.then((result) => { window.initialOutcome = result; });
  });
  const result = await page.evaluate((url) => window.chronozarr.viewer.loadStore(url, { workers: 0 }), next);
  expect(result.firstPaintMs).toEqual(expect.any(Number));
  await expect.poll(() => page.evaluate(() => window.initialOutcome)).toBeNull();
  await expect.poll(() => aborted).toBe(true);
  expect(requests, 'the early root is reused once and never retried after cancellation').toBe(1);
  await held.continue().catch(() => {});
});

test('a superseded metadata read aborts and settles before the old response arrives', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_sharded'));
  const slow = await storeUrl('u16_plain');
  const next = await storeUrl('u8_rgb');
  let held;
  await page.route(`${slow}zarr.json`, (route) => { held = route; });
  await page.evaluate((url) => {
    const { viewer } = window.chronozarr;
    window.oldOpenOutcome = 'pending';
    window.metadataAborted = false;
    window.oldOpen = viewer.loadStore(url, {
      workers: 0,
      fetch: (request) => {
        request.signal.addEventListener('abort', () => { window.metadataAborted = true; }, { once: true });
        return fetch(request);
      },
    }).then((result) => { window.oldOpenOutcome = result; return result; }, () => { window.oldOpenOutcome = 'rejected'; });
  }, slow);
  await expect.poll(() => Boolean(held)).toBe(true);
  const result = await page.evaluate((url) => window.chronozarr.viewer.loadStore(url, { workers: 0 }), next);
  expect(result.firstPaintMs).toEqual(expect.any(Number));
  await expect.poll(() => page.evaluate(() => ({ outcome: window.oldOpenOutcome, aborted: window.metadataAborted }))).toEqual({ outcome: null, aborted: true });
  expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(next);
  await held.continue().catch(() => {});
});

test('an abort callback can replace the attempted open and keeps its own metadata cancellation', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_sharded'));
  const slow = await storeUrl('u16_plain');
  const interrupted = await storeUrl('u8_rgb');
  const reentrant = await storeUrl('u16_partial');
  const final = await storeUrl('i16_band');
  const held = new Map();
  for (const url of [slow, reentrant]) await page.route(`${url}zarr.json`, (route) => { held.set(url, route); });
  await page.evaluate(([slow, reentrant]) => {
    const { viewer } = window.chronozarr;
    window.reentrantOutcome = 'not started';
    window.oldOpen = viewer.loadStore(slow, {
      workers: 0,
      fetch: (request) => {
        request.signal.addEventListener('abort', () => {
          window.reentrantOutcome = 'pending';
          window.reentrantOpen = viewer.loadStore(reentrant, { workers: 0 })
            .then((result) => { window.reentrantOutcome = result; return result; }, () => { window.reentrantOutcome = 'rejected'; });
        }, { once: true });
        return fetch(request);
      },
    });
  }, [slow, reentrant]);
  await expect.poll(() => held.has(slow)).toBe(true);
  expect(await page.evaluate((url) => window.chronozarr.viewer.loadStore(url, { workers: 0 }), interrupted)).toBeNull();
  await expect.poll(() => held.has(reentrant)).toBe(true);
  expect(await page.evaluate(() => window.oldOpen)).toBeNull();
  const result = await page.evaluate((url) => window.chronozarr.viewer.loadStore(url, { workers: 0 }), final);
  expect(result.firstPaintMs).toEqual(expect.any(Number));
  await expect.poll(() => page.evaluate(() => window.reentrantOutcome)).toBeNull();
  expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(final);
  await Promise.all([...held.values()].map((route) => route.continue().catch(() => {})));
});

test('a superseded catalog open cancels its starting-timestep mask read', async ({ page, servers, storeUrl }) => {
  const initial = await storeUrl('u16_sharded');
  const slow = await storeUrl('u16_mask');
  const next = await storeUrl('u8_rgb');
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [
    { name: 'Initial', url: initial }, { name: 'Slow mask', url: slow }, { name: 'Replacement', url: next },
  ] }));
  await openViewer(page, servers, initial);
  const held = [];
  await page.route((url) => url.href.startsWith(slow) && url.pathname.includes('/mask/c/'), (route) => { held.push(route); });
  await page.locator('#catalog-select').selectOption(slow);
  await expect.poll(() => held.length).toBeGreaterThan(0);
  await page.evaluate(() => {
    window.catalogOutcome = 'pending';
    window.chronozarr.ready.then((result) => { window.catalogOutcome = result; });
  });
  await page.locator('#catalog-select').selectOption(next);
  expect((await page.evaluate(() => window.chronozarr.ready)).firstPaintMs).toEqual(expect.any(Number));
  await expect.poll(() => page.evaluate(() => window.catalogOutcome)).toBeNull();
  expect(await page.evaluate(() => window.chronozarr.viewer.store.url)).toBe(next);
  await Promise.all(held.map((route) => route.continue().catch(() => {})));
});

test('a stale startup callback does not close its store adopted by the replacement or warn about cancellation', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_sharded'));
  const next = await storeUrl('u16_plain');
  const warnings = [];
  page.on('console', (message) => { if (message.type() === 'warning' && message.text().startsWith('start time for ')) warnings.push(message.text()); });
  await page.evaluate((url) => {
    window.delayedStartup = window.chronozarr.viewer.loadStore(url, {
      workers: 0,
      startTime(store) {
        window.startupStore = store;
        return new Promise((resolve, reject) => { window.rejectStartup = reject; });
      },
    });
  }, next);
  await page.waitForFunction(() => Boolean(window.startupStore));
  const result = await page.evaluate(async (url) => {
    const { viewer } = window.chronozarr;
    const adopted = await viewer.loadStore(url, { store: window.startupStore });
    window.rejectStartup(new DOMException('Aborted', 'AbortError'));
    const old = await window.delayedStartup;
    const values = await viewer.store.getRaw(0, 0, 0, 0);
    return { old, firstPaintMs: adopted.firstPaintMs, values: values.length, status: viewer.status };
  }, next);
  expect(result).toMatchObject({ old: null, firstPaintMs: expect.any(Number), status: { store: 'open', frame: 'settled' } });
  expect(result.values).toBeGreaterThan(0);
  expect(warnings).toEqual([]);
});

test('a real startup-timestep failure still opens at the first timestep with its warning', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_sharded'));
  const next = await storeUrl('u16_plain');
  const warnings = [];
  page.on('console', (message) => { if (message.type() === 'warning' && message.text().startsWith('start time for ')) warnings.push(message.text()); });
  const result = await page.evaluate(async (url) => {
    const { viewer } = window.chronozarr;
    const opened = await viewer.loadStore(url, { workers: 0, startTime: () => { throw new Error('startup read failed'); } });
    return { firstPaintMs: opened.firstPaintMs, t: viewer.t, status: viewer.status };
  }, next);
  expect(result).toMatchObject({ firstPaintMs: expect.any(Number), t: 0, status: { store: 'open', frame: 'settled', error: null } });
  expect(warnings).toHaveLength(1);
  expect(warnings[0]).toContain('startup read failed');
});

test('a chunk-abort callback can adopt a checked store without outgoing teardown clearing its frame', async ({ page, servers, storeUrl }) => {
  const initial = await storeUrl('i16_band');
  const outgoing = await storeUrl('u16_plain');
  const replacement = await storeUrl('u8_rgb');
  await openViewer(page, servers, initial);
  // Obtain the real checked store from the URL field, without requiring source-only imports in built-viewer tests.
  await page.evaluate((url) => new Promise((resolve) => {
    const { viewer } = window.chronozarr;
    viewer.loadStore = (url, { store }) => {
      delete viewer.loadStore;
      window.checkedStore = store;
      window.checkedUrl = url;
      resolve();
      return Promise.resolve(null);
    };
    const input = document.getElementById('open-store-url');
    input.value = url;
    input.form.requestSubmit();
  }), replacement);
  const held = [];
  await page.route(/\/u16_plain\/\d+\/data\/c\/1\//, (route) => { held.push(route); });
  await page.evaluate(async (url) => {
    const { viewer } = window.chronozarr;
    window.armChunkAbort = false;
    await viewer.loadStore(url, {
      workers: 0,
      fetch: (request) => {
        if (/\/data\/c\/1\//.test(request.url)) request.signal.addEventListener('abort', () => {
          if (!window.armChunkAbort) return;
          window.armChunkAbort = false;
          window.fromChunkAbort = viewer.loadStore(window.checkedUrl, { store: window.checkedStore })
            .then((result) => ({ result }), (error) => ({ error: `${error.name}: ${error.message}` }));
        }, { once: true });
        return fetch(request);
      },
    });
  }, outgoing);
  await expect.poll(() => held.length).toBeGreaterThan(0);
  const outcome = await page.evaluate(async (url) => {
    const { viewer } = window.chronozarr;
    window.armChunkAbort = true;
    const interrupted = await viewer.loadStore(url, { workers: 0 });
    const reentrant = await window.fromChunkAbort;
    return { interrupted, reentrant, status: viewer.status, viewTiming: viewer.viewTiming, url: viewer.store.url };
  }, initial);
  expect(outcome.interrupted).toBeNull();
  expect(outcome.reentrant).toMatchObject({ result: { firstPaintMs: expect.any(Number) } });
  expect(outcome.status).toMatchObject({ store: 'open', frame: 'settled' });
  expect(outcome.viewTiming).not.toBeNull();
  expect(outcome.url).toBe(replacement.replace(/\/$/, ''));
  await Promise.all(held.map((route) => route.continue().catch(() => {})));
});

test('an error callback can open a fallback without the old error covering it', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_sharded'));
  const fallback = await storeUrl('u8_rgb');
  const invalid = `${servers.dataUrl}/invalid_store/`;
  await page.route(`${invalid}zarr.json`, (route) => route.fulfill({ json: {}, headers: { 'Access-Control-Allow-Origin': '*' } }));
  const result = await page.evaluate(async ([invalid, fallback]) => {
    const { viewer } = window.chronozarr;
    let recovery;
    viewer.hooks.error = ({ code }) => {
      if (code !== 'store_open_failed') return;
      delete viewer.hooks.error;
      recovery = viewer.loadStore(fallback, { workers: 0 });
    };
    const failure = await viewer.loadStore(invalid, { workers: 0 }).then(() => 'resolved', () => 'rejected');
    const recovered = await recovery;
    return { failure, firstPaintMs: recovered.firstPaintMs, status: viewer.status, url: viewer.store.url };
  }, [invalid, fallback]);
  expect(result.failure).toBe('rejected');
  expect(result.firstPaintMs).toEqual(expect.any(Number));
  expect(result.url).toBe(fallback);
  expect(result.status).toMatchObject({ store: 'open', frame: 'settled', error: null });
  await expect(page.locator('#error-overlay')).not.toHaveClass(/visible/);
});
