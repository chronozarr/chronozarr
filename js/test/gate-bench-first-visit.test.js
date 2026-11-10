// The marks of a first visit (support/gate-bench/visit-marks.js): what the page recorded and CDP logged, turned into milliseconds.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { visitMarks } from '../support/gate-bench/visit-marks.js';

const STORE = 'http://127.0.0.1:5001';
const request = (url, startedAt, endedAt, type = 'Fetch') => ({ url, type, startedAt, endedAt, bytes: 0, failed: false });

// CDP's times are monotonic seconds: the navigation starts at 10 s.
const REQUESTS = [
  request('http://127.0.0.1:5000/demo/index.html?store=x', 10, 10.01, 'Document'),
  request('http://127.0.0.1:5000/demo/viewer.js', 10.01, 10.05, 'Script'),
  request(`${STORE}/zarr.json`, 10.06, 10.09),
  request('http://127.0.0.1:5000/demo/codecs/zstd.js', 10.05, 10.2, 'Script'),
  request('http://127.0.0.1:5000/demo/codecs/zstd.js', 10.06, 10.08, 'Script'),
  request(`${STORE}/2/data/c/0/0/1/1`, 10.3, 10.4),
  request(`${STORE}/2/data/c/0/0/0/0`, 10.25, 10.4),
];
const PAGE = {
  shaderMs: 4.26,
  programs: 2,
  workers: [
    { spawnedAt: 70, firstMessageAt: 130 },
    { spawnedAt: 71, firstMessageAt: 110 },
    { spawnedAt: 72, firstMessageAt: 192 },
  ],
  firstFrameMs: 412.34,
  targetFrameMs: 430.05,
};

test('the marks are milliseconds from the start of the navigation, the work of the shaders, the workers and the codec', () => {
  assert.deepEqual(visitMarks(PAGE, REQUESTS, STORE), {
    firstRequestMs: 250,
    shaderMs: 4.3,
    programs: 2,
    workerReadyMs: 60,
    workers: 3,
    codecMs: 30,
    firstFrameMs: 412.3,
    targetFrameMs: 430.1,
  });
});

test('a mark that its source never produced is null, not 0', () => {
  const marks = visitMarks({ shaderMs: 0, programs: 0, workers: [{ spawnedAt: 70, firstMessageAt: null }], firstFrameMs: null, targetFrameMs: null }, REQUESTS.slice(0, 3), STORE);
  assert.deepEqual(marks, { firstRequestMs: null, shaderMs: null, programs: 0, workerReadyMs: null, workers: 1, codecMs: null, firstFrameMs: null, targetFrameMs: null });
});

test('a worker that has not spoken is left out of the median of those that have', () => {
  const marks = visitMarks({ ...PAGE, workers: [...PAGE.workers, { spawnedAt: 73, firstMessageAt: null }] }, REQUESTS, STORE);
  assert.equal(marks.workerReadyMs, 60);
  assert.equal(marks.workers, 4);
});

test('only requests to the store count as chunks, and the first chunk is the earliest by start', () => {
  const lateChunk = [...REQUESTS.filter((r) => !r.url.includes('/c/')), request(`${STORE}/2/data/c/3/0/0/0`, 10.5, 10.6), request('http://127.0.0.1:5000/c/1/other', 10.1, 10.2)];
  assert.equal(visitMarks(PAGE, lateChunk, STORE).firstRequestMs, 500);
});
