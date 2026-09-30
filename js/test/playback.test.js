import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Playback } from '../tileripper/playback.js';

/** A clock whose timers only fire when the test advances it; `lateness` delays every firing like a busy main thread. */
function fakeClock({ lateness = 0 } = {}) {
  let time = 0;
  let nextId = 1;
  let timers = [];
  return {
    now: () => time,
    setTimer(fn, ms) {
      const id = nextId++;
      timers.push({ id, at: time + ms, fn });
      return id;
    },
    clearTimer(id) {
      timers = timers.filter((timer) => timer.id !== id);
    },
    runUntil(target) {
      for (;;) {
        const due = timers.filter((timer) => timer.at <= target).sort((a, b) => a.at - b.at || a.id - b.id)[0];
        if (!due) break;
        timers = timers.filter((timer) => timer !== due);
        time = Math.max(time, due.at + lateness);
        due.fn();
      }
      time = Math.max(time, target);
    },
    /** The main thread was stuck until `target`: only the earliest timer fires, then, late. */
    stallThenFire(target) {
      const due = timers.sort((a, b) => a.at - b.at)[0];
      timers = timers.filter((timer) => timer !== due);
      time = target;
      due.fn();
    },
    get pending() {
      return timers.length;
    },
  };
}

function setup({ count = 10, stepsPerSecond = 4, ready = () => true, clock = fakeClock(), start = 0, ...extra } = {}) {
  const state = { index: start, steps: [], prepared: [], changes: 0 };
  const playback = new Playback({
    count,
    stepsPerSecond,
    getIndex: () => state.index,
    goTo: (index, direction) => {
      state.steps.push({ at: clock.now(), index, direction });
      state.index = index;
    },
    isReady: (index) => ready(index, clock.now()),
    prepare: (index) => state.prepared.push(index),
    onChange: () => state.changes++,
    now: clock.now,
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
    ...extra,
  });
  return { playback, state, clock };
}

test('cadence: the first step happens at once, then one per interval', () => {
  const { playback, state, clock } = setup({ stepsPerSecond: 4 });
  playback.play();
  clock.runUntil(1000);
  assert.deepEqual(state.steps.map((s) => s.at), [0, 250, 500, 750, 1000]);
  assert.deepEqual(state.steps.map((s) => s.index), [1, 2, 3, 4, 5]);
  assert.equal(playback.stats.steps, 5);
  assert.equal(playback.stats.held, 0);
});

test('timer lateness does not accumulate: 40 steps at 4/s still end on the 10 s grid', () => {
  const { playback, state, clock } = setup({ count: 100, stepsPerSecond: 4, clock: fakeClock({ lateness: 7 }) });
  playback.play();
  clock.runUntil(9999);
  assert.equal(state.steps.length, 40);
  assert.equal(state.steps.at(-1).at, 39 * 250 + 7);
  for (const [k, step] of state.steps.entries()) assert.equal(step.at, k === 0 ? 0 : k * 250 + 7);
});

test('looping: from the last timestep back to the first, always moving forward', () => {
  const { playback, state, clock } = setup({ count: 3, start: 1 });
  playback.play();
  clock.runUntil(1250);
  assert.deepEqual(state.steps.map((s) => s.index), [2, 0, 1, 2, 0, 1]);
  assert.ok(state.steps.every((s) => s.direction === 1));
});

test('holds on the current frame until the next timestep is ready, without skipping ahead', () => {
  const { playback, state, clock } = setup({ ready: (index, now) => index !== 2 || now >= 700 });
  playback.play();
  clock.runUntil(1300);
  const indexes = state.steps.map((s) => s.index);
  assert.deepEqual(indexes, indexes.map((_, i) => i + 1), 'every timestep is shown, in order');
  assert.equal(state.steps[0].at, 0);
  assert.equal(state.steps[1].at, 706, 'the first poll after the data arrived (polls every 8 ms from the due time 250)');
  assert.deepEqual(state.prepared, [2], 'the held step is prepared once, not on every poll');
  assert.equal(playback.stats.held, 1);
  assert.equal(playback.stats.longestHoldMs, 456);
  assert.equal(playback.stats.totalHoldMs, 456);
});

