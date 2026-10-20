// `chronozarr.bench(options)` hands its options to the benchmark: the speed-gate job (support/gate-bench/run.mjs) asks
// for fewer cold opens and switches than the console default, so that it can afford a run of each build several times.

import { expect, test } from './fixtures.js';
import { openViewer } from './viewer-helpers.js';

test('bench() runs the cold opens and switches it is asked for', async ({ page, servers, storeUrl }) => {
  await openViewer(page, servers, await storeUrl('u16_plain'));
  const results = await page.evaluate(() => window.chronozarr.bench({ coldRuns: 2, switches: 4 }));
  expect(results.coldOpenConsolidated).toHaveLength(2);
  expect(results.coldOpenPerArrayMetadata).toHaveLength(2);
  for (const phase of ['warmSwitchStepping', 'warmSwitchJumping', 'warmSwitchUploadFromCache']) expect(results[phase].n, phase).toBe(4);
});
