import assert from 'node:assert/strict';
import { test } from 'node:test';
import { describe, median, percentile, rafIsNominal, spread } from './stats.mjs';

test('median of odd and even counts, ignoring nulls', () => {
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([4, 1, 2, 3]), 2.5);
  assert.equal(median([null, 5, undefined, NaN]), 5);
  assert.equal(median([]), null);
});

test('percentile takes the sorted value at floor(q n)', () => {
  const values = Array.from({ length: 20 }, (_, i) => i + 1);
  assert.equal(percentile(values, 0.95), 20);
  assert.equal(percentile(values, 0.5), 11);
  assert.equal(percentile([7], 0.95), 7);
});

test('describe counts only finite values', () => {
  assert.deepEqual(describe([1, null, 3]), { n: 2, median: 2, p95: 3, min: 1, max: 3 });
  assert.deepEqual(describe([]), { n: 0, median: null, p95: null, min: null, max: null });
});

test('an idle page is nominal near 16.7 ms and not at 33.3 ms', () => {
  const range = { min: 15.5, max: 18 };
  assert.equal(rafIsNominal([16.6, 16.7, 16.8, 40], range), true);
  assert.equal(rafIsNominal([33.3, 33.4, 33.2], range), false);
  assert.equal(rafIsNominal([], range), false);
});

test('spread shows the median and the range of the rounds', () => {
  const format = (v) => v.toFixed(0);
  assert.equal(spread([10, 30, 20], format), '20 (10-30)');
  assert.equal(spread([5], format), '5');
  assert.equal(spread([null], format), 'n/a');
});
