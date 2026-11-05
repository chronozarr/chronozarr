import { afterEach, test } from 'node:test';
import assert from 'node:assert/strict';
import { yieldToMain } from '../demo/yield.js';

const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

/** A scheduler.postTask that holds its tasks until `run`, and drops one whose signal is aborted, rejecting with an AbortError as browsers do. */
function fakeScheduler() {
  const posted = [];
  return {
    posted,
    postTask(callback, options) {
      return new Promise((resolve, reject) => {
        const entry = {
          options,
          run() {
            if (options.signal.aborted) return;
            try {
              resolve(callback());
            } catch (error) {
              reject(error);
            }
          },
        };
        options.signal.addEventListener('abort', () => reject(new DOMException('The task was aborted', 'AbortError')), { once: true });
        posted.push(entry);
      });
    },
  };
}

afterEach(() => {
  delete globalThis.scheduler;
});

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

test('where scheduler.postTask exists the task is posted to it at its default priority', async () => {
  const scheduler = fakeScheduler();
  globalThis.scheduler = scheduler;
  const order = [];
  yieldToMain(() => order.push('ran'));
  assert.equal(scheduler.posted.length, 1);
  assert.equal('priority' in scheduler.posted[0].options, false, 'a task at background priority can wait behind normal tasks for ever');
  assert.deepEqual(order, [], 'nothing runs in the call');
  scheduler.posted[0].run();
  assert.deepEqual(order, ['ran']);
});

test('with scheduler.postTask a cancelled task is aborted, so the browser drops it, and the abort is not an unhandled rejection', async () => {
  const scheduler = fakeScheduler();
  globalThis.scheduler = scheduler;
  const rejections = [];
  const record = (reason) => rejections.push(reason);
  process.on('unhandledRejection', record);
  try {
    const order = [];
    const cancel = yieldToMain(() => order.push('cancelled'));
    yieldToMain(() => order.push('kept'));
    cancel();
    assert.deepEqual(scheduler.posted.map((entry) => entry.options.signal.aborted), [true, false]);
    for (const entry of scheduler.posted) entry.run();
    await settle();
    assert.deepEqual(order, ['kept']);
    assert.deepEqual(rejections, []);
  } finally {
    process.off('unhandledRejection', record);
  }
});

test('with scheduler.postTask a task that throws is reported as an uncaught error, not as an unhandled rejection', async () => {
  const scheduler = fakeScheduler();
  globalThis.scheduler = scheduler;
  const failure = new Error('the fill failed');
  const reported = [];
  const queueMicrotaskBefore = globalThis.queueMicrotask;
  globalThis.queueMicrotask = (callback) => reported.push(callback);
  try {
    yieldToMain(() => {
      throw failure;
    });
    scheduler.posted[0].run();
    await settle();
  } finally {
    globalThis.queueMicrotask = queueMicrotaskBefore;
  }
  assert.equal(reported.length, 1, 'the error was handed to be thrown outside the promise');
  assert.throws(reported[0], (error) => error === failure);
});
