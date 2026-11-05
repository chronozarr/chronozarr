// The buffer pump that loads ahead of a scrub asks for the next timesteps a batch at a time. Loaded side by side the steps
// of a batch share the link and arrive late together, the nearest one too, so on a link that carries one step a second
// the batch is one step (bufferBatch, as for a movie).
//
// A store of two levels (four cells, then one) whose chunks from timestep 12 on never arrive, so that a scrub that starts at
// the first timestep, with the coarse level of the ones before 12 in memory, has timesteps to load ahead of it and the pump's
// first batch stays on the wire. The measured bandwidth is set to 1 kB/s.

import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { expect, test } from './fixtures.js';

const N_TIME = 20;
const FIRST_HELD = 12;

const SPEC = { nTime: N_TIME, nBand: 1, height: 512, width: 512, chunk: 256, sharded: false, nLevels: 2, consolidated: true, specVersion: '0.3.0', nodata: 0 };

async function writeScrubStore(dir) {
  for (const [key, bytes] of buildSyntheticStore(SPEC).files) {
    const file = path.join(dir, 'scrub-batch', key);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, bytes);
  }
}

test('a scrub loads one timestep at a time on a link that carries one step a second', async ({ page, servers, stores }) => {
  await writeScrubStore(stores.dir);
  let release;
  const held = new Promise((resolve) => {
    release = resolve;
  });
  await page.route(`${servers.dataUrl}/**/c/**`, async (route) => {
    const t = Number(/\/c\/(\d+)\//.exec(new URL(route.request().url()).pathname)?.[1]);
    if (t >= FIRST_HELD) await held;
    await route.continue().catch(() => {});
  });
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(`${servers.dataUrl}/scrub-batch/`)}`);
  await page.waitForFunction(() => window.chronozarr?.viewer?.paintedT === 0, null, { timeout: 30_000 });

  // The coarse level of the timesteps that arrive is in memory, so the first timestep the pump asks for is ahead of the one on screen.
  await page.evaluate(async (count) => {
    const { store } = window.chronozarr.viewer;
    await Promise.all(Array.from({ length: count }, (_, t) => store.ensureRaw(1, 0, 0, t)));
  }, FIRST_HELD);

  // Record every ensureRaw call with the timestep on screen and the turn of the event loop it was made in: a batch is asked for in one turn.
  await page.evaluate(() => {
    const { viewer } = window.chronozarr;
    const { store } = viewer;
    store.bandwidthEstimate = () => 1000;
    const calls = [];
    let turn = 0;
    let open = false;
    const ensureRaw = store.ensureRaw.bind(store);
    store.ensureRaw = (lod, row, col, t, options) => {
      if (!open) {
        open = true;
        turn++;
        setTimeout(() => {
          open = false;
        }, 0);
      }
      calls.push({ lod, t, turn, ahead: t > viewer.t });
      return ensureRaw(lod, row, col, t, options);
    };
    window.__calls = calls;
    // Arrow keys 90 ms apart: a scrub of about 11 steps a second.
    window.__keys = setInterval(() => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })), 90);
  });
  await page.waitForFunction(() => window.__calls.some((call) => call.ahead), null, { timeout: 15_000 });
  const batches = await page.evaluate(() => {
    clearInterval(window.__keys);
    const byTurn = new Map();
    for (const { lod, t, turn, ahead } of window.__calls) {
      const key = `${turn}/${lod}`;
      byTurn.set(key, { ahead: ahead || byTurn.get(key)?.ahead, steps: new Set([...(byTurn.get(key)?.steps ?? []), t]) });
    }
    return [...byTurn.values()].map(({ ahead, steps }) => ({ ahead, steps: [...steps].sort((a, b) => a - b) }));
  });
  release();

  const pumped = batches.filter((batch) => batch.ahead);
  expect(pumped.length, 'the pump asked for timesteps ahead of the scrub').toBeGreaterThan(0);
  expect(Math.max(...pumped.map((batch) => batch.steps.length)), `timesteps asked for together: ${JSON.stringify(pumped)}`).toBe(1);
});
