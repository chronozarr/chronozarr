import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DEFAULT_STEPS_PER_SECOND, DEFAULT_WIRE_RATIO, BUFFER_BATCH, LINK_HEADROOM, MIN_LINK_STEPS_PER_SECOND, Playback, SPEEDS, bufferBatch, chooseMovieLevel, describeReason, linkAllows, linkSteps, roundSteps, snapSpeed, wireRatio } from '../demo/playback.js';

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
    prepare: (indices) => state.prepared.push(indices),
    onChange: () => state.changes++,
    now: display.now,
    requestFrame: display.requestFrame,
    cancelFrame: display.cancelFrame,
    ...extra,
  });
  return { playback, state, display };
}

const times = (state) => state.steps.map((s) => s.at);

test('a bounded rolling cache starts and completes multiple loops without requesting the entire series', () => {
  const count = 26;
  const resident = new Set([0]);
  const pending = new Map();
  let maxAhead = 15;
  let frameCapacity = 19;
  let ctx;
  const flush = () => {
    for (const [index, due] of pending) {
      if (due > ctx.display.now()) continue;
      pending.delete(index);
      resident.add(index);
    }
    while (resident.size > frameCapacity) {
      // Like the reader's forward-loop eviction order, retain the current and nearest future frames.
      const worst = [...resident].sort((a, b) => ((b - ctx.state.index + count) % count) - ((a - ctx.state.index + count) % count))[0];
      resident.delete(worst);
    }
  };
  const prepared = [];
  ctx = setup({
    count,
    stepsPerSecond: 60,
    maxAhead: () => maxAhead,
    ready: (index) => { flush(); return resident.has(index); },
    prepare: (indices) => {
      assert.ok(indices.length <= maxAhead);
      prepared.push(indices);
      for (const index of indices) if (!resident.has(index) && !pending.has(index)) pending.set(index, ctx.display.now() + 35);
    },
  });
  ctx.playback.play();
  assert.equal(ctx.playback.buffered.needed, 15);
  ctx.display.runUntil(1800);
  assert.ok(ctx.state.steps.length >= 100, 'crosses the wrap several times under a finite cache');
  assert.equal(ctx.playback.stats.bufferingPauses, 0);
  maxAhead = 5;
  frameCapacity = 9;
  flush();
  assert.equal(ctx.playback.buffered.needed, 5, 'a resized view or budget is reflected immediately');
  const stepsBefore = ctx.state.steps.length;
  ctx.display.runUntil(3600);
  assert.ok(ctx.state.steps.length >= stepsBefore + 100, 'continues under the smaller rolling budget');
  assert.equal(ctx.playback.stats.bufferingPauses, 0);
  for (let k = 0; k < ctx.state.steps.length; k++) assert.equal(ctx.state.steps[k].index, (k + 1) % count);
  assert.ok(prepared.some((indices) => indices.includes(0)), 'the rolling horizon crosses the loop boundary');
  ctx.playback.pause();
  const requests = prepared.length;
  ctx.display.runUntil(4000);
  assert.equal(prepared.length, requests, 'pause stops requesting refills');
});

test('bounded startup reacts to capacity and speed while buffering, without wrapping past the last frame', () => {
  let capacity = 8;
  const { playback, state, display } = setup({ count: 26, stepsPerSecond: 60, maxAhead: () => capacity, ready: () => false });
  playback.play();
  assert.deepEqual(state.prepared[0], [1, 2, 3, 4, 5, 6, 7, 8]);
  capacity = 3;
  display.runUntil(300);
  assert.equal(state.prepared.at(-1).length, 3);
  playback.setSpeed(1);
  assert.equal(playback.buffered.needed, 2);
  const finite = setup({ count: 26, start: 24, stepsPerSecond: 60, wrap: false, maxAhead: () => 3, ready: () => false });
  finite.playback.play();
  assert.deepEqual(finite.state.prepared[0], [25]);
});

