// The built viewer in a browser: its code arrives in two round trips at most instead of one per module level, and the
// reads that every visit makes first (catalog.json, the store's root zarr.json) start with the page, not after its script.
// A browser starts every import of a module as soon as it has read that module, so the number of round trips the scripts need is
// the longest chain of "this script started that one" in the network log (support/script-levels.js).
//
// The viewer is built here (support/build-viewer.mjs). CHRONOZARR_VIEWER_ROOT=<a web root with demo/ and favicon.svg> tests
// that layout instead; the unbundled js/ itself, or the tree of an earlier commit, fails it, which is what it is for.

import { cp, mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { startStaticServer } from '../support/static-server.js';
import { scriptLevels } from '../support/script-levels.js';
import { expect, test, writeBuiltViewerRoot } from './fixtures.js';
import { COMPRESSORS, compressedStore, writeTree } from './make-node-fixtures.mjs';

const GIVEN_ROOT = process.env.CHRONOZARR_VIEWER_ROOT;
// Scripts a page may need before its first frame: the viewer, then a codec that the viewer or the worker loads when the store needs it.
const MAX_SCRIPT_LEVELS = 2;
// The viewer, the decode worker, one codec, and one more for a CRS or a benchmark.
const MAX_DISTINCT_SCRIPTS = 4;

let root;
let app;
test.beforeAll(async () => {
  root = GIVEN_ROOT ? await mkdtemp(path.join(tmpdir(), 'chronozarr-given-viewer-')) : await writeBuiltViewerRoot();
  if (GIVEN_ROOT) await cp(path.resolve(GIVEN_ROOT), root, { recursive: true });
  app = await startStaticServer(root);
});
test.afterAll(async () => {
  await app.close();
  await rm(root, { recursive: true, force: true });
});

/** Open `address` and return what the browser requested until the first complete frame: url, type, the script or page that started it, and when. */
async function requestsUntilFirstFrame(page, address) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  const requests = [];
  cdp.on('Network.requestWillBeSent', (event) => {
    const initiator = event.initiator?.url ?? event.initiator?.stack?.callFrames?.[0]?.url ?? null;
    requests.push({ url: event.request.url, type: event.type, initiatorUrl: initiator, at: event.timestamp });
  });
  await page.goto(address);
  await page.waitForFunction(() => window.chronozarr?.ready);
  await page.evaluate(() => window.chronozarr.ready);
  return requests;
}

/** The page or script that started each of `requests`, without its query: only the inline script of the page itself names the page. */
const startedBy = (requests) => requests.map((request) => request.initiatorUrl?.split('?')[0]);

test('a link to a store loads its code in two sequential script requests at most, and reads the store before the script arrives', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('u16_plain');
  servers.data.requests.length = 0;
  const requests = await requestsUntilFirstFrame(page, `${app.url}/demo/index.html?store=${encodeURIComponent(store)}`);

  const scripts = requests.filter((request) => request.type === 'Script');
  expect(scriptLevels(requests), `script requests: ${scripts.map((s) => s.url.replace(app.url, '')).join(' ')}`).toBeLessThanOrEqual(MAX_SCRIPT_LEVELS);
  expect(new Set(scripts.map((s) => s.url)).size, 'distinct scripts').toBeLessThanOrEqual(MAX_DISTINCT_SCRIPTS);

  expect(startedBy(requests.filter((request) => request.url === `${store}zarr.json`)), 'the root metadata is read once, by the page itself').toEqual([`${app.url}/demo/index.html`]);
  expect(servers.data.requests.filter((r) => r.path === '/u16_plain/zarr.json').length, 'and no second request for it reaches the host').toBe(1);
});

test('a bare visit reads the catalog, then the root metadata of its first store, from the page, once each', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('u16_plain');
  await page.route('**/catalog.json', (route) => route.fulfill({ json: [{ name: 'Demo', url: store }] }));
  servers.data.requests.length = 0;
  const requests = await requestsUntilFirstFrame(page, `${app.url}/demo/index.html`);

  const catalog = requests.filter((request) => request.url === `${app.url}/demo/catalog.json`);
  const rootRead = requests.filter((request) => request.url === `${store}zarr.json`);
  expect(startedBy(catalog), 'one catalog read, by the page').toEqual([`${app.url}/demo/index.html`]);
  expect(startedBy(rootRead), 'one root read, by the page').toEqual([`${app.url}/demo/index.html`]);
  expect(rootRead[0].at, 'the root read follows the catalog, not the viewer script').toBeGreaterThan(catalog[0].at);
  expect(servers.data.requests.filter((r) => r.path === '/u16_plain/zarr.json').length).toBe(1);
  expect(scriptLevels(requests)).toBeLessThanOrEqual(MAX_SCRIPT_LEVELS);
});

test('an embed reads its store from the page and never the catalog', async ({ page, servers, storeUrl }) => {
  const store = await storeUrl('u16_plain');
  servers.data.requests.length = 0;
  app.requests.length = 0;
  const requests = await requestsUntilFirstFrame(page, `${app.url}/demo/index.html?embed=1&store=${encodeURIComponent(store)}`);
  expect(startedBy(requests.filter((r) => r.url === `${store}zarr.json`))).toEqual([`${app.url}/demo/index.html`]);
  expect(app.requests.filter((r) => r.path.endsWith('catalog.json'))).toEqual([]);
  expect(servers.data.requests.filter((r) => r.path === '/u16_plain/zarr.json').length).toBe(1);
});

