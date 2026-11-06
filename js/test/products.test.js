import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  computeStretchLo,
  describePixel,
  displayMode,
  findBand,
  inputConversion,
  inputIndices,
  makeTimeFormatter,
  ndvi,
  ndwi,
  normalizeBands,
  percentileRange,
  reinhardSrgb,
  resolveProducts,
  toPhysical,
} from '../shared/products.js';

const byId = (bands) => Object.fromEntries(resolveProducts(bands).map((p) => [p.id, p]));

test('products resolve against the store bands by common name and report the common names that are missing', () => {
  const products = byId(['B04', 'B08']);
  assert.equal(products.ndvi.available, true);
  assert.equal(products.band.available, true);
  for (const id of ['true_color', 'false_color', 'ndwi', 'water']) assert.equal(products[id].available, false, id);
  assert.deepEqual(products.true_color.missing, ['green', 'blue']);
  assert.deepEqual(products.water.missing, ['green']);

  assert.deepEqual(resolveProducts(['B02', 'B03', 'B04', 'B08']).filter((p) => !p.available).map((p) => p.id), ['dual_pol'], 'every product but the SAR one');
  assert.deepEqual(products.dual_pol.missing, ['copol', 'crosspol']);
});

test('a band answers to its common_name first, then to a name that is the common name, then to its Sentinel-2 name', () => {
  const bands = normalizeBands([
    { name: 'band_1', common_name: 'red', scale: 1 },
    { name: 'band_2', common_name: 'green', scale: 1 },
    { name: 'blue', scale: 1 },
    { name: 'B08', scale: 1 },
    { name: 'B04', scale: 1 },
  ]);
  assert.equal(findBand(bands, 'red'), 0, 'common_name beats the Sentinel-2 name B04 that comes later');
  assert.equal(findBand(bands, 'green'), 1);
  assert.equal(findBand(bands, 'blue'), 2, 'a band named like the common name');
  assert.equal(findBand(bands, 'nir'), 3, 'B08 is nir when nothing says otherwise');
  assert.equal(findBand(bands, 'swir16'), -1);
  assert.equal(findBand(normalizeBands([{ name: 'B04', common_name: 'nir', scale: 1 }]), 'red'), -1, 'a declared common_name is believed over the name');
});

test('band indices follow the product input order, not the store band order', () => {
  const bands = ['B08', 'B02', 'B04', 'B03'];
  const products = byId(bands);
  assert.deepEqual(inputIndices(products.true_color, 0), [2, 3, 1]);
  assert.deepEqual(inputIndices(products.ndvi, 0), [0, 2, -1]);
  assert.deepEqual(inputIndices(products.band, 3), [3, -1, -1]);
});

test('bands given as names are Sentinel-2 reflectance; objects carry their own scale and offset', () => {
  const [legacy] = normalizeBands(['B04']);
  assert.deepEqual([legacy.scale, legacy.offset, legacy.divisor], [1e-4, 0, 10000]);
  assert.equal(toPhysical(1354, legacy), 0.1354, 'divides by 10000 exactly as the shader does');

  const [plain, scaled, odd] = normalizeBands([{ name: 'rgb' }, { name: 'dem', scale: 0.5, offset: -100, units: 'm' }, { name: 'x', scale: 0.0003 }]);
  assert.deepEqual([plain.scale, plain.offset, plain.divisor], [1, 0, 0], 'defaults are scale 1, offset 0');
  assert.deepEqual([scaled.scale, scaled.offset, scaled.divisor], [0.5, -100, 2], 'a scale of 1/n divides');
  assert.equal(odd.divisor, 0, 'a scale that is not 1/n multiplies');
  assert.equal(toPhysical(300, scaled), 50);
  assert.equal(toPhysical(0, odd), 0);
});

