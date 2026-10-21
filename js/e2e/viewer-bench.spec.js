// `chronozarr.bench(options)` hands its options to the benchmark: the speed-gate job (support/gate-bench/run.mjs) asks
// for fewer cold opens and switches than the console default, so that it can afford a run of each build several times.

import { expect, test } from './fixtures.js';
import { openViewer } from './viewer-helpers.js';

const WARM_LOOPS = ['warmSwitchStepping', 'warmSwitchJumping', 'warmSwitchUploadFromCache'];

test('bench() runs the cold opens and switches it is asked for', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  const results = await page.evaluate(() => window.chronozarr.bench({ coldRuns: 2, switches: 4 }));
  expect(results.coldOpenConsolidated).toHaveLength(2);
  expect(results.coldOpenPerArrayMetadata).toHaveLength(2);
  for (const phase of WARM_LOOPS) expect(results[phase].n, phase).toBe(4);
});

// A switch is synchronous, so no response can arrive inside it: the bytes of a warm loop are those that arrive between its
// first switch and a settle after its last. Every chunk of this store is 2 MiB on the wire (no compression, one object per
// chunk), and on a local server what a loop fetches arrives within the settle.
test('bench() reports the bytes that its warm loops fetch', async ({ page, servers, storeUrl }) => {
  const CHUNK_BYTES = 512 * 512 * 4 * 2;
  await openViewer(page, servers, await storeUrl('beyond_idle'));
  const results = await page.evaluate(() => window.chronozarr.bench({ coldRuns: 1, switches: 8 }));
  const stepping = results.warmSwitchStepping;
  expect(stepping.wireBytes, 'stepping past the prefetched steps fetches chunks').toBeGreaterThanOrEqual(CHUNK_BYTES);
  for (const phase of WARM_LOOPS) {
    const { networkRequests, wireBytes, incomplete } = results[phase];
    expect(wireBytes % CHUNK_BYTES, `${phase}: whole chunks`).toBe(0);
    expect(wireBytes, `${phase}: no more than its requests carry`).toBeLessThanOrEqual(networkRequests * CHUNK_BYTES);
    expect(incomplete, `${phase}: frames drawn without every cell`).toBeGreaterThanOrEqual(0);
  }
});
