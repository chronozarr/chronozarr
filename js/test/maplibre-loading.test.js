import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ChronozarrLayer } from '../maplibre/layer.js';
import { buildSyntheticStore } from '../support/synthetic-store.js';

// The layer's loading logic against a stub GL and an in-memory store, without a browser: which requests it starts,
// which it cancels, what it draws and which timesteps it prefetches. The store sits at the equator, RES degrees per
// level-0 texel, so a map zoom of 13 draws level 0, a zoom of 12 level 1, and so on.
const CHUNK = 8;
const RES = 0.0001;
const WORLD = RES / 360; // mercator units per level-0 texel
const HANG_MS = 60_000; // a request that never lands unless it is aborted

/** Every texel of a chunk holds its own level, timestep and cell, so a draw can be traced back to the chunk it shows. */
const chunkValue = (lod, time, row, col) => ((lod * 64 + time) * 64 + row) * 64 + col;
const chunkOf = (value) => ({ lod: Math.floor(value / 262144), t: Math.floor(value / 4096) % 64, row: Math.floor(value / 64) % 64, col: value % 64 });

/**
 * Records what the layer uploads and draws. A draw carries the chunk in its data slot, the mask slot it names and the
 * mask slot uploaded with that chunk, its extent uniform, and the clip-space box of its mesh.
 */
