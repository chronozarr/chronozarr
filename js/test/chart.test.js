import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSeries, chartRange, seriesPath, seriesSpecs, timeFromX, windowPixels, xFromTime } from '../tileripper/chart.js';
import { resolveProducts } from '../tileripper/products.js';

const BANDS = ['B02', 'B03', 'B04', 'B08'];
const product = (id, bands = BANDS) => resolveProducts(bands).find((p) => p.id === id);
const pixel = (b02, b03, b04, b08) => Uint16Array.of(b02, b03, b04, b08);
const reading = (...pixels) => ({ pixels });

test('ndvi series: values from readings, a gap where the pixel has no data, undefined where not loaded', () => {
  const specs = seriesSpecs(product('ndvi'), BANDS, 0);
  const readings = [reading(pixel(0, 0, 1000, 3000)), reading(pixel(0, 0, 0, 0)), undefined, reading(pixel(0, 0, 3000, 1000))];
  const [series] = buildSeries(specs, readings, 0);
  assert.equal(series.label, 'NDVI');
  assert.deepEqual(series.domain, [-1, 1]);
  assert.equal(series.values.length, 4);
  assert.ok(Math.abs(series.values[0] - 0.5) < 1e-12);
  assert.equal(series.values[1], null, 'nodata is a gap, not zero');
  assert.equal(series.values[2], undefined, 'not loaded yet');
  assert.ok(Math.abs(series.values[3] + 0.5) < 1e-12);
});

test('true and false color chart three bands as reflectance, skipping nodata per band', () => {
  const readings = [reading(pixel(500, 800, 1000, 3000)), reading(pixel(0, 800, 0, 3000))];
  const true3 = buildSeries(seriesSpecs(product('true_color'), BANDS, 0), readings, 0);
  assert.deepEqual(true3.map((s) => s.id), ['B04', 'B03', 'B02']);
  assert.deepEqual(true3.map((s) => s.values[0]), [0.1, 0.08, 0.05]);
  assert.deepEqual(true3.map((s) => s.values[1]), [null, 0.08, null]);
  const false3 = buildSeries(seriesSpecs(product('false_color'), BANDS, 0), readings, 0);
  assert.deepEqual(false3.map((s) => s.id), ['B08', 'B04', 'B03']);
  assert.deepEqual(false3.map((s) => s.values[0]), [0.3, 0.1, 0.08]);
});

test('single band chart follows the chosen band, by name, whatever the band order', () => {
  const bands = ['B08', 'B04'];
  const specs = seriesSpecs(product('band', bands), bands, 1);
  const [series] = buildSeries(specs, [reading(Uint16Array.of(3000, 1200))], 0);
  assert.equal(series.id, 'B04');
  assert.equal(series.values[0], 0.12);
});

test('water fraction is the share of water pixels in the window, ignoring nodata pixels', () => {
  const water = pixel(0, 1100, 700, 250);
  const land = pixel(0, 900, 900, 3000);
  const empty = pixel(0, 0, 0, 0);
  const specs = seriesSpecs(product('water'), BANDS, 0);
  assert.equal(specs[0].window, 1);
  const [series] = buildSeries(specs, [reading(water, water, land, land, empty, empty, empty, empty, empty), reading(empty, empty), reading(water, land, land, land)], 0);
  assert.equal(series.values[0], 0.5, '2 water of 4 valid pixels');
  assert.equal(series.values[1], null, 'no valid pixel in the window');
  assert.equal(series.values[2], 0.25);
  assert.deepEqual(series.domain, [0, 1]);
});

test('windowPixels: 3x3 around the click, clicked pixel first, clipped and deduplicated at cell edges', () => {
  const inner = windowPixels(5, 5, 10, 10, 1);
  assert.equal(inner.length, 9);
  assert.deepEqual(inner[0], [5, 5]);
  const corner = windowPixels(0, 0, 10, 10, 1);
  assert.deepEqual(corner, [[0, 0], [1, 0], [0, 1], [1, 1]]);
  assert.deepEqual(windowPixels(3, 4, 10, 10, 0), [[3, 4]]);
  assert.equal(windowPixels(0, 0, 1, 1, 1).length, 1, 'a one-pixel cell');
});

test('chartRange: fixed domain for indices, data range with padding otherwise, constant series handled', () => {
  assert.deepEqual(chartRange([{ domain: [-1, 1], values: [0.2] }]), [-1, 1]);
  const [lo, hi] = chartRange([{ domain: null, values: [0.1, null, undefined, 0.3] }, { domain: null, values: [0.05, 0.2] }]);
  assert.ok(lo < 0.05 && hi > 0.3 && lo > 0 && hi < 0.4);
  const [clo, chi] = chartRange([{ domain: null, values: [0.2, 0.2] }]);
  assert.ok(clo < 0.2 && chi > 0.2);
  assert.deepEqual(chartRange([{ domain: null, values: [undefined, null] }]), [0, 1], 'nothing loaded yet');
});

test('seriesPath starts a new segment after each gap and skips unloaded timesteps', () => {
  const xOf = (t) => t * 10;
  const yOf = (v) => 100 - v * 100;
  assert.equal(seriesPath([0.5, 0.25, null, 0.75], xOf, yOf), 'M0.0,50.0L10.0,75.0M30.0,25.0');
  assert.equal(seriesPath([undefined, 0.5, undefined, 1], xOf, yOf), 'M10.0,50.0L30.0,0.0', 'unloaded timesteps do not break the line');
  assert.equal(seriesPath([null, undefined], xOf, yOf), '');
});

test('xFromTime and timeFromX are inverses and clamp to the axis', () => {
  for (const t of [0, 1, 57, 116]) assert.equal(timeFromX(xFromTime(t, 117, 30, 270), 117, 30, 270), t);
  assert.equal(timeFromX(-50, 117, 30, 270), 0);
  assert.equal(timeFromX(900, 117, 30, 270), 116);
  assert.equal(timeFromX(150, 1, 30, 270), 0);
  assert.equal(xFromTime(0, 1, 30, 270), 150);
});
