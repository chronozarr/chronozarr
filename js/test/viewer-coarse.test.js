import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ASSUMED_BANDWIDTH, COARSE_FRAME_BYTES, COARSE_LOOP_MIN_STEPS, COARSE_LOOP_SECONDS, COARSE_MAX_OVERHEAD, COARSE_MIN_DIRECT_MS, ancestorCells, coarseLoopDrifted, coarseLoopSteps, planCoarseStages, stageLeadMs } from '../demo/coarse.js';

const MB = 1024 * 1024;

test('ancestorCells: each cell lies in (row >> k, col >> k) of a level k coarser; duplicates collapse', () => {
  const cells = [[0, 0], [0, 1], [1, 0], [1, 1], [2, 3], [5, 7]];
  assert.deepEqual(ancestorCells(cells, 0, 0), cells, 'the same level');
  assert.deepEqual(ancestorCells(cells, 0, 1), [[0, 0], [1, 1], [2, 3]]);
  assert.deepEqual(ancestorCells(cells, 1, 3), [[0, 0], [1, 1]], 'levels 1 to 3: shift by 2');
  assert.deepEqual(ancestorCells([[5, 7]], 0, 3), [[0, 0]]);
  assert.deepEqual(ancestorCells([[9, 4]], 2, 3), [[4, 2]]);
  assert.deepEqual(ancestorCells([], 0, 2), []);
});

// Ucayali: 4 bands uint16; decoded bytes of the whole scene per level: 1 cell at level 3, 4 at level 2, 9 at level 1.
const ucayali = (lod) => ({ 0: 70 * MB, 1: 11.5 * MB, 2: 3.8 * MB, 3: 0.95 * MB })[lod];
const MBPS = 1024 * 1024; // bandwidth in bytes per second

test('coarse stages: the finest level whose frame is small, then finer levels while the previews stay cheap', () => {
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: ucayali }), [3], 'level 2 would take the previews to 41 % of the target');
  assert.deepEqual(planCoarseStages({ targetLod: 0, coarsestLod: 3, frameBytes: ucayali }), [3, 2], '0.95 + 3.8 MB of 70 MB; level 1 would be 23 %');
  assert.deepEqual(planCoarseStages({ targetLod: 2, coarsestLod: 3, frameBytes: ucayali, bandwidth: 2 * MBPS }), [], 'a 4 cell frame on a slow link: level 3 alone is 25 % of it');
  const small = (lod) => ({ 0: 40 * MB, 1: 10 * MB, 2: 1.2 * MB, 3: 0.3 * MB })[lod];
  assert.deepEqual(planCoarseStages({ targetLod: 0, coarsestLod: 3, frameBytes: small }), [2], 'the finest level under the limit is the first stage; level 1 would be 28 %');
  const huge = (lod) => ({ 0: 4000 * MB, 1: 100 * MB, 2: 10 * MB, 3: 1 * MB })[lod];
  assert.deepEqual(planCoarseStages({ targetLod: 0, coarsestLod: 3, frameBytes: huge }), [3, 2, 1], 'a frame big enough to afford every level: 2.8 % in all');
  const large = (lod) => ({ 0: 400 * MB, 1: 100 * MB, 2: 10 * MB, 3: 1 * MB })[lod];
  assert.deepEqual(planCoarseStages({ targetLod: 0, coarsestLod: 3, frameBytes: large }), [3, 2], 'level 1 would make it 28 %');
});

test('coarse stages: a frame that would arrive within a second is loaded directly, on the measured bandwidth or the assumed 50 Mbit/s', () => {
  const frame = (bytes) => (lod) => (lod === 1 ? bytes : 0.2 * MB);
  assert.equal(ASSUMED_BANDWIDTH, 6.25e6);
  assert.equal(COARSE_MIN_DIRECT_MS, 1000);
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: frame(6 * MB) }), [], '6 MB x 0.8 at 6.25 MB/s is 0.8 s');
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: frame(9 * MB) }), [2], '1.2 s: the small level 2 is the stage');
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: frame(9 * MB), bandwidth: 100e6 }), [], 'fast link: nothing to hide');
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: frame(3 * MB), bandwidth: 2 * MBPS }), [2], 'slow link: worth it');
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: frame(9 * MB), wireRatio: 0.4 }), [], 'better compression: 0.6 s');
});

test('coarse stages: none at the coarsest level, or when even the first stage is too big a share of the frame', () => {
  assert.deepEqual(planCoarseStages({ targetLod: 3, coarsestLod: 3, frameBytes: ucayali }), [], 'already at the coarsest level');
  assert.deepEqual(planCoarseStages({ targetLod: 2, coarsestLod: 3, frameBytes: ucayali }), [], 'Ucayali\'s 4 cell frame takes 0.5 s at 50 Mbit/s: directly');
  const close = (lod) => (lod === 1 ? 2.5 * MB : 0.95 * MB);
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: close, bandwidth: 0.5 * MBPS }), [], 'slow link, but the target is barely bigger than the stage');
  const worthIt = (lod) => (lod === 1 ? 10 * 0.95 * MB : 0.95 * MB);
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: worthIt, bandwidth: 0.5 * MBPS }), [2]);
});

test('coarse stages: when even the coarsest frame is big the coarsest level is still the first stage, if the frame can afford it', () => {
  const big = (lod) => [400, 160, 60, 12][lod] * MB;
  assert.deepEqual(planCoarseStages({ targetLod: 0, coarsestLod: 3, frameBytes: big }), [3], '12 MB of 400 MB; level 2 would make it 18 %');
  assert.deepEqual(planCoarseStages({ targetLod: 2, coarsestLod: 3, frameBytes: big }), [], '12 MB of 60 MB is 20 %');
});

