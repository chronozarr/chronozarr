import { test } from 'node:test';
import assert from 'node:assert/strict';
import { COLORMAP_STOPS, COLORMAPS, colormapColor, colormapGradient, colormapIndex } from '../shared/colormaps.js';
import { PRODUCT_GLSL } from '../shared/products-glsl.js';

test('every colormap but gray has COLORMAP_STOPS r, g, b triples within 0..1', () => {
  assert.equal(COLORMAPS[0].id, 'gray');
  assert.equal(COLORMAPS[0].stops, null);
  for (const { id, stops } of COLORMAPS.slice(1)) {
    assert.equal(stops.length, 3 * COLORMAP_STOPS, id);
    assert.ok(stops.every((c) => c >= 0 && c <= 1), id);
  }
});

test('colormapIndex finds ids and returns -1 for anything else', () => {
  assert.equal(colormapIndex('gray'), 0);
  assert.equal(colormapIndex('viridis'), 1);
  for (const unknown of ['Viridis', 'jet', '', null, undefined]) assert.equal(colormapIndex(unknown), -1, String(unknown));
});

test('colormapColor: gray is the value itself; a colormap hits its stops exactly and interpolates between them', () => {
  assert.deepEqual(colormapColor(0, 0.25), [0.25, 0.25, 0.25]);
  const viridis = COLORMAPS[1].stops;
  assert.deepEqual(colormapColor(1, 0), viridis.slice(0, 3));
  assert.deepEqual(colormapColor(1, 1), viridis.slice(-3));
  assert.deepEqual(colormapColor(1, 1 / (COLORMAP_STOPS - 1)), viridis.slice(3, 6));
  const between = colormapColor(1, 0.5 / (COLORMAP_STOPS - 1));
  between.forEach((c, i) => assert.ok(Math.abs(c - (viridis[i] + viridis[3 + i]) / 2) < 1e-12, `channel ${i}`));
});

test('colormapColor clamps values outside 0..1 to the ends', () => {
  assert.deepEqual(colormapColor(2, -3), colormapColor(2, 0));
  assert.deepEqual(colormapColor(2, 7), colormapColor(2, 1));
  assert.deepEqual(colormapColor(0, -1), [0, 0, 0]);
});

test('colormapGradient runs from the first stop to the last, left to right', () => {
  assert.equal(colormapGradient(0), 'linear-gradient(to right, rgb(0, 0, 0), rgb(255, 255, 255))');
  const viridis = colormapGradient(1);
  assert.ok(viridis.startsWith('linear-gradient(to right, rgb(68, 1, 84), '), viridis);
  assert.ok(viridis.endsWith(', rgb(253, 231, 37))'), viridis);
  assert.equal(viridis.match(/rgb\(/g).length, COLORMAP_STOPS);
});

test('the shader table holds the same stops as COLORMAPS, in order, gray left out', () => {
  const table = PRODUCT_GLSL.match(/const vec3 COLORMAP_TABLE\[(\d+)\] = vec3\[\d+\]\(([\s\S]*?)\);/);
  assert.ok(table, 'COLORMAP_TABLE in PRODUCT_GLSL');
  const values = [...table[2].matchAll(/-?\d+\.\d+/g)].map((m) => Number(m[0]));
  const expected = COLORMAPS.flatMap((colormap) => colormap.stops ?? []);
  assert.equal(Number(table[1]), expected.length / 3);
  assert.deepEqual(values, expected);
  assert.match(PRODUCT_GLSL, new RegExp(`const int COLORMAP_STOPS = ${COLORMAP_STOPS};`));
  assert.match(PRODUCT_GLSL, /if \(product != 5 \|\| colormap <= 0\) return color;/);
});
