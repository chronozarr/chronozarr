import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LOOKAHEAD_MAX_STEPS, MIN_SPAN_MS, SCRUB_WINDOW_MS, ScrubSpeed, chooseScrubLevel, scrubAhead } from '../demo/scrub.js';
import { LINK_HEADROOM, linkAllows } from '../demo/playback.js';

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

test('scrubAhead: the next second of the scrub, nearest first, the way it is going', () => {
  assert.deepEqual(scrubAhead({ t: 10, direction: 1, speed: 4, count: 117 }), [11, 12, 13, 14]);
  assert.deepEqual(scrubAhead({ t: 10, direction: -1, speed: 3.2, count: 117 }), [9, 8, 7, 6], 'a part of a step still needs the whole timestep');
  assert.deepEqual(scrubAhead({ t: 10, direction: 1, speed: 0, count: 117 }), []);
});

test('scrubAhead: stops at the ends of the axis and at the prefetch horizon', () => {
  assert.deepEqual(scrubAhead({ t: 114, direction: 1, speed: 25, count: 117 }), [115, 116]);
  assert.deepEqual(scrubAhead({ t: 116, direction: 1, speed: 25, count: 117 }), [], 'nothing left to load past the last timestep');
  assert.deepEqual(scrubAhead({ t: 2, direction: -1, speed: 25, count: 117 }), [1, 0]);
  const fast = scrubAhead({ t: 0, direction: 1, speed: 400, count: 117 });
  assert.equal(fast.length, LOOKAHEAD_MAX_STEPS);
  assert.deepEqual(fast.slice(0, 3), [1, 2, 3]);
});

// What the link has to feed at each level of the Ucayali overview: the wire bytes of one step, at 9, 4 and 1 cells.
const MB = 1e6;
const WIRE_BYTES_PER_STEP = { 1: 12 * MB, 2: 3 * MB, 3: 0.75 * MB };
const linkFor = ({ bandwidthMBs, coldFraction = 1 }) => (lod, speed) => linkAllows({ bytesPerStep: WIRE_BYTES_PER_STEP[lod], stepsPerSecond: speed, coldFraction, bandwidth: bandwidthMBs * MB });

test('chooseScrubLevel: a scrub the link can feed stays at the normal level', () => {
  const linkOk = linkFor({ bandwidthMBs: 60 });
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: 3, linkOk }), 1, '36 MB/s of the 42 MB/s the link allows');
  assert.ok(12 * MB * 3 < LINK_HEADROOM * 60 * MB);
});

test('chooseScrubLevel: a fast scrub on a slow link takes the sharpest level the link can feed', () => {
  const baseLod = 1;
  assert.equal(chooseScrubLevel({ baseLod, deepestLod: 3, speed: 3, linkOk: linkFor({ bandwidthMBs: 25 }) }), 2, 'the normal level needs 36 MB/s of 17.5; the next needs 9');
  assert.equal(chooseScrubLevel({ baseLod, deepestLod: 3, speed: 6.7, linkOk: linkFor({ bandwidthMBs: 40 }) }), 2, '80 MB/s of 28 no; 20 MB/s yes');
  assert.equal(chooseScrubLevel({ baseLod, deepestLod: 3, speed: 6.7, linkOk: linkFor({ bandwidthMBs: 25 }) }), 3, '20 MB/s of 17.5 no; 5 yes');
});

test('chooseScrubLevel: never coarser than deepestLod, even when a coarser level would be fed', () => {
  const linkOk = linkFor({ bandwidthMBs: 25 });
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: 6.7, linkOk }), 3);
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 2, speed: 6.7, linkOk }), 1, 'level 3 would pass, but a view zoomed in by 2x is four times out at level 2');
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 2, speed: 6.7, linkOk: linkFor({ bandwidthMBs: 40 }) }), 2);
  assert.equal(chooseScrubLevel({ baseLod: 2, deepestLod: 1, speed: 6.7, linkOk }), 2, 'a deepest level finer than the normal one leaves the normal level');
});

test('chooseScrubLevel: when no level up to deepestLod can be fed the normal level stays', () => {
  const linkOk = linkFor({ bandwidthMBs: 10 });
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: 100, linkOk }), 1, 'a coarser level that is not fed either only costs resolution');
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 1, speed: 100, linkOk }), 1, 'no level to drop to');
});

test('chooseScrubLevel: timesteps in memory cost the link nothing, whatever the speed', () => {
  const warm = linkFor({ bandwidthMBs: 5, coldFraction: 0 });
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: 400, linkOk: warm }), 1);
  const halfWarm = linkFor({ bandwidthMBs: 40, coldFraction: 0.25 });
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: 6.7, linkOk: halfWarm }), 1, '80 MB/s x 0.25 = 20 of 28');
});

test('chooseScrubLevel: no scrub, no coarser level', () => {
  const never = () => assert.fail('the link is not consulted when nobody scrubs');
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: 0, linkOk: never }), 1);
});

test('chooseScrubLevel: asks the link about the scrub speed', () => {
  const asked = [];
  const linkOk = (lod, speed) => {
    asked.push([lod, speed]);
    return lod === 3;
  };
  assert.equal(chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: 12.5, linkOk }), 3);
  assert.deepEqual(asked, [[1, 12.5], [2, 12.5], [3, 12.5]], 'from the normal level down, no further than the first that passes');
});

test('a scrub: coarse while it is fast on a slow link, normal again once it stops', () => {
  const linkOk = linkFor({ bandwidthMBs: 40 });
  const { scrub, lastAt } = drag({ intervalMs: 40, count: 20 });
  const levelAt = (now) => chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: scrub.speed(now), linkOk });
  assert.equal(levelAt(lastAt), 3, '25 steps/s: 75 MB/s of the 28 the link allows is too much for level 2, 19 is fine');
  assert.equal(levelAt(lastAt + SCRUB_WINDOW_MS - 1), 3, 'the pointer is still down, nothing has moved for a moment');
  assert.equal(levelAt(lastAt + SCRUB_WINDOW_MS), 1, 'no step for a window: the frame sharpens');
  scrub.reset();
  assert.equal(levelAt(lastAt + 10), 1, 'pointer released');
});

test('a scrub: slowing down brings the sharper levels back while the pointer is still down', () => {
  const linkOk = linkFor({ bandwidthMBs: 60 });
  const scrub = new ScrubSpeed();
  let at = 1000;
  const step = (intervalMs, steps) => {
    for (let i = 0; i < steps; i++, at += intervalMs) scrub.note(at, 1);
    at -= intervalMs;
    return chooseScrubLevel({ baseLod: 1, deepestLod: 3, speed: scrub.speed(at), linkOk });
  };
  assert.equal(step(40, 12), 3, '25 steps/s: 75 MB/s of the 42 the link allows is too much for level 2');
  assert.equal(step(150, 4), 2, '6.7 steps/s: 80 MB/s is too much for level 1, 20 is fine');
  assert.equal(step(300, 3), 1, '3.3 steps/s: 40 MB/s of 42');
});
