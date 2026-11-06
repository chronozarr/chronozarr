// The pump that loads ahead of a scrub holds demand for its whole run (ChronoStore.holdDemand). Without the hold, the reader's idle
// prefetch is parked only while a read is in flight, and on a link that carries one step a second the pump asks for one timestep at
// a time: when it lands, demand is zero until the continuation asks for the next, and the prefetch starts requests in that gap.
//
// Pause the page clock during the scrub. Release its coarse requests one at a time while a native-level prefetch waits.

import path from 'node:path';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { expect, test } from './fixtures.js';
import { writeTree } from './make-node-fixtures.mjs';

const N_TIME = 10;
const SPEC = { nTime: N_TIME, nBand: 1, height: 512, width: 512, chunk: 256, sharded: false, nLevels: 2, consolidated: true, specVersion: '0.3.0', nodata: 0 };

test('the pump of a scrub starts no speculative request while it runs, in the gaps between its reads too', async ({ page, servers, stores }) => {
  const dir = path.join(stores.dir, 'pump-hold');
  await writeTree(dir, buildSyntheticStore(SPEC).files);
  const url = `${servers.dataUrl}/pump-hold/`;
  await page.clock.install();
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
  await page.waitForFunction(() => window.chronozarr?.viewer?.paintedT === 0, null, { timeout: 30_000 });
  await page.clock.pauseAt(new Date(Date.now() + 1000));

  await page.evaluate(async (storeUrl) => {
    const { viewer } = window.chronozarr;
    const releases = new Map();
    const requests = [];
    let scrubbing = false;
    const spy = async (input, init) => {
      const { pathname } = new URL(input.url ?? input);
      const chunk = /\/(\d+)\/data\/c\/(\d+)\//.exec(pathname);
      if (scrubbing && chunk) {
        const [lod, t] = chunk.slice(1).map(Number);
        requests.push({ lod, t });
        if (lod === 1) await new Promise((resolve) => releases.set(t, resolve));
      }
      return globalThis.fetch(input, init);
    };
    // With the clock paused, the reopened viewer does not launch its idle prefetch timer.
    await viewer.loadStore(storeUrl, { fetch: spy, workers: 0 });
    const { store } = viewer;
    await Promise.all([1, 2].map((t) => store.ensureRaw(1, 0, 0, t)));
    store.bandwidthEstimate = () => 1000;
    window.__requests = requests;
    window.__release = (t) => releases.get(t)();
    scrubbing = true;
    viewer.goToTime(1);
    viewer.goToTime(2);
    viewer.renderNow();
    window.__prefetch = store.prefetch({ lod: 0, cells: [[0, 0]], t: 2, maxSteps: 1 });
  }, url);

  const requested = () => page.evaluate(() => window.__requests);
  for (let t = 3; t < N_TIME; t++) {
    await expect.poll(requested).toContainEqual({ lod: 1, t });
    expect((await requested()).filter(({ lod }) => lod === 0), `speculative requests while coarse step ${t} is in flight`).toEqual([]);
    await page.evaluate((step) => window.__release(step), t);
  }
  const result = await page.evaluate(() => window.__prefetch);
  expect(result.errors).toEqual([]);
  expect(result.fetched, 'prefetch resumes once the pump releases demand').toBe(1);
  expect((await requested()).at(-1)).toEqual({ lod: 0, t: 2 });
});