test('coarse stages: limits can be overridden', () => {
  assert.equal(COARSE_FRAME_BYTES, 1.5 * MB);
  assert.equal(COARSE_MAX_OVERHEAD, 0.15);
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: ucayali, maxBytes: 5 * MB }), [], 'level 2 now counts as small but is 33 % of the target');
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: ucayali, maxBytes: 5 * MB, maxOverhead: 0.4 }), [2]);
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: ucayali, maxOverhead: 0.5 }), [3, 2]);
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: ucayali, maxOverhead: 0.01 }), []);
  assert.deepEqual(planCoarseStages({ targetLod: 1, coarsestLod: 3, frameBytes: ucayali, minDirectMs: 1e9 }), []);
});

test('stage lead: a round trip plus the time the stage\'s bytes take at the link\'s rate', () => {
  const coarse = 0.7e6; // the level-3 chunk of the Ucayali store on the wire
  const links = [
    { link: 'the assumed 50 Mbit/s', bandwidth: ASSUMED_BANDWIDTH, roundTripMs: 45, lead: 157 },
    { link: '20 MB/s', bandwidth: 20e6, roundTripMs: 45, lead: 80 },
    { link: '2 MB/s', bandwidth: 2e6, roundTripMs: 50, lead: 400 },
    { link: '1 MB/s', bandwidth: 1e6, roundTripMs: 50, lead: 750 },
    { link: '0.5 MB/s', bandwidth: 0.5e6, roundTripMs: 50, lead: 1450 },
  ];
  for (const { link, bandwidth, roundTripMs, lead } of links) {
    const ms = stageLeadMs({ wireBytes: coarse, bandwidth, roundTripMs });
    assert.equal(ms, lead, link);
    assert.ok(ms >= (coarse * 1000) / bandwidth, `${link}: the next stage waits at least as long as the coarse chunk takes to arrive`);
  }
  const lead = (wireBytes, bandwidth, roundTripMs = 50) => stageLeadMs({ wireBytes, bandwidth, roundTripMs });
  assert.ok(lead(coarse, 1e6) > lead(coarse, 2e6) && lead(coarse, 2e6) > lead(coarse, 20e6), 'the slower the link, the longer the wait');
  assert.ok(lead(2 * coarse, 1e6) > lead(coarse, 1e6), 'the more bytes the stage has to fetch, the longer the wait');
  assert.ok(lead(coarse, 20e6, 200) > lead(coarse, 20e6, 50), 'a store that answers slowly gets a longer wait');
});

test('stage lead: at least 30 ms, at most 5 s', () => {
  assert.equal(stageLeadMs({ wireBytes: 0, bandwidth: ASSUMED_BANDWIDTH, roundTripMs: 0 }), 30, 'a stage with nothing left to fetch on a local store: hardly any lead');
  assert.equal(stageLeadMs({ wireBytes: 0, bandwidth: ASSUMED_BANDWIDTH, roundTripMs: 2 }), 30);
  assert.equal(stageLeadMs({ wireBytes: 0.7e6, bandwidth: 50e3, roundTripMs: 50 }), 5000, 'a link that would take 14 s does not hold the next stage for that long');
  assert.equal(stageLeadMs({ wireBytes: 0.7e6, bandwidth: 0.2e6, roundTripMs: 50 }), 3550, 'but a 1.6 Mbit/s link still gets the whole transfer');
});

// ---- the coarse loop's window ----

test('coarse loop: as many timesteps as the measured link carries in 20 s, the whole loop when that is more, never fewer than eight', () => {
  const step = 0.69e6; // a level-3 step of the Ucayali store on the wire
  assert.equal(COARSE_LOOP_SECONDS, 20);
  assert.equal(COARSE_LOOP_MIN_STEPS, 8);
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: 0.7e6, count: 117 }), 20, 'a 0.7 MB/s link: 14 MB, 20 steps');
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: 2e6, count: 117 }), 57);
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: 6e6, count: 117 }), 117, 'from about 4 MB/s up it is the whole loop');
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: 600e6, count: 117 }), 117, 'a fast local link: all of it, as before');
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: 0.1e6, count: 117 }), 8, 'a link slower than the floor still gets two seconds of a movie');
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: 0.1e6, count: 5 }), 5, 'a loop shorter than the floor is the loop');
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: null, count: 117 }), 117, 'nothing measured: the whole loop');
  assert.equal(coarseLoopSteps({ bytesPerStep: 0, bandwidth: 1e6, count: 117 }), 117);
  assert.equal(coarseLoopSteps({ bytesPerStep: step, bandwidth: 0.7e6, count: 117, seconds: 60 }), 60, 'the window is an option');
});

test('coarse loop: planned again once the playhead is more than a quarter of the window away, either way round the loop', () => {
  const plan = { planned: 10, steps: 20, count: 117 };
  assert.equal(coarseLoopDrifted({ ...plan, t: 10 }), false);
  assert.equal(coarseLoopDrifted({ ...plan, t: 15 }), false, 'five steps is a quarter of 20');
  assert.equal(coarseLoopDrifted({ ...plan, t: 16 }), true);
  assert.equal(coarseLoopDrifted({ ...plan, t: 4 }), true, 'behind it too');
  assert.equal(coarseLoopDrifted({ ...plan, t: 115 }), true, 'around the wrap: 115 is 12 behind 10');
  assert.equal(coarseLoopDrifted({ planned: 115, steps: 20, count: 117, t: 1 }), false, 'and 1 is 3 ahead of 115');
  assert.equal(coarseLoopDrifted({ planned: 0, steps: 117, count: 117, t: 60 }), false, 'a loop that covers every timestep is never planned again');
});