test('conversion per shader input comes from the bands the product uses; unused inputs are the identity', () => {
  const bands = normalizeBands([
    { name: 'B08', common_name: 'nir', scale: 1e-4, offset: -0.1 },
    { name: 'B04', common_name: 'red', scale: 0.5 },
  ]);
  const ndviProduct = resolveProducts(bands).find((p) => p.id === 'ndvi');
  const conversion = inputConversion(ndviProduct, bands, 0);
  assert.deepEqual(conversion.unitScale, [1e-4, 0.5, 1]);
  assert.deepEqual(conversion.unitDivisor, [10000, 2, 0]);
  assert.deepEqual(conversion.unitOffset, [-0.1, 0, 0]);
  assert.deepEqual(Object.keys(conversion).sort(), ['unitDivisor', 'unitOffset', 'unitScale'], 'no key collides with the camera\'s cx, cy, scale');
});

test('display: 8-bit RGB with scale 1 shows as it is; unscaled or float single bands get a linear stretch; Sentinel-2 bands keep the reflectance look', () => {
  const rgb = normalizeBands([{ name: 'r', common_name: 'red' }, { name: 'g', common_name: 'green' }, { name: 'b', common_name: 'blue' }]);
  const rgbProducts = byId(rgb);
  assert.deepEqual(displayMode(rgbProducts.true_color, rgb, 0, 'uint8'), { mode: 'linear', fixed: true, range: [0, 255] });
  assert.equal(displayMode(rgbProducts.true_color, rgb, 0, 'uint16').mode, 'reflectance', '16-bit RGB with scale 1 is not display-ready');
  const scaledRgb = normalizeBands(rgb.map((b) => ({ ...b, scale: 1 / 255 })));
  assert.equal(displayMode(byId(scaledRgb).true_color, scaledRgb, 0, 'uint8').mode, 'reflectance', 'a scale other than 1 means physical values');
  const offsetRgb = normalizeBands(rgb.map((b) => ({ ...b, offset: 5 })));
  assert.equal(displayMode(byId(offsetRgb).true_color, offsetRgb, 0, 'uint8').mode, 'reflectance');

  const float = normalizeBands([{ name: 'depth', units: 'm' }]);
  assert.deepEqual(displayMode(byId(float).band, float, 0, 'float32'), { mode: 'linear', fixed: false, range: null });
  const signed = normalizeBands([{ name: 'elevation', scale: 1 }]);
  assert.equal(displayMode(byId(signed).band, signed, 0, 'int16').fixed, false);
  const classes = normalizeBands([{ name: 'class', scale: 1 }]);
  assert.equal(displayMode(byId(classes).band, classes, 0, 'uint8').mode, 'linear', 'values up to 255 are not reflectance');

  const s2 = normalizeBands(['B02', 'B03', 'B04', 'B08']);
  const s2Products = byId(s2);
  assert.equal(displayMode(s2Products.band, s2, 2, 'uint16').mode, 'reflectance');
  assert.equal(displayMode(s2Products.true_color, s2, 0, 'uint16').mode, 'reflectance');
  assert.equal(displayMode(s2Products.ndvi, s2, 0, 'uint16').mode, 'reflectance', 'indices keep their color ramps');
  assert.equal(displayMode(rgbProducts.ndvi, rgb, 0, 'uint8').mode, 'reflectance');
});

test('ndvi and ndwi return null when both inputs are zero', () => {
  assert.equal(ndvi(0, 0), null);
  assert.equal(ndwi(0, 0), null);
  assert.equal(ndvi(3000, 1000), 0.5);
  assert.equal(ndwi(1000, 3000), -0.5);
});

