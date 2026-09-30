import { test } from 'node:test';
import assert from 'node:assert/strict';
import { SlotPool } from '../maplibre/slots.js';
import { centreDistance2, coarseFootprint, overlapsView, selectLod } from '../maplibre/view.js';
import { createProjection, mercatorPerTexel } from '../maplibre/projection.js';

const WORLD_METRES = 40075016.68557849;
// One 10 m texel of the live Ucayali store (UTM 18S, 7.6 S): 10.126 m of the mercator world, from pyproj.
const UCAYALI = (10.126298563 / WORLD_METRES);
const LEVELS = 4;

const lodAt = (zoom, options = {}) => selectLod({ mercatorPerTexel0: UCAYALI, levelCount: LEVELS, zoom, ...options });

test('LOD follows zoom: the level whose texels are closest to one CSS pixel, finer as you zoom in', () => {
  // px per level-0 texel = 10.126 m / (78271.5 m per px at zoom 0 / 2^zoom)... so zoom 12.5 gives 0.75, zoom 12 gives 0.53.
  const table = [[16, 0], [14, 0], [13, 0], [12.5, 0], [12.2, 1], [12, 1], [11.5, 1], [11.2, 2], [11, 2], [10.5, 2], [10.2, 3], [10, 3], [9.5, 3], [5, 3], [0, 3]];
  for (const [zoom, lod] of table) assert.equal(lodAt(zoom), lod, `zoom ${zoom}`);
});

test('LOD never increases as zoom increases, and changes by at most one level per half zoom step', () => {
  let previous = lodAt(0);
  for (let zoom = 0; zoom <= 20; zoom += 0.05) {
    const lod = lodAt(zoom);
    assert.ok(lod <= previous, `zoom ${zoom}: level ${lod} after ${previous}`);
    assert.ok(previous - lod <= 1, `zoom ${zoom}: skipped a level`);
    previous = lod;
  }
});

test('bias 0 is the spec rule (largest level whose texels are no bigger than a pixel); negative bias picks finer levels; result is clamped', () => {
  // At zoom 12.2 a level-0 texel is 0.65 px: level 0 is smaller than a pixel, level 1 (1.3 px) is bigger.
  assert.equal(lodAt(12.2, { bias: 0 }), 0);
  assert.equal(lodAt(12.2, { bias: 0.5 }), 1);
  assert.equal(lodAt(12.2, { bias: -1 }), 0);
  assert.equal(lodAt(12.2, { bias: 10 }), LEVELS - 1);
  assert.equal(lodAt(12.2, { bias: -10 }), 0);
  assert.equal(selectLod({ mercatorPerTexel0: UCAYALI, levelCount: 1, zoom: 3 }), 0);
  assert.equal(selectLod({ mercatorPerTexel0: UCAYALI, levelCount: 9, zoom: 3 }), 8);
});

test('LOD depends on the store latitude through mercatorPerTexel: the same 10 m store needs a finer level at 60 N than at the equator', () => {
  const at = (epsg, x, y) => mercatorPerTexel(createProjection(epsg), [10, 0, x, 0, -10, y], 200, 200);
  const equator = at('EPSG:32631', 499000, 1000);
  const north = at('EPSG:32631', 499000, 6_650_000);
  assert.ok(north / equator > 1.9 && north / equator < 2.1, `ratio ${north / equator}`);
  const zoom = 12;
  assert.ok(selectLod({ mercatorPerTexel0: north, levelCount: LEVELS, zoom }) < selectLod({ mercatorPerTexel0: equator, levelCount: LEVELS, zoom }));
});

// Mercator [0, 1] -> clip matrix of an unrotated view: clip = ((m - centre) * scale), as MapLibre hands it over (column-major).
function viewMatrix(cx, cy, scaleX, scaleY) {
  const m = new Float64Array(16);
  m[0] = scaleX;
  m[5] = -scaleY;
  m[10] = 1;
  m[15] = 1;
  m[12] = -cx * scaleX;
  m[13] = cy * scaleY;
  return m;
}
const square = (x, y, half) => [x - half, y - half, x + half, y - half, x + half, y + half, x - half, y + half];

test('overlapsView keeps cells that touch the screen (plus a margin) and drops the rest', () => {
  const matrix = viewMatrix(0.3, 0.5, 1000, 1000); // screen spans 0.3 +- 0.001 in x and 0.5 +- 0.001 in y
  assert.ok(overlapsView(matrix, square(0.3, 0.5, 0.0001)));
  assert.ok(overlapsView(matrix, square(0.3, 0.5, 0.5)), 'a cell bigger than the screen contains it');
  assert.ok(overlapsView(matrix, square(0.3008, 0.5, 0.0004)), 'straddling the right edge');
  assert.ok(!overlapsView(matrix, square(0.302, 0.5, 0.0004)));
  assert.ok(!overlapsView(matrix, square(0.3, 0.498, 0.0004)));
  assert.ok(!overlapsView(matrix, square(0.3, 0.502, 0.0004)));
  assert.ok(!overlapsView(matrix, square(0.298, 0.5, 0.0004)));
  // Clip x of this cell is 1.1 to 1.2: outside the screen, inside a margin of 0.2.
  assert.ok(!overlapsView(matrix, square(0.30115, 0.5, 0.00005)));
  assert.ok(overlapsView(matrix, square(0.30115, 0.5, 0.00005), 0.2));
});

test('overlapsView keeps a cell with a vertex at or behind the camera plane, where the clip test would divide by zero', () => {
  const matrix = viewMatrix(0.3, 0.5, 1000, 1000);
  matrix[7] = 100; // w = 100 * y + 1 (perspective): w <= 0 for y <= -0.01
  assert.ok(overlapsView(matrix, square(0.3, -0.05, 0.001)));
  matrix[7] = 0;
  assert.ok(!overlapsView(matrix, square(0.3, 0.6, 0.001)));
});

