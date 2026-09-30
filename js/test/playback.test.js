import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_STEPS_PER_SECOND, Playback, SPEEDS, chooseMovieLevel, snapSpeed } from '../tileripper/playback.js';

const FRAME_60 = 1000 / 60;

/**
 * A display: frames arrive every `frameMs` (plus optional jitter) whether or not anyone asked for one, and
 * the callback requested since the previous frame is called with that frame's timestamp, like
 * requestAnimationFrame. `now()` is the time of the last frame.
 */
function fakeDisplay({ frameMs = FRAME_60, jitter = () => 0 } = {}) {
  let time = 0;
  let frameNo = 0;
  let pending = null;
  return {
    now: () => time,
    requestFrame(callback) {
      pending = callback;
      return pending;
    },
    cancelFrame() {
      pending = null;
    },
    runUntil(target) {
      while ((frameNo + 1) * frameMs + jitter(frameNo + 1) <= target) {
        frameNo++;
        time = frameNo * frameMs + jitter(frameNo);
        const callback = pending;
        pending = null;
        callback?.(time);
      }
    },
    get frameNo() {
      return frameNo;
    },
    get waiting() {
      return pending !== null;
    },
  };
}

function setup({ count = 10, stepsPerSecond = 4, ready = () => true, display = fakeDisplay(), start = 0, ...extra } = {}) {
  const state = { index: start, steps: [], prepared: [], changes: 0 };
  const playback = new Playback({
    count,
    stepsPerSecond,
    getIndex: () => state.index,
    goTo: (index, direction) => {
      state.steps.push({ at: display.now(), frame: display.frameNo, index, direction });
      state.index = index;
    },
    isReady: (index) => ready(index, display.now()),
    prepare: (index) => state.prepared.push(index),
    onChange: () => state.changes++,
    now: display.now,
    requestFrame: display.requestFrame,
    cancelFrame: display.cancelFrame,
    ...extra,
  });
  return { playback, state, display };
}

const times = (state) => state.steps.map((s) => s.at);

test('cadence: the first step happens at once, then each step on the frame nearest its due time', () => {
  const { playback, state, display } = setup({ stepsPerSecond: 4 });
  playback.play();
  display.runUntil(1005);
  assert.equal(state.steps.length, 5);
  assert.deepEqual(state.steps.map((s) => s.index), [1, 2, 3, 4, 5]);
  for (const [k, at] of times(state).entries()) assert.ok(Math.abs(at - k * 250) <= FRAME_60 / 2 + 1e-6, `step ${k} at ${at}`);
  assert.equal(playback.stats.held, 0);
});

test('60 steps/s on a 60 Hz display is one step on every frame, never two', () => {
  const { playback, state, display } = setup({ count: 1000, stepsPerSecond: 60 });
  playback.play();
  display.runUntil(2000);
  const frames = state.steps.map((s) => s.frame);
  assert.ok(frames.length >= 119 && frames.length <= 121, `${frames.length} steps in 2 s`);
  for (let i = 1; i < frames.length; i++) assert.equal(frames[i] - frames[i - 1], 1, 'consecutive frames');
});

test('rates that do not divide the refresh rate average out: 24 and 40 steps/s at 60 Hz', () => {
  for (const rate of [24, 40]) {
    const { playback, state, display } = setup({ count: 10000, stepsPerSecond: rate });
    playback.play();
    display.runUntil(10000);
    assert.ok(Math.abs(state.steps.length - rate * 10) <= 2, `${rate}/s gave ${state.steps.length} steps in 10 s`);
    const gaps = state.steps.slice(1).map((s, i) => s.frame - state.steps[i].frame);
    assert.ok(gaps.every((gap) => gap >= 1 && gap <= 3), `frame gaps ${new Set(gaps)}`);
  }
});