test('describePixel reports indices from physical values, only when their bands exist, and none for a nodata pixel', () => {
  const both = describePixel(Uint16Array.of(1354, 2303), ['B04', 'B08'], 0);
  assert.equal(both.hasNdvi, true);
  assert.equal(both.hasNdwi, false);
  assert.ok(Math.abs(both.ndvi - (0.2303 - 0.1354) / (0.2303 + 0.1354)) < 1e-12);
  assert.deepEqual(both.bands.map((b) => [b.name, b.stored, b.value, b.reflectance]), [['B04', 1354, 0.1354, true], ['B08', 2303, 0.2303, true]]);

  const water = describePixel(Uint16Array.of(1100, 250), ['B03', 'B08'], 0);
  assert.equal(water.isWater, true);
  assert.equal(water.hasNdvi, false);

  const empty = describePixel(Uint16Array.of(0, 2303), ['B04', 'B08'], 0);
  assert.equal(empty.ndvi, null, 'a band at nodata leaves no index');
  assert.equal(empty.valid, true, 'the other band is valid');
  assert.deepEqual(empty.bands.map(({ stored, value }) => [stored, value]), [[0, null], [2303, 0.2303]]);
  assert.equal(describePixel(Uint16Array.of(0, 2303), ['B04', 'B08']).ndvi, 1, 'without a nodata value a zero is a value: red 0');
});

test('describePixel with band objects: offsets, units, and indices that use the offset', () => {
  const bands = [
    { name: 'nir', common_name: 'nir', scale: 0.0001, offset: -0.1 },
    { name: 'red', common_name: 'red', scale: 0.0001, offset: -0.1 },
  ];
  const pixel = describePixel(Float32Array.of(3000, 1000), bands, null);
  assert.ok(Math.abs(pixel.bands[0].value - 0.2) < 1e-12);
  assert.ok(Math.abs(pixel.bands[1].value - 0.0) < 1e-12);
  assert.equal(pixel.ndvi, 1, 'red reflectance 0 after the offset: (0.2 - 0) / (0.2 + 0)');
  assert.equal(pixel.bands[0].reflectance, true, 'scaled by 1/10000 with no units: reflectance, offset or not');

  const depth = describePixel(Float32Array.of(2.5), [{ name: 'depth', units: 'm' }], -9999);
  assert.deepEqual(depth.bands, [{ name: 'depth', units: 'm', reflectance: false, stored: 2.5, value: 2.5 }]);
  assert.equal(depth.hasNdvi, false);
});

test('reinhard tone map is monotonic and stretch ignores nodata and small samples', () => {
  assert.ok(reinhardSrgb(0.05) < reinhardSrgb(0.1));
  assert.equal(computeStretchLo([[0, 0, 0], [0.12, 0.09, 0.07]]), 0, 'too few samples');
  const samples = Array.from({ length: 200 }, (_, i) => [0.1 + i * 0.001, 0.08 + i * 0.001, 0.06 + i * 0.001]);
  samples.push([0, 0, 0]);
  const lo = computeStretchLo(samples);
  assert.ok(lo > 0 && lo < 1);
});

test('percentileRange: 2nd to 98th percentile of the finite values; a flat series gets a width; nothing gives null', () => {
  const values = Array.from({ length: 101 }, (_, i) => i);
  assert.deepEqual(percentileRange([...values, NaN, Infinity]), [2, 98]);
  assert.deepEqual(percentileRange([5, 5, 5]), [5, 6]);
  assert.equal(percentileRange([NaN]), null);
  assert.equal(percentileRange([]), null);
});

test('time labels: months for month-start timesteps, ISO dates otherwise', () => {
  assert.equal(makeTimeFormatter(['2024-01-01T00:00:00Z', '2024-02-01T00:00:00Z'])(1), 'Feb 2024');
  assert.equal(makeTimeFormatter(['2024-01-01T00:00:00Z', '2024-01-17T00:00:00Z'])(1), '2024-01-17');
});

