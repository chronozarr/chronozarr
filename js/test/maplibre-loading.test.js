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
 * back to the timestep and level of its data. With `hang`, every chunk request stays pending until it is aborted.
 */
async function openLayer(t, { cells = 2, levels = 1, nTime = 6, mask = false, hang = false, values = (time, lod) => 100 + time + 10 * lod, layerOptions = {}, storeOptions = {} } = {}) {
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
  if (hang) memory.delayMs = HANG_MS;
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

/** The chunk requests that reached the store, as 'lod/row/col/t', with whether each was cancelled. */
function chunkRequests(memory) {
  return memory.log.flatMap(({ key, signal }) => {
    const match = /^\/(\d+)\/data\/c\/(\d+)\/0\/(\d+)\/(\d+)$/.exec(key);
    return match ? [{ id: `${match[1]}/${match[3]}/${match[4]}/${match[2]}`, aborted: signal.aborted }] : [];
  });
}

const ids = (lod, rows, cols, t) => rows.flatMap((row) => cols.map((col) => `${lod}/${row}/${col}/${t}`)).sort();
const ALL = [0, 1, 2, 3, 4, 5];
const flush = () => new Promise(setImmediate);
const idsOf = (requests) => requests.map((request) => request.id).sort();

test('a pan cancels the loads of the cells that left the view and keeps the rest', async (t) => {
  const { layer, memory, view, frame, size } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, hang: true, storeOptions: { maxRequests: 64 } });
  view.matrix = viewMatrix({ left: 0, right: 0.4 }, size); // columns 0 to 2
  frame();
  await flush();
  assert.deepEqual(idsOf(chunkRequests(memory)), ids(0, ALL, [0, 1, 2], 0));
  view.matrix = viewMatrix({ left: 0.3, right: 0.7 }, size); // columns 1 to 4
  frame();
  await flush();
  const requests = chunkRequests(memory);
  assert.deepEqual(idsOf(requests.filter((request) => request.aborted)), ids(0, ALL, [0], 0), 'column 0 left the view');
  assert.deepEqual(idsOf(requests.filter((request) => !request.aborted)), ids(0, ALL, [1, 2, 3, 4], 0), 'columns 1 and 2 stayed, columns 3 and 4 entered');
  assert.equal(layer.stats.pending, 24, 'the cancelled loads are no longer pending');
});

test('a zoom out cancels the loads of the finer level, so the loads of the new level start at once', async (t) => {
  const { memory, view, frame } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, hang: true });
  frame();
  await flush();
  assert.equal(chunkRequests(memory).length, 12, 'the request limit lets 12 of the 36 level-0 loads start');
  view.zoom = 12;
  frame();
  await flush();
  const requests = chunkRequests(memory);
  assert.ok(requests.slice(0, 12).every((request) => request.aborted), 'the level-0 loads that had started were cancelled');
  assert.deepEqual(idsOf(requests.slice(12)), ids(1, [0, 1, 2], [0, 1, 2], 0), 'all 9 level-1 loads started');
  assert.ok(requests.slice(12).every((request) => !request.aborted));
});

test('a time change cancels the loads of the old timestep and starts those of the new one', async (t) => {
  const { layer, memory, frame } = await openLayer(t, { cells: 2, nTime: 3, hang: true });
  frame();
  layer.setTime(2);
  frame();
  await flush();
  const requests = chunkRequests(memory);
  assert.deepEqual(idsOf(requests.filter((request) => request.aborted)), ids(0, [0, 1], [0, 1], 0));
  assert.deepEqual(idsOf(requests.filter((request) => !request.aborted)), ids(0, [0, 1], [0, 1], 2));
});

const FIRST = 100; // the value of a level-0 chunk at timestep 0 (see openLayer)

test('the first frame after a zoom out draws the finer cells that are still on the GPU', async (t) => {
  const { view, frame, settle } = await openLayer(t, { cells: 6, levels: 2, nTime: 2 });
  await settle();
  assert.deepEqual(frame().draws, Array(36).fill(FIRST), 'level 0 is on screen');
  view.zoom = 12;
  const first = frame();
  assert.equal(first.lod, 1);
  assert.equal(first.uploads, 0, 'the stand-in costs no upload');
  assert.deepEqual(first.draws, Array(36).fill(FIRST), 'the 36 level-0 cells cover the area while the 9 level-1 cells load');
  await settle();
  assert.deepEqual(frame().draws, Array(9).fill(110), 'level 1 replaces them once loaded');
});

test('a zoom out over two levels draws the finest cells on the GPU that cover each cell', async (t) => {
  const { view, frame, settle } = await openLayer(t, { cells: 12, levels: 3, nTime: 2 });
  await settle();
  view.zoom = 11;
  const first = frame();
  assert.equal(first.lod, 2);
  assert.deepEqual(first.draws, Array(144).fill(FIRST), 'level 1 was never drawn, so the 144 level-0 cells stand in for the 9 level-2 cells');
});

test('a zoom out over more than two levels leaves the cells blank until they load', async (t) => {
  const { view, frame, settle } = await openLayer(t, { cells: 24, levels: 4, nTime: 2 });
  await settle();
  view.zoom = 10;
  const first = frame();
  assert.equal(first.lod, 3);
  assert.deepEqual(first.draws, [], 'level 0 is three levels finer than the 9 level-3 cells');
});

test('a finer cell stands in only with the timestep being drawn', async (t) => {
  const { layer, view, frame, settle } = await openLayer(t, { cells: 6, levels: 2, nTime: 2 });
  await settle();
  layer.setTime(1);
  view.zoom = 12;
  assert.deepEqual(frame().draws, [], 'level 0 holds timestep 0 only; it would mix timesteps');
  await settle();
  assert.deepEqual(frame().draws, Array(9).fill(111));
});

for (const [direction, from, to, expected] of [['backwards', 10, 9, [8, 7, 6]], ['forwards', 10, 11, [12, 13, 14]]]) {
  test(`scrubbing ${direction} prefetches the timesteps in that direction first`, async (t) => {
    const { layer, store, memory, settle } = await openLayer(t, { cells: 1, nTime: 24, layerOptions: { prefetch: true, t: from } });
    const watch = watchPrefetch(store);
    await settle();
    layer.setTime(to);
    await settle();
    await watch.first;
    await watch.runs[0].result;
    const fetched = chunkRequests(memory).map((request) => Number(request.id.split('/')[3])).filter((time) => time !== from && time !== to);
    assert.deepEqual(fetched.slice(0, 3).sort((a, b) => a - b), [...expected].sort((a, b) => a - b), `the first timesteps fetched, in fetch order: ${fetched.slice(0, 6)}`);
  });
}
