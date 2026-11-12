import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ChronoStore } from '../chronozarr/decoder.js';
import { ChronozarrLayer } from '../maplibre/layer.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

// The layer's lifecycle against a stub GL and an in-memory store, without a browser: what it holds and reports after it
// opens, fails or is removed, and what GL objects a failed setup leaves behind.
const map = { triggerRepaint() {}, getLayer() { return false; }, getZoom() { return 0; } };

/**
 * A GL stand-in that counts the shaders, programs and textures created and deleted. With `failFragment` the fragment
 * shader does not compile; `state.glError` is what getError reports (0 is NO_ERROR).
 */
function countingGl({ failFragment = false, glError = 0 } = {}) {
  const constants = new Map([['NO_ERROR', 0]]);
  const state = { glError };
  const counts = { createShader: 0, deleteShader: 0, createProgram: 0, deleteProgram: 0, createTexture: 0, deleteTexture: 0 };
  const gl = new Proxy({}, {
    get(_, name) {
      if (name === 'getParameter') return () => 2048;
      if (name === 'getError') return () => state.glError;
      if (name === 'createShader') {
        return (type) => {
          counts.createShader++;
          return { type };
        };
      }
      if (name === 'getShaderParameter') return (shader) => !(failFragment && shader.type === constants.get('FRAGMENT_SHADER'));
      if (name === 'getShaderInfoLog') return () => 'stub compile error';
      if (name === 'getProgramParameter') return () => true;
      if (name === 'getUniformLocation') return (_, uniform) => uniform;
      if (name === 'getAttribLocation') return () => 0;
      if (Object.hasOwn(counts, name)) {
        return () => {
          counts[name]++;
          return {};
        };
      }
      if (/^create/.test(name)) return () => ({});
      if (/^[A-Z_0-9]+$/.test(name)) {
        if (!constants.has(name)) constants.set(name, constants.size);
        return constants.get(name);
      }
      return () => {};
    },
  });
  return { gl, counts, state };
}

/** A layer on a two-timestep, one-cell in-memory store, with every 'error' event it emits collected in `errors`. */
function newLayer(t, { layerOptions = {}, storeOptions = {} } = {}) {
  const memory = buildSyntheticStore({ nTime: 2, nBand: 1, height: 2, width: 2, chunk: 2, sharded: false,
    crs: 'EPSG:4326', transform: [1, 0, 0, 0, -1, 2], bandObjects: [{ name: 'value', scale: 1, offset: 0 }] });
  const layer = new ChronozarrLayer({ url: 'memory://maplibre-lifecycle', product: 'band', ...layerOptions, storeOptions: { store: memory, workers: 0, ...storeOptions } });
  t.after(() => layer.remove());
  const errors = [];
  layer.on('error', (event) => errors.push(event));
  return { layer, memory, errors };
}

const rootReads = (memory) => memory.log.filter(({ key }) => key === '/zarr.json').length;

test('a layer whose GPU setup fails while it opens holds no store, and the store it opened is closed', async (t) => {
  t.mock.method(console, 'error', () => {});
  const close = t.mock.method(ChronoStore.prototype, 'close');
  const { layer } = newLayer(t, { layerOptions: { gpuBudgetBytes: 1 } });
  layer.onAdd(map, countingGl().gl);
  await assert.rejects(layer.opened, /GPU budget/);
  assert.equal(layer.store, null);
  assert.equal(layer.times, null);
  assert.equal(layer.products, null);
  assert.equal(layer.bandNames, null);
  assert.equal(layer.bounds, null);
  assert.equal(close.mock.callCount(), 1, 'the store that was opened was closed');
});

test('a layer that opened and then fails to set up the GPU releases its store, and adding it again opens the store anew', async (t) => {
  t.mock.method(console, 'error', () => {});
  const close = t.mock.method(ChronoStore.prototype, 'close');
  const { layer, memory } = newLayer(t);
  layer.onAdd(map, null);
  const first = await layer.opened;
  layer.onAdd(map, countingGl({ failFragment: true }).gl);
  assert.equal(layer.store, null, 'the failed layer holds no store');
  assert.equal(layer.products, null);
  assert.equal(close.mock.callCount(), 1, 'the store was closed');
  const reopened = new Promise((resolve) => layer.on('open', (event) => resolve(event.store)));
  layer.onAdd(map, countingGl().gl);
  const second = await reopened;
  assert.notEqual(second, first);
  assert.equal(layer.store, second);
  assert.equal(rootReads(memory), 2, 'the store was read again');
  assert.ok(layer.stats.slots > 0, 'the GPU was set up');
});

test('adding a layer a second time while its store opens reads the store once', async (t) => {
  const { layer, memory } = newLayer(t);
  layer.onAdd(map, null);
  layer.onAdd(map, countingGl().gl);
  const store = await layer.opened;
  assert.equal(rootReads(memory), 1);
  assert.equal(layer.store, store);
  assert.ok(layer.stats.slots > 0, 'the open in progress set up the GPU with the context of the second call');
});


test('a texture allocation that fails deletes the program and the textures it created', async (t) => {
  t.mock.method(console, 'error', () => {});
  const { layer } = newLayer(t);
  const { gl, counts } = countingGl({ glError: 1282 });
  layer.onAdd(map, gl);
  await assert.rejects(layer.opened, /GL error 1282/);
  assert.equal(counts.createTexture, 1);
  assert.equal(counts.deleteTexture, 1, 'the texture was deleted');
  assert.equal(counts.createProgram, 1);
  assert.equal(counts.deleteProgram, 1, 'the program was deleted');
  assert.equal(layer.stats.slots, 0, 'the layer holds no GPU pool');
});

test('a fragment shader that fails to compile leaves no shader behind', async (t) => {
  t.mock.method(console, 'error', () => {});
  const { layer } = newLayer(t);
  const { gl, counts } = countingGl({ failFragment: true });
  layer.onAdd(map, gl);
  await assert.rejects(layer.opened, /shader compile failed/);
  assert.equal(counts.createShader, 2);
  assert.equal(counts.deleteShader, 2, 'the vertex shader was deleted too');
});
