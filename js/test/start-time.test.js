// Where the full viewer starts in time: only a bare visit to the demo chooses; any link to a view keeps t=0 when it
// leaves t out. The rule reads the coarsest level's coverage, else its mask, else nothing.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openStore } from '../chronozarr/decoder.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';
import { isBareVisit, latestCoveredTime } from '../demo/start-time.js';

/** A reader-shaped store whose coarsest level is one 4x4 chunk over a 3x3 level; `shares[t]` of the 9 pixels are set. */
function fakeStore({ shares, kind = 'coverage' }) {
  const reads = [];
  const chunk = (t) => {
    const values = new Uint8Array(16);
    const on = Math.round(shares[t] * 9);
    let set = 0;
    for (let y = 0; y < 3; y++) for (let x = 0; x < 3; x++) if (set < on) (values[y * 4 + x] = 2), set++;
    values[3] = values[15] = 9; // padding outside the 3x3 level: never counted
    return values;
  };
  const get = (lod, row, col, t) => {
    reads.push(t);
    return Promise.resolve(chunk(t));
  };
  return {
    reads,
    times: shares.map((_, t) => t),
    levels: [{ lod: 0 }, { lod: 1, height: 3, width: 3, chunkHeight: 4, chunkWidth: 4, gridRows: 1, gridCols: 1 }],
    hasCoverage: kind === 'coverage',
    hasMask: kind === 'mask',
    getCoverage: get,
    getMask: get,
  };
}

test('a bare visit is an address with no store and no view; anything else is a link to a view', () => {
  assert.equal(isBareVisit(''), true);
  assert.equal(isBareVisit('?utm_source=talk'), true);
  for (const search of ['?t=3', '?p=ndvi', '?b=B08', '?r=0,1', '?z=2', '?c=1,2', '?store=https%3A%2F%2Fx.org%2Fs']) {
    assert.equal(isBareVisit(search), false, search);
  }
});

test('starts at the latest timestep whose coverage reaches half, reading newest first and stopping there', async () => {
  const store = fakeStore({ shares: [1, 1, 0.8, 0.2, 0.1] });
  assert.equal(await latestCoveredTime(store), 2);
  assert.deepEqual(store.reads, [4, 3, 2], 'stops at the first timestep that qualifies');
});

test('a well-covered last timestep costs one read', async () => {
  const store = fakeStore({ shares: [0.2, 0.9] });
  assert.equal(await latestCoveredTime(store), 1);
  assert.deepEqual(store.reads, [1]);
});

test('uses the mask when the store has no coverage plane', async () => {
  assert.equal(await latestCoveredTime(fakeStore({ shares: [1, 0.6, 0.3], kind: 'mask' })), 1);
});

test('a store with neither plane starts at its last timestep and reads nothing', async () => {
  const store = fakeStore({ shares: [0, 0, 0], kind: 'none' });
  assert.equal(await latestCoveredTime(store), 2);
  assert.deepEqual(store.reads, []);
});

test('when no recent timestep reaches half, the best covered one wins, within the lookback', async () => {
  const store = fakeStore({ shares: [1, 0.4, 0.1, 0.3] });
  assert.equal(await latestCoveredTime(store, { lookback: 3 }), 1, 'timestep 0 is outside the lookback');
});

test('padding outside the level is not counted: a fully observed 3x3 level is 100 %, not 9/16', async () => {
  assert.equal(await latestCoveredTime(fakeStore({ shares: [1, 1] }), { share: 1 }), 1);
});

test('reads the coarsest level of a real store through the reader', async () => {
  const spec = { nTime: 4, nBand: 1, height: 200, width: 200, chunk: 128, sharded: false, nLevels: 2, mask: true, consolidated: true };
  const store = await openStore('memory://mask', { store: buildSyntheticStore(spec), workers: 0 });
  try {
    // The synthetic mask is valid on three pixels in four at every timestep, so the last timestep qualifies.
    assert.equal(await latestCoveredTime(store), 3);
    assert.equal(await latestCoveredTime(store, { share: 0.9 }), 3, 'none reaches 90 %: the best of the lookback, newest on a tie');
  } finally {
    store.close();
  }
});