for (const built of [false, true]) {
  test(`${built ? 'built' : 'source'} viewer decodes after the first Zstd module request gets a 503`, async ({ page, stores, servers, storeUrl, consoleErrors }) => {
    const name = built ? 'retry-zstd-built' : 'retry-zstd-source';
    const dir = path.join(stores.dir, name);
    const spec = { nTime: 2, nBand: 1, height: 2, width: 2, chunk: 2, sharded: false, consolidated: true, transform: [10, 0, 500000, 0, -10, 4000000] };
    await writeTree(dir, compressedStore(spec, COMPRESSORS.zstd));
    const store = `${servers.dataUrl}/${name}/`;
    // The source server here always serves the modules, even when the suite's default app serves a bundle.
    const source = built ? null : await startStaticServer(path.resolve(import.meta.dirname, '..'));
    try {
      const origin = (source ?? app).url;
      const codec = built ? '/demo/codecs/zstd.js' : '/vendor/numcodecs/zstd.js';
      const requests = [];
      await page.route((url) => url.origin === origin && url.pathname === codec, (route) => {
        requests.push(route.request().url());
        return requests.length === 1 ? route.fulfill({ status: 503, body: 'temporarily unavailable' }) : route.continue();
      });
      const plain = await storeUrl('u16_plain');
      await page.goto(`${origin}/demo/index.html?store=${encodeURIComponent(plain)}`);
      await page.waitForFunction(() => window.chronozarr?.ready);
      await page.evaluate(() => window.chronozarr.ready);
      const result = await page.evaluate(async (url) => {
        const { viewer } = window.chronozarr;
        // Start the open on the main thread, then read two chunks through the real decoder.
        viewer.loadStore(url, { workers: 0 }).catch(() => {});
        while (viewer.store?.url !== url) await new Promise((resolve) => setTimeout(resolve, 0));
        const read = async (t) => {
          try {
            return Array.from(await viewer.store.getRaw(0, 0, 0, t));
          } catch (error) {
            return error.message;
          }
        };
        return [await read(0), await read(1)];
      }, store);
      // The failed import reports a resource error even though the decoder recovers.
      for (let i = consoleErrors.length - 1; i >= 0; i--) {
        if (/Failed to load resource: .*503/.test(consoleErrors[i])) consoleErrors.splice(i, 1);
      }
      expect(result).toEqual([[1000, 1013, 1007, 1020], [1040, 1050, 1047, 1057]]);
      expect(requests).toHaveLength(2);
      expect(requests[1]).not.toBe(requests[0]);
    } finally {
      await source?.close();
    }
  });
}

test('the decode worker also retries a failed Zstd module request', async ({ page, consoleErrors }) => {
  await page.route(`${app.url}/worker-probe.html`, (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>Decode worker</title>' }));
  await page.goto(`${app.url}/worker-probe.html`);
  const codec = new URL('/demo/codecs/zstd.js', app.url);
  // Playwright's context routing intercepts dedicated-worker requests as well as page requests.
  const requests = [];
  await page.context().route((url) => url.origin === codec.origin && url.pathname === codec.pathname, (route) => {
    requests.push(route.request().url());
    return requests.length === 1 ? route.fulfill({ status: 503, body: 'temporarily unavailable' }) : route.continue();
  });
  const bytes = [...COMPRESSORS.zstd.compress(new Uint8Array(Uint16Array.of(37).buffer))];
  const result = await page.evaluate(async (bytes) => {
    const worker = new Worker('/demo/decode-worker.js', { type: 'module' });
    const send = (message) => new Promise((resolve, reject) => {
      worker.onmessage = ({ data }) => resolve(data);
      worker.onerror = reject;
      worker.postMessage(message);
    });
    const spec = { key: 'retry-zstd', dtype: 'uint16', shape: [1], codecs: [
      { name: 'bytes', configuration: { endian: 'little' } }, { name: 'zstd', configuration: { level: 5 } },
    ] };
    try {
      const ready = await send({ type: 'init', zarritaUrl: 'bundled' });
      const first = await send({ type: 'decode', id: 1, spec, bytes: Uint8Array.from(bytes) });
      const retry = await send({ type: 'decode', id: 2, spec, bytes: Uint8Array.from(bytes) });
      return { ready: ready.type, first: first.type, retry: retry.type, values: retry.data && [...retry.data] };
    } finally {
      worker.terminate();
    }
  }, bytes);
  for (let i = consoleErrors.length - 1; i >= 0; i--) {
    if (/Failed to load resource: .*503/.test(consoleErrors[i])) consoleErrors.splice(i, 1);
  }
  expect(result).toEqual({ ready: 'ready', first: 'error', retry: 'decoded', values: [37] });
  expect(requests).toHaveLength(2);
  expect(requests[1]).not.toBe(requests[0]);
});
