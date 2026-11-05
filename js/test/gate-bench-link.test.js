// The figures and the comparison of the link A/B (support/gate-bench/link-compare.js): what a run's raw result turns into,
// and which differences between the base and the head are flagged.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LINKS, LINK_METRICS, SCENARIOS, compareLinks, distinctFrames, dragFigures, heldMs, jumpFigures, linkSummaryMarkdown, playFigures, runFigures } from '../support/gate-bench/link-compare.js';

const paint = (at, t, lod, kind = 'target') => ({ type: 'paint', at, t, lod, kind });

/**
 * A drag as a slow link makes the viewer emit it: the viewer asks for timestep t every 150 ms from 1000, draws the first four at
 * level 3 (the store rests at level 2), then has nothing whole for the next ones and draws the frame on screen again at every
 * render (kind 'previous', still timestep 4; 'kept' never fires, as nothing is wrong with that frame), paints the frame of
 * timestep 4 once more when a cell arrives, and shows timestep 8 at level 3 just after the pointer is released at 2200 and at level
 * 2 at 3400. The main thread is blocked from 1750 to 1950, so the timer of step 5 runs 200 ms late and the next one at once. The
 * shape is that of a dump of one such drag at 8 Mbit/s.
 */
function slowDrag(overrides = {}) {
  const events = [
    paint(1155, 1, 3, 'fallback'),
    paint(1306, 2, 3),
    paint(1455, 3, 3),
    paint(1605, 4, 3),
    paint(1650, 4, 3),
    paint(1755, 4, 3, 'previous'),
    paint(1955, 4, 3, 'previous'),
    paint(2055, 4, 3, 'previous'),
    paint(2205, 4, 3, 'previous'),
    paint(2260, 8, 3, 'fallback'),
    paint(3400, 8, 2),
  ];
  const dispatched = { 1: 1150, 2: 1300, 3: 1450, 4: 1600, 5: 1950, 6: 1950, 7: 2050, 8: 2200 };
  const inputs = Object.entries(dispatched).map(([t, at]) => ({ t: Number(t), due: 1000 + Number(t) * 150, at }));
  return {
    events,
    inputs,
    samples: samples({ inputs, events, from: 1000, to: 2200 }),
    beginAt: 1000,
    releaseAt: 2200,
    normalLod: 2,
    from: 0,
    to: 8,
    capMs: 60000,
    ...overrides,
  };
}

/** What the page samples at every 16.7 ms animation frame: the timestep asked for and the one on screen. */
function samples({ inputs, events, from, to }) {
  const frames = distinctFrames(events.filter((event) => event.type === 'paint'));
  const rows = [];
  for (let at = from; at <= to; at += 16.7) {
    rows.push({ at, t: inputs.filter((input) => input.at <= at).at(-1)?.t ?? 0, shownT: frames.filter((frame) => frame.at <= at).at(-1)?.t ?? 0 });
  }
  return rows;
}

test('a repainted frame is not a frame: the frame on screen drawn again (previous) and a repeated paint are left out', () => {
  const frames = distinctFrames(slowDrag().events);
  assert.deepEqual(frames.map((frame) => [frame.t, frame.lod]), [[1, 3], [2, 3], [3, 3], [4, 3], [8, 3], [8, 2]]);
  // A paint of kind 'previous' is never a frame, whatever it shows.
  assert.deepEqual(distinctFrames([paint(10, 4, 2), paint(20, 3, 3, 'previous'), paint(30, 5, 2)]).map((frame) => frame.t), [4, 5]);
});

test('a drag that the viewer holds on a stale frame scores no frames for the repaints, and the time it was held', () => {
  const { figures } = dragFigures(slowDrag());
  assert.equal(figures.framesAtTarget, 0, 'timestep 4 drawn again four times is not a frame at the target level');
  assert.equal(figures.framesCoarser, 4);
  assert.equal(figures.skipped, 3, 'of the timesteps 1 to 7 between the ends, 5, 6 and 7 were never shown');
  assert.ok(figures.heldMs > 150 && figures.heldMs < 300, `held from the request of timestep 5 until the release, about 14 animation frames: ${figures.heldMs}`);
  assert.equal(figures.settleMs, 1200);
});

test('a build that holds more scores worse than one that shows frames, however often it repaints', () => {
  // The same slow drag where the viewer shows a frame for every step has more frames, no hold and no stale repaints.
  const shown = [1, 2, 3, 4, 5, 6, 7].map((t) => paint(1000 + t * 150 + 5, t, 3));
  const prompt = slowDrag({ events: [...shown, paint(2260, 8, 3, 'fallback'), paint(3400, 8, 2)] });
  prompt.samples = samples({ inputs: prompt.inputs, events: prompt.events, from: 1000, to: 2200 });
  const stuck = dragFigures(slowDrag()).figures;
  const moving = dragFigures(prompt).figures;
  assert.ok(moving.framesCoarser > stuck.framesCoarser);
  assert.ok(moving.heldMs < stuck.heldMs);
  assert.ok(moving.skipped < stuck.skipped);
});