test('after a hold the schedule restarts at the moment the step happened: no catch-up burst', () => {
  const { playback, state, clock } = setup({ ready: (index, now) => index !== 2 || now >= 2000 });
  playback.play();
  clock.runUntil(3000);
  const times = state.steps.map((s) => s.at);
  assert.equal(times[1], 2002);
  assert.deepEqual(times.slice(1, 5), [2002, 2252, 2502, 2752]);
});

test('a timer that fires more than an interval late does not produce a burst either', () => {
  const { playback, state, clock } = setup();
  playback.play();
  clock.runUntil(250);
  assert.equal(state.steps.length, 2);
  clock.stallThenFire(2250);
  assert.equal(state.steps.length, 3, 'one step for the late timer');
  clock.runUntil(2250 + 250);
  assert.equal(state.steps.length, 4);
  assert.equal(state.steps[3].at - state.steps[2].at, 250, 'the grid restarts at the late step');
});

test('pause stops stepping and cancels the timer; play continues from the timestep on screen', () => {
  const { playback, state, clock } = setup();
  playback.play();
  clock.runUntil(600);
  assert.equal(state.steps.length, 3);
  playback.pause();
  assert.equal(playback.playing, false);
  assert.equal(clock.pending, 0);
  clock.runUntil(5000);
  assert.equal(state.steps.length, 3);
  playback.play();
  assert.equal(state.steps.at(-1).index, 4, 'resumes after the last timestep shown');
  assert.equal(playback.stats.steps, 1, 'statistics restart with each play');
});

test('a manual scrub pauses playback: at once via pause(), and on the next tick if the timestep moved under it', () => {
  const first = setup();
  first.playback.play();
  first.clock.runUntil(300);
  first.playback.pause();
  first.state.index = 7;
  first.clock.runUntil(2000);
  assert.equal(first.state.steps.length, 2);

  const second = setup();
  second.playback.play();
  second.clock.runUntil(300);
  const before = second.state.steps.length;
  second.state.index = 7;
  second.clock.runUntil(1000);
  assert.equal(second.playback.playing, false, 'notices that someone else moved the timestep');
  assert.equal(second.state.steps.length, before, 'and does not advance from the new position');
});

test('speed can change mid-play: the next step is one new interval after the last one', () => {
  const { playback, state, clock } = setup({ count: 100 });
  playback.play();
  clock.runUntil(520);
  assert.deepEqual(state.steps.map((s) => s.at), [0, 250, 500]);
  playback.setSpeed(10);
  clock.runUntil(820);
  assert.deepEqual(state.steps.map((s) => s.at), [0, 250, 500, 600, 700, 800]);
  playback.setSpeed(1);
  clock.runUntil(1790);
  assert.equal(state.steps.length, 6, 'a slower speed waits a full new interval after the last step');
  clock.runUntil(1800);
  assert.equal(state.steps.length, 7);
  assert.equal(playback.stepsPerSecond, 1);
});

test('changing speed while a step is held does not release it early', () => {
  let open = false;
  const { playback, state, clock } = setup({ ready: (index) => index !== 2 || open });
  playback.play();
  clock.runUntil(400);
  assert.equal(state.steps.length, 1);
  playback.setSpeed(15);
  clock.runUntil(600);
  assert.equal(state.steps.length, 1, 'still held');
  open = true;
  clock.runUntil(620);
  assert.equal(state.steps.length, 2);
  clock.runUntil(620 + 67);
  assert.equal(state.steps.length, 3, 'the new speed applies after the hold');
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
  const { playback, state, clock } = setup({ count: 1 });
  playback.play();
  clock.runUntil(500);
  assert.deepEqual(state.steps.map((s) => s.index), [0, 0, 0]);
});
