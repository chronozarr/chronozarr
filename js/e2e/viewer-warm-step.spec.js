// A manual step less than 300 ms after the one before is a scrub, and a scrub loads ahead of itself with a pump (see
// viewer-scrub-batch.spec.js). When the timestep it steps to and the whole lookahead are in memory already there is nothing to
// load, and a step that draws from memory must not start a pump: its controller, its checks of what is ready and its sizing from
// the measured bandwidth are work on the path of every warm step.
//
// 36 cells at level 0 (as the gate bench's store), three levels, every chunk of level 0 loaded before the steps, which are 120 ms
// apart as the gate bench's are. The level is not pinned: a pinned level (the gate bench's) has no scrub level to choose, so no pump.

import { existsSync } from 'node:fs';
import path from 'node:path';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { expect, test } from './fixtures.js';
import { writeTree } from './make-node-fixtures.mjs';

const STEPS = 8;
const STEP_PAUSE_MS = 120;
const SPEC = { nTime: 12, nBand: 1, height: 384, width: 384, chunk: 64, sharded: false, nLevels: 3, consolidated: true, specVersion: '0.3.0', nodata: 0 };

test('a step to a timestep that is in memory, with the lookahead in memory too, starts no pump', async ({ page, servers, stores }) => {
  const dir = path.join(stores.dir, 'warm-step');
  if (!existsSync(dir)) await writeTree(dir, buildSyntheticStore(SPEC).files);
  const url = `${servers.dataUrl}/warm-step/`;
  await page.goto(`${servers.appUrl}/demo/index.html?store=${encodeURIComponent(url)}`);
  await page.waitForFunction(() => window.chronozarr?.viewer?.paintedT === 0, null, { timeout: 30_000 });
  await page.evaluate(async (storeUrl) => {
    const { viewer } = window.chronozarr;
    await viewer.loadStore(storeUrl, { maxCacheBytes: 2 * 1024 ** 3 });
    const { store } = viewer;
    const level = store.levels[0];
    const reads = [];
    for (let t = 0; t < store.times.length; t++) for (let row = 0; row < level.gridRows; row++) for (let col = 0; col < level.gridCols; col++) reads.push(store.getRaw(0, row, col, t));
    await Promise.all(reads);
    for (let quiet = 0; quiet < 10; quiet = store.stats.network.inflight === 0 ? quiet + 1 : 0) await new Promise((resolve) => setTimeout(resolve, 50));
  }, url);

  const steps = await page.evaluate(
    async ({ count, pauseMs }) => {
      const { viewer } = window.chronozarr;
      const { store, renderer } = viewer;
      // A pump starts with a controller of its own, and nothing else that a step does while it draws from memory makes one.
      let controllers = 0;
      let asked = 0;
      const Native = window.AbortController;
      window.AbortController = class extends Native {
        constructor() {
          super();
          controllers++;
        }
      };
      const ensureRaw = store.ensureRaw.bind(store);
      store.ensureRaw = (...args) => {
        asked++;
        return ensureRaw(...args);
      };
      const result = [];
      try {
        viewer.goToTime(0);
        viewer.renderNow();
        for (let t = 1; t <= count; t++) {
          await new Promise((resolve) => setTimeout(resolve, pauseMs));
          controllers = 0;
          asked = 0;
          viewer.goToTime(t);
          const frame = viewer.renderNow();
          renderer.finish();
          result.push({ t, controllers, asked, lod: frame.lod, cells: frame.cells, complete: frame.complete });
        }
      } finally {
        window.AbortController = Native;
        store.ensureRaw = ensureRaw;
      }
      return result;
    },
    { count: STEPS, pauseMs: STEP_PAUSE_MS },
  );

  expect(steps.map((step) => step.complete), 'every frame is complete, at level 0 with its 36 cells').toEqual(Array(STEPS).fill(true));
  expect(steps.map((step) => `${step.lod}/${step.cells}`)).toEqual(Array(STEPS).fill('0/36'));
  expect(steps.map((step) => step.asked), 'chunks asked for').toEqual(Array(STEPS).fill(0));
  expect(steps.map((step) => step.controllers), 'pumps started').toEqual(Array(STEPS).fill(0));
});
