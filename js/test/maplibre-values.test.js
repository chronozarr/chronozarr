import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ChronozarrLayer } from '../maplibre/layer.js';
import { computeStretchLo, dualPolRanges } from '../shared/products.js';
import { buildSyntheticStore, maskValue } from '../support/synthetic-store.js';

const MATRIX = [2, 0, 0, 0, 0, -2, 0, 0, 0, 0, 1, 0, -1, 1, 0, 1];
const map = { triggerRepaint() {}, getLayer() { return false; }, getZoom() { return 0; } };

// Capture the public render method's shader inputs without requiring a browser or GPU.
function captureGl() {
  const uniforms = new Map();
  const constants = new Map([['NO_ERROR', 0]]);
  const uploads = [];
  let alignment = 4;
  const gl = new Proxy({}, {
    get(_, name) {
      if (name === 'getParameter') return (parameter) => parameter === constants.get('UNPACK_ALIGNMENT') ? alignment : 2048;
      if (name === 'getError') return () => 0;
      if (name === 'pixelStorei') return (parameter, value) => { if (parameter === constants.get('UNPACK_ALIGNMENT')) alignment = value; };
      if (name === 'texSubImage3D') return () => uploads.push(alignment);
      if (name === 'getShaderParameter' || name === 'getProgramParameter') return () => true;
      if (name === 'getUniformLocation') return (_, uniform) => uniform;
      if (name === 'getAttribLocation') return () => 0;
      if (name === 'uniform1f' || name === 'uniform3f') return (uniform, ...values) => uniforms.set(uniform, values);
      if (/^create/.test(name)) return () => ({});
      if (/^[A-Z_0-9]+$/.test(name)) {
        if (!constants.has(name)) constants.set(name, constants.size);
        return constants.get(name);
      }
      return () => {};
    },
  });
  return { gl, uniforms, uploads, alignment: () => alignment };
}

async function openLayer(t, spec, options = {}) {
  const memory = buildSyntheticStore({ nTime: 1, nBand: 1, height: 2, width: 2, chunk: 2, sharded: false,
    crs: 'EPSG:4326', transform: [1, 0, 0, 0, -1, 2], bandObjects: [{ name: 'value', scale: 1, offset: 0 }], ...spec });
  const layer = new ChronozarrLayer({ url: 'memory://maplibre-values', product: 'band', ...options, storeOptions: { store: memory, workers: 0, ...options.storeOptions } });
  t.after(() => layer.remove());
  layer.onAdd(map, null);
  const store = await layer.opened;
  return { layer, store, memory };
}

async function renderLayer(layer, store) {
  for (let row = 0; row < store.levels[0].gridRows; row++) {
    for (let col = 0; col < store.levels[0].gridCols; col++) {
      await store.getRaw(0, row, col, 0);
      if (store.hasMask) await store.getMask(0, row, col, 0);
    }
  }
  const { gl, uniforms, uploads, alignment } = captureGl();
  layer.onAdd(map, gl);
  layer.render({ defaultProjectionData: { mainMatrix: MATRIX } });
  assert.equal(layer.stats.frames, 1);
  assert.ok(uploads.length > 0 && uploads.every((value) => value === 1), 'all data and mask uploads use tightly packed rows');
  assert.equal(alignment(), 4, 'the shared GL unpack alignment is restored');
  return uniforms;
}

test('pixel inspection hides masked values and indices, and retains a valid zero', async (t) => {
  const { layer } = await openLayer(t, { nBand: 2, mask: true,
    bandObjects: [{ name: 'red', scale: 1 }, { name: 'nir', scale: 1 }],
    values: (_, b, y, x) => x === 1 ? (b === 0 ? 0 : 2) : (b === 0 ? 4 : 8) }, { product: 'ndvi' });
  const masked = await layer.getValueAt([0.5, 1.5]);
  assert.equal(masked.valid, false);
  assert.deepEqual(masked.bands.map(({ stored, value }) => [stored, value]), [[null, null], [null, null]]);
  assert.equal(masked.ndvi, null);
  assert.equal(masked.ndwi, null);
  assert.equal(masked.isWater, false);
  const valid = await layer.getValueAt([1.5, 1.5]);
  assert.equal(valid.valid, true);
  assert.deepEqual(valid.bands.map(({ stored }) => stored), [0, 2]);
  assert.equal(valid.ndvi, 1);
});

test('linear range excludes a masked outlier', async (t) => {
  const { layer, store } = await openLayer(t, { dtype: 'float32', mask: true,
    values: (_, b, y, x) => y === 0 && x === 0 ? 1e6 : 10 + 2 * y + x });
  const uniforms = await renderLayer(layer, store);
  assert.deepEqual(uniforms.get('u_rangeLo'), [11, 11, 11]);
  assert.deepEqual(uniforms.get('u_rangeHi'), [12, 12, 12]);
});

for (const mask of [false, true]) {
  test(`linear range ${mask ? 'retains' : 'excludes'} nodata when mask=${mask}`, async (t) => {
    const { layer, store } = await openLayer(t, { dtype: 'float32', mask,
      values: (_, b, y, x) => x === 1 && y === 0 ? 0 : 10 + y + x });
    const uniforms = await renderLayer(layer, store);
    assert.deepEqual(uniforms.get('u_rangeLo'), Array(3).fill(mask ? 0 : 10));
    assert.deepEqual(uniforms.get('u_rangeHi'), Array(3).fill(mask ? 11 : 12));
  });
}

