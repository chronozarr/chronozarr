import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSeries, chartRange, gapFilledTimes, seriesPath, seriesSpecs, timeFromX, validAt, windowPixels, xFromTime } from '../demo/chart.js';
import { normalizeBands, resolveProducts } from '../demo/products.js';

const BANDS = normalizeBands(['B02', 'B03', 'B04', 'B08']);
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
  const bands = normalizeBands(['B08', 'B04']);
  const specs = seriesSpecs(product('band', bands), bands, 1);
  const [series] = buildSeries(specs, [reading(Uint16Array.of(3000, 1200))], 0);
  assert.equal(series.id, 'B04');
  assert.equal(series.label, 'B04 reflectance');
  assert.equal(series.values[0], 0.12);
});

test('charts use each band\'s scale and offset, call float nodata a gap, and label by name and role', () => {
  const bands = normalizeBands([
    { name: 'r', common_name: 'red', scale: 0.5, offset: 10 },
    { name: 'green', common_name: 'green' },
    { name: 'B', common_name: 'blue', scale: 2 },
  ]);
  const series = buildSeries(seriesSpecs(product('true_color', bands), bands, 0), [reading(Float32Array.of(4, 5, 6)), reading(Float32Array.of(NaN, 5, -1))], -1);
  assert.deepEqual(series.map((s) => s.label), ['r red', 'green', 'B blue'], 'a name equal to the role is not repeated');
  assert.deepEqual(series.map((s) => s.values[0]), [12, 5, 12]);
  assert.deepEqual(series.map((s) => s.values[1]), [null, 5, null], 'NaN and the nodata value are gaps');

  const depth = normalizeBands([{ name: 'depth', units: 'm' }]);
  const [single] = buildSeries(seriesSpecs(product('band', depth), depth, 0), [reading(Float32Array.of(2.5))], null);
  assert.equal(single.label, 'depth (m)');
  assert.equal(single.values[0], 2.5);
});

test('an index is a gap when either of its bands is at nodata', () => {
  const [series] = buildSeries(seriesSpecs(product('ndvi'), BANDS, 0), [reading(pixel(0, 0, 0, 3000)), reading(pixel(0, 0, 1000, 0))], 0);
  assert.deepEqual(series.values, [null, null]);
});

test('gapFilledTimes: timesteps with coverage 0 that have a value; none without coverage', () => {
  const values = [0.2, 0.3, null, 0.4, undefined, 0.5];
  assert.deepEqual(gapFilledTimes(values, [5, 0, 0, 3, 0, undefined]), [1], 'not the gap without a value (null), not the unloaded (undefined), not unknown coverage');
  assert.deepEqual(gapFilledTimes(values, null), []);
  assert.deepEqual(gapFilledTimes([1, 2], [0, 0]), [0, 1]);
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

test('a masked-out timestep is a gap in the chart, even when the pixel holds a plausible value there', () => {
  const specs = seriesSpecs(product('true_color'), BANDS, 0);
  const readings = [
    { pixels: [pixel(500, 800, 1000, 3000)], valid: [true] },
    { pixels: [pixel(500, 800, 1000, 3000)], valid: [false] },
    undefined,
    { pixels: [pixel(600, 900, 1100, 3100)], valid: [true] },
  ];
  const series = buildSeries(specs, readings, null);
  assert.deepEqual(series.map((s) => s.values), [[0.1, null, undefined, 0.11], [0.08, null, undefined, 0.09], [0.05, null, undefined, 0.06]]);
});

test('with a mask the nodata value is not compared: a stored 0 is a value where the mask says valid', () => {
  const [series] = buildSeries(seriesSpecs(product('band', normalizeBands(['B04'])), normalizeBands(['B04']), 0), [{ pixels: [Uint16Array.of(0)], valid: [true] }, { pixels: [Uint16Array.of(0)] }], null);
  assert.deepEqual(series.values, [0, 0]);
  const [compared] = buildSeries(seriesSpecs(product('band', normalizeBands(['B04'])), normalizeBands(['B04']), 0), [{ pixels: [Uint16Array.of(0)] }], 0);
  assert.deepEqual(compared.values, [null], 'a store without a mask still reads its nodata value as a gap');
});

test('a series on the clicked pixel does not borrow a valid neighbour when that pixel is masked out', () => {
  const window3 = [pixel(0, 0, 1000, 3000), pixel(0, 0, 2000, 3000), pixel(0, 0, 3000, 1000)];
  const [series] = buildSeries(seriesSpecs(product('ndvi'), BANDS, 0), [{ pixels: window3, valid: [false, true, true] }, { pixels: window3, valid: [true, false, false] }], null);
  assert.equal(series.values[0], null, 'the clicked pixel (first) is masked: a gap, not the neighbours');
  assert.ok(Math.abs(series.values[1] - 0.5) < 1e-12, 'the clicked pixel valid: its own value');
});

test('water fraction counts only the unmasked pixels of the window, and is a gap when all are masked', () => {
  const water = pixel(0, 1100, 700, 250);
  const land = pixel(0, 900, 900, 3000);
  const specs = seriesSpecs(product('water'), BANDS, 0);
  const window4 = [water, water, land, land];
  const [series] = buildSeries(specs, [{ pixels: window4, valid: [true, false, true, true] }, { pixels: window4, valid: [false, false, false, false] }, { pixels: window4, valid: [false, false, true, true] }], null);
  assert.ok(Math.abs(series.values[0] - 1 / 3) < 1e-12, '1 water of 3 unmasked pixels');
  assert.equal(series.values[1], null, 'every pixel of the window masked out');
  assert.equal(series.values[2], 0, 'the masked water pixels do not count');
});

test('validAt reads the mask at each window pixel with the chunk width as row stride', () => {
  const chunkWidth = 8;
  const mask = new Uint8Array(chunkWidth * 4).fill(1);
  mask[1 * chunkWidth + 3] = 0;
  assert.deepEqual(validAt(mask, chunkWidth, [[3, 1], [2, 1], [3, 2], [4, 1]]), [false, true, true, true]);
});
