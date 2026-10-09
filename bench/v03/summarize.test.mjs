import assert from 'node:assert/strict';
import { test } from 'node:test';
import { derive, summarize } from './summarize.mjs';

const net = (requests, bytes) => ({ requests, bytes, aborted: 0, byKind: {} });
const phase = (stepMs, requests, bytes) => ({ steps: stepMs.map((ms) => ({ ms, firstWholeMs: ms, level: 1 })), net: net(requests, bytes), origin: { requests: 0, rangeBytes: 0 } });

/** A session record in the shape session.mjs writes, with 30 steps of `stepMs`. */
function record({ system = 'A', round = 1, openMs = 100, stepMs = 5, jumpMs = 50, level = 1, tile = null }) {
  return {
    round,
    system,
    profile: 'natural',
    view: 'overview',
    load: { loadavg1: 3.5, ncpu: 16 },
    raf: { preMedianMs: 16.7, postMedianMs: 16.7 },
    open: { ms: openMs, level: tile ? undefined : level, tileZoom: tile?.z, groundMetresPerPixel: tile?.res, cells: 9, net: net(12, 9e6) },
    step: phase(Array.from({ length: 30 }, () => stepMs), 30, 30e6),
    revisit: phase(Array.from({ length: 30 }, () => 1), 0, 0),
    jump: phase([jumpMs], 9, 9e6),
    idle: { net: net(5, 5e6) },
    session: { net: net(100, 120e6), origin: { requests: 40, rangeBytes: 50e6 } },
  };
}

test('derive reads the pixel size from the store level, or from the tile zoom of TiTiler', () => {
  assert.equal(derive(record({ level: 1 })).resolutionM, 20);
  assert.equal(derive(record({ system: 'C', tile: { z: 14, res: 4.7 } })).resolutionM, 4.7);
});

test('derive takes the median and p95 of the steps and counts steps that never completed', () => {
  const steps = [...Array.from({ length: 27 }, () => 10), 90, 90, null];
  const run = record({});
  run.step.steps = steps.map((ms) => ({ ms, level: 1 }));
  const d = derive(run);
  assert.equal(d.stepMedianMs, 10);
  assert.equal(d.stepP95Ms, 90);
  assert.equal(d.stepNeverCompleted, 1);
  assert.equal(d.stepBytesPerStep, 1e6);
});

test('the summary has one row per system and a cell per profile and view, with the spread of the rounds', () => {
  const runs = [record({ round: 1, openMs: 100 }), record({ round: 2, openMs: 300 }), record({ system: 'B', round: 1, openMs: 50 })];
  const out = summarize({ profiles: ['natural'], views: ['overview'], systems: ['A', 'B', 'C'], runs, discards: [{ profile: 'natural', view: 'overview', system: 'A', round: 3, reason: 'animation-frame median 33.3/33.3 ms' }] });
  assert.match(out.markdown, /PROVISIONAL/);
  assert.match(out.markdown, /A chronozarr viewer \(n=2\)/);
  assert.match(out.markdown, /200 ms \(100 ms-300 ms\)/);
  assert.match(out.markdown, /B zarr-layer \(n=1\)/);
  assert.doesNotMatch(out.markdown, /C COG/);
  assert.match(out.markdown, /Discarded attempts: natural overview A round 3/);
  assert.equal(out.json.length, 2);
  assert.equal(out.json[0].pooled.stepMs.n, 60);
});
