// The reads that index.html starts before the viewer script has loaded (its second inline script) and the module that hands
// their responses to the viewer: the right addresses, started at once, taken once, and never in place of a different read.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { openStore } from '../chronozarr/decoder.js';
import { fetchWithEarlyReads, takeEarlyRead, takeEarlyRootMs } from '../demo/early.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

const html = readFileSync(new URL('../demo/index.html', import.meta.url), 'utf8');
const earlyScript = [...html.slice(0, html.indexOf('<style>')).matchAll(/<script>([\s\S]*?)<\/script>/g)][1]?.[1];

const CATALOG = [
  { name: 'first', url: 'https://data.example.org/first' },
  { name: 'second', url: 'https://data.example.org/second' },
];

/** Run the inline script on a page at `pageUrl`; `answers` maps an address to the body fetch gives it. Returns the addresses it fetched, in order. */
async function runEarlyScript(pageUrl, answers = {}) {
  const location = new URL(pageUrl);
  const fetched = [];
  const window = {};
  const fetchStub = (url) => {
    const address = String(url);
    fetched.push(address);
    if (!(address in answers)) return Promise.reject(new TypeError('Failed to fetch'));
    const body = answers[address];
    return Promise.resolve({ ok: true, status: 200, body, clone: () => ({ json: async () => body }), json: async () => body });
  };
  vm.runInNewContext(earlyScript, { URL, URLSearchParams, location, window, fetch: fetchStub, performance });
  await new Promise((resolve) => setTimeout(resolve, 0));
  return { fetched, reads: window.chronozarrEarly };
}

const PAGE = 'https://chronozarr.org/demo/index.html';
const CATALOG_URL = 'https://chronozarr.org/demo/catalog.json';
const encoded = (store) => encodeURIComponent(store);

test('index.html has the early-read script after the one that sets the embed attributes, before the stylesheet', () => {
  assert.ok(earlyScript, 'a second inline script before <style>');
  assert.match(earlyScript, /chronozarrEarly/);
});

test('a bare visit reads the catalog, then the root metadata of its first store', async () => {
  const { fetched, reads } = await runEarlyScript(PAGE, { [CATALOG_URL]: CATALOG });
  assert.deepEqual(fetched, [CATALOG_URL, 'https://data.example.org/first/zarr.json']);
  assert.deepEqual([...reads.keys()], [CATALOG_URL, 'https://data.example.org/first/zarr.json']);
});

test('a catalog entry is resolved against the catalog address, as loadCatalog does', async () => {
  const { fetched } = await runEarlyScript(PAGE, { [CATALOG_URL]: [{ name: 'local', url: '../stores/v03' }] });
  assert.deepEqual(fetched, [CATALOG_URL, 'https://chronozarr.org/stores/v03/zarr.json']);
});

test('a ?store= link reads that store and the catalog at once, and not the catalog store', async () => {
  const { fetched } = await runEarlyScript(`${PAGE}?store=${encoded('https://data.example.org/mine')}&t=3`, { [CATALOG_URL]: CATALOG });
  assert.deepEqual(fetched, ['https://data.example.org/mine/zarr.json', CATALOG_URL]);
});

test('an embed reads its store and never the catalog', async () => {
  const { fetched } = await runEarlyScript(`${PAGE}?embed=1&store=${encoded('https://data.example.org/mine')}`, { [CATALOG_URL]: CATALOG });
  assert.deepEqual(fetched, ['https://data.example.org/mine/zarr.json']);
});

test('an embed with no store reads nothing', async () => {
  assert.deepEqual((await runEarlyScript(`${PAGE}?embed=1`, { [CATALOG_URL]: CATALOG })).fetched, []);
});

test('a catalog that cannot be read starts no store read, and fails quietly', async () => {
  const { fetched, reads } = await runEarlyScript(PAGE, {});
  assert.deepEqual(fetched, [CATALOG_URL]);
  assert.equal(await reads.get(CATALOG_URL), null, 'a failed read is null, for the viewer to read again');
  for (const empty of [[], {}, null]) assert.deepEqual((await runEarlyScript(PAGE, { [CATALOG_URL]: empty })).fetched, [CATALOG_URL]);
});

// The address of the early read is the address the reader asks for, or the early response is never used.
test('the early read of a store is the very request that openStore makes for its root metadata', async () => {
  const stores = [
    'https://data.example.org/a/store',
    'https://data.example.org/a/store/',
    'https://data.example.org/a/store?X-Amz-Signature=abc&X-Amz-Expires=60',
    'https://data.example.org/a/store/?sig=1',
    'https://data.example.org:8443/store#fragment',
    '../data/store',
    '/root/store',
  ];
  for (const store of stores) {
    const page = `${PAGE}?embed=1&store=${encoded(store)}`;
    const { fetched } = await runEarlyScript(page);
    assert.equal(fetched.length, 1, store);
    // The viewer resolves the address against the page before it opens the store.
    const address = new URL(store, PAGE).href;
    const files = buildSyntheticStore({ nTime: 2, nBand: 1, height: 8, width: 8, chunk: 8, sharded: false, consolidated: true }).files;
    const requested = [];
    await openStore(address, {
      workers: 0,
      retryDelaysMs: [],
      fetch: async (request) => {
        requested.push(request.url);
        const body = files.get(new URL(request.url).pathname.replace(/^.*?(\/zarr\.json)$/, '$1'));
        return body ? new Response(body, { status: 200 }) : new Response('', { status: 404 });
      },
    });
    assert.equal(fetched[0], requested[0], `${store}: the first read of the reader`);
  }
});

