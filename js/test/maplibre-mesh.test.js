import { test } from 'node:test';
import assert from 'node:assert/strict';
import { FLOATS_PER_VERTEX, MAX_DIVISIONS, buildMesh, originMatrix } from '../maplibre/mesh.js';
import { createProjection, levelTransform, lonLatToMercator, pixelToCrs } from '../maplibre/projection.js';

const projection = createProjection('EPSG:32718');
const LEVEL0 = [10, 0, 485650, 0, -10, 9169880]; // the live Ucayali store
const FULL = { x0: 0, y0: 0, x1: 512, y1: 512 };

/** Exact mercator position of a texel corner (u, v) of the chunk whose top-left texel is chunkOrigin. */
function exact(transform, chunkOrigin, u, v) {
  return lonLatToMercator(...projection.toLonLat(...pixelToCrs(transform, chunkOrigin.col + u, chunkOrigin.row + v)));
}

function vertex(mesh, i, j) {
  const side = mesh.divisions + 1;
  const k = (j * side + i) * FLOATS_PER_VERTEX;
  return { mx: mesh.origin[0] + mesh.vertices[k], my: mesh.origin[1] + mesh.vertices[k + 1], u: mesh.vertices[k + 2], v: mesh.vertices[k + 3] };
}

/** Largest distance, in mercator units, between the bilinear mesh and the exact projection at the centre of every quad. */
function maxWarpError(mesh, transform, chunkOrigin) {
  const n = mesh.divisions;
  let worst = 0;
  for (let j = 0; j < n; j++) {
    for (let i = 0; i < n; i++) {
      const corners = [vertex(mesh, i, j), vertex(mesh, i + 1, j), vertex(mesh, i, j + 1), vertex(mesh, i + 1, j + 1)];
      const mx = corners.reduce((s, c) => s + c.mx, 0) / 4;
      const my = corners.reduce((s, c) => s + c.my, 0) / 4;
      const u = corners.reduce((s, c) => s + c.u, 0) / 4;
      const v = corners.reduce((s, c) => s + c.v, 0) / 4;
      const [ex, ey] = exact(transform, chunkOrigin, u, v);
      worst = Math.max(worst, Math.hypot(mx - ex, my - ey));
    }
  }
  return worst;
}

test('mesh layout: (n+1)^2 vertices, 6 n^2 in-range indices, texel coordinates span the rectangle', () => {
  const mesh = buildMesh({ projection, transform: LEVEL0, chunkOrigin: { col: 512, row: 1024 }, rect: { x0: 0, y0: 0, x1: 512, y1: 300 }, divisions: 8 });
  assert.equal(mesh.vertices.length, 81 * FLOATS_PER_VERTEX);
  assert.equal(mesh.indices.length, 6 * 64);
  assert.ok(mesh.indices.every((index) => index < 81));
  assert.deepEqual([vertex(mesh, 0, 0).u, vertex(mesh, 0, 0).v], [0, 0]);
  assert.deepEqual([vertex(mesh, 8, 8).u, vertex(mesh, 8, 8).v], [512, 300]);
  assert.equal(vertex(mesh, 4, 4).u, 256);
  assert.equal(vertex(mesh, 4, 4).v, 150);
  // Every quad is split into two triangles with non-zero area in texel space: u, v are an affine map of the grid.
  for (let t = 0; t < mesh.indices.length; t += 3) {
    const [a, b, c] = [0, 1, 2].map((k) => {
      const at = mesh.indices[t + k] * FLOATS_PER_VERTEX;
      return [mesh.vertices[at + 2], mesh.vertices[at + 3]];
    });
    const area = Math.abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2;
    assert.ok(area > 0);
  }
});

