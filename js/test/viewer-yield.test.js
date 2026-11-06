import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { yieldAfterPaint, yieldToMain } from '../demo/yield.js';

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

/** Animation frames that the test runs: `runFrame` calls what was asked for since the previous one, and nothing runs before it. */
function fakeFrames() {
  const queued = new Map();
  let nextId = 1;
  globalThis.requestAnimationFrame = (callback) => {
    queued.set(nextId, callback);
    return nextId++;
  };
  globalThis.cancelAnimationFrame = (id) => {
    queued.delete(id);
  };
  return {
    pending: () => queued.size,
    runFrame() {
      const callbacks = [...queued.values()];
      queued.clear();
      for (const callback of callbacks) callback(0);
    },
  };
}

afterEach(() => {
  delete globalThis.requestAnimationFrame;
  delete globalThis.cancelAnimationFrame;
});

test('a task yielded after paint waits for the next frame, then runs in a task of its own after the frame', async () => {
  const frames = fakeFrames();
  const order = [];
  yieldAfterPaint(() => order.push('slice'));
  await settle();
  assert.deepEqual(order, [], 'no frame, no slice');
  frames.runFrame();
  order.push('frame');
  assert.deepEqual(order, ['frame'], 'the slice is not run by the frame');
  await settle();
  assert.deepEqual(order, ['frame', 'slice']);
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
