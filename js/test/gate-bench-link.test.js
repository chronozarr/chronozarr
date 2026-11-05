// The figures and the comparison of the link A/B (support/gate-bench/link-compare.js): what a run's raw result turns into,
// and which differences between the base and the head are flagged.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LINKS, LINK_METRICS, SCENARIOS, compareLinks, dragFigures, jumpFigures, linkSummaryMarkdown, playFigures, runFigures } from '../support/gate-bench/link-compare.js';

const paint = (at, t, lod, kind = 'target') => ({ type: 'paint', at, t, lod, kind });

/** A drag of timesteps 0 to 4, one every 100 ms from time 1000, released at 1400, drawn at level 2 until the frame at level 1 at 1900. */
function drag(overrides = {}) {
  return {
    events: [paint(1010, 1, 2), paint(1110, 2, 2), paint(1310, 4, 2), { type: 'kept', at: 1250, t: 3 }, { type: 'kept', at: 1600, t: 4 }, paint(1900, 4, 1)],
    inputs: [1, 2, 3, 4].map((t) => ({ at: 1000 + t * 100 - 100, t })),
    releaseAt: 1400,
    normalLod: 1,
    from: 0,
    to: 4,
    capMs: 60000,
    ...overrides,
  };
}

test('a drag counts the frames at and below the target level and the holds up to the release, and the steps never shown', () => {
  const { figures } = dragFigures(drag());
  assert.equal(figures.framesAtTarget, 0);
  assert.equal(figures.framesCoarser, 3);
  assert.equal(figures.held, 1, 'the hold after the release is not the drag');
  assert.equal(figures.skipped, 1, 'timestep 3 was never drawn');
  assert.equal(figures.settleMs, 500);
});

test('the lag of a step is the wait for the first frame that shows its timestep or a later one', () => {
  const { figures, detail } = dragFigures(drag());
  // Inputs at 1000 (t 1), 1100 (t 2), 1200 (t 3), 1300 (t 4); answers at 1010, 1110, 1310 (t 4 answers t 3 too), 1310.
  assert.equal(figures.lagMedianMs, 10);
  assert.equal(figures.lagP95Ms, 110);
  assert.equal(detail.stepsNeverAnswered, 0);
});

test('a drag whose last frame never comes reports the wait as the figure and says so', () => {
  const { figures, timedOut } = dragFigures(drag({ events: [paint(1010, 1, 2)] }));
  assert.equal(figures.settleMs, 60000);
  assert.deepEqual(timedOut, ['settleMs']);
});

test('a drag with no frame at all has no lag', () => {
  const { figures, timedOut } = dragFigures(drag({ events: [] }));
  assert.equal(figures.lagMedianMs, 60000);
  assert.deepEqual(timedOut, ['settleMs', 'lagMs']);
});

test('a jump times the first frame of the timestep at any level and the one at the target level, from the press', () => {
  const raw = { events: [paint(100, 3, 2), paint(150, 12, 3, 'fallback'), paint(400, 12, 2), paint(700, 12, 1), paint(900, 13, 1)], at: 120, to: 12, normalLod: 1, capMs: 60000 };
  const { figures, detail } = jumpFigures(raw);
  assert.equal(figures.firstFrameMs, 30);
  assert.equal(figures.targetFrameMs, 580);
  assert.equal(detail.firstFrameLod, 3);
});

test('a jump that never reaches the target level reports the wait', () => {
  const { figures, timedOut } = jumpFigures({ events: [paint(150, 12, 3, 'fallback')], at: 120, to: 12, normalLod: 1, capMs: 60000 });
  assert.equal(figures.firstFrameMs, 30);
  assert.equal(figures.targetFrameMs, 60000);
  assert.deepEqual(timedOut, ['targetFrameMs']);
});

test('playback reports the steps, the rate between the first and last step, the wait for the first and the pauses', () => {
  const stats = { steps: 21, bufferingPauses: 2, bufferingMs: 1500, longestBufferingMs: 900, initialBufferMs: 2000, firstStepAt: 3000, lastStepAt: 8000 };
  const { figures, detail } = playFigures({ stats, startAt: 1000, windowMs: 30000, normalLod: 2, movieLod: 3 });
  assert.deepEqual(figures, { steps: 21, stepsPerSecond: 4, firstStepMs: 2000, pauses: 2, pausedMs: 1500 });
  assert.equal(detail.movieLod, 3);
});

test('playback that never took a step reports a rate of 0 and the window as its wait', () => {
  const stats = { steps: 0, bufferingPauses: 0, bufferingMs: 0, longestBufferingMs: 0, initialBufferMs: null, firstStepAt: null, lastStepAt: null };
  const { figures, timedOut } = playFigures({ stats, startAt: 1000, windowMs: 30000, normalLod: 2, movieLod: 2 });
  assert.equal(figures.stepsPerSecond, 0);
  assert.equal(figures.firstStepMs, 30000);
  assert.deepEqual(timedOut, ['firstStepMs']);
});

test('every scenario adds the requests and the megabytes the network log counted', () => {
  const raw = { events: [paint(150, 12, 1)], at: 120, to: 12, normalLod: 1, capMs: 60000 };
  const { figures, detail } = runFigures('jump', raw, { requests: 14, bytes: 7_350_000, failed: 2 });
  assert.equal(figures.requests, 14);
  assert.equal(figures.wireMB, 7.4);
  assert.equal(detail.failedRequests, 2);
});