test('an interval is held when the frame on screen at its end is older than the timestep asked for at its start, the one frame that a draw takes excepted', () => {
  const one = (at, t, shownT) => ({ at, t, shownT });
  // Asked for 2 at 20, drawn by 60: the interval 20 to 40 (asked for at its start, still not drawn) is held, 0 to 20 and 40 to 60 are not.
  assert.equal(heldMs([one(0, 1, 1), one(20, 2, 1), one(40, 2, 1), one(60, 2, 2)], 0, 100), 20);
  assert.equal(heldMs([one(0, 1, 1), one(20, 2, 1), one(40, 2, 1), one(60, 2, 2)], 30, 100), 0, 'only samples inside the drag count');
  assert.equal(heldMs([one(0, 3, null), one(20, 3, null)], 0, 100), 0, 'no frame on screen yet is not an older one');
  assert.equal(heldMs([one(0, 3, 1), one(20, 3, 3)], 0, 100), 0, 'asked for at its start and drawn by its end: the one frame that a draw takes');
});

test('an interval in which the timestep asked for changes is held too: that is where a stalled main thread leaves the canvas', () => {
  const one = (at, t, shownT) => ({ at, t, shownT });
  // Asked for 2 at 20 and for 3 at 40 (a timer that ran late), still showing 1 at 40 and at 60 (when 2 is drawn): 20 to 40 and 40 to 60 are both held.
  assert.equal(heldMs([one(0, 1, 1), one(20, 2, 1), one(40, 3, 1), one(60, 3, 2)], 0, 100), 40);
  // Timestep 2 is asked for at 20, then the main thread is blocked until 400, when the late inputs for 3 and 4 have run before the
  // next animation frame: the interval 20 to 400 is where the canvas was stuck on 1 while 2 was asked for, though 4 is asked for at its end.
  assert.equal(heldMs([one(0, 1, 1), one(20, 2, 1), one(400, 4, 1), one(417, 4, 2)], 0, 500), 380 + 17);
});

test('a drag at 40 ms per step that the viewer never draws is held for the whole of it, not for the intervals between the steps', () => {
  // Steps due every 40 ms, 2.4 animation frames apart: 4 in 10 animation frames start a new timestep.
  const inputs = Array.from({ length: 10 }, (_, i) => ({ t: i + 1, due: 1000 + (i + 1) * 40, at: 1000 + (i + 1) * 40 }));
  const events = [paint(1045, 1, 3, 'fallback')];
  const stuck = dragFigures({ events, inputs, samples: samples({ inputs, events, from: 1000, to: 1400 }), beginAt: 1000, releaseAt: 1400, normalLod: 2, from: 0, to: 11, capMs: 60000 });
  // Timestep 2 is asked for at 1080: from the animation frame after it to the release is about 300 ms; counting only the
  // intervals with an unchanged timestep would give about 60 % of that.
  assert.ok(stuck.figures.heldMs > 280, `${stuck.figures.heldMs}`);
});

test('the lag of a step is the wait for the first frame that shows its timestep or a later one, from when the step was due', () => {
  const { figures } = dragFigures(slowDrag());
  // Due at 1150, 1300, 1450, 1600: answered at once. Due at 1750, 1900, 2050, 2200: all by timestep 8 at 2260, 510, 360, 210 and 60 later.
  assert.equal(figures.lagMedianMs, 33);
  assert.equal(figures.lagP95Ms, 510);
  assert.equal(figures.stepsNeverAnswered, 0);
});

test('a step whose timer ran late waits from when it was due: the blocked main thread is the viewer\'s cost', () => {
  const late = slowDrag();
  const onTime = slowDrag({ inputs: late.inputs.map((input) => ({ ...input, at: input.due })) });
  assert.ok(dragFigures(late).figures.lagP95Ms >= dragFigures(onTime).figures.lagP95Ms);
  // Step 5 was due at 1750 and dispatched at 1950: from the dispatch the wait for timestep 8 at 2260 is 310 ms, from the due time 510.
  assert.equal(dragFigures(late).figures.lagP95Ms, 510);
});

test('a drag whose last frame never comes reports the wait as the figure and says so', () => {
  const { figures, timedOut } = dragFigures(slowDrag({ events: slowDrag().events.slice(0, 5) }));
  assert.equal(figures.settleMs, 60000);
  assert.ok(timedOut.includes('settleMs'));
});