function stubGl() {
  const constants = new Map([['NO_ERROR', 0]]);
  const state = { uploads: 0, draws: [], uniforms: new Map(), slotValue: new Map(), maskSlotOf: new Map(), lastData: null, vao: null, arrayBuffer: null };
  const box = () => {
    const matrix = state.uniforms.get('u_matrix');
    const vertices = state.vao?.vbo?.data;
    const out = [Infinity, Infinity, -Infinity, -Infinity];
    for (let k = 0; k < vertices.length; k += 4) {
      const x = matrix[0] * vertices[k] + matrix[4] * vertices[k + 1] + matrix[12];
      const y = matrix[1] * vertices[k] + matrix[5] * vertices[k + 1] + matrix[13];
      out[0] = Math.min(out[0], x);
      out[1] = Math.min(out[1], y);
      out[2] = Math.max(out[2], x);
      out[3] = Math.max(out[3], y);
    }
    return out;
  };
  const gl = new Proxy({}, {
    get(_, name) {
      if (name === 'state') return state;
      if (name === 'getParameter') return () => 2048;
      if (name === 'getError') return () => 0;
      if (name === 'getShaderParameter' || name === 'getProgramParameter') return () => true;
      if (name === 'getUniformLocation') return (_, uniform) => uniform;
      if (name === 'getAttribLocation') return () => 0;
      if (name === 'uniform1i') return (uniform, value) => state.uniforms.set(uniform, value);
      if (name === 'uniform2f') return (uniform, x, y) => state.uniforms.set(uniform, [x, y]);
      if (name === 'uniformMatrix4fv') return (uniform, _, matrix) => state.uniforms.set(uniform, Array.from(matrix));
      if (name === 'bindVertexArray') return (vao) => { state.vao = vao; };
      if (name === 'bindBuffer') return (target, buffer) => { if (target === constants.get('ARRAY_BUFFER')) state.arrayBuffer = buffer; };
      if (name === 'bufferData') return (target, data) => { if (target === constants.get('ARRAY_BUFFER')) state.arrayBuffer.data = data; };
      if (name === 'vertexAttribPointer') return () => { state.vao.vbo = state.arrayBuffer; };
      if (name === 'texSubImage3D') {
        return (...args) => {
          const slot = args[4]; // data uploads carry every band of a slot: layer z = slot * bands, and the test store has one band
          if (args[10] instanceof Uint8Array) {
            state.maskSlotOf.set(state.lastData, slot);
            return;
          }
          state.uploads++;
          state.lastData = slot;
          state.slotValue.set(slot, args[10][0]);
        };
      }
      if (name === 'drawElements') {
        return () => {
          const dataBase = state.uniforms.get('u_dataBase');
          state.draws.push({ ...chunkOf(state.slotValue.get(dataBase)), mask: state.uniforms.get('u_maskLayer'), uploadedMask: state.maskSlotOf.get(dataBase), extent: state.uniforms.get('u_extent'), box: box() });
        };
      }
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
 * Clip-space matrix of a map that shows the given part of a store of width x height texels: left, right, top and
 * bottom are fractions of its width and height. The axes scale independently, which the layer does not mind.
 */
function viewMatrix({ left = 0, right = 1, top = 0, bottom = 1 }, width, height = width) {
  const worldWidth = width * WORLD;
  const worldHeight = height * WORLD;
  const x0 = 0.5 + left * worldWidth;
  const y0 = 0.5 - worldHeight + top * worldHeight;
  const sx = 2 / ((right - left) * worldWidth);
  const sy = 2 / ((bottom - top) * worldHeight);
  return [sx, 0, 0, 0, 0, -sy, 0, 0, 0, 0, 1, 0, -(x0 + (right - left) * worldWidth / 2) * sx, (y0 + (bottom - top) * worldHeight / 2) * sy, 0, 1];
}

/**
 * A layer on a map with a stub GL. The map shows the whole store at `view.zoom`; a test moves it by setting
 * `view.matrix`, and may set `view.repaint` to draw when the layer asks for a frame. With `hang`, every chunk request stays pending until it is aborted; `delayMs` is the latency of each.
 */
async function openLayer(t, { cells = 2, width = cells * CHUNK, height = cells * CHUNK, levels = 1, nTime = 6, mask = false, hang = false, delayMs = 0, layerOptions = {}, storeOptions = {} } = {}) {
  const memory = buildSyntheticStore({ nTime, nBand: 1, height, width, chunk: CHUNK, sharded: false, nLevels: levels, mask, dtype: 'float32',
    crs: 'EPSG:4326', transform: [RES, 0, 0, 0, -RES, height * RES], bandObjects: [{ name: 'value', scale: 1, offset: 0 }],
    values: (time, band, y, x, lod) => chunkValue(lod, time, Math.floor(y / CHUNK), Math.floor(x / CHUNK)) });
  const view = { zoom: 13, matrix: viewMatrix({}, width, height) };
  const map = { triggerRepaint() { view.repaint?.(); }, getLayer() { return false; }, getZoom() { return view.zoom; } };
  const layer = new ChronozarrLayer({ url: 'memory://maplibre-loading', product: 'band', range: [0, 1], ...layerOptions, storeOptions: { store: memory, workers: 0, ...storeOptions } });
  t.after(() => layer.remove());
  const gl = stubGl();
  layer.onAdd(map, gl);
  const store = await layer.opened;
  memory.delayMs = hang ? HANG_MS : delayMs;
  const frame = () => {
    gl.state.uploads = 0;
    gl.state.draws.length = 0;
    layer.render({ defaultProjectionData: { mainMatrix: view.matrix } });
    return { uploads: gl.state.uploads, draws: [...gl.state.draws], lod: layer.lod };
  };
  /** Draws frames, letting finished loads land in between, until the layer reports ready. */
  const settle = async () => {
    for (let i = 0; i < 300; i++) {
      frame();
      await new Promise((resolve) => setTimeout(resolve, 2));
      if (layer.stats.state === 'ready') return;
    }
    assert.fail(`the layer did not settle: ${JSON.stringify(layer.stats)}`);
  };
  return { layer, store, memory, view, frame, settle, width, height };
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

/** The chunk requests that reached the store, as 'lod/row/col/t', with whether each was cancelled. */
function chunkRequests(memory) {
  return memory.log.flatMap(({ key, signal }) => {
    const match = /^\/(\d+)\/data\/c\/(\d+)\/0\/(\d+)\/(\d+)$/.exec(key);
    return match ? [{ id: `${match[1]}/${match[3]}/${match[4]}/${match[2]}`, aborted: signal.aborted }] : [];
  });
}

const range = (n) => Array.from({ length: n }, (_, i) => i);
const ids = (lod, rows, cols, t) => rows.flatMap((row) => cols.map((col) => `${lod}/${row}/${col}/${t}`)).sort();
const idsOf = (requests) => requests.map((request) => request.id).sort();
const drawn = (draws) => draws.map(({ lod, row, col, t }) => `${lod}/${row}/${col}/${t}`).sort();
const flush = () => new Promise(setImmediate);
const ALL = range(6);
const PAST_GRACE_MS = 500; // longer than STALE_LOAD_GRACE_MS in layer.js

/** Moves the clock the layer reads forward, so a grace period passes without the test waiting for it. */
function skewClock(t) {
  const now = performance.now.bind(performance);
  let skew = 0;
  t.mock.method(performance, 'now', () => now() + skew);
  return (ms) => { skew += ms; };
}

/**
 * Each draw sits where its cell lies on a map that shows the whole store (clip space), has the extent of the valid
 * texels of its chunk, and names the mask slot that was uploaded with its data chunk.
 */
function assertPlaced(draws, { width, height }) {
  for (const { lod, row, col, box, extent, mask, uploadedMask } of draws) {
    const side = CHUNK * 2 ** lod;
    const u0 = col * side;
    const u1 = Math.min(u0 + side, width);
    const v0 = row * side;
    const v1 = Math.min(v0 + side, height);
    const expected = [-1 + 2 * u0 / width, 1 - 2 * v1 / height, -1 + 2 * u1 / width, 1 - 2 * v0 / height];
    const id = `${lod}/${row}/${col}`;
    expected.forEach((value, i) => assert.ok(Math.abs(box[i] - value) < 1e-4, `cell ${id}: mesh box ${box.map((v) => v.toFixed(4))}, expected ${expected.map((v) => v.toFixed(4))}`));
    assert.deepEqual(extent, [Math.min(CHUNK, Math.ceil(width / 2 ** lod) - col * CHUNK), Math.min(CHUNK, Math.ceil(height / 2 ** lod) - row * CHUNK)], `extent of cell ${id}`);
    assert.equal(mask, uploadedMask, `cell ${id} names the mask slot that was uploaded with its data`);
  }
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

test('a pan cancels the loads of the cells that left the view once they have been out of it for a while, and keeps the rest', async (t) => {
  const advance = skewClock(t);
  const { layer, memory, view, frame, width, height } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, hang: true, storeOptions: { maxRequests: 64 } });
  view.matrix = viewMatrix({ left: 0, right: 0.4 }, width, height); // columns 0 to 2
  frame();
  await flush();
  assert.deepEqual(idsOf(chunkRequests(memory)), ids(0, ALL, [0, 1, 2], 0));
  view.matrix = viewMatrix({ left: 0.3, right: 0.7 }, width, height); // columns 1 to 4
  frame();
  await flush();
  assert.deepEqual(idsOf(chunkRequests(memory).filter((request) => request.aborted)), [], 'column 0 has just left the view; a pan back would need it');
  advance(PAST_GRACE_MS);
  frame();
  await flush();
  const requests = chunkRequests(memory);
  assert.deepEqual(idsOf(requests.filter((request) => request.aborted)), ids(0, ALL, [0], 0), 'column 0 stayed out of the view');
  assert.deepEqual(idsOf(requests.filter((request) => !request.aborted)), ids(0, ALL, [1, 2, 3, 4], 0), 'columns 1 and 2 stayed, columns 3 and 4 entered');
  assert.equal(layer.stats.pending, 24, 'the cancelled loads are no longer pending');
});

test('panning back and forth requests every chunk once, however often a cell leaves the view', async (t) => {
  const { memory, view, frame, width, height } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, hang: true, storeOptions: { maxRequests: 64 } });
  const left = viewMatrix({ left: 0, right: 0.4 }, width, height);
  const right = viewMatrix({ left: 0.3, right: 0.7 }, width, height);
  for (const matrix of [left, right, left, right, left]) {
    view.matrix = matrix;
    frame();
    await flush();
  }
  const requests = chunkRequests(memory);
  assert.deepEqual(idsOf(requests), ids(0, ALL, [0, 1, 2, 3, 4], 0), 'columns 0 to 4, once each');
  assert.deepEqual(requests.filter((request) => request.aborted), []);
});

test('a coarser load stays while a visible cell stands in on it, and goes once none does', async (t) => {
  const advance = skewClock(t);
  const { memory, view, frame, width, height } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, hang: true, storeOptions: { maxRequests: 64 } });
  view.zoom = 12;
  frame(); // the 9 level-1 cells
  await flush();
  view.zoom = 13;
  view.matrix = viewMatrix({ left: 0.55, right: 1 }, width, height); // level-0 columns 3 to 5, under level-1 columns 1 and 2
  frame();
  advance(PAST_GRACE_MS);
  frame();
  await flush();
  const requests = chunkRequests(memory);
  assert.deepEqual(idsOf(requests.filter((request) => request.aborted)), ids(1, [0, 1, 2], [0], 0), 'level-1 column 0 lies under no visible cell');
  assert.deepEqual(idsOf(requests.filter((request) => !request.aborted)), [...ids(1, [0, 1, 2], [1, 2], 0), ...ids(0, ALL, [3, 4, 5], 0)].sort());
});