test('dual-pol ranges use masked validity including a valid nodata-valued channel', async (t) => {
  const bands = [{ name: 'VV', scale: 1, units: 'dB' }, { name: 'VH', scale: 1, units: 'dB' }];
  const values = (_, b, y, x) => y === 0 && x === 0 ? 1e6 : (b === 0 ? (x === 1 && y === 0 ? 0 : -10) : -20 - y - x);
  const { layer, store } = await openLayer(t, { dtype: 'float32', mask: true, nBand: 2, bandObjects: bands, values }, { product: 'dual_pol' });
  const uniforms = await renderLayer(layer, store);
  const ranges = dualPolRanges([[0, -21], [-10, -21]]);
  assert.deepEqual(uniforms.get('u_rangeLo'), ranges.map(([lo]) => lo));
  assert.deepEqual(uniforms.get('u_rangeHi'), ranges.map(([, hi]) => hi));
});

test('reflectance stretch excludes masked pixels', async (t) => {
  const values = (_, b, y, x) => maskValue(0, y, x) ? 1000 + b * 500 + y * 100 + x : 1;
  const { layer, store } = await openLayer(t, { mask: true, nBand: 3, width: 4, height: 4, chunk: 4,
    bandObjects: ['red', 'green', 'blue'].map((name) => ({ name, scale: 0.0001, units: 'reflectance' })), values }, { product: 'true_color' });
  const uniforms = await renderLayer(layer, store);
  const samples = [];
  for (let y = 0; y < 4; y++) for (let x = 0; x < 4; x++) {
    if (maskValue(0, y, x)) samples.push([0, 1, 2].map((b) => values(0, b, y, x) * 0.0001));
  }
  assert.deepEqual(uniforms.get('u_stretchLo'), [computeStretchLo(samples)]);
});

test('mask lookup uses the chunk stride on a partial edge cell', async (t) => {
  const { layer, store } = await openLayer(t, { dtype: 'float32', mask: true, height: 3, width: 1,
    values: (_, b, y) => y === 0 ? 1e6 : 10 + y });
  const uniforms = await renderLayer(layer, store);
  assert.deepEqual(uniforms.get('u_rangeLo'), [11, 11, 11]);
  assert.deepEqual(uniforms.get('u_rangeHi'), [12, 12, 12]);
});

test('an empty masked frame does not fix the range before a valid timestep arrives', async (t) => {
  const { layer, store, memory } = await openLayer(t, { dtype: 'float32', mask: true, nTime: 2,
    values: (time, b, y, x) => 10 + 10 * time + y + x });
  memory.files.get('/0/mask/c/0/0/0').fill(0);
  const uniforms = await renderLayer(layer, store);
  assert.deepEqual(uniforms.get('u_rangeHi'), [1, 1, 1]);
  await store.getRaw(0, 0, 0, 1);
  await store.getMask(0, 0, 0, 1);
  layer.setTime(1);
  layer.render({ defaultProjectionData: { mainMatrix: MATRIX } });
  assert.deepEqual(uniforms.get('u_rangeLo'), [20, 20, 20]);
  assert.deepEqual(uniforms.get('u_rangeHi'), [22, 22, 22]);
});

test('automatic range recovers from GPU-resident cells evicted from the CPU cache', async (t) => {
  const { layer, store } = await openLayer(t, { dtype: 'float32', mask: true, nBand: 2,
    bandObjects: [{ name: 'red', scale: 1 }, { name: 'nir', scale: 1 }],
    values: (_, b, y, x) => 10 + 10 * b + y + x }, { product: 'ndvi' });
  // Start with no display range to measure, then discard CPU data while GPU copies remain.
  const uniforms = await renderLayer(layer, store);
  store.clearCache();
  layer.setProduct('band');
  layer.render({ defaultProjectionData: { mainMatrix: MATRIX } });
  for (let i = 0; i < 100 && layer.stats.pending > 0; i++) await new Promise(setImmediate);
  layer.render({ defaultProjectionData: { mainMatrix: MATRIX } });
  assert.deepEqual(uniforms.get('u_rangeLo'), [11, 11, 11]);
  assert.deepEqual(uniforms.get('u_rangeHi'), [12, 12, 12]);
});

for (const empty of [false, true]) {
  test(`a one-cell CPU cache recovers complete samples and then settles; empty=${empty}`, async (t) => {
    const { layer, store, memory } = await openLayer(t, { dtype: 'float32', mask: true, width: 4,
      values: (_, b, y, x) => x < 2 ? 10 + y + x : 1e6 },
    { storeOptions: { totalBytes: 16, decodedBytes: 16, compressedBytes: 0 } });
    memory.files.get('/0/mask/c/0/0/1').fill(0);
    if (empty) memory.files.get('/0/mask/c/0/0/0').fill(0);
    await store.getRaw(0, 0, 0, 0);
    await store.getMask(0, 0, 0, 0);
    const { gl, uniforms } = captureGl();
    layer.onAdd(map, gl);
    for (let i = 0; i < 20; i++) {
      layer.render({ defaultProjectionData: { mainMatrix: MATRIX } });
      await new Promise(setImmediate);
    }
    assert.equal(layer.stats.state, 'ready');
    assert.deepEqual(uniforms.get('u_rangeLo'), Array(3).fill(empty ? 0 : 11));
    assert.deepEqual(uniforms.get('u_rangeHi'), Array(3).fill(empty ? 1 : 12));
    const reads = memory.log.length;
    for (let i = 0; i < 5; i++) {
      layer.render({ defaultProjectionData: { mainMatrix: MATRIX } });
      await new Promise(setImmediate);
    }
    assert.equal(memory.log.length, reads, 'a completed view causes no repeated source reads');
  });
}