test('steps that no frame answers count as the wait of the run in the lag, and are reported beside it', () => {
  // The viewer shows timesteps 1 to 4 and then nothing: inputs 5 to 8 are never answered.
  const { figures, timedOut } = dragFigures(slowDrag({ events: slowDrag().events.slice(0, 5) }));
  assert.equal(figures.stepsNeverAnswered, 4);
  assert.equal(figures.lagP95Ms, 60000);
  assert.ok(figures.lagMedianMs > 1000, `the four answered lags are 5 ms; the median is of eight: ${figures.lagMedianMs}`);
  assert.deepEqual(timedOut, ['settleMs', 'lagMs']);
  // A build that answers them all, late, reports a lag below the wait.
  assert.ok(dragFigures(slowDrag()).figures.lagP95Ms < figures.lagP95Ms + 1);
  assert.equal(dragFigures(slowDrag()).figures.stepsNeverAnswered, 0);
  assert.deepEqual(dragFigures(slowDrag()).timedOut, []);
});

test('a drag with no frame at all has no lag', () => {
  const { figures, timedOut } = dragFigures(slowDrag({ events: [] }));
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
  const { figures, detail } = playFigures({ stats, startAt: 1000, endAt: 31000, stallOpenSince: null, windowMs: 30000, normalLod: 2, movieLod: 3 });
  assert.deepEqual(figures, { steps: 21, stepsPerSecond: 4, firstStepMs: 2000, pauses: 2, pausedMs: 1500 });
  assert.equal(detail.movieLod, 3);
});

test('a stall that is still open when the window ends is added to the time paused, which playback adds only when a pause ends', () => {
  // It ran dry at 24000 and was waiting at 31000: one pause counted, no time for it in the statistics.
  const dry = { steps: 8, bufferingPauses: 1, bufferingMs: 0, longestBufferingMs: 0, initialBufferMs: 500, firstStepAt: 1500, lastStepAt: 24000 };
  const { figures, detail } = playFigures({ stats: dry, startAt: 1000, endAt: 31000, stallOpenSince: 24000, windowMs: 30000, normalLod: 2, movieLod: 2 });
  assert.equal(figures.pauses, 1);
  assert.equal(figures.pausedMs, 7000);
  assert.equal(detail.stalledAtEndMs, 7000);
  assert.equal(detail.longestPauseMs, 7000);
  // With an earlier pause that ended, both count.
  const both = playFigures({ stats: { ...dry, bufferingPauses: 2, bufferingMs: 1500, longestBufferingMs: 1500 }, startAt: 1000, endAt: 31000, stallOpenSince: 24000, windowMs: 30000, normalLod: 2, movieLod: 2 });
  assert.equal(both.figures.pausedMs, 8500);
  assert.equal(both.detail.longestPauseMs, 7000);
  // A build that stalls for good no longer looks better than one that recovers.
  const recovered = playFigures({ stats: { ...dry, bufferingMs: 3000, longestBufferingMs: 3000, lastStepAt: 30500 }, startAt: 1000, endAt: 31000, stallOpenSince: null, windowMs: 30000, normalLod: 2, movieLod: 2 });
  assert.ok(figures.pausedMs > recovered.figures.pausedMs);
});

test('playback that never took a step reports a rate of 0 and the window as its wait', () => {
  const stats = { steps: 0, bufferingPauses: 0, bufferingMs: 0, longestBufferingMs: 0, initialBufferMs: null, firstStepAt: null, lastStepAt: null };
  const { figures, timedOut } = playFigures({ stats, startAt: 1000, endAt: 31000, stallOpenSince: null, windowMs: 30000, normalLod: 2, movieLod: 2 });
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
  // 100 to 200 ms held is twice as long, but it is not above the floor of 100 ms.
  assert.deepEqual(flags(compareLinks(runs(5, { base: { heldMs: 100 }, head: { heldMs: (v) => v * 1.9 } }))), [], 'under the floor of 100 ms');
});

test('a metric without a direction is shown and never flagged', () => {
  assert.deepEqual(flags(compareLinks(runs(5, { head: { framesCoarser: (v) => v * 3 } }))), []);
});

test('a head with something where the base had nothing is flagged beyond the floor, and has no ratio', () => {
  const result = compareLinks(runs(5, { base: { heldMs: 0 }, head: { heldMs: () => 500 } }));
  const row = result.groups[0].rows.find((candidate) => candidate.key === 'heldMs');
  assert.equal(row.verdict, 'worse');
  assert.equal(row.ratio, null);
  assert.match(linkSummaryMarkdown(result), /\| Time on an older timestep than asked for \(a coarser frame of it is not older\) \| 0\.0 ms \(0\.0 ms to 0\.0 ms\) \| 500 ms \(500 ms to 500 ms\) \| n\/a \| \*\*worse\*\* \|/);
});

test('zero on both sides is a ratio of 1', () => {
  const row = compareLinks(runs(5, { base: { heldMs: 0 }, head: { heldMs: () => 0 } })).groups[0].rows.find((candidate) => candidate.key === 'heldMs');
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
