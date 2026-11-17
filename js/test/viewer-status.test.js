import { test } from 'node:test';
import assert from 'node:assert/strict';
import { frameOutcome } from '../demo/status.js';

test('with no store open the outcome is none, whatever else the render says', () => {
  assert.equal(frameOutcome(false, false, false, 0, false), 'none');
  assert.equal(frameOutcome(false, true, true, 0, false), 'none');
  assert.equal(frameOutcome(false, false, false, 4, true), 'none');
});

test('the frame asked for, on the canvas at the level the view settles on, is settled', () => {
  assert.equal(frameOutcome(true, true, true, 0, false), 'settled');
});

test('the frame asked for, on the canvas at a fast scrub coarser level, is a preview', () => {
  assert.equal(frameOutcome(true, true, false, 0, false), 'preview');
});

test('a frame on the canvas wins over missing cells: a scrub that shows a finer level than it draws at is not loading', () => {
  assert.equal(frameOutcome(true, true, true, 2, false), 'settled');
  assert.equal(frameOutcome(true, true, false, 2, true), 'preview');
});

test('the frame asked for not on the canvas while cells are on their way is loading', () => {
  assert.equal(frameOutcome(true, false, false, 4, false), 'loading');
});

test('a frame whose cells are all in memory but that could not be drawn yet is loading, not unavailable', () => {
  assert.equal(frameOutcome(true, false, false, 0, false), 'loading');
  assert.equal(frameOutcome(true, false, false, 0, true), 'loading', 'no cell is missing, so none can have failed');
});

test('missing cells of which only some have failed for good leave the frame loading', () => {
  assert.equal(frameOutcome(true, false, false, 3, false), 'loading');
});

test('missing cells that have all failed for good make the frame unavailable', () => {
  assert.equal(frameOutcome(true, false, false, 3, true), 'unavailable');
  assert.equal(frameOutcome(true, false, true, 1, true), 'unavailable');
});
