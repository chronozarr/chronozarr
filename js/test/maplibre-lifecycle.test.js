import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ChronoStore } from '../chronozarr/decoder.js';
import { ChronozarrLayer } from '../maplibre/layer.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

// The layer's lifecycle against a stub GL and an in-memory store, without a browser: what it holds and reports after it
// opens, fails or is removed, and what GL objects a failed setup leaves behind.
const MATRIX = [2, 0, 0, 0, 0, -2, 0, 0, 0, 0, 1, 0, -1, 1, 0, 1];
const map = { triggerRepaint() {}, getLayer() { return false; }, getZoom() { return 0; } };
const flush = () => new Promise(setImmediate);

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

test('a layer that opened and then fails to set up the GPU keeps answering getValueAt from its store', async (t) => {
  t.mock.method(console, 'error', () => {});
  const { layer, errors } = newLayer(t);
  layer.onAdd(map, null);
  const store = await layer.opened;
  layer.onAdd(map, countingGl({ failFragment: true }).gl);
  await flush();
  assert.equal(layer.stats.state, 'failed');
  assert.deepEqual(errors.map((event) => event.fatal), [true]);
  assert.equal(layer.store, store);
  const value = await layer.getValueAt([0.5, 1.5]);
  assert.equal(value.valid, true);
  assert.equal(value.bands.length, 1);
});

test('a layer that opened and then fails to set up the GPU sets it up on the same store when it is added again', async (t) => {
  t.mock.method(console, 'error', () => {});
  const close = t.mock.method(ChronoStore.prototype, 'close');
  const { layer, memory } = newLayer(t);
  let opens = 0;
  layer.on('open', () => { opens += 1; });
  layer.onAdd(map, null);
  const store = await layer.opened;
  layer.onAdd(map, countingGl({ failFragment: true }).gl);
  layer.onAdd(map, countingGl().gl);
  const during = layer.getValueAt([0.5, 1.5]);
  assert.equal(layer.store, store, 'the layer keeps the store it opened');
  assert.equal((await during).bands[0].value, 1000);
  await flush();
  assert.equal(await layer.opened, layer.store);
  assert.equal(close.mock.callCount(), 0, 'no store was closed');
  assert.equal(rootReads(memory), 1, 'the store was read once');
  assert.equal(opens, 1, "'open' fired once");
  assert.ok(layer.stats.slots > 0, 'the GPU was set up');
  assert.notEqual(layer.stats.state, 'failed');
  assert.equal((await layer.getValueAt([0.5, 1.5])).bands[0].value, 1000);
});

test('a layer that opened and whose GPU setup fails twice stays failed and keeps its store open', async (t) => {
  t.mock.method(console, 'error', () => {});
  const close = t.mock.method(ChronoStore.prototype, 'close');
  const { layer, errors } = newLayer(t);
  layer.onAdd(map, null);
  const store = await layer.opened;
  layer.onAdd(map, countingGl({ failFragment: true }).gl);
  layer.onAdd(map, countingGl({ failFragment: true }).gl);
  await flush();
  assert.equal(layer.stats.state, 'failed');
  assert.deepEqual(errors.map((event) => event.fatal), [true, true]);
  assert.equal(layer.store, store, 'the layer keeps the store it opened');
  assert.equal(close.mock.callCount(), 0, 'the store was not closed');
  assert.equal(await layer.opened, store, 'opened is still resolved with that store');
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

test('a store the layer refuses fails the layer for good: the stats say failed, the error is fatal and opened rejects', async (t) => {
  t.mock.method(console, 'error', () => {});
  const { layer, errors } = newLayer(t, { layerOptions: { t: 5 } });
  layer.onAdd(map, null);
  await assert.rejects(layer.opened, /timestep 5 is outside/);
  await flush();
  assert.equal(layer.stats.state, 'failed');
  assert.deepEqual(errors.map((event) => event.fatal), [true]);
  assert.equal(layer.products, null, 'the refused store left no products');
  assert.equal(layer.bandNames, null);
});

test('a cell that fails to load reports an error that is not fatal, and the layer has not failed', async (t) => {
  t.mock.method(console, 'error', () => {});
  const { layer, memory, errors } = newLayer(t);
  layer.onAdd(map, countingGl().gl);
  await layer.opened;
  const get = memory.get.bind(memory);
  memory.get = (key, options) => key.includes('/data/c/') ? Promise.reject(new Error('the disk is gone')) : get(key, options);
  layer.render({ defaultProjectionData: { mainMatrix: MATRIX } });
  for (let i = 0; i < 100 && errors.length === 0; i++) await flush();
  assert.equal(errors.length, 1);
  assert.equal(errors[0].fatal, false);
  assert.ok(errors[0].error instanceof Error);
  assert.notEqual(layer.stats.state, 'failed');
});

test('a removed layer says so in its stats', async (t) => {
  const { layer } = newLayer(t);
  layer.onAdd(map, null);
  await layer.opened;
  layer.remove();
  assert.equal(layer.stats.state, 'removed');
});

test('a layer removed before it is added says removed, opens nothing and cannot be added', async (t) => {
  const { layer, memory } = newLayer(t);
  layer.remove();
  assert.equal(layer.stats.state, 'removed');
  await assert.rejects(layer.opened, /was removed/);
  assert.throws(() => layer.onAdd(map, null), /was removed; create a new layer/);
  await flush();
  assert.equal(rootReads(memory), 0, 'the store was never read');
  assert.equal(layer.store, null);
});

test('a layer removed while its store opens says removed, and the store that opens after that is closed', async (t) => {
  const close = t.mock.method(ChronoStore.prototype, 'close');
  let layer;
  // The decode worker starts after the open has read every piece of metadata and right before the store exists: the
  // layer is removed there, so the open completes for a layer that is gone.
  const spawnWorker = () => {
    layer.remove();
    const worker = {
      postMessage(message) {
        if (message.type === 'init') queueMicrotask(() => worker.onmessage?.({ data: { type: 'ready' } }));
      },
      terminate() {},
    };
    return worker;
  };
  ({ layer } = newLayer(t, { storeOptions: { workers: 1, spawnWorker } }));
  layer.onAdd(map, null);
  await assert.rejects(layer.opened, /was removed/);
  for (let i = 0; i < 100 && close.mock.callCount() === 0; i++) await flush();
  assert.equal(layer.stats.state, 'removed');
  assert.equal(close.mock.callCount(), 1, 'the store was closed');
  assert.equal(layer.store, null);
});

test('a render without the matrix of MapLibre 5 reports a non-fatal error once, and the layer has not failed', async (t) => {
  t.mock.method(console, 'error', () => {});
  const { layer, errors } = newLayer(t);
  layer.onAdd(map, countingGl().gl);
  await layer.opened;
  layer.render({});
  layer.render({});
  await flush();
  assert.equal(errors.length, 1, 'reported once');
  assert.equal(errors[0].fatal, false);
  assert.match(errors[0].error.message, /needs MapLibre GL JS >= 5/);
  assert.notEqual(layer.stats.state, 'failed');
});
