import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  LOOKAHEAD_SECONDS,
  LOW_MARK,
  MIN_SPAN_MS,
  SCRUB_WINDOW_MS,
  ScrubSpeed,
  chooseScrubLevel,
  readyRun,
  scrubLevels,
  scrubNeed,
  stepsAhead,
} from '../demo/scrub.js';

/** A drag: one step of one timestep every `intervalMs` from `start`, `count` of them; returns the tracker and the time of the last step. */
function drag({ intervalMs, count, start = 1000, distance = 1 }) {
  const scrub = new ScrubSpeed();
  let at = start;
  for (let i = 0; i < count; i++) {
    at = start + i * intervalMs;
    scrub.note(at, distance);
  }
  return { scrub, lastAt: at };
}

test('ScrubSpeed: nothing, or a single step, is not a scrub', () => {
  const scrub = new ScrubSpeed();
  assert.equal(scrub.speed(0), 0);
  assert.equal(scrub.latestAt, null);
  scrub.note(1000, 1);
  assert.equal(scrub.speed(1000), 0, 'one arrow press is a step, not a speed');
  assert.equal(scrub.speed(1100), 0);
  assert.equal(scrub.latestAt, 1000);
});

test('ScrubSpeed: a steady drag reads its own speed, wherever its steps fall against the window', () => {
  for (const intervalMs of [40, 150]) {
    for (const jitter of [0, 7, 19]) {
      for (const count of [6, 9, 30]) {
        const { scrub, lastAt } = drag({ intervalMs, count, start: 1000 + jitter });
        const expected = 1000 / intervalMs;
        assert.ok(Math.abs(scrub.speed(lastAt) - expected) < 0.01, `${intervalMs} ms steps, ${count} of them: ${scrub.speed(lastAt)} vs ${expected}`);
      }
    }
  }
});

test('ScrubSpeed: an event that moves several timesteps counts all of them', () => {
  const { scrub, lastAt } = drag({ intervalMs: 50, count: 10, distance: 3 });
  assert.ok(Math.abs(scrub.speed(lastAt) - 60) < 0.01, '3 timesteps every 50 ms');
  const jump = new ScrubSpeed();
  jump.note(1000, 40);
  jump.note(1200, 1);
  assert.ok(Math.abs(jump.speed(1200) - 5) < 0.01, 'the first step of the window is where the scrub starts, not distance covered');
});

test('ScrubSpeed: two events a frame apart do not read as a sprint; a drag reads low at first, not high', () => {
  const scrub = new ScrubSpeed();
  scrub.note(1000, 1);
  scrub.note(1016, 1);
  assert.ok(Math.abs(scrub.speed(1016) - 1000 / MIN_SPAN_MS) < 0.01, 'a pointerdown and the first pointermove: one step over at least MIN_SPAN_MS');
  const fast = drag({ intervalMs: 40, count: 3 });
  assert.ok(Math.abs(fast.scrub.speed(fast.lastAt) - 2000 / MIN_SPAN_MS) < 0.01, 'two steps in 80 ms: 13 steps/s until the drag has run for MIN_SPAN_MS');
  assert.ok(fast.scrub.speed(fast.lastAt) < 25);
});

test('ScrubSpeed: follows a change of pace within one window', () => {
  const scrub = new ScrubSpeed();
  let at = 1000;
  for (let i = 0; i < 20; i++, at += 40) scrub.note(at, 1);
  assert.ok(Math.abs(scrub.speed(at - 40) - 25) < 0.01, 'fast');
  // Slow steps from here on: after one window of them the fast ones are out of the estimate.
  const slowFrom = at;
  while (at <= slowFrom + SCRUB_WINDOW_MS + 150) {
    scrub.note(at, 1);
    at += 150;
  }
  assert.ok(Math.abs(scrub.speed(at - 150) - 1000 / 150) < 0.01, `slow now: ${scrub.speed(at - 150)}`);
});

test('ScrubSpeed: holds between steps and is 0 once a window has passed without one', () => {
  const { scrub, lastAt } = drag({ intervalMs: 40, count: 12 });
  const speed = scrub.speed(lastAt);
  assert.ok(speed > 0);
  assert.equal(scrub.speed(lastAt + 100), speed, 'between two steps of a steady drag the speed does not sag');
  assert.equal(scrub.speed(lastAt + SCRUB_WINDOW_MS - 1), speed);
  assert.equal(scrub.speed(lastAt + SCRUB_WINDOW_MS), 0, 'no step for the whole window: the scrub is over');
  assert.equal(scrub.speed(lastAt + 10 * SCRUB_WINDOW_MS), 0);
});

