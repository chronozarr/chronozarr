// The verdict of the speed-gate job (support/gate-bench): which differences between the base and the head build fail it.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { METRICS, compareBuilds, summaryMarkdown } from '../support/gate-bench/compare.js';

const QUIET = { serverRequests: 0, readerRequests: 0, readerBytes: 0, incomplete: 0 };
const BASE_FIGURES = { coldOpenMs: 400, decodeMs: 6, warmStepMs: 20, warmJumpMs: 40 };

/** Pairs of runs, base and head, whose figures are the base ones times `scale` (per pair: a number or a function of the pair). */
function runs(pairs, { head = {}, headLoop = () => QUIET, baseLoop = () => QUIET } = {}) {
  const records = [];
  for (let pair = 0; pair < pairs; pair++) {
    // Each pair runs on a slightly different machine state, as on a shared runner.
    const drift = 1 + 0.03 * Math.sin(pair);
    const figures = Object.fromEntries(Object.entries(BASE_FIGURES).map(([key, value]) => [key, value * drift]));
    const headFigures = Object.fromEntries(Object.entries(figures).map(([key, value]) => [key, typeof head[key] === 'function' ? head[key](value, pair) : value * (head[key] ?? 1)]));
    records.push({ variant: 'base', pair, figures, warmLoop: baseLoop(pair) });
    records.push({ variant: 'head', pair, figures: headFigures, warmLoop: headLoop(pair) });
  }
  return records;
}

test('the same build on both sides passes', () => {
  const result = compareBuilds(runs(5));
  assert.deepEqual(result.failures, []);
  for (const row of result.rows) assert.equal(row.ratio, 1, row.key);
});

test('every metric fails once the head is slower than its tolerance allows', () => {
  for (const metric of METRICS) {
    const result = compareBuilds(runs(5, { head: { [metric.key]: (1 + metric.tolerance) * 1.2 } }));
    assert.equal(result.failures.length, 1, metric.key);
    assert.match(result.failures[0], new RegExp(metric.label), metric.key);
  }
});

test('a slowdown inside the tolerance passes', () => {
  for (const metric of METRICS) {
    const result = compareBuilds(runs(5, { head: { [metric.key]: 1 + metric.tolerance * 0.8 } }));
    assert.deepEqual(result.failures, [], metric.key);
  }
});

test('a faster head passes', () => {
  const result = compareBuilds(runs(5, { head: Object.fromEntries(METRICS.map((m) => [m.key, 0.5])) }));
  assert.deepEqual(result.failures, []);
});

test('a slowdown smaller than the floor in milliseconds passes, however large the ratio', () => {
  for (const metric of METRICS) {
    // From a base far below the floor, a ratio of 2 adds less than the floor.
    const base = metric.floorMs / 4;
    const result = compareBuilds(runs(5, { head: { [metric.key]: () => base * 2 } }).map((r) => (r.variant === 'base' ? { ...r, figures: { ...r.figures, [metric.key]: base } } : r)));
    assert.deepEqual(result.failures, [], metric.key);
  }
});

test('one slow pair of five does not fail; three do', () => {
  // The other four pairs are 10 % slower: inside the tolerance, but far enough above the floor that only the median of the ratios keeps the slow pair out.
  const onePair = compareBuilds(runs(5, { head: { coldOpenMs: (value, pair) => (pair === 2 ? value * 3 : value * 1.1) } }));
  assert.deepEqual(onePair.failures, []);
  const threePairs = compareBuilds(runs(5, { head: { coldOpenMs: (value, pair) => (pair < 3 ? value * 3 : value) } }));
  assert.equal(threePairs.failures.length, 1);
});

test('any request or byte in a warm loop of the head fails, whatever the timings', () => {
  for (const field of ['serverRequests', 'readerRequests', 'readerBytes']) {
    const result = compareBuilds(runs(5, { headLoop: (pair) => (pair === 4 ? { ...QUIET, [field]: 1 } : QUIET) }));
    assert.equal(result.failures.length, 1, field);
    assert.match(result.failures[0], /warm/i, field);
  }
});

test('traffic in a warm loop of the base is reported, not failed', () => {
  const result = compareBuilds(runs(3, { baseLoop: () => ({ ...QUIET, serverRequests: 2, readerBytes: 1000 }) }));
  assert.deepEqual(result.failures, []);
  assert.deepEqual(result.traffic.base, { serverRequests: 6, readerRequests: 0, readerBytes: 3000, incomplete: 0 });
});

test('an incomplete frame in a warm loop of the head fails: that switch was not warm', () => {
  const result = compareBuilds(runs(3, { headLoop: (pair) => (pair === 0 ? { ...QUIET, incomplete: 1 } : QUIET) }));
  assert.equal(result.failures.length, 1);
  assert.match(result.failures[0], /incomplete/);
});

test('a figure that is missing, not a number or not positive stops the comparison, naming the metric, the build and the pair', () => {
  // A renamed field of bench() reaches compare.js as undefined; NaN compares false with any tolerance and would pass.
  for (const bad of [undefined, Number.NaN, Number.POSITIVE_INFINITY, 0, -1]) {
    for (const variant of ['base', 'head']) {
      const records = runs(3).map((r) => (r.variant === variant && r.pair === 1 ? { ...r, figures: { ...r.figures, decodeMs: bad } } : r));
      // The runner's log counts pairs from 1.
      assert.throws(() => compareBuilds(records), new RegExp(`decodeMs of the ${variant} run of pair 2`), `${variant} ${bad}`);
    }
  }
});

test('fewer than three pairs cannot decide', () => {
  assert.throws(() => compareBuilds(runs(2)), /3 pairs/);
});

test('the summary gives the absolute figures of both builds, the gates and the verdict', () => {
  const result = compareBuilds(runs(5, { head: { coldOpenMs: 2 } }));
  const text = summaryMarkdown(result, { environment: ['Chromium 140, SwiftShader'] });
  for (const metric of METRICS) assert.match(text, new RegExp(metric.label));
  assert.match(text, /≤ 500 ms/);
  assert.match(text, /Chromium 140, SwiftShader/);
  assert.match(text, /fail/i);
  const headColdOpen = Math.round(result.rows.find((row) => row.key === 'coldOpenMs').head.median);
  assert.ok(headColdOpen > 790 && text.includes(`${headColdOpen} ms`), 'the head cold open, about 2 x 400 ms, in absolute terms');
});
