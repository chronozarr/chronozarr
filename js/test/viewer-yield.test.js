import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { SliceScheduler, yieldAfterPaint, yieldToMain } from '../demo/yield.js';
import { framesInSequence } from '../demo/scrub.js';

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

test('a yielded task runs in a task of its own, after the code that yielded it, in the order they were yielded', async () => {
  const order = [];
  yieldToMain(() => order.push('first'));
  yieldToMain(() => order.push('second'));
  Promise.resolve().then(() => order.push('microtask'));
  order.push('caller');
  await settle();
  assert.deepEqual(order, ['caller', 'microtask', 'first', 'second']);
});

test('a task that yields again continues in the next task, so a long job runs in slices that other tasks can come between', async () => {
  const order = [];
  let slice = 0;
  const job = () => {
    order.push(`slice ${slice}`);
    if (++slice < 3) yieldToMain(job);
  };
  yieldToMain(job);
  yieldToMain(() => order.push('other'));
  await settle();
  assert.deepEqual(order, ['slice 0', 'other', 'slice 1', 'slice 2'], 'the task of someone else ran between the slices');
});

test('a cancelled task never runs and does not hold back the ones behind it', async () => {
  const order = [];
  const cancel = yieldToMain(() => order.push('cancelled'));
  yieldToMain(() => order.push('kept'));
  cancel();
  await settle();
  assert.deepEqual(order, ['kept']);
});

test('cancelling after the task ran, or twice, does nothing', async () => {
  let runs = 0;
  const cancel = yieldToMain(() => runs++);
  await settle();
  cancel();
  cancel();
  assert.equal(runs, 1);
});

test('a canceller kept after its task ran does not cancel a later yield of the same function', async () => {
  let runs = 0;
  const job = () => runs++;
  const first = yieldToMain(job);
  await settle();
  yieldToMain(job);
  first();
  await settle();
  assert.equal(runs, 2);
});

/** Animation frames that the test runs: `runFrame(start)` calls what was asked for since the previous one with the time `start`, and nothing runs before it. */
function fakeFrames() {
  const queued = new Map();
  let nextId = 1;
  let requests = 0;
  globalThis.requestAnimationFrame = (callback) => {
    requests++;
    queued.set(nextId, callback);
    return nextId++;
  };
  globalThis.cancelAnimationFrame = (id) => {
    queued.delete(id);
  };
  return {
    pending: () => queued.size,
    requests: () => requests,
    runFrame(start = 0) {
      const callbacks = [...queued.values()];
      queued.clear();
      for (const callback of callbacks) callback(start);
    },
  };
}

afterEach(() => {
  delete globalThis.requestAnimationFrame;
  delete globalThis.cancelAnimationFrame;
});

test('a task yielded after paint waits for the next frame, then runs in a task of its own after the frame, with the time of the frame', async () => {
  const frames = fakeFrames();
  const order = [];
  yieldAfterPaint((start) => order.push(`slice of the frame at ${start}`));
  await settle();
  assert.deepEqual(order, [], 'no frame, no slice');
  frames.runFrame(250);
  order.push('frame');
  assert.deepEqual(order, ['frame'], 'the slice is not run by the frame');
  await settle();
  assert.deepEqual(order, ['frame', 'slice of the frame at 250']);
});

test('a job that continues itself after paint runs one slice per frame; one that yields to main does not wait for frames', async () => {
  const frames = fakeFrames();
  const run = async (yielding) => {
    let slices = 0;
    const job = () => {
      if (++slices < 5) yielding(job);
    };
    yielding(job);
    await settle();
    return () => slices;
  };

  const paced = await run(yieldAfterPaint);
  assert.equal(paced(), 0, 'no frame yet');
  for (let frame = 1; frame <= 3; frame++) {
    frames.runFrame();
    await settle();
    assert.equal(paced(), frame, `after frame ${frame}`);
  }
  await settle();
  assert.equal(paced(), 3, 'none between frames');

  const idle = await run(yieldToMain);
  assert.equal(idle(), 5, 'all five slices ran with no frame');
});

