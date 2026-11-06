// The verdict of the speed-gate job (support/gate-bench): which differences between the base and the head build fail it.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { COMPACT_METRICS, METRICS, compareBuilds, summaryMarkdown } from '../support/gate-bench/compare.js';
import { guardsMode, invalidRunsNotice, overloadedRuns } from '../support/gate-bench/guards.js';

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

test('the compact pass compares the cold open and the warm switches against the same gates, without decode', () => {
  assert.deepEqual(COMPACT_METRICS.map((metric) => metric.key), ['coldOpenMs', 'warmStepMs', 'warmJumpMs']);
  for (const metric of COMPACT_METRICS) {
    const plain = METRICS.find((candidate) => candidate.key === metric.key);
    assert.deepEqual({ ...metric, label: plain.label }, plain, 'the gate and its tolerance are those of the default pass');
    assert.match(metric.label, /, compact cache$/);
  }
});

test('records of the compact pass have no decode figure and are compared on the three metrics', () => {
  const records = runs(5).map((record) => ({ ...record, figures: Object.fromEntries(Object.entries(record.figures).filter(([key]) => key !== 'decodeMs')) }));
  const result = compareBuilds(records, { metrics: COMPACT_METRICS });
  assert.deepEqual(result.failures, []);
  assert.deepEqual(result.rows.map((row) => row.key), ['coldOpenMs', 'warmStepMs', 'warmJumpMs']);
  // The default metrics still need the decode figure.
  assert.throws(() => compareBuilds(records), /decodeMs of the base run of pair 1 is undefined/);
});

test('the compact pass fails on a slower head like the default pass, and the summary says which pass it is', () => {
  const records = runs(5, { head: { warmStepMs: 2 } }).map((record) => ({ ...record, figures: Object.fromEntries(Object.entries(record.figures).filter(([key]) => key !== 'decodeMs')) }));
  const result = compareBuilds(records, { metrics: COMPACT_METRICS });
  assert.equal(result.failures.length, 1);
  assert.match(result.failures[0], /Warm switch, stepping, compact cache/);
  assert.match(summaryMarkdown(result, { title: 'Speed gates, compact cache: head against base' }), /^## Speed gates, compact cache: head against base/);
  assert.match(summaryMarkdown(result), /^## Speed gates: head against base/);
});

const visit = (scale = 1) => ({ firstRequestMs: 100 * scale, shaderMs: 5 * scale, workerReadyMs: 60 * scale, codecMs: 8 * scale, firstFrameMs: 300 * scale, targetFrameMs: 320 * scale });

test('the first visit of both builds is in the result and the summary, as information that fails nothing', () => {
  const records = runs(5, { head: { coldOpenMs: 1 } }).map((record) => ({ ...record, firstVisit: visit(record.variant === 'head' ? 3 : 1) }));
  const result = compareBuilds(records);
  assert.deepEqual(result.failures, []);
  const target = result.firstVisit.rows.find((row) => row.key === 'targetFrameMs');
  assert.equal(target.base.median, 320);
  assert.equal(target.head.median, 960);
  assert.equal(target.ratio, 3);
  assert.equal(result.firstVisit.unavailable, 0);
  const text = summaryMarkdown(result);
  assert.match(text, /### First visit \(informational, not gated\)/);
  assert.match(text, /\| Navigation to the frame at the target level \| 320 ms \(320 to 320\) \| 960 ms \(960 to 960\) \| 3\.00 \|/);
});

test('a run whose first visit failed is counted and the other runs give the rows', () => {
  const records = runs(5).map((record) => ({ ...record, firstVisit: record.pair === 2 && record.variant === 'head' ? { error: 'no frame' } : visit() }));
  const result = compareBuilds(records);
  assert.equal(result.firstVisit.unavailable, 1);
  assert.equal(result.firstVisit.rows.find((row) => row.key === 'shaderMs').head.n, 4);
  assert.match(summaryMarkdown(result), /The first visit failed in 1 of the runs/);
});

test('a mark that no run produced shows a dash, and runs without a first visit leave the section out', () => {
  const marked = runs(3).map((record) => ({ ...record, firstVisit: { ...visit(), codecMs: null } }));
  const row = compareBuilds(marked).firstVisit.rows.find((candidate) => candidate.key === 'codecMs');
  assert.equal(row.base, null);
  assert.equal(row.ratio, null);
  assert.match(summaryMarkdown(compareBuilds(marked)), /\| Codec module load by the page \| – \| – \| – \|/);
  const plain = compareBuilds(runs(3));
  assert.equal(plain.firstVisit, null);
  assert.doesNotMatch(summaryMarkdown(plain), /First visit/);
});

test('an overloaded run gets an invalid summary instead of a gate verdict', () => {
  // The machine passed the check at the start of the process (load 6), and in the fourth pair the head ran while the load went to 84.
  const records = runs(5).map((record) => ({ ...record, load: record.variant === 'head' && record.pair === 3 ? { start: 6, end: 83.9, max: 84 } : { start: 6, end: 7, max: 7 } }));
  const overloaded = overloadedRuns(records, { mode: guardsMode({ env: {} }), cores: 16 });
  const invalid = invalidRunsNotice(overloaded, { total: records.length, cores: 16, label: (record) => `pair ${record.pair + 1} ${record.variant}` });
  const text = summaryMarkdown(null, { invalid, environment: ['Chromium 153, Metal'] });
  assert.match(text, /\*\*Invalid: 1 of 10 runs had a 1-minute load above 8 \(half of 16 logical cores\) while they ran:\*\* pair 4 head \(load 84\)/);
  assert.match(text, /The comparison was skipped/);
  assert.match(text, /Chromium 153, Metal/);
  assert.doesNotMatch(text, /\*\*(?:Pass|Fail)\.\*\*|Medians over/);
  assert.doesNotMatch(summaryMarkdown(compareBuilds(records)), /Invalid/);
});