test('a stale load that no load of the view waits behind runs to the end, and a pan back finds its chunk', async (t) => {
  const advance = skewClock(t);
  const { layer, memory, view, frame, settle, width, height } = await openLayer(t, { cells: 6, nTime: 2, delayMs: 150, storeOptions: { maxRequests: 64 } });
  const left = viewMatrix({ left: 0, right: 0.4 }, width, height); // columns 0 to 2
  const right = viewMatrix({ left: 0.6, right: 1 }, width, height); // columns 3 to 5
  view.matrix = right;
  await settle();
  view.matrix = left;
  frame();
  await flush();
  view.matrix = right; // on the GPU already: the view has no load of its own
  frame();
  advance(PAST_GRACE_MS);
  frame();
  await flush();
  assert.deepEqual(chunkRequests(memory).filter((request) => request.aborted), [], 'the loads of columns 0 to 2 were left running');
  assert.equal(layer.stats.pending, 18);
  assert.equal(layer.stats.state, 'loading', 'ready waits for the loads that nothing needs');
  view.matrix = left;
  await settle();
  const requests = chunkRequests(memory);
  assert.deepEqual(idsOf(requests), ids(0, ALL, ALL, 0), 'every chunk was requested once');
  assert.deepEqual(requests.filter((request) => request.aborted), []);
  assert.deepEqual(drawn(frame().draws), ids(0, ALL, [0, 1, 2], 0), 'the pan back draws the chunks that kept loading');
});