/** Pairs of runs of one scenario on one link: base figures, head figures a function of them and of the pair. */
function runs(pairs, { scenario = 'drag150', link = '8mbit-80ms', head = {}, base = {} } = {}) {
  const kind = SCENARIOS[scenario].kind;
  const baseFigures = Object.fromEntries(LINK_METRICS[kind].map((metric) => [metric.key, 1000]));
  const records = [];
  for (let pair = 0; pair < pairs; pair++) {
    const drift = 1 + 0.02 * Math.sin(pair);
    const figures = Object.fromEntries(Object.entries({ ...baseFigures, ...base }).map(([key, value]) => [key, value * drift]));
    const headFigures = Object.fromEntries(Object.entries(figures).map(([key, value]) => [key, key in head ? head[key](value) : value]));
    records.push({ variant: 'base', pair, link, scenario, figures, timedOut: [] }, { variant: 'head', pair, link, scenario, figures: headFigures, timedOut: [] });
  }
  return records;
}

const flags = (result) => result.groups.flatMap((group) => group.rows.filter((row) => row.verdict).map((row) => `${row.key}:${row.verdict}`));

test('the same build on both sides flags nothing', () => {
  assert.deepEqual(flags(compareLinks(runs(5))), []);
});

test('a metric where lower is better is flagged worse when the head is above the tolerance and the floor, better when below', () => {
  assert.deepEqual(flags(compareLinks(runs(5, { head: { settleMs: (v) => v * 1.5 } }))), ['settleMs:worse']);
  assert.deepEqual(flags(compareLinks(runs(5, { head: { settleMs: (v) => v * 0.5 } }))), ['settleMs:better']);
});

test('a metric where higher is better is flagged the other way round', () => {
  assert.deepEqual(flags(compareLinks(runs(5, { head: { framesAtTarget: (v) => v * 0.5 } }))), ['framesAtTarget:worse']);
  assert.deepEqual(flags(compareLinks(runs(5, { head: { framesAtTarget: (v) => v * 1.5 } }))), ['framesAtTarget:better']);
});

test('a change inside the tolerance, or smaller than the floor in the metric\'s own unit, is not flagged', () => {
  assert.deepEqual(flags(compareLinks(runs(5, { head: { settleMs: (v) => v * 1.1 } }))), []);
  // 3 to 6 held frames is twice as many, but 3 frames is not above the floor of 2.
  assert.deepEqual(flags(compareLinks(runs(5, { base: { held: 1 }, head: { held: (v) => v * 2 } }))), [], 'a doubling from 1 held frame is under the floor of 2');
});

test('a metric without a direction is shown and never flagged', () => {
  assert.deepEqual(flags(compareLinks(runs(5, { head: { framesCoarser: (v) => v * 3 } }))), []);
});

test('a head with something where the base had nothing is flagged beyond the floor, and has no ratio', () => {
  const result = compareLinks(runs(5, { base: { held: 0 }, head: { held: () => 10 } }));
  const row = result.groups[0].rows.find((candidate) => candidate.key === 'held');
  assert.equal(row.verdict, 'worse');
  assert.equal(row.ratio, null);
  assert.match(linkSummaryMarkdown(result), /\| Held: no whole frame ready \| 0 \(0 to 0\) \| 10 \(10 to 10\) \| n\/a \| \*\*worse\*\* \|/);
});

test('zero on both sides is a ratio of 1', () => {
  const row = compareLinks(runs(5, { base: { held: 0 }, head: { held: () => 0 } })).groups[0].rows.find((candidate) => candidate.key === 'held');
  assert.equal(row.ratio, 1);
});

test('fewer than three pairs shows the figures and flags nothing', () => {
  const result = compareLinks(runs(2, { head: { settleMs: (v) => v * 3 } }));
  assert.deepEqual(flags(result), []);
  assert.equal(result.groups[0].decided, false);
  assert.equal(result.groups[0].pairs, 2);
});

test('scenarios and links are compared on their own', () => {
  const records = [...runs(5, { scenario: 'jump', link: '50mbit-40ms' }), ...runs(5, { scenario: 'play4', link: '8mbit-80ms', head: { pausedMs: (v) => v * 2 } })];
  const result = compareLinks(records);
  assert.deepEqual(result.groups.map((group) => `${group.link}/${group.scenario}`), ['50mbit-40ms/jump', '8mbit-80ms/play4']);
  assert.deepEqual(result.flagged, [{ link: '8mbit-80ms', scenario: 'play4', key: 'pausedMs' }]);
});

test('a figure that is missing or not a number stops the comparison, naming the metric, the build, the pair and the run', () => {
  for (const bad of [undefined, Number.NaN, -1]) {
    const records = runs(3).map((record) => (record.variant === 'head' && record.pair === 1 ? { ...record, figures: { ...record.figures, lagP95Ms: bad } } : record));
    assert.throws(() => compareLinks(records), /lagP95Ms of the head run of pair 2 \(8mbit-80ms, drag150\)/, String(bad));
  }
});

test('the summary has a table per link and scenario with both builds and the flags, and counts the figures that hit a wait', () => {
  const records = runs(5, { head: { settleMs: (v) => v * 2 } }).map((record, i) => (i === 0 ? { ...record, timedOut: ['settleMs'] } : record));
  const text = linkSummaryMarkdown(compareLinks(records), { environment: ['Chromium 153, Metal'], records });
  assert.match(text, /### 8 Mbit\/s, 80 ms/);
  assert.match(text, new RegExp(SCENARIOS.drag150.label));
  assert.match(text, /\*\*worse\*\*/);
  assert.match(text, /1 metric is worse in the head/);
  assert.match(text, /1 figure is the wait of the scenario, not a measurement \(1 of the base, 0 of the head\)/);
  assert.match(text, /Chromium 153, Metal/);
});

test('the links are the two the benchmark states', () => {
  assert.deepEqual(Object.values(LINKS).map(({ mbps, latencyMs }) => [mbps, latencyMs]), [[50, 40], [8, 80]]);
});
