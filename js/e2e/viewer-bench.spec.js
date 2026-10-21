// `chronozarr.bench(options)` hands its options to the benchmark: the speed-gate job (support/gate-bench/run.mjs) asks
// for fewer cold opens and switches than the console default, so that it can afford a run of each build several times.

import { expect, test } from './fixtures.js';
import { openViewer } from './viewer-helpers.js';

const WARM_LOOPS = ['warmSwitchStepping', 'warmSwitchJumping', 'warmSwitchUploadFromCache'];
// Every chunk of the beyond_idle store is 2 MiB on the wire: no compression, one object per chunk.
const CHUNK_BYTES = 512 * 512 * 4 * 2;

test('bench() runs the cold opens and switches it is asked for', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  const results = await page.evaluate(() => window.chronozarr.bench({ coldRuns: 2, switches: 4 }));
  expect(results.coldOpenConsolidated).toHaveLength(2);
  expect(results.coldOpenPerArrayMetadata).toHaveLength(2);
  for (const phase of WARM_LOOPS) expect(results[phase].n, phase).toBe(4);
});

// The idle prefetch before the warm loops holds every chunk of this store, so nothing that happened before a loop (the cold
// opens, the prefetch) or in another loop may show up in its counts.
test('bench() reports no traffic for the warm loops of a store that the idle prefetch holds', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  const results = await page.evaluate(() => window.chronozarr.bench({ coldRuns: 1, switches: 4 }));
  for (const phase of WARM_LOOPS) {
    expect(results[phase].networkRequests, `${phase}: requests`).toBe(0);
    expect(results[phase].wireBytes, `${phase}: bytes`).toBe(0);
  }
});

// A switch is synchronous, so no response can arrive inside it: the bytes of a warm loop are those that arrive between its
// first switch and a settle after its last. On a local server what a loop fetches arrives within the settle.
test('bench() reports the bytes that its warm loops fetch', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('beyond_idle'));
  const results = await page.evaluate(() => window.chronozarr.bench({ coldRuns: 1, switches: 8 }));
  const stepping = results.warmSwitchStepping;
  expect(stepping.wireBytes, 'stepping past the prefetched steps fetches chunks').toBeGreaterThanOrEqual(CHUNK_BYTES);
  for (const phase of WARM_LOOPS) {
    const { networkRequests, wireBytes } = results[phase];
    expect(wireBytes % CHUNK_BYTES, `${phase}: whole chunks`).toBe(0);
    expect(wireBytes, `${phase}: no more than its requests carry`).toBeLessThanOrEqual(networkRequests * CHUNK_BYTES);
  }
});

// With every chunk response 400 ms late, the last requests of the stepping loop are still on the way 1 s after its last
// switch. The loop waits for them: none is in flight when it stops counting, and no later loop receives bytes it did not
// ask for. Its bytes do not equal its requests times a chunk, because the viewer aborts some of its prefetch requests
// as the loop moves on.
test('bench() counts the responses that arrive after a warm loop has switched for the last time', async ({ page, servers, storeUrl }) => {
  const url = await storeUrl('beyond_idle');
  await page.route(
    (address) => address.href.startsWith(url) && address.pathname.includes('/data/c/'),
    async (route) => {
      const response = await route.fetch();
      await new Promise((resolve) => setTimeout(resolve, 400));
      await route.fulfill({ response });
    },
  );
  await openViewer(page, servers, url);
  const results = await page.evaluate(() => window.chronozarr.bench({ coldRuns: 1, switches: 8 }));
  expect(results.warmSwitchStepping.wireBytes, 'stepping past the prefetched steps fetches chunks').toBeGreaterThanOrEqual(CHUNK_BYTES);
  for (const phase of WARM_LOOPS) {
    const { networkRequests, wireBytes, inflightAtEnd } = results[phase];
    expect(inflightAtEnd, `${phase}: requests still on the way when it stopped counting`).toBe(0);
    expect(wireBytes, `${phase}: no bytes it did not ask for`).toBeLessThanOrEqual(networkRequests * CHUNK_BYTES);
  }
});