test('a cancelled task after paint never runs, whether it is cancelled before its frame or between the frame and its task', async () => {
  const frames = fakeFrames();
  const order = [];
  const beforeFrame = yieldAfterPaint(() => order.push('before the frame'));
  assert.equal(frames.pending(), 1);
  beforeFrame();
  assert.equal(frames.pending(), 0, 'the frame that was asked for is cancelled');
  const afterFrame = yieldAfterPaint(() => order.push('after the frame'));
  frames.runFrame();
  afterFrame();
  yieldAfterPaint(() => order.push('kept'));
  frames.runFrame();
  await settle();
  assert.deepEqual(order, ['kept']);
});

const FRAME_MS = 1000 / 60;
const BUDGET_MS = FRAME_MS / 2;

/**
 * A job that uploads `uploadMs` at a time in slices that end with the upload that passes `sliceMs`, as the GPU fill does,
 * on a clock that only the uploads and the test move. `paced` is what the scheduler asks to know whether frames follow each other.
 */
function fakeFill({ uploadMs = 0.3, sliceMs = 4, paced = () => true } = {}) {
  const fill = { clock: 0, left: 0, uploaded: 0, sliceStarts: [], paced };
  const slice = () => {
    const started = fill.clock;
    fill.sliceStarts.push(started);
    while (fill.left > 0) {
      fill.left--;
      fill.uploaded++;
      fill.clock += uploadMs;
      if (fill.clock - started > sliceMs) break;
    }
    return fill.left > 0;
  };
  fill.scheduler = new SliceScheduler({ slice, isPaced: () => fill.paced(), budgetMs: () => BUDGET_MS, now: () => fill.clock });
  /** A frame that begins at `start`, whose paint takes `paintMs`; the slices that follow it run before this returns. */
  fill.frame = async (frames, start, paintMs = 2) => {
    fill.clock = start + paintMs;
    frames.runFrame(start);
    await settle();
  };
  return fill;
}

test('while frames follow each other the job works for half of each frame, so that a movie of 36 cells at 60 steps/s gets most of its uploads before the paint', async () => {
  const frames = fakeFrames();
  const fill = fakeFill();
  fill.scheduler.schedule();
  const perFrame = [];
  for (let frame = 0; frame < 20; frame++) {
    // Each step needs the 36 cells of a timestep on the GPU; whatever the job has not uploaded the paint uploads.
    fill.left += 36;
    const before = { uploaded: fill.uploaded, slices: fill.sliceStarts.length };
    const start = frame * FRAME_MS;
    await fill.frame(frames, start);
    perFrame.push({ uploaded: fill.uploaded - before.uploaded, slices: fill.sliceStarts.slice(before.slices).map((at) => at - start) });
    fill.left = 0;
  }
  for (const { uploaded, slices } of perFrame) {
    assert.ok(uploaded >= 28, `${uploaded} of the 36 uploads ran before the paint (one slice of 4 ms is 14)`);
    assert.ok(slices.slice(1).every((after) => after < BUDGET_MS), `a slice after the first began ${slices.at(-1)} ms into the frame, past the budget of ${BUDGET_MS}`);
  }
  const slicesBefore = fill.sliceStarts.length;
  await settle();
  assert.equal(fill.sliceStarts.length, slicesBefore, 'none between frames');
});

test('the first slice after a paint runs even when the paint used up the budget, and no more do', async () => {
  const frames = fakeFrames();
  const fill = fakeFill();
  fill.left = 200;
  fill.scheduler.schedule();
  await fill.frame(frames, 0, BUDGET_MS * 1.5);
  assert.equal(fill.sliceStarts.length, 1);
  await fill.frame(frames, FRAME_MS, 0);
  assert.equal(fill.sliceStarts.length, 3, 'a frame with the time for it runs slices until the budget is gone');
});