test('refresh-rate clamp: the speed is capped at the measured display rate', () => {
  const slow = setup({ count: 10000, stepsPerSecond: 60, display: fakeDisplay({ frameMs: 1000 / 30 }) });
  assert.equal(slow.playback.refreshHz, null);
  assert.equal(slow.playback.effectiveStepsPerSecond, 60, 'unclamped until the refresh rate has been measured');
  slow.playback.play();
  slow.display.runUntil(4000);
  assert.ok(Math.abs(slow.playback.refreshHz - 30) < 0.01);
  assert.ok(Math.abs(slow.playback.effectiveStepsPerSecond - 30) < 0.01);
  assert.equal(slow.playback.stepsPerSecond, 60, 'the requested speed is unchanged');
  const afterWarmUp = slow.state.steps.filter((s) => s.at > 1000);
  assert.ok(Math.abs(afterWarmUp.length - 90) <= 2, `${afterWarmUp.length} steps in 3 s on a 30 Hz display`);
  for (let i = 1; i < afterWarmUp.length; i++) assert.equal(afterWarmUp[i].frame - afterWarmUp[i - 1].frame, 1, 'one step per frame, none skipped or doubled');

  const fast = setup({ count: 10000, stepsPerSecond: 15 });
  fast.playback.play();
  fast.display.runUntil(2000);
  assert.ok(Math.abs(fast.playback.refreshHz - 60) < 0.01);
  assert.equal(fast.playback.effectiveStepsPerSecond, 15, 'a speed below the refresh rate is left alone');
});

test('the effective speed is reported through onChange when the measurement changes it', () => {
  const { playback, state, display } = setup({ count: 1000, stepsPerSecond: 60, display: fakeDisplay({ frameMs: 1000 / 30 }) });
  playback.play();
  const afterPlay = state.changes;
  display.runUntil(1000);
  assert.ok(state.changes > afterPlay, 'the UI is told that 60 became 30');
  const reported = state.changes;
  display.runUntil(2000);
  assert.equal(state.changes, reported, 'and not again while it stays 30');
});

test('frame jitter does not accumulate: 40 steps at 4/s stay on the 10 s grid', () => {
  const jitter = (n) => ((n * 7919) % 5) - 2;
  const { playback, state, display } = setup({ count: 100, stepsPerSecond: 4, display: fakeDisplay({ jitter }) });
  playback.play();
  display.runUntil(10100);
  assert.equal(state.steps.length, 41);
  for (const [k, at] of times(state).entries()) assert.ok(Math.abs(at - k * 250) <= FRAME_60 / 2 + 2 + 1e-6, `step ${k} at ${at}`);
});

test('looping: from the last timestep back to the first, always moving forward', () => {
  const { playback, state, display } = setup({ count: 3, start: 1 });
  playback.play();
  display.runUntil(1260);
  assert.deepEqual(state.steps.map((s) => s.index), [2, 0, 1, 2, 0, 1]);
  assert.ok(state.steps.every((s) => s.direction === 1));
});

test('the wrap: after the last timestep the next step is t=0, moving forward, and it keeps going until paused', () => {
  const { playback, state, display } = setup({ count: 5, stepsPerSecond: 30, start: 4 });
  playback.play();
  display.runUntil(10000);
  assert.equal(state.steps[0].index, 0, 't=0 follows the last timestep');
  assert.equal(state.steps[0].direction, 1);
  assert.ok(state.steps.length >= 299 && state.steps.length <= 301, `looped for 10 s: ${state.steps.length} steps`);
  assert.deepEqual(state.steps.slice(0, 7).map((s) => s.index), [0, 1, 2, 3, 4, 0, 1]);
  assert.ok(state.steps.every((s) => s.direction === 1), 'never turns around at the wrap');
  assert.equal(playback.playing, true);
  playback.pause();
});

test('holds at the wrap are counted separately from holds elsewhere', () => {
  const { playback, display } = setup({ count: 4, stepsPerSecond: 30, ready: (index, now) => !(index === 0 && now < 700) && !(index === 2 && now < 100) });
  playback.play();
  display.runUntil(1500);
  assert.equal(playback.stats.held, 2, 'one hold at t=2 and one at the wrap');
  assert.equal(playback.stats.wrapHeld, 1);
  assert.ok(playback.stats.longestWrapHoldMs > 400, `the wrap waited for its data (${playback.stats.longestWrapHoldMs} ms)`);
  assert.ok(playback.stats.longestHoldMs >= playback.stats.longestWrapHoldMs);
});

