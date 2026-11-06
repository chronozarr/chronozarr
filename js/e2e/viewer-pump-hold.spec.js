// The pump that loads ahead of a scrub holds demand for its whole run (ChronoStore.holdDemand). Without the hold, the reader's idle
// prefetch is parked only while a read is in flight, and on a link that carries one step a second the pump asks for one timestep at
// a time: when it lands, demand is zero until the continuation asks for the next, and the prefetch starts requests in that gap.
//
// A store of two levels (four cells, then one) of 40 timesteps whose chunks arrive 100 ms after they are asked for, a scrub of arrow
// keys 140 ms apart and a measured bandwidth of 1 kB/s: the pump loads one timestep at a time and stays behind the scrub.

import { existsSync } from 'node:fs';
import path from 'node:path';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { expect, test } from './fixtures.js';
import { writeTree } from './make-node-fixtures.mjs';

const N_TIME = 40;
const CHUNK_DELAY_MS = 100;
const SCRUB_AFTER_PUMP_MS = 1200;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const SPEC = { nTime: N_TIME, nBand: 1, height: 512, width: 512, chunk: 256, sharded: false, nLevels: 2, consolidated: true, specVersion: '0.3.0', nodata: 0 };

test('the pump of a scrub starts no speculative request while it runs, in the gaps between its reads too', async ({ page, servers, stores }) => {
  const dir = path.join(stores.dir, 'pump-hold');
  if (!existsSync(dir)) await writeTree(dir, buildSyntheticStore(SPEC).files);
  await page.route(`${servers.dataUrl}/**/c/**`, async (route) => {
    await sleep(CHUNK_DELAY_MS);
    await route.continue().catch(() => {});
  });
  const url = `${servers.dataUrl}/pump-hold/`;
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
  await page.waitForFunction(() => window.chronozarr?.viewer?.paintedT === 0, null, { timeout: 30_000 });

  // Open the store again with a fetch that records every chunk request, and record every timestep the viewer asks the reader for.
  await page.evaluate(async (storeUrl) => {
    const { viewer } = window.chronozarr;
    const log = [];
    const spy = (input, init) => {
      const { pathname } = new URL(input.url ?? input);
      const chunk = /\/(\d+)\/data\/c\/(\d+)\/0\/(\d+)\/(\d+)$/.exec(pathname);
      if (chunk) log.push({ kind: 'fetch', key: chunk.slice(1).join('/') });
      return globalThis.fetch(input, init);
    };
    await viewer.loadStore(storeUrl, { fetch: spy });
    const { store } = viewer;
    store.bandwidthEstimate = () => 1000;
    const ensureRaw = store.ensureRaw.bind(store);
    store.ensureRaw = (lod, row, col, t, options) => {
      log.push({ kind: 'ask', key: [lod, t, row, col].join('/'), ahead: t > viewer.t });
      return ensureRaw(lod, row, col, t, options);
    };
    window.__log = log;
    window.__keys = setInterval(() => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })), 140);
  }, url);

  await page.waitForFunction(() => window.__log.some((entry) => entry.kind === 'ask' && entry.ahead), null, { timeout: 15_000 });
  await sleep(SCRUB_AFTER_PUMP_MS);
  const log = await page.evaluate(() => {
    clearInterval(window.__keys);
    return window.__log;
  });

  // From the first timestep the pump asked for ahead of the scrub on: a request for a chunk that nobody had asked for is the prefetch's.
  const pumpStart = log.findIndex((entry) => entry.kind === 'ask' && entry.ahead);
  const asked = new Set(log.slice(0, pumpStart).filter((entry) => entry.kind === 'ask').map((entry) => entry.key));
  const speculative = [];
  let pumped = 0;
  for (const entry of log.slice(pumpStart)) {
    if (entry.kind === 'ask') {
      asked.add(entry.key);
      if (entry.ahead) pumped++;
    } else if (!asked.has(entry.key)) speculative.push(entry.key);
  }
  expect(pumped, 'timesteps the pump asked for ahead of the scrub').toBeGreaterThanOrEqual(6);
  expect(speculative, 'chunks the prefetch started while the pump ran').toEqual([]);
});
