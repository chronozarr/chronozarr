import { test } from 'node:test';
import assert from 'node:assert/strict';
import { computeStretchLo, describePixel, inputIndices, makeTimeFormatter, ndvi, ndwi, reinhardSrgb, resolveProducts } from '../tileripper/products.js';

test('products resolve against the store band list and name what is missing', () => {
  const products = resolveProducts(['B04', 'B08']);
  const byId = Object.fromEntries(products.map((p) => [p.id, p]));
  assert.equal(byId.ndvi.available, true);
  assert.equal(byId.band.available, true);
  for (const id of ['true_color', 'false_color', 'ndwi', 'water']) assert.equal(byId[id].available, false, id);
  assert.deepEqual(byId.true_color.missing, ['B03', 'B02']);
  assert.deepEqual(byId.water.missing, ['B03']);

  const full = resolveProducts(['B02', 'B03', 'B04', 'B08']);
  assert.ok(full.every((p) => p.available));
});

test('band indices follow the product input order, not the store band order', () => {
  const bands = ['B08', 'B02', 'B04', 'B03'];
  const byId = Object.fromEntries(resolveProducts(bands).map((p) => [p.id, p]));
  assert.deepEqual(inputIndices(byId.true_color, bands, 0), [2, 3, 1]);
  assert.deepEqual(inputIndices(byId.ndvi, bands, 0), [0, 2, -1]);
  assert.deepEqual(inputIndices(byId.band, bands, 3), [3, -1, -1]);
});

test('ndvi and ndwi return null when both inputs are zero', () => {
  assert.equal(ndvi(0, 0), null);
  assert.equal(ndwi(0, 0), null);
  assert.equal(ndvi(3000, 1000), 0.5);
  assert.equal(ndwi(1000, 3000), -0.5);
});

test('describePixel reports indices only when their bands exist', () => {
  const both = describePixel(Uint16Array.of(1354, 2303), ['B04', 'B08']);
  assert.equal(both.hasNdvi, true);
  assert.equal(both.hasNdwi, false);
  assert.ok(Math.abs(both.ndvi - (2303 - 1354) / (2303 + 1354)) < 1e-12);
  assert.deepEqual(both.bands.map((b) => [b.name, b.dn, b.reflectance]), [['B04', 1354, 0.1354], ['B08', 2303, 0.2303]]);

  const water = describePixel(Uint16Array.of(1100, 250), ['B03', 'B08']);
  assert.equal(water.isWater, true);
  assert.equal(water.hasNdvi, false);
});

test('reinhard tone map is monotonic and stretch ignores nodata and small samples', () => {
  assert.ok(reinhardSrgb(0.05) < reinhardSrgb(0.1));
  assert.equal(computeStretchLo([[0, 0, 0], [1200, 900, 700]]), 0, 'too few samples');
  const samples = Array.from({ length: 200 }, (_, i) => [1000 + i * 10, 800 + i * 10, 600 + i * 10]);
  samples.push([0, 0, 0]);
  const lo = computeStretchLo(samples);
  assert.ok(lo > 0 && lo < 1);
});

test('time labels: months for month-start timesteps, ISO dates otherwise', () => {
  assert.equal(makeTimeFormatter(['2024-01-01T00:00:00Z', '2024-02-01T00:00:00Z'])(1), 'Feb 2024');
  assert.equal(makeTimeFormatter(['2024-01-01T00:00:00Z', '2024-01-17T00:00:00Z'])(1), '2024-01-17');
});
