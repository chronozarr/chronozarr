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