test('ScrubSpeed: reset ends the scrub at once; the next step starts a new one', () => {
  const { scrub, lastAt } = drag({ intervalMs: 40, count: 12 });
  scrub.reset();
  assert.equal(scrub.speed(lastAt), 0, 'pointer released');
  assert.equal(scrub.latestAt, null);
  scrub.note(lastAt + 50, 1);
  assert.equal(scrub.speed(lastAt + 50), 0, 'a single step again');
  scrub.note(lastAt + 90, 1);
  assert.ok(scrub.speed(lastAt + 90) > 0);
});

test('stepsAhead: the timesteps after t that the scrub reaches, nearest first, the way it is going', () => {
  assert.deepEqual(stepsAhead({ t: 10, direction: 1, count: 117, limit: 4 }), [11, 12, 13, 14]);
  assert.deepEqual(stepsAhead({ t: 10, direction: -1, count: 117, limit: 3 }), [9, 8, 7]);
  assert.deepEqual(stepsAhead({ t: 10, direction: 1, count: 117, limit: 0 }), []);
});

test('stepsAhead: stops at the ends of the axis', () => {
  assert.deepEqual(stepsAhead({ t: 114, direction: 1, count: 117, limit: 25 }), [115, 116]);
  assert.deepEqual(stepsAhead({ t: 116, direction: 1, count: 117, limit: 25 }), [], 'nothing left past the last timestep');
  assert.deepEqual(stepsAhead({ t: 2, direction: -1, count: 117, limit: 25 }), [1, 0]);
  assert.deepEqual(stepsAhead({ t: 0, direction: -1, count: 117, limit: 25 }), []);
});

test('readyRun: counts the steps ready in a row from the nearest; a step that is not ready ends the run', () => {
  const have = new Set([11, 12, 13, 15, 16]);
  const ahead = stepsAhead({ t: 10, direction: 1, count: 117, limit: 8 });
  assert.equal(readyRun(ahead, (step) => have.has(step)), 3, '14 is missing: 15 and 16 beyond it do not count');
  assert.equal(readyRun(ahead, () => true), 8);
  assert.equal(readyRun(ahead, () => false), 0);
  assert.equal(readyRun(ahead, (step) => step !== 11), 0, 'the nearest missing: nothing ahead is ready however much lies beyond');
  assert.equal(readyRun([], () => assert.fail('nothing to ask about')), 0);
});

test('readyRun: counts the way the scrub is going, not the way it came from', () => {
  const have = new Set([6, 7, 8, 9, 10, 11, 12]);
  const isReady = (step) => have.has(step);
  assert.equal(readyRun(stepsAhead({ t: 10, direction: 1, count: 117, limit: 8 }), isReady), 2, 'forward: 11 and 12');
  assert.equal(readyRun(stepsAhead({ t: 10, direction: -1, count: 117, limit: 8 }), isReady), 4, 'backward: 9 to 6');
});

test('scrubNeed: what the scrub uses during the lookahead, rounded up', () => {
  assert.equal(scrubNeed({ speed: 4, t: 10, direction: 1, count: 117 }), Math.ceil(4 * LOOKAHEAD_SECONDS));
  assert.equal(scrubNeed({ speed: 1000 / 150, t: 10, direction: -1, count: 117 }), Math.ceil((1000 / 150) * LOOKAHEAD_SECONDS));
  assert.ok(scrubNeed({ speed: 25, t: 10, direction: 1, count: 117 }) > scrubNeed({ speed: 7, t: 10, direction: 1, count: 117 }), 'a faster scrub needs more ahead');
});

test('scrubNeed: no more than is left in the scrub direction, and 0 when nobody scrubs', () => {
  assert.equal(scrubNeed({ speed: 25, t: 114, direction: 1, count: 117 }), 2);
  assert.equal(scrubNeed({ speed: 25, t: 3, direction: -1, count: 117 }), 3);
  assert.equal(scrubNeed({ speed: 25, t: 116, direction: 1, count: 117 }), 0, 'the end of the axis: nothing more to wait for');
  assert.equal(scrubNeed({ speed: 25, t: 0, direction: -1, count: 117 }), 0);
  assert.equal(scrubNeed({ speed: 0, t: 50, direction: 1, count: 117 }), 0);
});

/** The Ucayali overview: levels 1, 2 and 3 have 9, 4 and 1 cells. */
const OVERVIEW_CELLS = { 0: 36, 1: 9, 2: 4, 3: 1 };
const overview = (deepestLod = 3) => scrubLevels({ baseLod: 1, deepestLod, cellCount: (lod) => OVERVIEW_CELLS[lod] });
/** `ready` for a table of timesteps in a row ahead per level; a level that is not in the table has nothing. */
const readyOf = (table) => (lod) => table[lod] ?? 0;