test('the root read is timed from its request to its response, for the viewer to pace its stages by', async () => {
  const { reads } = await runEarlyScript(`${PAGE}?embed=1&store=${encoded('https://data.example.org/mine')}`, { 'https://data.example.org/mine/zarr.json': {} });
  const measured = reads.rootMs;
  assert.ok(measured >= 0, 'a duration in ms');
  assert.equal(takeEarlyRootMs(reads), measured);
  assert.equal(takeEarlyRootMs(reads), undefined, 'taken once');
  const failed = await runEarlyScript(`${PAGE}?embed=1&store=${encoded('https://data.example.org/mine')}`, {});
  assert.equal(takeEarlyRootMs(failed.reads), undefined, 'a read that failed has no round trip to report');
  assert.equal(takeEarlyRootMs(undefined), undefined, 'a page without the inline script');
});

test('takeEarlyRead hands each pending read out once', () => {
  const pending = Promise.resolve(null);
  const reads = new Map([['https://x.example/zarr.json', pending]]);
  assert.equal(takeEarlyRead('https://x.example/other.json', reads), undefined);
  assert.equal(takeEarlyRead('https://x.example/zarr.json', reads), pending);
  assert.equal(takeEarlyRead('https://x.example/zarr.json', reads), undefined);
  assert.equal(takeEarlyRead('https://x.example/zarr.json', undefined), undefined, 'a page without the inline script');
});

test('fetchWithEarlyReads: the first plain GET of an early address is the early response; the next is a real request', async () => {
  const early = new Response('early');
  const reads = new Map([['https://x.example/zarr.json', Promise.resolve(early)]]);
  const calls = [];
  const fetchNow = async (input) => (calls.push(String(input.url ?? input)), new Response('network'));
  const fetchEarly = fetchWithEarlyReads(fetchNow, reads);
  assert.equal(await fetchEarly(new Request('https://x.example/zarr.json')), early);
  assert.equal(await (await fetchEarly(new Request('https://x.example/zarr.json'))).text(), 'network');
  assert.deepEqual(calls, ['https://x.example/zarr.json']);
});

test('fetchWithEarlyReads takes a URL or a string as well as a Request', async () => {
  const reads = new Map([
    ['https://x.example/catalog.json', Promise.resolve(new Response('a'))],
    ['https://x.example/b.json', Promise.resolve(new Response('b'))],
  ]);
  const fetchEarly = fetchWithEarlyReads(() => assert.fail('went to the network'), reads);
  assert.equal(await (await fetchEarly(new URL('https://x.example/catalog.json'))).text(), 'a');
  assert.equal(await (await fetchEarly('https://x.example/b.json')).text(), 'b');
});

test('fetchWithEarlyReads leaves a range read, another method and another address to fetch, and keeps the early read for the plain GET', async () => {
  const early = new Response('early');
  const reads = new Map([['https://x.example/zarr.json', Promise.resolve(early)]]);
  const calls = [];
  const fetchEarly = fetchWithEarlyReads(async (input) => (calls.push(`${input.method} ${input.url} ${input.headers.get('range') ?? ''}`.trim()), new Response('network')), reads);
  await fetchEarly(new Request('https://x.example/zarr.json', { headers: { Range: 'bytes=0-9' } }));
  await fetchEarly(new Request('https://x.example/zarr.json', { method: 'HEAD' }));
  await fetchEarly(new Request('https://x.example/0/data/c/0/0/0/0'));
  assert.deepEqual(calls, ['GET https://x.example/zarr.json bytes=0-9', 'HEAD https://x.example/zarr.json', 'GET https://x.example/0/data/c/0/0/0/0']);
  assert.equal(await fetchEarly(new Request('https://x.example/zarr.json')), early, 'still there for the read it belongs to');
});

test('fetchWithEarlyReads reads again when the early read failed', async () => {
  const reads = new Map([['https://x.example/zarr.json', Promise.resolve(null)]]);
  const fetchEarly = fetchWithEarlyReads(async () => new Response('network'), reads);
  assert.equal(await (await fetchEarly(new Request('https://x.example/zarr.json'))).text(), 'network');
});

test('fetchWithEarlyReads works on a page that has no early reads', async () => {
  const fetchEarly = fetchWithEarlyReads(async () => new Response('network'), undefined);
  assert.equal(await (await fetchEarly(new Request('https://x.example/zarr.json'))).text(), 'network');
});