test('a stale load is cancelled after the grace while a load that the view needs is in flight', async (t) => {
  const advance = skewClock(t);
  const { memory, view, frame, settle, width, height } = await openLayer(t, { cells: 6, nTime: 2, delayMs: 100, storeOptions: { maxRequests: 64 } });
  const aborted = () => idsOf(chunkRequests(memory).filter((request) => request.aborted));
  view.matrix = viewMatrix({ left: 0.6, right: 1 }, width, height); // columns 3 to 5
  await settle();
  memory.delayMs = HANG_MS;
  view.matrix = viewMatrix({ left: 0, right: 0.4 }, width, height); // columns 0 to 2
  frame();
  await flush();
  view.matrix = viewMatrix({ left: 0.45, right: 1 }, width, height); // columns 2 to 5: column 2 is still loading, columns 0 and 1 are not needed
  frame();
  await flush();
  assert.deepEqual(aborted(), [], 'the grace has not passed');
  advance(PAST_GRACE_MS);
  frame();
  await flush();
  assert.deepEqual(aborted(), ids(0, ALL, [0, 1], 0), 'columns 0 and 1 were cancelled, column 2 still loads');
});

test('a stale load stays once the loads that the view needs have landed', async (t) => {
  const advance = skewClock(t);
  const { layer, memory, view, frame, width, height } = await openLayer(t, { cells: 6, nTime: 2, hang: true, storeOptions: { maxRequests: 64 } });
  view.matrix = viewMatrix({ left: 0, right: 0.4 }, width, height); // columns 0 to 2, which never land
  frame();
  await flush();
  memory.delayMs = 20;
  view.matrix = viewMatrix({ left: 0.6, right: 1 }, width, height); // columns 3 to 5
  frame();
  for (let i = 0; i < 100 && layer.stats.pending > 18; i++) await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(layer.stats.pending, 18, 'the loads of the view have landed; those of columns 0 to 2 hang');
  advance(PAST_GRACE_MS);
  frame();
  await flush();
  assert.deepEqual(chunkRequests(memory).filter((request) => request.aborted), [], 'nothing waits behind the old loads any more');
  assert.equal(layer.stats.pending, 18);
});