test('scrubLevels: from the normal level down to deepestLod, each level with fewer cells than the one before', () => {
  assert.deepEqual(overview(3), [1, 2, 3]);
  assert.deepEqual(overview(2), [1, 2], 'never coarser than deepestLod');
  assert.deepEqual(overview(1), [1]);
  assert.deepEqual(scrubLevels({ baseLod: 2, deepestLod: 1, cellCount: (lod) => OVERVIEW_CELLS[lod] }), [2], 'a deepest level finer than the normal one leaves the normal level');
});

test('scrubLevels: a level with as many cells as a sharper one is no cheaper and is left out', () => {
  const zoomedIn = { 0: 4, 1: 4, 2: 1, 3: 1 };
  assert.deepEqual(scrubLevels({ baseLod: 0, deepestLod: 1, cellCount: (lod) => zoomedIn[lod] }), [0], 'zoomed in, level 1 has the same four cells as level 0');
  assert.deepEqual(scrubLevels({ baseLod: 0, deepestLod: 3, cellCount: (lod) => zoomedIn[lod] }), [0, 2]);
});

// A lookahead of `need` timesteps; the level in use is dropped below `low` of them and left for a sharper one at `need`.
const need = 12;
const low = Math.ceil(LOW_MARK * need);

test('chooseScrubLevel: a scrub starts at the normal level and stays there while it holds enough ahead', () => {
  const levels = overview();
  assert.equal(chooseScrubLevel({ levels, need, ready: readyOf({ 1: need, 2: need, 3: 40 }) }), 1);
  assert.equal(chooseScrubLevel({ levels, need, ready: readyOf({ 1: low, 3: 40 }) }), 1, 'the idle prefetch left a little ahead: not enough to cover, enough to start');
});

test('chooseScrubLevel: the sharpest level that holds the lookahead when the normal level has nothing', () => {
  const levels = overview();
  assert.equal(chooseScrubLevel({ levels, need, ready: readyOf({ 1: 2, 2: need, 3: 40 }) }), 2);
  assert.equal(chooseScrubLevel({ levels, need, ready: readyOf({ 1: 2, 2: need - 1, 3: 40 }) }), 3, 'level 2 is a step short of covering it');
  assert.equal(chooseScrubLevel({ levels, need, ready: readyOf({ 1: 0, 2: 0, 3: need }) }), 3);
});

test('chooseScrubLevel: when no level holds the lookahead the deepest is chosen, the cheapest to load', () => {
  assert.equal(chooseScrubLevel({ levels: overview(), need, ready: readyOf({}) }), 3);
  assert.equal(chooseScrubLevel({ levels: overview(), need, ready: readyOf({ 1: low - 1, 2: need - 1, 3: need - 1 }) }), 3);
});

test('chooseScrubLevel: never coarser than deepestLod', () => {
  const dry = readyOf({});
  assert.equal(chooseScrubLevel({ levels: overview(2), need, ready: dry }), 2, 'level 3 is the coarse loop, but a view zoomed in 2x is four times out at level 2');
  assert.equal(chooseScrubLevel({ levels: overview(1), need, ready: dry }), 1, 'no level to drop to');
  assert.equal(chooseScrubLevel({ levels: overview(2), held: 2, need, ready: dry }), 2);
});

test('chooseScrubLevel: steps up to the sharpest level that holds the whole lookahead', () => {
  const levels = overview();
  assert.equal(chooseScrubLevel({ levels, held: 3, need, ready: readyOf({ 2: need, 3: 40 }) }), 2);
  assert.equal(chooseScrubLevel({ levels, held: 3, need, ready: readyOf({ 1: need, 2: need, 3: 40 }) }), 1, 'both fill: the sharper one');
  assert.equal(chooseScrubLevel({ levels, held: 3, need, ready: readyOf({ 1: need - 1, 2: need, 3: 40 }) }), 2, 'level 1 is a step short');
  assert.equal(chooseScrubLevel({ levels, held: 2, need, ready: readyOf({ 1: need, 2: need }) }), 1);
});

test('chooseScrubLevel: a sharper level that fills while the level in use is dry still wins over the drop', () => {
  assert.equal(chooseScrubLevel({ levels: overview(), held: 2, need, ready: readyOf({ 1: need, 2: 0, 3: 40 }) }), 1);
});

