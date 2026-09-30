// Warp meshes: each cell (or a part of one) is drawn as a grid of quads whose vertices are projected from the
// store CRS to Web Mercator in float64 here, so the GPU only interpolates between exact positions.
//
// Positions are stored relative to a per-mesh origin (the mercator position of the footprint centre). The
// vertex shader adds them to an origin-translated matrix composed in float64, which keeps the result exact
// at any zoom: absolute mercator coordinates in float32 would already be off by whole pixels at zoom 16.

import { lonLatToMercator, pixelToCrs } from './projection.js';

export const MAX_DIVISIONS = 255; // Uint16 indices: (n + 1)^2 vertices must stay below 65536.
export const FLOATS_PER_VERTEX = 4; // dx, dy (mercator offset from the origin), u, v (texel inside the chunk)

/**
 * Mesh for a rectangle of one chunk.
 *
 * @param {object} p
 * @param {{toLonLat:(x:number,y:number)=>[number,number]}} p.projection  store CRS projection
 * @param {number[]} p.transform  affine [a, b, c, d, e, f] of the chunk's pyramid level (pixel corner -> CRS)
 * @param {{col:number,row:number}} p.chunkOrigin  level pixel of the chunk's top-left texel
 * @param {{x0:number,y0:number,x1:number,y1:number}} p.rect  footprint in chunk texels (x right, y down)
 * @param {number} p.divisions  quads per side
 * @returns {{origin:[number,number], vertices:Float32Array, indices:Uint16Array, bounds:[number,number,number,number], divisions:number}}
 *   `bounds` is [minX, minY, maxX, maxY] in absolute mercator units.
 */
export function buildMesh({ projection, transform, chunkOrigin, rect, divisions }) {
  if (!Number.isInteger(divisions) || divisions < 1 || divisions > MAX_DIVISIONS) {
    throw new RangeError(`chronozarr maplibre: mesh divisions must be an integer in 1..${MAX_DIVISIONS}, got ${divisions}`);
  }
  const { x0, y0, x1, y1 } = rect;
  if (!(x1 > x0 && y1 > y0)) throw new RangeError(`chronozarr maplibre: empty mesh rectangle ${JSON.stringify(rect)}`);

  const side = divisions + 1;
  const mercator = (u, v) => {
    const [x, y] = pixelToCrs(transform, chunkOrigin.col + u, chunkOrigin.row + v);
    return lonLatToMercator(...projection.toLonLat(x, y));
  };
  const origin = mercator((x0 + x1) / 2, (y0 + y1) / 2);

  const vertices = new Float32Array(side * side * FLOATS_PER_VERTEX);
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (let j = 0; j < side; j++) {
    const v = y0 + ((y1 - y0) * j) / divisions;
    for (let i = 0; i < side; i++) {
      const u = x0 + ((x1 - x0) * i) / divisions;
      const [mx, my] = mercator(u, v);
      const k = (j * side + i) * FLOATS_PER_VERTEX;
      vertices[k] = mx - origin[0];
      vertices[k + 1] = my - origin[1];
      vertices[k + 2] = u;
      vertices[k + 3] = v;
      minX = Math.min(minX, mx);
      minY = Math.min(minY, my);
      maxX = Math.max(maxX, mx);
      maxY = Math.max(maxY, my);
    }
  }

  const indices = new Uint16Array(divisions * divisions * 6);
  let w = 0;
  for (let j = 0; j < divisions; j++) {
    for (let i = 0; i < divisions; i++) {
      const v00 = j * side + i;
      const v10 = v00 + 1;
      const v01 = v00 + side;
      const v11 = v01 + 1;
      indices.set([v00, v10, v01, v10, v11, v01], w);
      w += 6;
    }
  }
  return { origin, vertices, indices, bounds: [minX, minY, maxX, maxY], divisions };
}

/**
 * The clip-space matrix for drawing a mesh: `matrix` (column-major, mercator [0, 1] units to clip space, float64)
 * with the mesh origin folded in, so vertex offsets can stay small float32 values. Composed in float64, returned as float32.
 */
export function originMatrix(matrix, origin, out = new Float32Array(16)) {
  const [ox, oy] = origin;
  for (let i = 0; i < 12; i++) out[i] = matrix[i];
  for (let r = 0; r < 4; r++) out[12 + r] = matrix[r] * ox + matrix[4 + r] * oy + matrix[12 + r];
  return out;
}