test('every vertex lands on the exact projection of its texel corner, to float32 rounding of its offset from the cell centre', () => {
  for (const [lod, col, row] of [[0, 0, 0], [0, 4, 3], [2, 1, 0], [3, 0, 0]]) {
    const transform = levelTransform(LEVEL0, lod);
    const chunkOrigin = { col: col * 512, row: row * 512 };
    const mesh = buildMesh({ projection, transform, chunkOrigin, rect: FULL, divisions: 8 });
    for (let j = 0; j <= 8; j++) {
      for (let i = 0; i <= 8; i++) {
        const { mx, my, u, v } = vertex(mesh, i, j);
        const [ex, ey] = exact(transform, chunkOrigin, u, v);
        const k = (j * 9 + i) * FLOATS_PER_VERTEX;
        // A float32 offset d is stored with relative error 2^-24 (6e-8): that is the whole budget.
        assert.ok(Math.abs(mx - ex) <= 6e-8 * Math.abs(mesh.vertices[k]) + 1e-15 && Math.abs(my - ey) <= 6e-8 * Math.abs(mesh.vertices[k + 1]) + 1e-15, `lod ${lod} cell (${row}, ${col}) vertex (${i}, ${j}): off by ${mx - ex}, ${my - ey}`);
      }
    }
  }
});

test('warp error: 8x8 quads over a 512 px cell stay within 0.01 px at zoom 22 (interpolation plus float32 rounding); it falls as divisions grow', () => {
  const chunkOrigin = { col: 0, row: 0 };
  const pixelAtZoom22 = 1 / (512 * 2 ** 22);
  const error8 = maxWarpError(buildMesh({ projection, transform: LEVEL0, chunkOrigin, rect: FULL, divisions: 8 }), LEVEL0, chunkOrigin);
  assert.ok(error8 < 0.01 * pixelAtZoom22, `8x8 mesh is off by ${error8 / pixelAtZoom22} px at zoom 22`);

  // A deliberately huge cell (512 px of 5 km: 2560 km) shows the error scaling: more divisions, less error.
  const coarse = [5000, 0, 400000, 0, -5000, 9000000];
  const errors = [1, 2, 8, 32].map((divisions) => maxWarpError(buildMesh({ projection, transform: coarse, chunkOrigin, rect: FULL, divisions }), coarse, chunkOrigin));
  for (let i = 1; i < errors.length; i++) assert.ok(errors[i] < errors[i - 1] / 3, `errors ${errors} should fall by more than 3x per step`);
});

test('cells that share an edge share their edge vertices to 1e-11 of the world, so the raster has no seams', () => {
  const left = buildMesh({ projection, transform: LEVEL0, chunkOrigin: { col: 0, row: 512 }, rect: FULL, divisions: 8 });
  const right = buildMesh({ projection, transform: LEVEL0, chunkOrigin: { col: 512, row: 512 }, rect: FULL, divisions: 8 });
  const below = buildMesh({ projection, transform: LEVEL0, chunkOrigin: { col: 0, row: 1024 }, rect: FULL, divisions: 8 });
  for (let j = 0; j <= 8; j++) {
    const a = vertex(left, 8, j);
    const b = vertex(right, 0, j);
    assert.ok(Math.abs(a.mx - b.mx) < 1e-11 && Math.abs(a.my - b.my) < 1e-11, `east/west edge vertex ${j}`);
  }
  for (let i = 0; i <= 8; i++) {
    const a = vertex(left, i, 8);
    const b = vertex(below, i, 0);
    assert.ok(Math.abs(a.mx - b.mx) < 1e-11 && Math.abs(a.my - b.my) < 1e-11, `north/south edge vertex ${i}`);
  }
});

test('a sub-rectangle mesh (coarse stand-in for a fine cell) covers exactly that rectangle', () => {
  const transform = levelTransform(LEVEL0, 1);
  const rect = { x0: 256, y0: 0, x1: 512, y1: 256 };
  const mesh = buildMesh({ projection, transform, chunkOrigin: { col: 0, row: 0 }, rect, divisions: 4 });
  assert.deepEqual([vertex(mesh, 0, 0).u, vertex(mesh, 0, 0).v, vertex(mesh, 4, 4).u, vertex(mesh, 4, 4).v], [256, 0, 512, 256]);
  const [ex, ey] = exact(transform, { col: 0, row: 0 }, 256, 0);
  assert.ok(Math.abs(vertex(mesh, 0, 0).mx - ex) < 1e-11 && Math.abs(vertex(mesh, 0, 0).my - ey) < 1e-11);
});

