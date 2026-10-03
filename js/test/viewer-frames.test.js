import { test } from 'node:test';
import assert from 'node:assert/strict';
import { chooseFrame } from '../demo/frames.js';

/** `ready` lists the [lod, t] frames that are complete in memory. */
const choose = (ready, options) => {
  const have = new Set(ready.map(([lod, t]) => `${lod}/${t}`));
  return chooseFrame({ coarsestLod: 3, isReady: (lod, t) => have.has(`${lod}/${t}`), ...options });
};

test('the target level is shown when it is complete, whatever else is there', () => {
  assert.deepEqual(choose([[1, 5], [2, 5], [3, 5]], { targetLod: 1, t: 5 }), { lod: 1, t: 5, kind: 'target' });
});

test('otherwise the finest coarser level that is complete: the same timestep, less resolution', () => {
  assert.deepEqual(choose([[2, 5], [3, 5]], { targetLod: 1, t: 5 }), { lod: 2, t: 5, kind: 'fallback' });
  assert.deepEqual(choose([[3, 5]], { targetLod: 0, t: 5 }), { lod: 3, t: 5, kind: 'fallback' });
  assert.deepEqual(choose([[0, 4], [1, 4], [3, 5]], { targetLod: 1, t: 5, previous: { lod: 1, t: 4 } }), { lod: 3, t: 5, kind: 'fallback' }, 'the right timestep at a coarse level beats the previous frame');
});

test('with no level complete for this timestep the frame on screen stays, if it is still complete for the view', () => {
  assert.deepEqual(choose([[1, 4]], { targetLod: 1, t: 5, previous: { lod: 1, t: 4 } }), { lod: 1, t: 4, kind: 'previous' });
  assert.equal(choose([], { targetLod: 1, t: 5, previous: { lod: 1, t: 4 } }), null, 'it is not complete any more (the view moved): nothing is drawn');
  assert.equal(choose([[1, 4]], { targetLod: 1, t: 5 }), null, 'nothing on screen yet');
});

test('the previous frame at a finer level than the target is allowed (after a zoom out); one at the target timestep is not offered twice', () => {
  assert.deepEqual(choose([[0, 4]], { targetLod: 2, t: 5, previous: { lod: 0, t: 4 } }), { lod: 0, t: 4, kind: 'previous' });
  assert.equal(choose([], { targetLod: 1, t: 5, previous: { lod: 2, t: 5 } }), null, 'the previous frame is a level already tried for this timestep');
});

test('the coarsest level bounds the search', () => {
  assert.equal(choose([[4, 5]], { targetLod: 1, t: 5, coarsestLod: 3 }), null);
  assert.deepEqual(choose([[3, 5]], { targetLod: 3, t: 5 }), { lod: 3, t: 5, kind: 'target' });
});