test('the grace ends in a frame of its own, so a map that draws only when asked still cancels the stale loads', async (t) => {
  const { view, frame, memory, width, height } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, hang: true, storeOptions: { maxRequests: 8 } });
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const advance = skewClock(t);
  let scheduled = false;
  view.repaint = () => {
    if (scheduled) return;
    scheduled = true;
    setImmediate(() => {
      scheduled = false;
      frame();
    });
  };
  view.matrix = viewMatrix({ left: 0, right: 0.4 }, width, height); // columns 0 to 2: the limiter admits 8 of 18 loads
  frame();
  await flush();
  view.matrix = viewMatrix({ left: 0.6, right: 1 }, width, height); // columns 3 to 5; the last frame the map draws on its own
  frame();
  await flush();
  assert.equal(chunkRequests(memory).length, 8, 'the old loads still hold the request slots');
  advance(PAST_GRACE_MS);
  t.mock.timers.tick(PAST_GRACE_MS);
  await flush();
  await flush();
  const requests = chunkRequests(memory);
  assert.ok(requests.slice(0, 8).every((request) => request.aborted), 'the loads of columns 0 to 2 were cancelled');
  assert.equal(requests.length, 16, 'the freed request slots went to the new view');
  assert.ok(requests.slice(8).every((request) => !request.aborted && /^0\/\d\/[345]\/0$/.test(request.id)), 'columns 3 to 5');
});

test('a zoom out cancels the loads of the finer level, so the loads of the new level start', async (t) => {
  const advance = skewClock(t);
  const { memory, view, frame } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, hang: true });
  frame();
  await flush();
  assert.equal(chunkRequests(memory).length, 12, 'the request limit lets 12 of the 36 level-0 loads start');
  view.zoom = 12;
  frame();
  advance(PAST_GRACE_MS);
  frame();
  await flush();
  const requests = chunkRequests(memory);
  assert.ok(requests.slice(0, 12).every((request) => request.aborted), 'the level-0 loads that had started were cancelled');
  assert.deepEqual(idsOf(requests.slice(12)), ids(1, [0, 1, 2], [0, 1, 2], 0), 'all 9 level-1 loads started');
  assert.ok(requests.slice(12).every((request) => !request.aborted));
});

test('with prefetch on, the loads of the cells that left the view are cancelled too, and the new view starts', async (t) => {
  const advance = skewClock(t);
  const { layer, store, memory, view, frame, width, height } = await openLayer(t, { cells: 6, levels: 2, nTime: 3, hang: true, layerOptions: { prefetch: true }, storeOptions: { maxRequests: 8 } });
  const watch = watchPrefetch(store);
  view.matrix = viewMatrix({ left: 0, right: 0.4 }, width, height); // columns 0 to 2
  layer.setTime(1);
  frame();
  await watch.first; // the plan now owns the 8 loads in flight, which the limiter admits out of 18
  await flush();
  assert.equal(chunkRequests(memory).length, 8);
  view.matrix = viewMatrix({ left: 0.6, right: 1 }, width, height); // columns 3 to 5
  frame();
  advance(PAST_GRACE_MS);
  frame();
  await flush();
  const requests = chunkRequests(memory);
  assert.ok(requests.slice(0, 8).every((request) => request.aborted), 'the loads of columns 0 to 2 were cancelled although the prefetch owned them');
  assert.equal(requests.length, 16, 'the freed request slots went to the new view');
  assert.ok(requests.slice(8).every((request) => !request.aborted && /^0\/\d\/[345]\/1$/.test(request.id)), 'columns 3 to 5');
});