test('centreDistance2 orders cells by distance from the screen centre', () => {
  const matrix = viewMatrix(0.3, 0.5, 1000, 1000);
  const near = centreDistance2(matrix, square(0.3, 0.5, 0.0001));
  const off = centreDistance2(matrix, square(0.3005, 0.5, 0.0001));
  const far = centreDistance2(matrix, square(0.3005, 0.5008, 0.0001));
  assert.ok(near < off && off < far, `${near} ${off} ${far}`);
  assert.ok(Math.abs(near) < 1e-12);
  matrix[7] = 100;
  assert.equal(centreDistance2(matrix, square(0.3, -0.05, 0.001)), Infinity);
});

test('coarseFootprint: the part of a coarse cell that covers a fine cell', () => {
  const chunk = { width: 512, height: 512 };
  const full = (width, height) => () => ({ width, height });
  // Fine cell (row 1, col 2) at level 0 is the lower-left quarter of coarse cell (0, 1) at level 1.
  assert.deepEqual(coarseFootprint({ lod: 0, row: 1, col: 2, extent: { width: 512, height: 512 } }, 1, chunk, full(512, 512)), { row: 0, col: 1, rect: { x0: 0, y0: 256, x1: 256, y1: 512 } });
  // Fine cell (row 0, col 3) is the upper-right quarter of the same coarse cell.
  assert.deepEqual(coarseFootprint({ lod: 0, row: 0, col: 3, extent: { width: 512, height: 512 } }, 1, chunk, full(512, 512)).rect, { x0: 256, y0: 0, x1: 512, y1: 256 });
  // Two levels up a cell is an eighth of the coarse cell per side.
  assert.deepEqual(coarseFootprint({ lod: 0, row: 5, col: 6, extent: { width: 512, height: 512 } }, 2, chunk, full(512, 512)), { row: 1, col: 1, rect: { x0: 256, y0: 128, x1: 384, y1: 256 } });
  // From level 1 to 3 the scale is 4: cell (2, 7) covers x 128..256 of coarse cell (0, 1).
  assert.deepEqual(coarseFootprint({ lod: 1, row: 2, col: 7, extent: { width: 512, height: 512 } }, 3, chunk, full(512, 512)), { row: 0, col: 1, rect: { x0: 384, y0: 256, x1: 512, y1: 384 } });
});

test('coarseFootprint follows fractional footprints of edge cells and never reaches beyond the coarse cell extent', () => {
  const chunk = { width: 512, height: 512 };
  // Edge cell 199 texels wide (col 5 of a 2759 px wide level 0) and 300 tall: half of that in the level-1 cell (0, 2), which starts at its texel 256.
  const fine = { lod: 0, row: 0, col: 5, extent: { width: 199, height: 300 } };
  assert.deepEqual(coarseFootprint(fine, 1, chunk, () => ({ width: 356, height: 512 })), { row: 0, col: 2, rect: { x0: 256, y0: 0, x1: 355.5, y1: 150 } });
  assert.deepEqual(coarseFootprint(fine, 1, chunk, () => ({ width: 350, height: 140 })).rect, { x0: 256, y0: 0, x1: 350, y1: 140 });
});

test('SlotPool hands out free slots, then evicts the least recently used one that the current frame is not using', () => {
  const pool = new SlotPool(3);
  const a = pool.allocate('a', 1);
  const b = pool.allocate('b', 1);
  const c = pool.allocate('c', 2);
  assert.deepEqual([a.evicted, b.evicted, c.evicted], [null, null, null]);
  assert.equal(new Set([a.slot, b.slot, c.slot]).size, 3);
  assert.equal(pool.size, 3);

  // Frame 3 uses c only: a and b are candidates; a and b were last used in frame 1 (tie: first found), then touch b.
  pool.slotOf('b', 3);
  const d = pool.allocate('d', 3);
  assert.equal(d.evicted, 'a');
  assert.equal(d.slot, a.slot);
  assert.equal(pool.slotOf('a'), -1);
  assert.equal(pool.slotOf('d'), a.slot);

  // Frame 4 touches d and c; b (last used in frame 3) is the oldest.
  pool.slotOf('d', 4);
  pool.slotOf('c', 4);
  const e = pool.allocate('e', 4);
  assert.equal(e.evicted, 'b');
});

test('SlotPool never evicts a slot the current frame uses: a full frame gets null', () => {
  const pool = new SlotPool(2);
  pool.allocate('a', 7);
  pool.allocate('b', 7);
  assert.equal(pool.allocate('c', 7), null);
  assert.notEqual(pool.slotOf('a'), -1);
  assert.notEqual(pool.slotOf('b'), -1);
  assert.notEqual(pool.allocate('c', 8), null, 'the next frame may evict them');
});

test('SlotPool: re-allocating a resident key returns its slot without evicting; clear forgets everything; bad sizes throw', () => {
  const pool = new SlotPool(2);
  const first = pool.allocate('a', 1);
  assert.deepEqual(pool.allocate('a', 2), { slot: first.slot, evicted: null });
  assert.equal(pool.size, 1);
  pool.clear();
  assert.equal(pool.size, 0);
  assert.equal(pool.slotOf('a'), -1);
  assert.notEqual(pool.allocate('b', 1), null);
  assert.notEqual(pool.allocate('c', 1), null);
  for (const n of [0, -1, 1.5, NaN]) assert.throws(() => new SlotPool(n), RangeError, String(n));
});