test('describePixel with a validity mask: masked-out means no values and no indices, and the mask replaces nodata', () => {
  const bands = ['B04', 'B08'];
  const masked = describePixel(Uint16Array.of(1354, 2303), bands, 0, 0);
  assert.equal(masked.valid, false);
  assert.deepEqual(masked.bands.map((b) => [b.name, b.stored, b.value]), [['B04', null, null], ['B08', null, null]]);
  assert.equal(masked.ndvi, null);
  assert.equal(masked.isWater, false);
  assert.equal(masked.hasNdvi, true, 'the bands exist, the pixel has no value');

  const valid = describePixel(Uint16Array.of(1354, 2303), bands, 0, 1);
  assert.equal(valid.valid, true);
  assert.equal(valid.bands[0].value, 0.1354);

  // A declared nodata of 0 is not compared once there is a mask (spec 2.3): a valid pixel holding 0 is data.
  const zero = describePixel(Uint16Array.of(0, 2303), bands, 0, 1);
  assert.equal(zero.valid, true);
  assert.equal(zero.bands[0].value, 0, 'the mask makes a value equal to the sentinel valid');
  assert.equal(zero.ndvi, 1, 'red 0 is a value here');
  assert.equal(describePixel(Uint16Array.of(0, 2303), bands, 0, null).ndvi, null, 'without a mask the same pixel is missing');
});

test('describePixel validity without a mask: invalid only when every band is at nodata (or NaN)', () => {
  assert.equal(describePixel(Uint16Array.of(0, 0), ['B04', 'B08'], 0).valid, false);
  assert.equal(describePixel(Uint16Array.of(0, 5), ['B04', 'B08'], 0).valid, true);
  assert.equal(describePixel(Uint16Array.of(0, 0), ['B04', 'B08'], null).valid, true, 'no nodata value declared: zeros are values');
  assert.equal(describePixel(Float32Array.of(NaN), [{ name: 'depth' }], null).valid, false);
  assert.equal(describePixel(Float32Array.of(-9999), [{ name: 'depth' }], -9999).valid, false);
});

// The results computeStretchLo and percentileRange must give, computed with ordinary arrays sorted by a comparator.
function referenceStretchLo(rgbSamples) {
  const tones = [];
  for (const [r, g, b] of rgbSamples) {
    if (r === 0 && g === 0 && b === 0) continue;
    tones.push(reinhardSrgb(r), reinhardSrgb(g), reinhardSrgb(b));
  }
  if (tones.length < 30) return 0;
  tones.sort((a, b) => a - b);
  return tones[Math.floor(tones.length * 0.02)];
}

function referencePercentileRange(values) {
  const finite = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (finite.length === 0) return null;
  const lo = finite[Math.floor(finite.length * 0.02)];
  const hi = finite[Math.min(finite.length - 1, Math.floor(finite.length * 0.98))];
  return hi > lo ? [lo, hi] : [lo, lo + 1];
}

function seededRandom(seed) {
  let state = seed;
  return () => {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    return state / 2 ** 32;
  };
}

test('computeStretchLo gives the same value as sorting every tone with a comparator, for every sample count and kind of data', () => {
  const random = seededRandom(1);
  const kinds = {
    'uniform reflectance': () => random() * 0.3,
    'duplicates': () => Math.round(random() * 20) / 100,
    'mostly nodata': () => (random() < 0.8 ? 0 : random() * 0.3),
    'a few large values': () => (random() < 0.05 ? 5 + random() * 100 : random() * 0.2),
    'negative reflectance': () => random() * 0.2 - 0.05,
    'constant': () => 0.12,
  };
  // Nine samples give 27 tones, below the 30 needed; ten give exactly 30.
  const counts = [0, 1, 2, 9, 10, 11, 49, 50, 51, 100, 333, 1200];
  for (const [kind, value] of Object.entries(kinds)) {
    for (const count of counts) {
      const samples = Array.from({ length: count }, () => [value(), value(), value()]);
      assert.equal(computeStretchLo(samples), referenceStretchLo(samples), `${kind}, ${count} samples`);
    }
  }
  const nodata = Array.from({ length: 40 }, () => [0, 0, 0]);
  assert.equal(computeStretchLo(nodata), 0, 'only all-zero samples');
  assert.equal(computeStretchLo([...nodata, ...Array.from({ length: 10 }, () => [0.1, 0.1, 0.1])]), referenceStretchLo([...nodata, ...Array.from({ length: 10 }, () => [0.1, 0.1, 0.1])]), 'nodata does not count towards the 30 tones');
});