test('cadence: the first step happens at once, then each step on the frame nearest its due time', () => {
  const { playback, state, display } = setup({ stepsPerSecond: 4 });
  playback.play();
  display.runUntil(1005);
  assert.equal(state.steps.length, 5);
  assert.deepEqual(state.steps.map((s) => s.index), [1, 2, 3, 4, 5]);
  for (const [k, at] of times(state).entries()) assert.ok(Math.abs(at - k * 250) <= FRAME_60 / 2 + 1e-6, `step ${k} at ${at}`);
  assert.equal(playback.stats.bufferingPauses, 0);
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

test('playback starts at once when the next two seconds of frames are ready, and never asks for any', () => {
  const { playback, state, display } = setup({ count: 100, stepsPerSecond: 10 });
  playback.play();
  assert.equal(state.steps.length, 1, 'the first step is immediate');
  assert.equal(playback.buffering, false);
  display.runUntil(3000);
  assert.deepEqual(state.prepared, [], 'a full buffer needs no requests');
  assert.equal(playback.stats.bufferingPauses, 0);
  assert.equal(playback.stats.initialBufferMs, 0);
  assert.deepEqual(playback.buffered, { ahead: 20, needed: 20 });
});

test('it waits for two seconds of frames before it starts (the whole loop when that is shorter), asking for them in order', () => {
  // Ten frames a second arrive from the network: timestep k is ready from k * 100 ms.
  const { playback, state, display } = setup({ count: 100, stepsPerSecond: 10, ready: (index, now) => index * 100 <= now });
  playback.play();
  assert.equal(playback.playing, true, 'playing, as far as the UI is concerned, while it buffers');
  assert.equal(playback.buffering, true);
  assert.deepEqual(state.prepared[0], Array.from({ length: 20 }, (_, k) => k + 1), 'the next 20 timesteps, nearest first');
  display.runUntil(1900);
  assert.equal(state.steps.length, 0, 'not before frame 20 is in');
  display.runUntil(2100);
  assert.equal(playback.buffering, false);
  assert.equal(state.steps.length > 0, true);
  assert.ok(state.steps[0].at >= 2000 && state.steps[0].at < 2000 + FRAME_60, `started on the first frame with 20 frames ready (${state.steps[0].at})`);
  assert.ok(Math.abs(playback.stats.initialBufferMs - state.steps[0].at) < 1e-6);
  assert.equal(playback.stats.bufferingPauses, 0, 'the first wait is not a pause');

  const short = setup({ count: 5, stepsPerSecond: 30, ready: (index, now) => index !== 3 || now >= 500 });
  short.playback.play();
  short.display.runUntil(400);
  assert.equal(short.state.steps.length, 0, 'a loop of five waits for all four other frames');
  short.display.runUntil(600);
  assert.ok(short.state.steps.length > 0);
});

test('the buffered count is reported while it fills', () => {
  const { playback, state, display } = setup({ count: 100, stepsPerSecond: 10, ready: (index, now) => index * 100 <= now });
  playback.play();
  const afterPlay = state.changes;
  display.runUntil(500);
  assert.ok(state.changes > afterPlay, 'onChange follows the buffer filling');
  assert.equal(playback.buffered.needed, 20);
  assert.ok(playback.buffered.ahead >= 4 && playback.buffered.ahead <= 5, `ahead ${playback.buffered.ahead}`);
});

test('when the next frame is not ready it pauses and refills instead of holding, then resumes by itself without skipping or bursting', () => {
  // Timestep 6 is missing from 300 ms to 1500 ms; everything else is there.
  const { playback, state, display } = setup({ count: 10, stepsPerSecond: 4, ready: (index, now) => !(index === 6 && now >= 300 && now < 1500) });
  playback.play();
  display.runUntil(2600);
  const indexes = state.steps.map((s) => s.index);
  assert.deepEqual(indexes.slice(0, 8), [1, 2, 3, 4, 5, 6, 7, 8], 'every timestep in order, none skipped');
  assert.equal(playback.stats.bufferingPauses, 1);
  const dryAt = state.steps[4].at;
  const resumed = state.steps[5];
  assert.ok(resumed.at >= 1500 && resumed.at < 1500 + FRAME_60, `resumed on the first frame after the data arrived (${resumed.at})`);
  assert.ok(Math.abs(playback.stats.bufferingMs - (resumed.at - dryAt - 250)) < FRAME_60 + 1e-6, 'measured from when the step was due to when it resumed');
  assert.equal(playback.stats.longestBufferingMs, playback.stats.bufferingMs);
  const gaps = state.steps.slice(6, 9).map((step, i) => step.at - state.steps[5 + i].at);
  assert.ok(gaps.every((gap) => gap >= 250 - FRAME_60), `the grid restarted at the resume, no catch-up burst: ${gaps}`);
  assert.ok(state.prepared.length >= 2, 'it kept asking while it waited');
  assert.deepEqual(state.prepared.at(-1).slice(0, 2), [6, 7], 'for the timesteps after the one on screen');
});

test('a pause at the wrap is counted separately from pauses elsewhere', () => {
  // Timestep 2 is not there from 20 to 300 ms (the pause at 33 ms), timestep 0 not from 330 to 900 ms (the pause at the wrap).
  const { playback, display } = setup({ count: 4, stepsPerSecond: 30, ready: (index, now) => !(index === 2 && now >= 20 && now < 300) && !(index === 0 && now >= 330 && now < 900) });
  playback.play();
  display.runUntil(1500);
  assert.equal(playback.stats.bufferingPauses, 2, 'one at timestep 2 and one at the wrap');
  assert.equal(playback.stats.wrapPauses, 1);
  assert.ok(playback.stats.longestBufferingMs > 400, `the wrap waited for its data (${playback.stats.longestBufferingMs} ms)`);
});

test('it asks for more frames when fewer than a second are ready ahead, while playing, and pauses when they run out', () => {
  // Nothing past timestep 12 has arrived. At 4/s a start needs 8 frames and a second is 4.
  const { playback, state, display } = setup({ count: 100, stepsPerSecond: 4, ready: (index) => index <= 12 });
  playback.play();
  assert.equal(playback.buffering, false, 'twelve frames ahead are more than the eight a start needs');
  assert.deepEqual(state.prepared, []);
  display.runUntil(4000);
  assert.deepEqual(state.steps.map((s) => s.index), [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12], 'it plays every timestep there is');
  assert.deepEqual(state.prepared[0], [10, 11, 12, 13, 14, 15, 16, 17], 'on timestep 9 only three frames are left ahead, under a second: it asks for the next eight');
  assert.equal(playback.buffering, true, 'on timestep 12 the next frame is missing');
  assert.equal(playback.stats.bufferingPauses, 1);
  assert.equal(playback.playing, true, 'and playback is still on, waiting');
});

test('pausing while it buffers stops it; changing the speed while it buffers does not start it early', () => {
  let open = false;
  const { playback, state, display } = setup({ count: 100, stepsPerSecond: 4, ready: (index) => open || index < 3 });
  playback.play();
  display.runUntil(400);
  assert.equal(state.steps.length, 0);
  playback.setSpeed(15);
  display.runUntil(600);
  assert.equal(state.steps.length, 0, 'still buffering at the new speed (30 frames wanted now)');
  open = true;
  display.runUntil(800);
  assert.ok(state.steps.length >= 2);
  assert.ok(state.steps[0].at >= 600 && state.steps[0].at <= 600 + 2 * FRAME_60, 'started on the first frame after the data arrived');
  const gap = state.steps[1].at - state.steps[0].at;
  assert.ok(Math.abs(gap - 1000 / 15) <= FRAME_60, `the new speed applies (gap ${gap})`);

  const paused = setup({ count: 100, stepsPerSecond: 4, ready: () => false });
  paused.playback.play();
  paused.display.runUntil(300);
  assert.equal(paused.playback.buffering, true);
  paused.playback.pause();
  assert.equal(paused.playback.buffering, false);
  assert.equal(paused.playback.playing, false);
  assert.equal(paused.display.waiting, false);
});

test('without wrap it looks no further than the last timestep', () => {
  const { playback, state, display } = setup({ count: 6, stepsPerSecond: 30, wrap: false, start: 3, ready: (index) => index > 3 });
  playback.play();
  assert.equal(playback.buffering, false, 'only timesteps 4 and 5 lie ahead of 3; the wrapped ones are not asked for');
  display.runUntil(300);
  assert.deepEqual(state.steps.map((s) => s.index).slice(0, 2), [4, 5]);
  assert.deepEqual(state.prepared, []);
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
  const level = (options) => chooseMovieLevel(options).lod;
  assert.equal(level({ baseLod: 1, deepestLod: 3, fits: fitting(1, 2, 3) }), 1, 'fits at the normal level: unchanged');
  assert.equal(level({ baseLod: 1, deepestLod: 3, fits: fitting(2, 3) }), 2, 'the first coarser level that fits');
  assert.equal(level({ baseLod: 1, deepestLod: 3, fits: fitting(3) }), 3);
  assert.equal(level({ baseLod: 1, deepestLod: 3, fits: fitting() }), 3, 'nothing fits: the floor (4x zoom-out)');
  assert.equal(level({ baseLod: 3, deepestLod: 3, fits: fitting() }), 3, 'already at the coarsest level');
  assert.equal(level({ baseLod: 2, deepestLod: 1, fits: fitting() }), 2, 'the floor never makes the level finer than normal');
});

test('movie level: the link rule drops a level the cache could hold, and the reason says which rule did it', () => {
  const only = (...levels) => (lod) => levels.includes(lod);
  const choose = (memory, link) => chooseMovieLevel({ baseLod: 1, deepestLod: 3, fits: only(...memory), linkOk: only(...link) });
  assert.deepEqual(choose([1, 2, 3], [1, 2, 3]), { lod: 1, reason: null }, 'both rules hold at the normal level');
  assert.deepEqual(choose([1, 2, 3], [2, 3]), { lod: 2, reason: 'link' }, 'fits memory, link too slow');
  assert.deepEqual(choose([2, 3], [1, 2, 3]), { lod: 2, reason: 'memory' }, 'link fine, cache too small');
  assert.deepEqual(choose([1, 2, 3], [3]), { lod: 3, reason: 'link' }, 'several levels dropped for the link');
  assert.deepEqual(choose([3], [2, 3]), { lod: 3, reason: 'memory+link' }, 'each rule ruled out a level on the way');
  assert.deepEqual(choose([], []), { lod: 3, reason: 'memory+link' }, 'nothing qualifies: the floor');
  assert.deepEqual(chooseMovieLevel({ baseLod: 3, deepestLod: 3, fits: only(), linkOk: only() }), { lod: 3, reason: null }, 'no level to drop to: no reason');
  assert.deepEqual(chooseMovieLevel({ baseLod: 1, deepestLod: 3, fits: only(1, 2, 3) }), { lod: 1, reason: null }, 'linkOk is optional');
});

test('link rule: bytes per step x speed x share still to fetch must stay under 70 % of the measured bandwidth', () => {
  const MB = 1024 * 1024;
  const base = { bytesPerStep: 3 * MB, stepsPerSecond: 10, coldFraction: 1, bandwidth: 50 * MB };
  assert.equal(linkAllows(base), true, '30 MB/s of 50 MB/s is 60 %');
  assert.equal(linkAllows({ ...base, bandwidth: 40 * MB }), false, '30 of 40 MB/s is 75 %');
  assert.equal(linkAllows({ ...base, bandwidth: 30 * MB / LINK_HEADROOM }), false, 'exactly at the limit is not under it');
  assert.equal(linkAllows({ ...base, bandwidth: 30 * MB / LINK_HEADROOM + 1 }), true);
  assert.equal(linkAllows({ ...base, bandwidth: 5 * MB, coldFraction: 0 }), true, 'a loop already in memory needs no link');
  assert.equal(linkAllows({ ...base, bandwidth: 40 * MB, coldFraction: 0.5 }), true, 'half the loop in memory halves the need');
  assert.equal(linkAllows({ ...base, bandwidth: null }), true, 'no measurement: no objection');
  assert.equal(linkAllows({ ...base, bandwidth: 0 }), true);
  assert.equal(linkAllows({ ...base, stepsPerSecond: 60, bandwidth: 100 * MB }), false, 'a faster movie needs more');
});

test('wire ratio: compressed over decoded from the caches once they hold enough, a default before, clamped', () => {
  const MB = 1024 * 1024;
  assert.equal(wireRatio({ compressedBytes: null, decodedBytes: 100 * MB }), DEFAULT_WIRE_RATIO);
  assert.equal(wireRatio({ compressedBytes: 1 * MB, decodedBytes: 2 * MB }), DEFAULT_WIRE_RATIO, 'too little to tell');
  assert.equal(wireRatio({ compressedBytes: 70 * MB, decodedBytes: 100 * MB }), 0.7);
  assert.equal(wireRatio({ compressedBytes: 500 * MB, decodedBytes: 100 * MB }), 1.2);
  assert.equal(wireRatio({ compressedBytes: 1 * MB, decodedBytes: 100 * MB }), 0.2);
});

test('hint text names the rule: fits memory, link, or both', () => {
  assert.equal(describeReason('memory'), 'fits memory');
  assert.equal(describeReason('link'), 'link');
  assert.equal(describeReason('memory+link'), 'fits memory + link');
  assert.equal(describeReason(null), '');
});

// ---- the link sets the speed ----

test('link steps: the speed at which every frame ahead that is not in memory is in before the playhead gets there', () => {
  const base = { bytesPerStep: 700_000, bandwidth: 660_000 };
  const feeds = (LINK_HEADROOM * 660_000) / 700_000; // frames per second that 70 % of the link brings
  const cold = (from, to) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
  assert.ok(Math.abs(linkSteps({ ...base, coldAhead: cold(1, 32) }) - feeds) < 1e-9, 'cold from the next frame on: what the link feeds, the boundary of linkAllows');
  assert.equal(linkAllows({ ...base, stepsPerSecond: feeds * 0.99, coldFraction: 1 }), true);
  assert.equal(linkAllows({ ...base, stepsPerSecond: feeds * 1.01, coldFraction: 1 }), false);
  assert.ok(Math.abs(linkSteps({ ...base, coldAhead: cold(3, 32) }) - (feeds * 32) / 30) < 1e-9, 'two frames in hand, the rest cold: the 30th missing frame is 32 steps ahead');
  assert.equal(linkSteps({ ...base, coldAhead: [] }), Infinity, 'nothing missing: the link does not limit');
  assert.equal(linkSteps({ ...base, coldAhead: cold(1, 32), bandwidth: null }), Infinity, 'no bandwidth measured: nothing to go on');
  assert.equal(linkSteps({ ...base, coldAhead: cold(1, 32), bandwidth: 0 }), Infinity);
  assert.ok(Math.abs(linkSteps({ ...base, coldAhead: [40] }) - 40 * feeds) < 1e-9, 'one frame missing far ahead has time to come: it does not slow the movie');
  assert.ok(Math.abs(linkSteps({ ...base, coldAhead: [1, 2] }) - feeds) < 1e-9, 'the last frames missing right ahead of the playhead keep it at the speed of the link, not at the speed it had over the frames that were loaded');
  assert.ok(linkSteps({ ...base, coldAhead: [1, 2, 9, 10] }) <= linkSteps({ ...base, coldAhead: [1, 2] }), 'more missing frames never raise the speed');
});

test('the playback buffer loads as many timesteps at once as the measured link carries in a second, from one to three', () => {
  assert.equal(BUFFER_BATCH, 3);
  assert.equal(bufferBatch({ bytesPerStep: 524_000, bandwidth: 920_000 }), 1, 'a link that carries 1.8 steps a second loads them in order');
  assert.equal(bufferBatch({ bytesPerStep: 690_000, bandwidth: 660_000 }), 1, 'and one that carries less than a step still loads one');
  assert.equal(bufferBatch({ bytesPerStep: 524_000, bandwidth: 1_100_000 }), 2);
  assert.equal(bufferBatch({ bytesPerStep: 690_000, bandwidth: 12_000_000 }), 3, 'a fast link: three');
  assert.equal(bufferBatch({ bytesPerStep: 690_000, bandwidth: 12_000_000, max: 5 }), 5, 'a bigger maximum is an option');
  assert.equal(bufferBatch({ bytesPerStep: 690_000, bandwidth: null }), 3, 'nothing measured: three');
  assert.equal(bufferBatch({ bytesPerStep: 0, bandwidth: 660_000 }), 3);
});

test('the effective speed is the lowest of the requested speed, the display rate and what the link feeds, never below the floor', () => {
  let feeds = Infinity;
  const { playback, display } = setup({ count: 100, stepsPerSecond: 4, linkSteps: () => feeds });
  assert.equal(playback.effectiveStepsPerSecond, 4, 'not playing: nothing is limited');
  assert.equal(playback.linkLimited, false);
  playback.play();
  display.runUntil(500);
  assert.equal(playback.linkLimited, false, 'a link that feeds more than asked changes nothing');
  assert.equal(playback.effectiveStepsPerSecond, 4);
  feeds = 0.7;
  assert.equal(playback.effectiveStepsPerSecond, 0.7);
  assert.equal(playback.displayStepsPerSecond, 4, 'what the display would deliver on a link that feeds it');
  assert.equal(playback.linkStepsPerSecond, 0.7);
  assert.equal(playback.linkLimited, true);
  feeds = 0.01;
  assert.equal(playback.effectiveStepsPerSecond, MIN_LINK_STEPS_PER_SECOND, 'a link slower than the floor is not followed further down');
  feeds = 9;
  assert.equal(playback.effectiveStepsPerSecond, 4, 'a faster link does not raise it above the request');
  assert.equal(playback.linkLimited, false);
  playback.pause();
  feeds = 0.7;
  assert.equal(playback.effectiveStepsPerSecond, 4, 'paused: the link rule is off');
});

test('rounded for the interface: whole steps per second from 2, tenths below', () => {
  assert.equal(roundSteps(0.74), 0.7);
  assert.equal(roundSteps(1.96), 2);
  assert.equal(roundSteps(2.4), 2);
  assert.equal(roundSteps(59.94), 60);
});

test('a link-limited movie steps at the speed the link feeds and starts on two frames, not eight', () => {
  // Frames 1 and 2 are there, nothing after: two seconds at 1 step/s is all it waits for.
  const { playback, state, display } = setup({ count: 100, stepsPerSecond: 4, linkSteps: () => 1, ready: (index) => index <= 2 });
  playback.play();
  assert.equal(playback.buffered.needed, 2, 'two seconds at the speed that is delivered');
  assert.equal(playback.buffering, false, 'it starts on those two');
  assert.equal(state.steps.length, 1, 'the first step is at once');
  display.runUntil(1500);
  assert.deepEqual(state.steps.map((s) => s.index), [1, 2], 'then a step a second');
  assert.ok(Math.abs(state.steps[1].at - state.steps[0].at - 1000) < FRAME_60);

  const full = setup({ count: 100, stepsPerSecond: 4, ready: (index) => index <= 2 });
  full.playback.play();
  assert.equal(full.playback.buffering, true, 'at the speed asked for it would wait for eight');
  assert.equal(full.playback.buffered.needed, 8);
});

/**
 * A link that delivers one frame every `periodMs`, in the order the player asked for them, once it has something to deliver
 * (an idle link does not bank time). Frames 0 and 1 are loaded already.
 */
function slowLink(periodMs) {
  const link = { delivered: new Set([0, 1]), queue: [], nextAt: 0 };
  const pump = (now) => {
    while (link.queue.length > 0 && link.nextAt <= now) {
      link.delivered.add(link.queue.shift());
      link.nextAt += periodMs;
    }
    if (link.queue.length === 0) link.nextAt = Math.max(link.nextAt, now);
  };
  return {
    prepare: (indices) => {
      link.queue = indices.filter((index) => !link.delivered.has(index));
    },
    isReady: (index, now) => {
      pump(now);
      return link.delivered.has(index);
    },
  };
}

/**
 * 30 s of a 12-step loop at 4 steps/s over a link that delivers a frame every 0.75 s (1 MB/s, 750 KB steps, frames 0 and 1 in memory),
 * with the speed the link allows worked out by `rule` from the frames ahead that are not in memory, as the viewer does.
 */
function playShortLoopOverSlowLink(rule) {
  const COUNT = 12;
  const net = slowLink(750);
  const state = { index: 0 };
  const display = fakeDisplay();
  const playback = new Playback({
    count: COUNT,
    stepsPerSecond: 4,
    getIndex: () => state.index,
    goTo: (index) => {
      state.index = index;
    },
    isReady: (index) => net.isReady(index, display.now()),
    prepare: net.prepare,
    linkSteps: () => {
      const coldAhead = [];
      for (let k = 1; k < COUNT; k++) if (!net.isReady((state.index + k) % COUNT, display.now())) coldAhead.push(k);
      return rule(coldAhead);
    },
    now: display.now,
    requestFrame: display.requestFrame,
    cancelFrame: display.cancelFrame,
  });
  playback.play();
  display.runUntil(30_000);
  return playback.stats;
}

test('speeding up over frames that are loaded does not run the movie into the few that are not: the end of the first lap of a short loop', () => {
  const base = { bytesPerStep: 750_000, bandwidth: 1_000_000 };
  const justInTime = playShortLoopOverSlowLink((coldAhead) => linkSteps({ ...base, coldAhead }));
  // The share of the 11 frames ahead that is missing, taken as an average: with three left it says 3.4 steps/s, and the movie runs into them.
  const average = playShortLoopOverSlowLink((coldAhead) => (coldAhead.length === 0 ? Infinity : (LINK_HEADROOM * base.bandwidth * 11) / (base.bytesPerStep * coldAhead.length)));
  assert.ok(average.bufferingPauses >= 1, `by the share alone it runs dry at the end of the lap (${average.bufferingPauses} pauses)`);
  assert.equal(justInTime.bufferingPauses, 0, 'frame by frame it never does');
  assert.ok(justInTime.steps >= 40, `and it still plays through the loaded frames at full speed after the first lap (${justInTime.steps} steps in 30 s)`);
});

/** 60 s of playback of 117 timesteps at 4 steps/s over a link that delivers a frame every 1.25 s (0.8 steps/s), with the link rule saying `linkStepsPerSecond`. */
function playOverSlowLink(linkStepsPerSecond) {
  const net = slowLink(1250);
  const state = { index: 0, steps: 0 };
  const display = fakeDisplay();
  const playback = new Playback({
    count: 117,
    stepsPerSecond: 4,
    getIndex: () => state.index,
    goTo: (index) => {
      state.index = index;
      state.steps++;
    },
    isReady: (index) => net.isReady(index, display.now()),
    prepare: net.prepare,
    linkSteps: () => linkStepsPerSecond,
    now: display.now,
    requestFrame: display.requestFrame,
    cancelFrame: display.cancelFrame,
  });
  playback.play();
  display.runUntil(60_000);
  return { stats: playback.stats, steps: state.steps };
}

test('on a link that cannot keep up with the speed asked for, lowering the speed replaces repeated pauses with a steady slower pace', () => {
  // The link rule keeps 30 % in hand: 0.7 steps per second for a link that delivers 0.8.
  const uncapped = playOverSlowLink(Infinity);
  const capped = playOverSlowLink(0.7);
  assert.ok(uncapped.stats.bufferingPauses >= 4, `at the speed asked for it pauses again and again (${uncapped.stats.bufferingPauses} pauses, ${Math.round(uncapped.stats.bufferingMs)} ms)`);
  assert.equal(capped.stats.bufferingPauses, 0, 'at the speed the link feeds it never runs dry');
  assert.ok(capped.steps >= 38, `and it keeps a steady 0.7 steps per second: ${capped.steps} steps in 60 s`);
  assert.ok(capped.stats.initialBufferMs < 4000, `it starts after two frames, not eight (${Math.round(capped.stats.initialBufferMs)} ms)`);
  assert.ok(uncapped.stats.initialBufferMs > 7000, `eight frames take over seven seconds on that link (${Math.round(uncapped.stats.initialBufferMs)} ms)`);
});