test('when nothing moves the slices follow each other with no frame', async () => {
  fakeFrames();
  const fill = fakeFill({ paced: () => false });
  fill.left = 100;
  fill.scheduler.schedule();
  await settle();
  assert.equal(fill.left, 0);
  assert.ok(fill.sliceStarts.length > 1);
});

test('a movie that buffers is not paced: the job runs with no frame until the movie paints again', async () => {
  const frames = fakeFrames();
  const state = { scrubbing: false, playing: true, buffering: true, sinceCameraMoveMs: Infinity };
  const fill = fakeFill({ paced: () => framesInSequence(state) });
  fill.left = 100;
  fill.scheduler.schedule();
  await settle();
  assert.equal(fill.left, 0, 'the window filled while the movie waited for the network');

  state.buffering = false;
  fill.left = 100;
  fill.scheduler.schedule();
  await settle();
  assert.equal(fill.left, 100, 'playing: it waits for a frame');
  await fill.frame(frames, 0);
  assert.ok(fill.left < 100);
});

test('a slice that is waiting for the other way is replaced; one that waits in the right way is left as it is', async () => {
  const frames = fakeFrames();
  const fill = fakeFill({ paced: () => false });
  fill.left = 100;
  fill.scheduler.schedule();
  fill.paced = () => true;
  fill.scheduler.schedule();
  await settle();
  assert.equal(fill.uploaded, 0, 'the message slice was cancelled: this one waits for a frame');
  assert.equal(frames.pending(), 1);
  fill.scheduler.schedule();
  assert.equal(frames.pending(), 1);
  assert.equal(frames.requests(), 1, 'asking again does not ask for another frame');
  fill.paced = () => false;
  fill.scheduler.schedule();
  assert.equal(frames.pending(), 0);
  await settle();
  assert.equal(fill.left, 0, 'the way it waited changed, so it ran with no frame');
});

test('cancelUnpaced drops a slice that waits for a message and leaves one that waits for a paint', async () => {
  const frames = fakeFrames();
  const unpaced = fakeFill({ paced: () => false });
  unpaced.left = 10;
  unpaced.scheduler.schedule();
  unpaced.scheduler.cancelUnpaced();
  await settle();
  assert.equal(unpaced.uploaded, 0);

  const paced = fakeFill();
  paced.left = 10;
  paced.scheduler.schedule();
  paced.scheduler.cancelUnpaced();
  await paced.frame(frames, 0);
  assert.equal(paced.left, 0);
});

test('a gesture that moves the view in every frame cannot keep a paced job from running', async () => {
  const frames = fakeFrames();
  const fill = fakeFill({ paced: () => true });
  fill.scheduler.schedule();
  for (let frame = 0; frame < 30; frame++) {
    // The input of the frame (a pointer move) drops what waits for a message, then the frame runs, then it draws and asks for the job.
    fill.scheduler.cancelUnpaced();
    fill.left += 14;
    await fill.frame(frames, frame * FRAME_MS);
    fill.scheduler.schedule();
  }
  assert.ok(fill.sliceStarts.length >= 30, `${fill.sliceStarts.length} slices in 30 frames`);
});

test('cancel stops a job that waits for a frame, and one that waits between two slices of a frame', async () => {
  const frames = fakeFrames();
  const fill = fakeFill();
  fill.left = 500;
  fill.scheduler.schedule();
  fill.scheduler.cancel();
  await fill.frame(frames, 0);
  assert.equal(fill.uploaded, 0);

  fill.scheduler.schedule();
  fill.clock = 2;
  frames.runFrame(0);
  fill.scheduler.cancel();
  await settle();
  assert.equal(fill.uploaded, 0, 'cancelled after the frame, before its slice');
});

test('a job with nothing left schedules nothing until it is asked again', async () => {
  const frames = fakeFrames();
  const fill = fakeFill();
  fill.left = 5;
  fill.scheduler.schedule();
  await fill.frame(frames, 0);
  assert.equal(frames.pending(), 0);
  fill.left = 5;
  fill.scheduler.schedule();
  await fill.frame(frames, FRAME_MS);
  assert.equal(fill.left, 0);
});
