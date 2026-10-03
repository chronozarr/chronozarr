import { test } from 'node:test';
import assert from 'node:assert/strict';
import { decodeView, encodeView, pixelToProjected, projectedToPixel } from '../demo/permalink.js';

const NORTH_UP = [10, 0, 746090, 0, -10, 2540440];
const ROTATED = [8.66, 5, 500000, 5, -8.66, 4100000];
const STORE = { count: 117, productIds: ['true_color', 'ndvi', 'band'], bands: ['B02', 'B03', 'B04', 'B08'], transform: NORTH_UP };

test('affine transform round trip, including a rotated grid', () => {
  for (const transform of [NORTH_UP, ROTATED]) {
    for (const [col, row] of [[0, 0], [1234.5, 987.25], [2764, 2758]]) {
      const { x, y } = pixelToProjected(transform, col, row);
      const back = projectedToPixel(transform, x, y);
      assert.ok(Math.abs(back.col - col) < 1e-6 && Math.abs(back.row - row) < 1e-6, `${transform} ${col},${row}`);
    }
  }
  assert.deepEqual(pixelToProjected(NORTH_UP, 10, 20), { x: 746190, y: 2540240 });
});

test('view round trip with projected coordinates', () => {
  const view = { t: 71, productId: 'ndvi', zoom: 2.5, center: { col: 1234.5, row: 987.25 } };
  const query = encodeView(view, { transform: NORTH_UP });
  assert.equal(query, 't=71&p=ndvi&z=2.5&c=758435,2530568', 'short form, comma kept readable');
  const back = decodeView(query, STORE);
  assert.equal(back.t, 71);
  assert.equal(back.productId, 'ndvi');
  assert.equal(back.zoom, 2.5);
  assert.ok(Math.abs(back.center.col - 1234.5) <= 0.051 && Math.abs(back.center.row - 987.25) <= 0.051, 'within the 1 m rounding of the projected center');
});

test('view round trip in level-0 pixels when the store has no transform', () => {
  const query = encodeView({ t: 0, zoom: 0.268123, center: { col: 1100.04, row: 1400.96 } });
  assert.equal(query, 't=0&z=0.268&c=1100,1401');
  const back = decodeView(query, { ...STORE, transform: null });
  assert.deepEqual(back, { t: 0, zoom: 0.268, center: { col: 1100, row: 1401 } });
});

test('single-band product carries its band name', () => {
  const query = encodeView({ productId: 'band', bandName: 'B04' });
  assert.equal(query, 'p=band&b=B04');
  assert.deepEqual(decodeView(query, STORE), { productId: 'band', bandName: 'B04' });
});

test('an untouched view has an empty query, and an empty query decodes to no view', () => {
  assert.equal(encodeView({}), '');
  assert.deepEqual(decodeView('', STORE), {});
});

test('sub-unit pixels keep millimetre precision in projected coordinates', () => {
  const transform = [0.5, 0, 1000, 0, -0.5, 2000];
  const query = encodeView({ center: { col: 10.3, row: 20.7 } }, { transform });
  assert.equal(query, 'c=1005.15,1989.65');
  const back = decodeView(query, { ...STORE, transform });
  assert.ok(Math.abs(back.center.col - 10.3) < 0.01 && Math.abs(back.center.row - 20.7) < 0.01);
});

test('invalid or out-of-range values are ignored, valid ones kept', () => {
  assert.deepEqual(decodeView('t=abc&p=nope&z=-2&c=12&b=B99', STORE), {});
  assert.deepEqual(decodeView('t=117', STORE), {}, 'one past the last timestep');
  assert.deepEqual(decodeView('t=116', STORE), { t: 116 });
  assert.deepEqual(decodeView('t=-3', STORE), {});
  assert.deepEqual(decodeView('t=1.5', STORE), {});
  assert.deepEqual(decodeView('z=0', STORE), {});
  assert.deepEqual(decodeView('c=1,', STORE), {});
  assert.deepEqual(decodeView('c=1,2,3', STORE), {});
  assert.deepEqual(decodeView('c=a,b', STORE), {});
  assert.deepEqual(decodeView('p=ndwi', STORE), {}, 'a product the store cannot draw');
  assert.equal(decodeView('t=5&p=nope&z=3', STORE).t, 5);
  assert.equal(decodeView('t=5&p=nope&z=3', STORE).zoom, 3);
});

test('a leading ? and extra parameters (store=) are tolerated', () => {
  assert.deepEqual(decodeView('?store=https%3A%2F%2Fx%2Fs&t=4&p=ndvi', STORE), { t: 4, productId: 'ndvi' });
});
