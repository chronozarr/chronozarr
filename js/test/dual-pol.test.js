import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSeries, seriesSpecs } from '../demo/chart.js';
import { BACKGROUND, shadeTexel } from '../e2e/cpu-render.js';
import { PRODUCT_GLSL } from '../shared/products-glsl.js';
import {
  PRODUCTS,
  channelBounds,
  defaultProductIndex,
  displayMode,
  dualPolChannels,
  dualPolRanges,
  findBand,
  findPolarisations,
  inputConversion,
  inputIndices,
  normalizeBands,
  percentileRange,
  resolveProducts,
} from '../shared/products.js';

/** Band objects as a SAR store declares them: dB stored as uint16, scale 0.01, offset -60. */
const sar = (...bands) => normalizeBands(bands.map((band) => ({ scale: 0.01, offset: -60, units: 'dB', ...(typeof band === 'string' ? { name: band } : band) })));
const byId = (bands) => Object.fromEntries(resolveProducts(bands).map((p) => [p.id, p]));

test('VV with VH and HH with HV are the co-pol and cross-pol bands, in any store order and any case', () => {
  assert.deepEqual(findPolarisations(sar('VV', 'VH')), [0, 1]);
  assert.deepEqual(findPolarisations(sar('VH', 'VV')), [1, 0]);
  assert.deepEqual(findPolarisations(sar('HH', 'HV')), [0, 1]);
  assert.deepEqual(findPolarisations(sar('mask_x', 'hv', 'Hh')), [2, 1]);
  const bands = sar('VV', 'VH');
  assert.equal(findBand(bands, 'copol'), 0);
  assert.equal(findBand(bands, 'crosspol'), 1);
});

test('a quad-pol store pairs VV with VH, and falls back to HH with HV only when VV and VH are not both there', () => {
  assert.deepEqual(findPolarisations(sar('HH', 'HV', 'VH', 'VV')), [3, 2], 'VV and VH win');
  assert.deepEqual(findPolarisations(sar('VV', 'VH', 'HH', 'HV')), [0, 1]);
  assert.deepEqual(findPolarisations(sar('HH', 'HV', 'VV')), [0, 1], 'no VH: HH with HV');
  assert.deepEqual(findPolarisations(sar('HH', 'HV', 'VH')), [0, 1], 'no VV: HH with HV');
});

test('VV is never paired with HV, nor HH with VH, and one polarisation alone is no pair', () => {
  for (const names of [['VV', 'HV'], ['HH', 'VH'], ['VV'], ['HH'], ['VH', 'HV'], ['VV', 'HH']]) {
    assert.deepEqual(findPolarisations(sar(...names)), [-1, -1], names.join('+'));
  }
  const products = byId(sar('VV', 'HV'));
  assert.equal(products.dual_pol.available, false);
  assert.deepEqual(products.dual_pol.missing, ['copol', 'crosspol']);
  assert.equal(products.band.available, true, 'the single band stays');
});

test('a declared common_name is the role first; a band with another common_name is not matched by its name', () => {
  const declared = sar({ name: 'a', common_name: 'copol' }, { name: 'b', common_name: 'crosspol' }, 'VV', 'VH');
  assert.deepEqual(findPolarisations(declared), [0, 1], 'the declared bands beat VV and VH');
  assert.deepEqual(findPolarisations(sar('VV', 'VH', { name: 'z', common_name: 'crosspol' })), [0, 2]);
  assert.deepEqual(findPolarisations(sar({ name: 'x', common_name: 'copol' }, 'VV', 'VH')), [0, 2], 'the role that is not declared is found by name');
  assert.deepEqual(findPolarisations(sar({ name: 'x', common_name: 'copol' }, 'HV')), [0, -1], 'a name needs its whole pair');
  assert.deepEqual(findPolarisations(sar({ name: 'VV', common_name: 'red' }, 'VH')), [-1, -1], 'VV is red here, not co-pol');
  assert.equal(findBand(declared, 'crosspol'), 1);
});

test('the dual-pol product resolves to (copol, crosspol) and converts each from its own scale and offset', () => {
  const bands = sar('HV', 'HH', 'mask_like');
  const product = byId(bands).dual_pol;
  assert.equal(product.available, true);
  assert.deepEqual(product.indices, [1, 0, -1]);
  assert.equal(product.shader, 6);
  assert.deepEqual(inputIndices(product, 2), [1, 0, -1], 'the band choice is for the single band only');
  const mixed = normalizeBands([{ name: 'VV', scale: 0.01, offset: -60 }, { name: 'VH', scale: 0.02, offset: -70 }]);
  const conversion = inputConversion(byId(mixed).dual_pol, mixed, 0);
  assert.deepEqual(conversion.unitScale, [0.01, 0.02, 1]);
  assert.deepEqual(conversion.unitOffset, [-60, -70, 0]);
  assert.equal(findBand(normalizeBands(['B02', 'B03', 'B04', 'B08']), 'copol'), -1, 'Sentinel-2 bands are not SAR');
});

test('the viewer opens a store with a pair on the composite, and every other store on the product it opened on before', () => {
  const open = (bands) => {
    const products = resolveProducts(bands);
    return products[defaultProductIndex(products)].id;
  };
  assert.equal(open(sar('HH', 'HV')), 'dual_pol');
  assert.equal(open(sar('VV', 'VH')), 'dual_pol');
  assert.equal(open(sar('VV', 'HV')), 'band', 'no pair: the single band');
  assert.equal(open(['B02', 'B03', 'B04', 'B08']), 'true_color');
  assert.equal(open(['B04', 'B08']), 'ndvi');
  assert.equal(open(['B04']), 'band');
  assert.equal(open([{ name: 'red' }, { name: 'green' }, { name: 'blue' }, { name: 'VV' }, { name: 'VH' }]), 'true_color', 'list order decides between real products');
  assert.equal(defaultProductIndex(resolveProducts([])), -1, 'a store with no bands has no product');
});