test('holds on the current frame until the next timestep is ready, without skipping ahead', () => {
  const { playback, state, display } = setup({ ready: (index, now) => index !== 2 || now >= 705 });
  playback.play();
  display.runUntil(1300);
  const indexes = state.steps.map((s) => s.index);
  assert.deepEqual(indexes, indexes.map((_, i) => i + 1), 'every timestep is shown, in order');
  assert.equal(state.steps[0].at, 0);
  const resumed = state.steps[1];
  assert.ok(resumed.at >= 705 && resumed.at < 705 + FRAME_60, `resumed on the first frame after the data arrived (${resumed.at})`);
  assert.deepEqual(state.prepared, [2], 'the held step is prepared once, not on every frame');
  assert.equal(playback.stats.held, 1);
  assert.ok(Math.abs(playback.stats.longestHoldMs - (resumed.at - 250)) < 1e-6, 'measured from the moment the step was due');
  assert.equal(playback.stats.totalHoldMs, playback.stats.longestHoldMs);
});

test('after a hold the schedule restarts at the moment the step happened: no catch-up burst', () => {
  const { playback, state, display } = setup({ count: 1000, stepsPerSecond: 30, ready: (index, now) => index !== 2 || now >= 2000 });
  playback.play();
  display.runUntil(3000);
  assert.ok(state.steps[1].at >= 2000);
  const after = state.steps.slice(1, 10);
  const gapFrames = after.slice(1).map((s, i) => s.frame - after[i].frame);
  assert.ok(gapFrames.every((gap) => gap === 2), `steady 30/s after the hold, at most one step per frame: ${gapFrames}`);
});

test('a frame that comes more than an interval late does not produce a burst either', () => {
  let time = 0;
  let index = 0;
  let pending = null;
  const stepTimes = [];
  const playback = new Playback({
    count: 1000,
    stepsPerSecond: 10,
    getIndex: () => index,
    goTo: (next) => {
      index = next;
      stepTimes.push(time);
    },
    isReady: () => true,
    now: () => time,
    requestFrame: (callback) => (pending = callback),
    cancelFrame: () => {
      pending = null;
    },
  });
  const frame = (at) => {
    time = at;
    const callback = pending;
    pending = null;
    callback(at);
  };
  playback.play();
  frame(100);
  assert.deepEqual(stepTimes, [0, 100]);
  frame(2100);
  assert.equal(stepTimes.length, 3, 'one step for the late frame');
  frame(2117);
  frame(2133);
  assert.equal(stepTimes.length, 3, 'the next frames are not due yet');
  frame(2200);
  assert.deepEqual(stepTimes.slice(2), [2100, 2200], 'the grid restarted at the late step');
});

test('pause stops stepping and cancels the frame request; play continues from the timestep on screen', () => {
  const { playback, state, display } = setup();
  playback.play();
  display.runUntil(600);
  assert.equal(state.steps.length, 3);
  playback.pause();
  assert.equal(playback.playing, false);
  assert.equal(display.waiting, false);
  display.runUntil(5000);
  assert.equal(state.steps.length, 3);
  playback.play();
  assert.equal(state.steps.at(-1).index, 4, 'resumes after the last timestep shown');
  assert.equal(playback.stats.steps, 1, 'statistics restart with each play');
});

test('a manual scrub pauses playback: at once via pause(), and on the next frame if the timestep moved under it', () => {
  const first = setup();
  first.playback.play();
  first.display.runUntil(300);
  first.playback.pause();
  first.state.index = 7;
  first.display.runUntil(2000);
  assert.equal(first.state.steps.length, 2);

  const second = setup();
  second.playback.play();
  second.display.runUntil(300);
  const before = second.state.steps.length;
  second.state.index = 7;
  second.display.runUntil(1000);
  assert.equal(second.playback.playing, false, 'notices that someone else moved the timestep');
  assert.equal(second.state.steps.length, before, 'and does not advance from the new position');
});