test('wobbling across a level boundary keeps every load, and the layer reaches ready', async (t) => {
  const { layer, memory, view, frame, settle } = await openLayer(t, { cells: 6, levels: 2, nTime: 2, delayMs: 100, storeOptions: { maxRequests: 64 } });
  for (let i = 0; i < 30; i++) {
    view.zoom = i % 2 ? 12.2 : 12.3; // level 0 and level 1
    frame();
    await flush();
  }
  await settle();
  const requests = chunkRequests(memory);
  assert.deepEqual(idsOf(requests), [...ids(0, ALL, ALL, 0), ...ids(1, [0, 1, 2], [0, 1, 2], 0)].sort(), 'the 36 + 9 chunks, each requested once');
  assert.deepEqual(requests.filter((request) => request.aborted), []);
  assert.equal(layer.stats.state, 'ready');
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

// 19 x 12 cells at level 0, 10 x 6 at level 1, 5 x 3 at level 2 and 3 x 2 at level 3: not square, not a power of two, with
// partial cells at every edge, and more than 4 cells at the coarsest level, so the first view is not seeded from it.
const WIDE = { width: 150, height: 90, levels: 4, nTime: 3 };
const LEVEL_0 = ids(0, range(12), range(19), 0);

test('the first frame after a zoom out draws every finer cell that is on the GPU, in its place and with its mask', async (t) => {
  const { view, frame, settle } = await openLayer(t, { ...WIDE, mask: true });
  await settle();
  assert.deepEqual(drawn(frame().draws), LEVEL_0, 'level 0 is on screen');
  view.zoom = 12;
  const first = frame();
  assert.equal(first.lod, 1);
  assert.equal(first.uploads, 0, 'the stand-in costs no upload');
  assert.deepEqual(drawn(first.draws), LEVEL_0, 'each of the 228 level-0 cells once, while the 60 level-1 cells load');
  assertPlaced(first.draws, WIDE);
  await settle();
  const final = frame();
  assert.deepEqual(drawn(final.draws), ids(1, range(6), range(10), 0), 'level 1 replaces them once loaded');
  assertPlaced(final.draws, WIDE);
});

test('a zoom out over two levels draws the finest cells on the GPU that cover each cell', async (t) => {
  const { view, frame, settle } = await openLayer(t, { ...WIDE, mask: true });
  await settle();
  view.zoom = 11;
  const first = frame();
  assert.equal(first.lod, 2);
  assert.deepEqual(drawn(first.draws), LEVEL_0, 'level 1 was never drawn, so the level-0 cells stand in for the 15 level-2 cells');
  assertPlaced(first.draws, WIDE);
});

test('a zoom out over more than two levels leaves the cells blank until they load', async (t) => {
  const { view, frame, settle } = await openLayer(t, WIDE);
  await settle();
  view.zoom = 10;
  const first = frame();
  assert.equal(first.lod, 3);
  assert.deepEqual(first.draws, [], 'level 0 is three levels finer than the 6 level-3 cells');
});

test('a finer cell stands in only with the timestep being drawn', async (t) => {
  const { layer, view, frame, settle } = await openLayer(t, { cells: 6, levels: 2, nTime: 2 });
  await settle();
  layer.setTime(1);
  view.zoom = 12;
  assert.deepEqual(frame().draws, [], 'level 0 holds timestep 0 only; it would mix timesteps');
  await settle();
  assert.deepEqual(drawn(frame().draws), ids(1, [0, 1, 2], [0, 1, 2], 1));
});

test('a stand-in shows the timestep being drawn, not what the level showed on an earlier visit', async (t) => {
  const { layer, view, frame, settle } = await openLayer(t, WIDE);
  view.zoom = 12;
  await settle(); // level 1 at timestep 0
  view.zoom = 13;
  layer.setTime(2);
  await settle(); // level 0 at timestep 2
  view.zoom = 12;
  const first = frame();
  assert.equal(first.lod, 1);
  assert.deepEqual(drawn(first.draws), ids(0, range(12), range(19), 2), 'timestep 2 from level 0, not timestep 0 from level 1');
});

test('a time change at the same level holds the previous timestep while the new one loads', async (t) => {
  const { layer, memory, frame, settle } = await openLayer(t, { cells: 6, levels: 2, nTime: 2 });
  await settle();
  memory.delayMs = HANG_MS;
  layer.setTime(1);
  assert.deepEqual(drawn(frame().draws), ids(0, ALL, ALL, 0), 'timestep 0 stays on screen');
});

test('a wobble across a level boundary during a time change still holds the previous timestep on return', async (t) => {
  const { layer, memory, view, frame, settle } = await openLayer(t, { cells: 6, levels: 2, nTime: 2 });
  await settle();
  memory.delayMs = HANG_MS;
  layer.setTime(1);
  assert.deepEqual(drawn(frame().draws), ids(0, ALL, ALL, 0), 'timestep 0 held at level 0');
  view.zoom = 12;
  frame();
  view.zoom = 13;
  assert.deepEqual(drawn(frame().draws), ids(0, ALL, ALL, 0), 'and again after the round trip through level 1');
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
