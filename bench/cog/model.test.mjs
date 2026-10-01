import assert from 'node:assert/strict';
import test from 'node:test';
import { simulate, simulatePhase } from './model.mjs';

const MB = 1e6;
const link = (extra = {}) => ({ rttMs: 100, mbps: 8, parallel: 6, ...extra }); // 8 Mbit/s = 1 MB/s

test('one request takes a round trip plus its bytes over the link', () => {
  const { totalMs } = simulate([{ id: 'a', bytes: 1 * MB }], link());
  assert.ok(Math.abs(totalMs - 1100) < 1e-6, String(totalMs));
});

test('parallel requests share the link and overlap their round trips', () => {
  const { totalMs, finishMs } = simulate([{ id: 'a', bytes: 1 * MB }, { id: 'b', bytes: 1 * MB }], link());
  assert.ok(Math.abs(totalMs - 2100) < 1e-6, String(totalMs));
  assert.ok(Math.abs(finishMs.get('a') - 2100) < 1e-6);
});

test('a limit of one request in flight serialises them, round trip included', () => {
  const { totalMs } = simulate([{ id: 'a', bytes: 1 * MB }, { id: 'b', bytes: 1 * MB }], link({ parallel: 1 }));
  assert.ok(Math.abs(totalMs - 2200) < 1e-6, String(totalMs));
});

test('a request that depends on another starts after it finishes', () => {
  const { totalMs } = simulate([{ id: 'header', bytes: 0 }, { id: 'tile', bytes: 1 * MB, after: ['header'] }], link());
  assert.ok(Math.abs(totalMs - 1200) < 1e-6, String(totalMs));
});

test('a smaller request finishes early and the larger one then gets the whole link', () => {
  const { finishMs } = simulate([{ id: 'small', bytes: 0.5 * MB }, { id: 'large', bytes: 1.5 * MB }], link());
  // Both transfer from t=100: 0.5 MB each at half the link by t=1100, then 1 MB left alone: t=2100.
  assert.ok(Math.abs(finishMs.get('small') - 1100) < 1e-6, String(finishMs.get('small')));
  assert.ok(Math.abs(finishMs.get('large') - 2100) < 1e-6, String(finishMs.get('large')));
});

test('with unlimited bandwidth only round trips remain', () => {
  const chain = [{ id: 'a', bytes: 5 * MB }, { id: 'b', bytes: 5 * MB, after: ['a'] }, { id: 'c', bytes: 5 * MB, after: ['b'] }];
  const { totalMs } = simulate(chain, link({ mbps: Infinity }));
  assert.equal(totalMs, 300);
});

test('sequential steps add up, pipelined steps overlap within the parallel limit', () => {
  const step = [{ id: 'x', bytes: 0 }];
  const steps = [step, step, step];
  const sequential = simulatePhase(steps, link({ mbps: Infinity }), { sequential: true });
  assert.equal(sequential.totalMs, 300);
  assert.deepEqual(sequential.stepMs, [100, 100, 100]);
  const pipelined = simulatePhase(steps, link({ mbps: Infinity }), { sequential: false });
  assert.equal(pipelined.totalMs, 100);
  const capped = simulatePhase(steps, link({ mbps: Infinity, parallel: 1 }), { sequential: false });
  assert.equal(capped.totalMs, 300);
});

test('a dependency on a request that does not exist is an error naming it', () => {
  assert.throws(() => simulate([{ id: 'a', bytes: 1, after: ['missing'] }], link()), /unknown request missing/);
});

test('a dependency cycle is an error rather than a hang', () => {
  assert.throws(() => simulate([{ id: 'a', bytes: 1, after: ['b'] }, { id: 'b', bytes: 1, after: ['a'] }], link()), /never became ready/);
});
