// The buffer pump that loads ahead of a scrub keeps a few timesteps in flight, as many as the measured link carries in a
// second (bufferBatch, as for a movie): loaded side by side, steps share the link and arrive late together, the nearest one
// too, so on a link that carries one step a second the pump asks for one step at a time. The next step is asked for as soon
// as one lands, not when the whole group has, so what is in flight stays full.
//
// A store of two levels (four cells, then one) whose chunks from timestep 12 on arrive when the test lets them, so that a
// scrub that starts at the first timestep, with the coarse level of the ones before 12 in memory, has timesteps to load
// ahead of it and the pump's requests stay on the wire.

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

/**
 * Open the store with the chunks of the timesteps from FIRST_HELD on held back, and start a scrub (arrow keys 140 ms apart, about 7
 * steps a second, over timesteps 1 to 11) with the measured bandwidth at `bandwidth` bytes per second. Returns `release(t)`, which lets
 * the chunks of timestep t arrive, and `asked(t)`, whether the pump has asked for timestep t ahead of the one on screen.
 */
async function startScrub({ page, servers, stores }, bandwidth) {
  await writeScrubStore(stores.dir);
  const gates = new Map();
  const gate = (t) => {
    if (!gates.has(t)) {
      const held = {};
      held.promise = new Promise((resolve) => {
        held.release = resolve;
      });
      gates.set(t, held);
    }
    return gates.get(t);
  };
  await page.route(`${servers.dataUrl}/**/c/**`, async (route) => {
    const t = Number(/\/c\/(\d+)\//.exec(new URL(route.request().url()).pathname)?.[1]);
    if (t >= FIRST_HELD) await gate(t).promise;
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
  await page.evaluate((estimate) => {
    const { viewer } = window.chronozarr;
    const { store } = viewer;
    store.bandwidthEstimate = () => estimate;
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
      calls.push({ lod, t, turn, ahead: t > viewer.t, signal: options?.signal });
      return ensureRaw(lod, row, col, t, options);
    };
    window.__calls = calls;
    window.__keys = setInterval(() => document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })), 140);
  }, bandwidth);
  return {
    release: (t) => gate(t).release(),
    asked: (...steps) => page.waitForFunction((wanted) => wanted.every((t) => window.__calls.some((call) => call.ahead && call.t === t)), steps, { timeout: 15_000 }),
    notAsked: (t) => page.evaluate((step) => !window.__calls.some((call) => call.ahead && call.t === step), t),
  };
}

test('a scrub loads one timestep at a time on a link that carries one step a second', async ({ page, servers, stores }) => {
  const { release, asked } = await startScrub({ page, servers, stores }, 1000);
  await asked(12);
  const batches = await page.evaluate(() => {
    clearInterval(window.__keys);
    const byTurn = new Map();
    for (const { lod, t, turn, ahead } of window.__calls) {
      const key = `${turn}/${lod}`;
      byTurn.set(key, { ahead: ahead || byTurn.get(key)?.ahead, steps: new Set([...(byTurn.get(key)?.steps ?? []), t]) });
    }
    return [...byTurn.values()].map(({ ahead, steps }) => ({ ahead, steps: [...steps].sort((a, b) => a - b) }));
  });
  for (let t = FIRST_HELD; t < N_TIME; t++) release(t);

  const pumped = batches.filter((batch) => batch.ahead);
  expect(pumped.length, 'the pump asked for timesteps ahead of the scrub').toBeGreaterThan(0);
  expect(Math.max(...pumped.map((batch) => batch.steps.length)), `timesteps asked for together: ${JSON.stringify(pumped)}`).toBe(1);
});

test('on a link that carries one step a second the next timestep is asked for when the previous one lands', async ({ page, servers, stores }) => {
  const { release, asked, notAsked } = await startScrub({ page, servers, stores }, 1000);
  await asked(12);
  expect(await notAsked(13), 'one on the wire').toBe(true);
  release(12);
  await asked(13);
  expect(await notAsked(14), 'and again one').toBe(true);
  for (let t = FIRST_HELD; t < N_TIME; t++) release(t);
});

test('on a fast link three timesteps stay in flight: the next is asked for as soon as one lands, not when all three have', async ({ page, servers, stores }) => {
  const { release, asked, notAsked } = await startScrub({ page, servers, stores }, 1e12);
  await asked(12, 13, 14);
  expect(await notAsked(15), 'three on the wire').toBe(true);
  release(12);
  await asked(15);
  expect(await notAsked(16), 'three again, with 13 and 14 still on the wire').toBe(true);
  for (let t = FIRST_HELD; t < N_TIME; t++) release(t);
});

test('a jump cancels what the pump has on the wire for the old position', async ({ page, servers, stores }) => {
  const { release, asked } = await startScrub({ page, servers, stores }, 1e12);
  await asked(12, 13, 14);
  const before = await page.evaluate(() => window.__calls.filter(({ t }) => t >= 12 && t <= 14).map(({ signal }) => signal.aborted));
  expect(before, 'the three timesteps are wanted while the scrub goes on').not.toContain(true);

  // A jump to the last timestep, far from the scrub's: the three steps ahead of the old position are not wanted any more.
  const after = await page.evaluate((t) => {
    clearInterval(window.__keys);
    window.chronozarr.viewer.goToTime(t);
    return window.__calls.filter((call) => call.t >= 12 && call.t <= 14).map(({ signal }) => signal.aborted);
  }, N_TIME - 1);
  for (let t = FIRST_HELD; t < N_TIME; t++) release(t);
  expect(after, 'the pump cancelled its three timesteps on the wire').not.toContain(false);
});