test('speed can change mid-play: the next step is one new interval after the last one', () => {
  const { playback, state, display } = setup({ count: 1000 });
  playback.play();
  display.runUntil(520);
  assert.equal(state.steps.length, 3);
  const lastAt = state.steps.at(-1).at;
  playback.setSpeed(10);
  display.runUntil(820);
  const at = times(state);
  assert.ok(at.length >= 6 && at.length <= 7, `${at.length} steps`);
  assert.ok(Math.abs(at[3] - (lastAt + 100)) <= FRAME_60 / 2 + 1e-6, 'first step at the new interval');
  playback.setSpeed(1);
  const stepsBefore = state.steps.length;
  const lastNow = state.steps.at(-1).at;
  display.runUntil(lastNow + 980);
  assert.equal(state.steps.length, stepsBefore, 'a slower speed waits a full new interval after the last step');
  display.runUntil(lastNow + 1030);
  assert.equal(state.steps.length, stepsBefore + 1);
  assert.equal(playback.stepsPerSecond, 1);
});

test('changing speed while a step is held does not release it early', () => {
  let open = false;
  const { playback, state, display } = setup({ ready: (index) => index !== 2 || open });
  playback.play();
  display.runUntil(400);
  assert.equal(state.steps.length, 1);
  playback.setSpeed(15);
  display.runUntil(600);
  assert.equal(state.steps.length, 1, 'still held');
  open = true;
  display.runUntil(800);
  assert.ok(state.steps.length >= 3);
  assert.ok(state.steps[1].at >= 600 && state.steps[1].at <= 600 + 2 * FRAME_60, 'released on the first frame after the data arrived');
  const gap = state.steps[2].at - state.steps[1].at;
  assert.ok(Math.abs(gap - 1000 / 15) <= FRAME_60, `the new speed applies after the hold (gap ${gap})`);
});

test('onChange fires for play, pause and speed changes', () => {
  const { playback, state } = setup();
  playback.play();
  playback.setSpeed(8);
  playback.pause();
  assert.equal(state.changes, 3);
  playback.toggle();
  assert.equal(playback.playing, true);
  playback.toggle();
  assert.equal(playback.playing, false);
});

test('invalid settings are rejected with the reason', () => {
  assert.throws(() => setup({ stepsPerSecond: 0 }), /steps per second must be positive, got 0/);
  assert.throws(() => setup({ count: 0 }), /at least one timestep/);
  const { playback } = setup();
  assert.throws(() => playback.setSpeed(-3), /positive, got -3/);
  assert.throws(() => playback.setSpeed(Number.NaN), /positive/);
});

test('a single-timestep store loops onto itself without error', () => {
  const { playback, state, display } = setup({ count: 1 });
  playback.play();
  display.runUntil(520);
  assert.deepEqual(state.steps.map((s) => s.index), [0, 0, 0]);
});

test('speed table: 1 to 15 in steps of 1, then 20, 24, 30, 40, 48, 60; default 4 is selectable', () => {
  assert.deepEqual(SPEEDS, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 20, 24, 30, 40, 48, 60]);
  assert.ok(SPEEDS.includes(DEFAULT_STEPS_PER_SECOND));
  assert.equal(DEFAULT_STEPS_PER_SECOND, 4);
  assert.deepEqual([0, 1, 4.4, 16, 18, 22, 35, 59, 61, 500].map(snapSpeed), [1, 1, 4, 15, 20, 20, 30, 60, 60, 60]);
});

test('movie level: the normal level if the loop fits, else the first coarser level where it does, never past the floor', () => {
  const fitting = (...levels) => (lod) => levels.includes(lod);
  assert.equal(chooseMovieLevel({ baseLod: 1, deepestLod: 3, fits: fitting(1, 2, 3) }), 1, 'fits at the normal level: unchanged');
  assert.equal(chooseMovieLevel({ baseLod: 1, deepestLod: 3, fits: fitting(2, 3) }), 2, 'the first coarser level that fits');
  assert.equal(chooseMovieLevel({ baseLod: 1, deepestLod: 3, fits: fitting(3) }), 3);
  assert.equal(chooseMovieLevel({ baseLod: 1, deepestLod: 3, fits: fitting() }), 3, 'nothing fits: the floor (4x zoom-out)');
  assert.equal(chooseMovieLevel({ baseLod: 3, deepestLod: 3, fits: fitting() }), 3, 'already at the coarsest level');
  assert.equal(chooseMovieLevel({ baseLod: 2, deepestLod: 1, fits: fitting() }), 2, 'the floor never makes the level finer than normal');
});
