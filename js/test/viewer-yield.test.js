import { test } from 'node:test';
import assert from 'node:assert/strict';
import { yieldToMain } from '../demo/yield.js';

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