test('computeStretchLo leaves tones that are not finite out of the percentile and of the count', () => {
  const random = seededRandom(2);
  const finite = Array.from({ length: 200 }, () => [random() * 0.3, random() * 0.3, random() * 0.3]);
  // The 2nd percentile of the tones that carry a value. -0.2 tone-maps to -Infinity; NaN and Infinity reflectance to NaN.
  const percentileOfFiniteTones = (samples) => {
    const tones = samples.filter(([r, g, b]) => !(r === 0 && g === 0 && b === 0)).flatMap((sample) => sample.map(reinhardSrgb)).filter(Number.isFinite).sort((a, b) => a - b);
    return tones.length < 30 ? 0 : tones[Math.floor(tones.length * 0.02)];
  };
  const expected = referenceStretchLo(finite);
  assert.ok(expected > 0 && expected < 1);

  // Pixels with no value at all do not move the result, however many there are (half of all samples, then most).
  for (const nodata of [[NaN, NaN, NaN], [Infinity, -Infinity, NaN], [-0.2, -0.2, -0.2]]) {
    for (const count of [1, 20, 200, 1000]) {
      const samples = [...finite, ...Array.from({ length: count }, () => nodata)];
      assert.equal(computeStretchLo(samples), expected, `${count} samples of ${nodata}`);
    }
  }

  // One channel without a value drops that tone only; the other two of the sample still count.
  const partial = finite.map((sample, i) => (i % 3 === 0 ? [NaN, sample[1], sample[2]] : i % 3 === 1 ? [sample[0], Infinity, sample[2]] : [sample[0], sample[1], -0.2]));
  assert.equal(computeStretchLo(partial), percentileOfFiniteTones(partial));
  const mixed = [...finite, ...partial, [NaN, 0.1, 0.1], [0, NaN, 0], [0, 0, 0]];
  assert.equal(computeStretchLo(mixed), percentileOfFiniteTones(mixed));

  // The 30-tone minimum counts finite tones: 9 samples are 27 tones, and any number of NaN samples does not add to them.
  const few = finite.slice(0, 9);
  assert.equal(computeStretchLo([...few, ...Array.from({ length: 100 }, () => [NaN, NaN, NaN])]), 0);
  assert.equal(computeStretchLo([...finite.slice(0, 10), ...Array.from({ length: 100 }, () => [NaN, NaN, NaN])]), referenceStretchLo(finite.slice(0, 10)));
  assert.equal(computeStretchLo([[NaN, NaN, NaN], [NaN, NaN, NaN]]), 0);
});

test('percentileRange gives the same range as sorting the finite values with a comparator, for every count and kind of data', () => {
  const random = seededRandom(3);
  const kinds = {
    'uniform': () => random() * 5,
    'duplicates': () => Math.round(random() * 10),
    'negative': () => random() * 40 - 30,
    'with NaN and infinities': () => (random() < 0.1 ? [NaN, Infinity, -Infinity][Math.floor(random() * 3)] : random() * 5),
    'only non-finite and one value': () => (random() < 0.99 ? NaN : 1),
    'constant': () => 5,
  };
  const counts = [0, 1, 2, 3, 49, 50, 51, 99, 100, 101, 102, 999, 5000];
  for (const [kind, value] of Object.entries(kinds)) {
    for (const count of counts) {
      const values = Array.from({ length: count }, value);
      assert.deepEqual(percentileRange(values), referencePercentileRange(values), `${kind}, ${count} values`);
    }
  }
});