test('the composite is a linear display with one held range per channel, which the user cannot adjust', () => {
  const bands = sar('HH', 'HV');
  const products = byId(bands);
  assert.deepEqual(displayMode(products.dual_pol, bands, 0, 'uint16'), { mode: 'linear', fixed: true, range: null, perChannel: true });
  assert.deepEqual(displayMode(products.band, bands, 1, 'uint16'), { mode: 'linear', fixed: false, range: null }, 'the single band keeps its adjustable stretch');
  assert.ok(PRODUCTS.every((p, i) => p.shader === i), 'the composite is the next shader number');
});

test('channels are co-pol, cross-pol and their difference in dB', () => {
  assert.deepEqual(dualPolChannels(-12, -20), [-12, -20, 8]);
  assert.deepEqual(dualPolChannels(-25.5, -18), [-25.5, -18, -7.5], 'cross-pol above co-pol gives a negative ratio');
});

test('per-channel ranges are the 2nd to 98th percentile of each channel over the valid samples, as the single band computes them', () => {
  const samples = Array.from({ length: 100 }, (_, i) => [-30 + 0.2 * i, -40 + 0.1 * i]);
  const ranges = dualPolRanges(samples);
  assert.deepEqual(ranges, [
    percentileRange(samples.map(([co]) => co)),
    percentileRange(samples.map(([, cross]) => cross)),
    percentileRange(samples.map(([co, cross]) => co - cross)),
  ]);
  const [co, cross, ratio] = ranges;
  assert.ok(Math.abs(co[0] - -29.6) < 1e-9 && Math.abs(co[1] - -10.4) < 1e-9, `co-pol ${co}`);
  assert.ok(Math.abs(cross[0] - -39.8) < 1e-9 && Math.abs(cross[1] - -30.2) < 1e-9, `cross-pol ${cross}`);
  assert.ok(Math.abs(ratio[0] - 10.2) < 1e-9 && Math.abs(ratio[1] - 19.8) < 1e-9, `ratio ${ratio}`);
});

test('a sample with a non-finite value is left out of every channel, and no valid sample gives no ranges', () => {
  const clean = Array.from({ length: 50 }, (_, i) => [-20 + i * 0.1, -30 + i * 0.05]);
  const withGaps = [...clean, [NaN, -25], [-15, NaN], [Infinity, -20], [NaN, NaN]];
  assert.deepEqual(dualPolRanges(withGaps), dualPolRanges(clean));
  assert.equal(dualPolRanges([]), null);
  assert.equal(dualPolRanges([[NaN, 1], [1, NaN]]), null);
  assert.deepEqual(dualPolRanges([[-10, -20], [-10, -20]]), [[-10, -9], [-20, -19], [10, 11]], 'a constant channel is spread by 1, as for the single band');
});

test('a range for the shader is one pair for every channel or one pair per channel', () => {
  assert.deepEqual(channelBounds([2, 5]), { lo: [2, 2, 2], hi: [5, 5, 5] });
  assert.deepEqual(channelBounds([[-30, -10], [-40, -20], [0, 20]]), { lo: [-30, -40, 0], hi: [-10, -20, 20] });
});

test('the shader stretches co-pol to red, cross-pol to green and the dB difference to blue, each over its own range', () => {
  assert.match(PRODUCT_GLSL, /product == 6 \? vec3\(x\.x, x\.y, x\.x - x\.y\) : x/);
  const bands = sar('HH', 'HV');
  const product = byId(bands).dual_pol;
  const frame = {
    shader: product.shader,
    inputs: inputIndices(product, 0),
    ...inputConversion(product, bands, 0),
    display: 'linear',
    range: [[-30, -10], [-40, -20], [0, 20]],
    nodata: null,
    stretchLo: 0,
  };
  // stored 4000 -> -20 dB, 3000 -> -30 dB: co 0.5 of its range, cross 0.5, ratio 10 dB 0.5.
  assert.deepEqual(shadeTexel(frame, [4000, 3000]).map((c) => Math.round(c)), [128, 128, 128]);
  // co-pol -10 dB (top of its range), cross-pol -45 dB (below its range), ratio 35 dB (above its range).
  assert.deepEqual(shadeTexel(frame, [5000, 1500]).map((c) => Math.round(c)), [255, 0, 255]);
  const nodataFrame = { ...frame, nodata: 0 };
  assert.deepEqual(shadeTexel(nodataFrame, [0, 0]), BACKGROUND, 'both bands at nodata is background');
});

test('the chart of the composite has the co-pol, cross-pol and ratio lines in dB, and a gap where either band has no data', () => {
  const bands = sar('HH', 'HV');
  const product = byId(bands).dual_pol;
  const specs = seriesSpecs(product, bands, 0);
  assert.deepEqual(specs.map((s) => s.label), ['HH co-pol', 'HV cross-pol', 'co/cross ratio']);
  const readings = [{ pixels: [Uint16Array.of(4000, 3000)] }, { pixels: [Uint16Array.of(0, 3000)] }, { pixels: [Uint16Array.of(4000, 0)] }];
  const series = buildSeries(specs, readings, 0);
  const rounded = (values) => values.map((v) => (v === null ? null : Math.round(v * 100) / 100));
  assert.deepEqual(rounded(series[0].values), [-20, null, -20]);
  assert.deepEqual(rounded(series[1].values), [-30, -30, null]);
  assert.deepEqual(rounded(series[2].values), [10, null, null]);
});