test('bounds enclose every vertex and the origin is inside them', () => {
  const mesh = buildMesh({ projection, transform: LEVEL0, chunkOrigin: { col: 1024, row: 0 }, rect: FULL, divisions: 8 });
  const [minX, minY, maxX, maxY] = mesh.bounds;
  for (let j = 0; j <= 8; j++) {
    for (let i = 0; i <= 8; i++) {
      const { mx, my } = vertex(mesh, i, j);
      assert.ok(mx >= minX - 1e-10 && mx <= maxX + 1e-10 && my >= minY - 1e-10 && my <= maxY + 1e-10);
    }
  }
  assert.ok(mesh.origin[0] > minX && mesh.origin[0] < maxX && mesh.origin[1] > minY && mesh.origin[1] < maxY);
  // North is up: smaller mercator y at the top row of texels.
  assert.ok(vertex(mesh, 0, 0).my < vertex(mesh, 0, 8).my);
});

test('invalid meshes are refused with the offending value', () => {
  const base = { projection, transform: LEVEL0, chunkOrigin: { col: 0, row: 0 }, rect: FULL };
  for (const divisions of [0, -1, 1.5, MAX_DIVISIONS + 1, NaN]) assert.throws(() => buildMesh({ ...base, divisions }), RangeError, String(divisions));
  assert.doesNotThrow(() => buildMesh({ ...base, divisions: MAX_DIVISIONS }));
  assert.throws(() => buildMesh({ ...base, rect: { x0: 10, y0: 0, x1: 10, y1: 5 }, divisions: 2 }), /empty mesh rectangle/);
  assert.throws(() => buildMesh({ ...base, rect: { x0: 689.75, y0: 0, x1: 178, y1: 128 }, divisions: 2 }), /689\.75/);
});

test('origin-relative matrices keep sub-pixel accuracy at zoom 20, where absolute float32 positions are off by pixels', () => {
  const zoom = 20;
  const world = 512 * 2 ** zoom; // CSS pixels around the world
  const [width, height] = [1280, 800];
  const [cx, cy] = lonLatToMercator(-75.0, -7.63);
  // Column-major mercator [0, 1] -> clip matrix of an unrotated, unpitched view centred on (cx, cy), as MapLibre hands it over.
  const matrix = new Float64Array(16);
  matrix[0] = (2 * world) / width;
  matrix[5] = (-2 * world) / height;
  matrix[10] = 1;
  matrix[15] = 1;
  matrix[12] = -cx * matrix[0];
  matrix[13] = -cy * matrix[5];

  const mesh = buildMesh({ projection, transform: LEVEL0, chunkOrigin: { col: 1024, row: 1024 }, rect: FULL, divisions: 8 });
  const rtc = originMatrix(matrix, mesh.origin);
  const clipToPixels = (x, y) => [((x + 1) / 2) * width, ((1 - y) / 2) * height];
  let worstRtc = 0;
  let worstAbsolute = 0;
  for (let j = 0; j <= 8; j++) {
    for (let i = 0; i <= 8; i++) {
      const { mx, my } = vertex(mesh, i, j);
      const k = (j * 9 + i) * FLOATS_PER_VERTEX;
      const [dx, dy] = [mesh.vertices[k], mesh.vertices[k + 1]];
      const truth = clipToPixels(matrix[0] * mx + matrix[12], matrix[5] * my + matrix[13]);
      const viaRtc = clipToPixels(rtc[0] * dx + rtc[12], rtc[5] * dy + rtc[13]);
      const viaAbsolute = clipToPixels(matrix[0] * Math.fround(mx) + matrix[12], matrix[5] * Math.fround(my) + matrix[13]);
      worstRtc = Math.max(worstRtc, Math.hypot(viaRtc[0] - truth[0], viaRtc[1] - truth[1]));
      worstAbsolute = Math.max(worstAbsolute, Math.hypot(viaAbsolute[0] - truth[0], viaAbsolute[1] - truth[1]));
    }
  }
  assert.ok(worstRtc < 0.01, `origin-relative error ${worstRtc} px`);
  assert.ok(worstAbsolute > 1, `absolute float32 error ${worstAbsolute} px: the test no longer demonstrates why the origin is folded in`);
});