test('chooseScrubLevel: between the marks nothing changes, so one step more or less does not flip the level', () => {
  const levels = overview();
  assert.equal(chooseScrubLevel({ levels, held: 2, need, ready: readyOf({ 1: need - 1, 2: low, 3: 40 }) }), 2, 'level 1 almost fills and level 2 is running low: stay');
  assert.equal(chooseScrubLevel({ levels, held: 2, need, ready: readyOf({ 1: need - 1, 2: need, 3: 40 }) }), 2);
  // Level 1 fills and is taken; the scrub then uses a step of it, so it holds one less than the mark: it stays.
  let held = chooseScrubLevel({ levels, held: 2, need, ready: readyOf({ 1: need, 2: 30 }) });
  assert.equal(held, 1);
  for (let ready = need - 1; ready >= low; ready--) {
    held = chooseScrubLevel({ levels, held, need, ready: readyOf({ 1: ready, 2: 30, 3: 40 }) });
    assert.equal(held, 1, `${ready} of ${need} ahead at level 1 is still enough to stay`);
  }
});

test('chooseScrubLevel: steps down when the level in use runs below the low mark, to the sharpest level that holds the lookahead', () => {
  const levels = overview();
  assert.equal(chooseScrubLevel({ levels, held: 1, need, ready: readyOf({ 1: low - 1, 2: need, 3: 40 }) }), 2);
  assert.equal(chooseScrubLevel({ levels, held: 1, need, ready: readyOf({ 1: low - 1, 2: need - 1, 3: 40 }) }), 3, 'level 2 does not hold the lookahead either');
  assert.equal(chooseScrubLevel({ levels, held: 2, need, ready: readyOf({ 2: low - 1, 3: 40 }) }), 3);
  assert.equal(chooseScrubLevel({ levels, held: 3, need, ready: readyOf({}) }), 3, 'the deepest level has nowhere to go');
});

test('chooseScrubLevel: a held level that is not among the levels (the camera moved) starts again from the normal level', () => {
  assert.equal(chooseScrubLevel({ levels: [0, 2], held: 3, need, ready: readyOf({ 0: need }) }), 0);
});

test('chooseScrubLevel: with nothing to wait for the normal level is chosen', () => {
  assert.equal(chooseScrubLevel({ levels: overview(), held: 3, need: 0, ready: readyOf({}) }), 1, 'every level holds enough of nothing');
  assert.equal(chooseScrubLevel({ levels: overview(), held: 2, need: 0, ready: readyOf({}) }), 1);
});

test('a scrub: coarse while it is fast and little is loaded, the normal level again once it stops', () => {
  const levels = overview();
  const { scrub, lastAt } = drag({ intervalMs: 40, count: 20 });
  const levelAt = (now, ready, t = 20) => chooseScrubLevel({ levels, held: 3, need: scrubNeed({ speed: scrub.speed(now), t, direction: 1, count: 117 }), ready });
  const loaded = readyOf({ 1: 3, 3: 90 });
  assert.equal(levelAt(lastAt, loaded), 3, '25 steps/s: a lookahead of dozens of steps; level 1 has three');
  assert.equal(levelAt(lastAt + SCRUB_WINDOW_MS - 1, loaded), 3, 'the pointer is still down, nothing has moved for a moment');
  assert.equal(levelAt(lastAt + SCRUB_WINDOW_MS, loaded), 1, 'no step for a window: the frame sharpens');
  scrub.reset();
  assert.equal(levelAt(lastAt + 10, loaded), 1, 'pointer released');
});

test('a scrub: slowing down shortens the lookahead until a sharper level holds it', () => {
  const levels = overview();
  const needAt = (speed) => scrubNeed({ speed, t: 20, direction: 1, count: 117 });
  // Level 1 holds what a 3.3 steps/s scrub needs, level 2 what a 6.7 steps/s one does, level 3 (the coarse loop) nearly everything.
  const loaded = readyOf({ 1: needAt(1000 / 300), 2: needAt(1000 / 150), 3: 90 });
  const scrub = new ScrubSpeed();
  let at = 1000;
  const step = (intervalMs, steps, held) => {
    for (let i = 0; i < steps; i++, at += intervalMs) scrub.note(at, 1);
    at -= intervalMs;
    return chooseScrubLevel({ levels, held, need: needAt(scrub.speed(at)), ready: loaded });
  };
  assert.equal(step(40, 12, 3), 3, '25 steps/s: nothing but level 3 holds the lookahead');
  assert.equal(step(150, 4, 3), 2, '6.7 steps/s: level 2 holds it, level 1 does not');
  assert.equal(step(300, 3, 2), 1, '3.3 steps/s: level 1 holds it');
});
