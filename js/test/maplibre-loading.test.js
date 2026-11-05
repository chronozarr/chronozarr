import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ChronozarrLayer } from '../maplibre/layer.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

// The layer's loading logic against a stub GL and an in-memory store, without a browser: which requests it starts,
// which it cancels, what it draws and which timesteps it prefetches. The store sits at the equator, RES degrees per
// level-0 texel, so a map zoom of 13 draws level 0 and a zoom of 12 draws level 1.
const CHUNK = 8;
const RES = 0.0001;
const WORLD = RES / 360; // mercator units per level-0 texel
const HANG_MS = 60_000; // a request that never lands unless it is aborted

/** Records what the layer uploads and draws. A draw is identified by the first value of the chunk in its slot. */
function stubGl() {
  const constants = new Map([['NO_ERROR', 0]]);
  const state = { uploads: [], draws: [], uniforms: new Map(), slotFirst: new Map() };
  const gl = new Proxy({}, {
    get(_, name) {
      if (name === 'state') return state;
      if (name === 'getParameter') return () => 2048;
      if (name === 'getError') return () => 0;
      if (name === 'getShaderParameter' || name === 'getProgramParameter') return () => true;
      if (name === 'getUniformLocation') return (_, uniform) => uniform;
      if (name === 'getAttribLocation') return () => 0;
      if (name === 'uniform1i') return (uniform, value) => state.uniforms.set(uniform, value);
      if (name === 'texSubImage3D') {
        return (...args) => {
          // Data uploads carry every band of a slot: layer z = slot * bands.
          state.uploads.push(args[4]);
          state.slotFirst.set(args[4], args[10][0]);
        };
      }
      if (name === 'drawElements') return () => state.draws.push(state.slotFirst.get(state.uniforms.get('u_dataBase')));
      if (/^create/.test(name)) return () => ({});
      if (/^[A-Z_0-9]+$/.test(name)) {
        if (!constants.has(name)) constants.set(name, constants.size + 1);
        return constants.get(name);
      }
      return () => {};
    },
  });
  return gl;
}

/**
 * Clip-space matrix of a map that shows the given part of the store: left, right, top and bottom are fractions of the
 * store's width and height. The axes scale independently, which the layer does not mind.
 */
function viewMatrix({ left = 0, right = 1, top = 0, bottom = 1 }, size) {
  const width = size * WORLD;
  const x0 = 0.5 + left * width;
  const y0 = 0.5 - width + top * width;
  const sx = 2 / ((right - left) * width);
  const sy = 2 / ((bottom - top) * width);
  return [sx, 0, 0, 0, 0, -sy, 0, 0, 0, 0, 1, 0, -(x0 + (right - left) * width / 2) * sx, (y0 + (bottom - top) * width / 2) * sy, 0, 1];
}

/**
 * A layer on a map with a stub GL. `values(t, lod)` is the value every texel of a chunk takes, so a draw can be traced
 * back to the timestep and level of its data.
 */
async function openLayer(t, { cells = 2, levels = 1, nTime = 6, mask = false, values = (time, lod) => 100 + time + 10 * lod, layerOptions = {}, storeOptions = {} } = {}) {
  const size = cells * CHUNK;
  const memory = buildSyntheticStore({ nTime, nBand: 1, height: size, width: size, chunk: CHUNK, sharded: false, nLevels: levels, mask,
    crs: 'EPSG:4326', transform: [RES, 0, 0, 0, -RES, size * RES], bandObjects: [{ name: 'value', scale: 1, offset: 0 }],
    values: (time, band, y, x, lod) => values(time, lod) });
  const view = { zoom: 13, matrix: viewMatrix({}, size) };
  const map = { triggerRepaint() {}, getLayer() { return false; }, getZoom() { return view.zoom; } };
  const layer = new ChronozarrLayer({ url: 'memory://maplibre-loading', product: 'band', range: [0, 1], ...layerOptions, storeOptions: { store: memory, workers: 0, ...storeOptions } });
  t.after(() => layer.remove());
  const gl = stubGl();
  layer.onAdd(map, gl);
  const store = await layer.opened;
  const frame = () => {
    gl.state.uploads.length = 0;
    gl.state.draws.length = 0;
    layer.render({ defaultProjectionData: { mainMatrix: view.matrix } });
    return { uploads: gl.state.uploads.length, draws: [...gl.state.draws], lod: layer.lod };
  };
  /** Draws frames, letting finished loads land in between, until the layer reports ready. */
  const settle = async () => {
    for (let i = 0; i < 50; i++) {
      frame();
      await new Promise(setImmediate);
      if (layer.stats.state === 'ready') return;
    }
    assert.fail(`the layer did not settle: ${JSON.stringify(layer.stats)}`);
  };
  return { layer, store, memory, view, frame, settle, size };
}

/** Wraps store.prefetch so the test sees each call's arguments and its result, and waits for the first call. */
function watchPrefetch(store) {
  const runs = [];
  const prefetch = store.prefetch.bind(store);
  let notify;
  const first = new Promise((resolve) => { notify = resolve; });
  store.prefetch = (job) => {
    const result = prefetch(job);
    runs.push({ job, result });
    notify();
    return result;
  };
  return { runs, first };
}

test('prefetch fetches the masks through the reader as speculative reads', async (t) => {
  const { layer, store, settle } = await openLayer(t, { mask: true, layerOptions: { prefetch: true } });
  const masksRead = [];
  const getMask = store.getMask.bind(store);
  store.getMask = (lod, row, col, time, options) => {
    masksRead.push(time);
    return getMask(lod, row, col, time, options);
  };
  const watch = watchPrefetch(store);
  await settle();
  await watch.first;
  const result = await watch.runs[0].result;
  assert.deepEqual(result.errors, []);
  assert.equal(result.masks, 4 * 5, 'the reader started the mask of every prefetched chunk itself, 4 cells x 5 timesteps');
  assert.deepEqual(masksRead.filter((time) => time !== layer.t), [], 'the layer asked for no mask of another timestep at demand priority');
});
