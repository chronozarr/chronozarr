import assert from 'node:assert/strict';
import { test } from 'node:test';
import { blockAround, placedRight } from './display.mjs';

/** A 13 x 13 patch whose columns [from, to) and rows [rowFrom, rowTo) hold `block`, everything else `other`. */
function patch({ from, to, rowFrom, rowTo, centre, origin = [100, 200], block = [43, 54, 28], other = [10, 10, 10] }) {
  const size = 13;
  const rgb = [];
  for (let i = 0; i < size; i++) for (let j = 0; j < size; j++) rgb.push(...(j >= from && j < to && i >= rowFrom && i < rowTo ? block : other));
  return { centre, origin, size, rgb };
}

test('a block centred on the position has zero offset and its own size', () => {
  const block = blockAround(patch({ from: 5, to: 8, rowFrom: 5, rowTo: 8, centre: [106.5, 206.5] }));
  assert.deepEqual([block.widthPx, block.heightPx], [3, 3]);
  assert.equal(block.offsetX, 0);
  assert.equal(block.offsetY, 0);
  assert.deepEqual(block.colour, [43, 54, 28]);
  assert.equal(placedRight(block, 2.9), true);
});

test('a position on a block edge reports the block that holds its pixel, with a sub-pixel offset', () => {
  // the centre is at device x 106.76 (pixel 106, column 6) just before the block's right edge: the block is columns 5-6, 2 px wide
  const block = blockAround(patch({ from: 5, to: 7, rowFrom: 5, rowTo: 8, centre: [106.76, 206.5] }));
  assert.equal(block.widthPx, 2);
  assert.ok(Math.abs(block.offsetX + 0.76) < 1e-9);
});

test('a block one level-0 pixel to the side is not where the pixel should be', () => {
  // centre at device x 106.5 (column 6) but the block with the pixel colour is columns 9-11: the centre pixel is background
  const wrong = blockAround(patch({ from: 9, to: 12, rowFrom: 5, rowTo: 8, centre: [106.5, 206.5] }));
  assert.deepEqual(wrong.colour, [10, 10, 10]);
  assert.equal(wrong.truncated, true, 'the background run reaches the patch edge');
  assert.equal(placedRight(wrong, 2.9), false);
  // a block that holds the centre pixel at its left end (columns 5-8, middle at 107.0) against a centre at 105.0: offset 2.0
  const shifted = blockAround(patch({ from: 5, to: 9, rowFrom: 5, rowTo: 8, centre: [105.0, 206.5] }));
  assert.equal(shifted.offsetX, 2);
  assert.equal(placedRight(shifted, 2.9), false);
});
